"""Tests for authenticated, paired, bounded Auto Export HTTP ingestion."""

from __future__ import annotations

import http.client
import json
import argparse
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from commands import cmd_import
from daemon import ProfileRuntime
from http_ingest import (
    HttpIngestManager,
    IngestError,
    TokenRegistry,
    validate_upload,
)
from http_ingest_server import HttpIngestServer
from profiles import Profile


def _profile(tmp_path: Path, name: str = "adam") -> Profile:
    """Create an enabled HTTP profile rooted in a temporary directory."""
    return Profile(
        name=name,
        telegram_id=11,
        root=tmp_path / "profiles" / name,
        operator=name == "adam",
        import_source="http",
    )


def _headers(
    kind: str,
    *,
    session_id: str = "2589AA62-C39F-4FF2-8BC7-2D11D5488A7F",
) -> dict[str, str]:
    """Return production-shaped Auto Export headers for a payload kind."""
    return {
        "automation-name": kind.title(),
        "automation-aggregation": "Days" if kind == "metrics" else "Minutes",
        "automation-period": "Default",
        "automation-id": "788509F9-3AFA-42CE-850C-E4D2DDB02F12",
        "session-id": session_id,
    }


def _body(kind: str) -> bytes:
    """Return a minimal payload accepted by the production parser."""
    if kind == "metrics":
        data = {
            "metrics": [
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [{"date": "2026-08-02 00:00:00 +0000", "qty": 1234}],
                }
            ]
        }
    else:
        data = {
            "workouts": [
                {
                    "name": "Outdoor Walk",
                    "start": "2026-08-02 08:00:00 +0000",
                    "duration": 1800,
                    "route": [
                        {
                            "latitude": 53.3,
                            "longitude": -6.2,
                            "timestamp": "2026-08-02 08:00:00 +0000",
                        }
                    ],
                }
            ]
        }
    return json.dumps({"data": data}).encode("utf-8")


class TestTokenRegistry:
    def test_maps_token_to_profile_without_storing_plaintext(
        self, tmp_path: Path
    ) -> None:
        registry = TokenRegistry(tmp_path / "ingest_tokens.json")

        token = registry.create_token("adam")

        assert registry.authenticate(token) == "adam"
        assert token not in registry.path.read_text(encoding="utf-8")
        assert registry.path.stat().st_mode & 0o777 == 0o600

    def test_rotation_revokes_existing_token(self, tmp_path: Path) -> None:
        registry = TokenRegistry(tmp_path / "ingest_tokens.json")
        old = registry.create_token("adam")

        new = registry.create_token("adam", revoke_existing=True)

        assert registry.authenticate(old) is None
        assert registry.authenticate(new) == "adam"


class TestValidateUpload:
    @pytest.mark.parametrize("kind", ["metrics", "workouts"])
    def test_accepts_production_shape(self, kind: str) -> None:
        result = validate_upload(_headers(kind), _body(kind))

        assert result.kind == kind
        assert result.size == len(_body(kind))

    def test_rejects_wrong_metrics_aggregation(self) -> None:
        headers = _headers("metrics")
        headers["automation-aggregation"] = "Default"

        with pytest.raises(IngestError, match="must be Days"):
            validate_upload(headers, _body("metrics"))

    def test_rejects_non_default_period(self) -> None:
        headers = _headers("workouts")
        headers["automation-period"] = "Previous 7 Days"

        with pytest.raises(IngestError, match="Date Range must be Default"):
            validate_upload(headers, _body("workouts"))

    def test_rejects_mixed_or_mislabeled_payload(self) -> None:
        with pytest.raises(IngestError, match="only the metrics array"):
            validate_upload(_headers("metrics"), _body("workouts"))

    def test_rejects_invalid_route_coordinate(self) -> None:
        payload = json.loads(_body("workouts"))
        payload["data"]["workouts"][0]["route"][0]["latitude"] = 91

        with pytest.raises(IngestError, match="invalid coordinates"):
            validate_upload(_headers("workouts"), json.dumps(payload).encode())


class TestHttpIngestManager:
    def test_stages_only_latest_pair_and_records_bounded_receipt(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        ready: list[tuple[str, str]] = []
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=600,
            on_pair_ready=lambda name, digest: ready.append((name, digest)),
        )
        metrics = validate_upload(_headers("metrics"), _body("metrics"))
        workouts = validate_upload(_headers("workouts"), _body("workouts"))

        first = manager.accept("adam", metrics, _body("metrics"))
        second = manager.accept("adam", workouts, _body("workouts"))

        assert first.pair_ready is False
        assert second.pair_ready is True
        assert len(ready) == 1
        assert list((profile.http_cache / "Metrics").glob("*.json")) == [
            profile.http_cache / "Metrics" / "latest.json"
        ]
        assert list((profile.http_cache / "Workouts").glob("*.json")) == [
            profile.http_cache / "Workouts" / "latest.json"
        ]

        digest = ready[0][1]
        generation = manager.begin_import("adam", digest)
        assert generation is not None
        assert (generation / "Metrics" / "latest.json").read_bytes() == _body("metrics")
        manager.finish_import("adam", digest, success=True)
        assert generation.exists() is False
        assert manager.pending_pairs() == []
        assert manager.status()["adam"]["last_imported_at"] is not None

    def test_identical_retry_does_not_reimport_completed_pair(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        ready: list[tuple[str, str]] = []
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=600,
            on_pair_ready=lambda name, digest: ready.append((name, digest)),
        )
        metrics = validate_upload(_headers("metrics"), _body("metrics"))
        workouts = validate_upload(_headers("workouts"), _body("workouts"))
        manager.accept("adam", metrics, _body("metrics"))
        manager.accept("adam", workouts, _body("workouts"))
        digest = ready[-1][1]
        assert manager.begin_import("adam", digest) is not None
        manager.finish_import("adam", digest, success=True)

        retry = manager.accept("adam", workouts, _body("workouts"))

        assert retry.duplicate is True
        assert retry.pair_ready is False
        assert len(ready) == 1

    def test_next_cycle_waits_until_both_uploads_advance(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        ready: list[tuple[str, str]] = []
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=600,
            on_pair_ready=lambda name, digest: ready.append((name, digest)),
        )
        first_session = "2589AA62-C39F-4FF2-8BC7-2D11D5488A7F"
        second_metrics_session = "3589AA62-C39F-4FF2-8BC7-2D11D5488A7F"
        second_workouts_session = "4589AA62-C39F-4FF2-8BC7-2D11D5488A7F"
        first_metrics_body = _body("metrics")
        second_metrics_payload = json.loads(first_metrics_body)
        second_metrics_payload["data"]["metrics"][0]["data"][0]["qty"] = 2345
        second_metrics_body = json.dumps(second_metrics_payload).encode()

        manager.accept(
            "adam",
            validate_upload(
                _headers("metrics", session_id=first_session), first_metrics_body
            ),
            first_metrics_body,
        )
        manager.accept(
            "adam",
            validate_upload(
                _headers("workouts", session_id=first_session), _body("workouts")
            ),
            _body("workouts"),
        )
        first_digest = ready[-1][1]
        assert manager.begin_import("adam", first_digest) is not None
        manager.finish_import("adam", first_digest, success=True)

        metrics_only = manager.accept(
            "adam",
            validate_upload(
                _headers("metrics", session_id=second_metrics_session),
                second_metrics_body,
            ),
            second_metrics_body,
        )

        assert metrics_only.pair_ready is False
        assert manager.pending_pairs() == []
        assert len(ready) == 1

        complete_pair = manager.accept(
            "adam",
            validate_upload(
                _headers("workouts", session_id=second_workouts_session),
                _body("workouts"),
            ),
            _body("workouts"),
        )

        assert complete_pair.pair_ready is True
        assert len(ready) == 2

    def test_next_cycle_does_not_reuse_half_of_an_inflight_pair(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        ready: list[tuple[str, str]] = []
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=600,
            on_pair_ready=lambda name, digest: ready.append((name, digest)),
        )
        first_metrics = validate_upload(
            _headers("metrics", session_id="2589AA62-C39F-4FF2-8BC7-2D11D5488A7F"),
            _body("metrics"),
        )
        first_workouts = validate_upload(
            _headers("workouts", session_id="3589AA62-C39F-4FF2-8BC7-2D11D5488A7F"),
            _body("workouts"),
        )
        manager.accept("adam", first_metrics, _body("metrics"))
        manager.accept("adam", first_workouts, _body("workouts"))
        first_digest = ready[-1][1]
        assert manager.begin_import("adam", first_digest) is not None

        next_metrics_payload = json.loads(_body("metrics"))
        next_metrics_payload["data"]["metrics"][0]["data"][0]["qty"] = 2345
        next_metrics_body = json.dumps(next_metrics_payload).encode()
        metrics_only = manager.accept(
            "adam",
            validate_upload(
                _headers("metrics", session_id="4589AA62-C39F-4FF2-8BC7-2D11D5488A7F"),
                next_metrics_body,
            ),
            next_metrics_body,
        )

        assert metrics_only.pair_ready is False

        complete_pair = manager.accept(
            "adam",
            validate_upload(
                _headers("workouts", session_id="5589AA62-C39F-4FF2-8BC7-2D11D5488A7F"),
                _body("workouts"),
            ),
            _body("workouts"),
        )

        assert complete_pair.pair_ready is True
        assert len(ready) == 2

    def test_existing_receipt_gains_generation_markers_before_next_cycle(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        ready: list[tuple[str, str]] = []
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=600,
            on_pair_ready=lambda name, digest: ready.append((name, digest)),
        )
        manager.accept(
            "adam",
            validate_upload(_headers("metrics"), _body("metrics")),
            _body("metrics"),
        )
        manager.accept(
            "adam",
            validate_upload(_headers("workouts"), _body("workouts")),
            _body("workouts"),
        )
        first_digest = ready[-1][1]
        assert manager.begin_import("adam", first_digest) is not None
        manager.finish_import("adam", first_digest, success=True)

        state_path = profile.http_cache / ".ingest_state.json"
        legacy_state = json.loads(state_path.read_text(encoding="utf-8"))
        legacy_state.pop("last_import_uploads")
        state_path.write_text(json.dumps(legacy_state), encoding="utf-8")
        restarted = HttpIngestManager(
            {"adam": profile},
            pair_window_s=600,
            on_pair_ready=lambda name, digest: ready.append((name, digest)),
        )
        next_metrics_payload = json.loads(_body("metrics"))
        next_metrics_payload["data"]["metrics"][0]["data"][0]["qty"] = 2345
        next_metrics_body = json.dumps(next_metrics_payload).encode()

        metrics_only = restarted.accept(
            "adam",
            validate_upload(
                _headers("metrics", session_id="3589AA62-C39F-4FF2-8BC7-2D11D5488A7F"),
                next_metrics_body,
            ),
            next_metrics_body,
        )

        assert metrics_only.pair_ready is False
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert set(persisted["last_import_uploads"]) == {"metrics", "workouts"}


class TestPairStatus:
    def test_reports_which_half_is_still_missing(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=600,
            on_pair_ready=lambda name, digest: None,
        )

        manager.accept(
            "adam",
            validate_upload(_headers("metrics"), _body("metrics")),
            _body("metrics"),
        )

        state = manager.status()["adam"]
        assert state["pair_state"] == "waiting"
        assert "Workouts" in state["pair_detail"]

    def test_reports_split_when_halves_straddle_the_pair_window(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        profile = _profile(tmp_path)
        ready: list[tuple[str, str]] = []
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=600,
            on_pair_ready=lambda name, digest: ready.append((name, digest)),
        )
        manager.accept(
            "adam",
            validate_upload(_headers("metrics"), _body("metrics")),
            _body("metrics"),
        )
        state_path = profile.http_cache / ".ingest_state.json"
        stale = json.loads(state_path.read_text(encoding="utf-8"))
        stale["uploads"]["metrics"]["received_at"] = "2026-08-02T08:00:00+00:00"
        state_path.write_text(json.dumps(stale), encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="http_ingest"):
            accepted = manager.accept(
                "adam",
                validate_upload(_headers("workouts"), _body("workouts")),
                _body("workouts"),
            )

        assert accepted.pair_ready is False
        assert ready == []
        state = manager.status()["adam"]
        assert state["pair_state"] == "split"
        assert "pairing window" in state["pair_detail"]
        assert "will not import yet" in caplog.text


class TestHttpImportSafety:
    def test_refuses_direct_unpaired_http_cache_import(self, tmp_path: Path) -> None:
        args = argparse.Namespace(
            source="http",
            data_dir=str(tmp_path),
            db=str(tmp_path / "health.db"),
            http_pair_verified=False,
        )

        with pytest.raises(SystemExit):
            cmd_import(args)

    def test_daemon_report_refresh_skips_unpaired_http_cache(
        self, tmp_path: Path
    ) -> None:
        runtime = ProfileRuntime(
            "test-model",
            tmp_path / "health.db",
            tmp_path / "context",
            health_dir=tmp_path / "Imports/http",
            import_source="http",
        )

        with patch("commands.cmd_import") as command:
            result = runtime._runners._run_import()

        command.assert_not_called()
        assert result is not None
        assert result.import_skipped is True


class TestHttpIngestServer:
    def test_refuses_non_loopback_bind(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        registry = TokenRegistry(tmp_path / "ingest_tokens.json")
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=600,
            on_pair_ready=lambda _name, _digest: None,
        )

        with pytest.raises(ValueError, match="must bind to 127.0.0.1"):
            HttpIngestServer(
                "0.0.0.0",
                0,
                registry=registry,
                manager=manager,
                max_bytes=1024 * 1024,
            )

    def test_authenticates_and_accepts_without_exposing_profile(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        registry = TokenRegistry(tmp_path / "ingest_tokens.json")
        token = registry.create_token("adam")
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=600,
            on_pair_ready=lambda _name, _digest: None,
        )
        server = HttpIngestServer(
            "127.0.0.1",
            0,
            registry=registry,
            manager=manager,
            max_bytes=1024 * 1024,
        )
        server.start()
        host, port = server.address
        try:
            connection = http.client.HTTPConnection(host, port, timeout=5)
            body = _body("metrics")
            headers = {
                **_headers("metrics"),
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            connection.request("POST", "/v1/auto-export", body=body, headers=headers)
            response = connection.getresponse()
            result = json.loads(response.read())
            connection.close()
        finally:
            server.stop()

        assert response.status == 202
        assert result == {
            "accepted": "metrics",
            "duplicate": False,
            "pair_ready": False,
        }
        assert "profile" not in result

    def test_rejects_invalid_token_before_storing_body(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        registry = TokenRegistry(tmp_path / "ingest_tokens.json")
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=600,
            on_pair_ready=lambda _name, _digest: None,
        )
        server = HttpIngestServer(
            "127.0.0.1",
            0,
            registry=registry,
            manager=manager,
            max_bytes=1024 * 1024,
        )
        server.start()
        host, port = server.address
        try:
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request(
                "POST",
                "/v1/auto-export",
                body=_body("metrics"),
                headers={
                    **_headers("metrics"),
                    "Authorization": "Bearer wrong",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            response.read()
            connection.close()
        finally:
            server.stop()

        assert response.status == 401
        assert not profile.http_cache.exists()
