"""Tests for authenticated, paired, bounded Auto Export HTTP ingestion."""

from __future__ import annotations

import http.client
import json
import argparse
import base64
import gzip
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import http_ingest
from commands import cmd_import
from config import HTTP_INGEST_BATCH_CAPTURE_MAX_FILES
from daemon import ProfileRuntime
from http_ingest import (
    HttpIngestManager,
    assess_ingest_health,
    IngestError,
    TokenRegistry,
    newest_expected_day,
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


def _body(kind: str, *, variant: int = 0) -> bytes:
    """Return a minimal payload accepted by the production parser.

    Args:
        kind: Either ``metrics`` or ``workouts``.
        variant: Nudges one quantity so a later upload of the same kind hashes
            differently, the way a real refreshed export does.
    """
    if kind == "metrics":
        data = {
            "metrics": [
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [
                        {"date": "2026-08-02 00:00:00 +0000", "qty": 1234 + variant}
                    ],
                }
            ]
        }
    else:
        data = {
            "workouts": [
                {
                    "name": "Outdoor Walk",
                    "start": "2026-08-02 08:00:00 +0000",
                    "duration": 1800 + variant,
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

        with pytest.raises(IngestError, match="must be Hours"):
            validate_upload(headers, _body("metrics"))

    @pytest.mark.parametrize("aggregation", ["Hours", "hours", "Days", "days"])
    def test_metrics_accepts_hours_or_days(self, aggregation: str) -> None:
        """Hours is the documented setting; Days stays accepted.

        Regression cover for 2026-08-21: the receiver hard-required Days, so
        following the setup guide's move to Hours made every Metrics upload
        fail with 422 while Workouts kept succeeding. Days remains valid
        because it was the documented setting beforehand and every daily total
        parses identically either way.
        """
        headers = _headers("metrics")
        headers["automation-aggregation"] = aggregation

        assert validate_upload(headers, _body("metrics")).kind == "metrics"

    @pytest.mark.parametrize("aggregation", ["Hours", "Days", "Default"])
    def test_workouts_still_require_minutes(self, aggregation: str) -> None:
        """Coarser workout bins silently empty every split's HR and cadence."""
        headers = _headers("workouts")
        headers["automation-aggregation"] = aggregation

        with pytest.raises(IngestError, match="must be Minutes"):
            validate_upload(headers, _body("workouts"))

    @pytest.mark.parametrize(
        "period", ["Default", "Last 7 Days", "Last 30 Days", "Previous 7 Days"]
    )
    def test_accepts_any_date_range(self, period: str) -> None:
        # A wider Metrics window turns an outage into something recoverable;
        # oversized exports are caught by the entry caps, not by this header.
        headers = _headers("workouts")
        headers["automation-period"] = period

        assert validate_upload(headers, _body("workouts")).kind == "workouts"

    def test_rejects_a_missing_period_header(self) -> None:
        headers = _headers("workouts")
        headers["automation-period"] = "  "

        with pytest.raises(IngestError, match="automation-period"):
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

    def test_next_cycle_imports_a_refreshed_half_against_its_partner(
        self, tmp_path: Path
    ) -> None:
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

        # The Workouts half is unchanged but still well inside the pairing
        # window, so it is current data. Holding the new Metrics back until
        # Workouts also changed stranded whichever half arrived second.
        assert metrics_only.pair_ready is True
        assert len(ready) == 2
        second_digest = ready[-1][1]
        assert manager.begin_import("adam", second_digest) is not None
        manager.finish_import("adam", second_digest, success=True)

        unchanged_pair = manager.accept(
            "adam",
            validate_upload(
                _headers("workouts", session_id=second_workouts_session),
                _body("workouts"),
            ),
            _body("workouts"),
        )

        # A new session id over identical bytes is a resend, not new data.
        assert unchanged_pair.pair_ready is False
        assert manager.pending_pairs() == []
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

    def test_failed_import_stays_visible_until_a_retry_succeeds(
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
        digest = ready[-1][1]
        manager.begin_import("adam", digest)
        manager.finish_import("adam", digest, success=False, error="disk full")

        next_body = json.dumps(
            {
                "data": {
                    "metrics": [
                        {
                            "name": "step_count",
                            "units": "count",
                            "data": [{"date": "2026-08-03 00:00:00 +0000", "qty": 999}],
                        }
                    ]
                }
            }
        ).encode()
        manager.accept(
            "adam",
            validate_upload(
                _headers("metrics", session_id="3589AA62-C39F-4FF2-8BC7-2D11D5488A7F"),
                next_body,
            ),
            next_body,
        )

        assert manager.status()["adam"]["last_error"]["message"] == "disk full"

        retry_digest = ready[-1][1]
        manager.begin_import("adam", retry_digest)
        manager.finish_import("adam", retry_digest, success=True)

        assert manager.status()["adam"]["last_error"] is None


class TestPayloadArchive:
    def _manager(self, profile: Profile) -> HttpIngestManager:
        """Return a manager that stages uploads for one profile."""
        return HttpIngestManager(
            {profile.name: profile},
            pair_window_s=600,
            on_pair_ready=lambda name, digest: None,
        )

    def _archived(self, profile: Profile, kind: str) -> list[Path]:
        """Return every archived payload for one kind, oldest name first."""
        return sorted((profile.import_archive / kind).rglob("*.json.gz"))

    @pytest.mark.parametrize("kind", ["metrics", "workouts"])
    def test_archives_raw_payload_gzipped_and_recoverable(
        self, tmp_path: Path, kind: str
    ) -> None:
        profile = _profile(tmp_path)
        manager = self._manager(profile)

        manager.accept(
            profile.name, validate_upload(_headers(kind), _body(kind)), _body(kind)
        )

        archived = self._archived(profile, kind)
        assert len(archived) == 1
        assert gzip.decompress(archived[0].read_bytes()) == _body(kind)

    def test_archive_is_one_file_per_kind_named_for_the_day(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        manager = self._manager(profile)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        manager.accept(
            profile.name,
            validate_upload(_headers("metrics"), _body("metrics")),
            _body("metrics"),
        )

        archived = self._archived(profile, "metrics")[0]
        assert archived.name == f"{today}.json.gz"
        assert archived.parent == profile.import_archive / "metrics"

    def test_repeated_uploads_collapse_to_the_last_one_of_the_day(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        manager = self._manager(profile)
        # Auto Export re-sends a rolling window every few minutes and does not
        # order JSON keys stably, so unchanged content still arrives as fresh
        # bytes. One snapshot a day is the point; the next day's covers today.
        earlier = _body("metrics")
        later = json.dumps(
            {
                "data": {
                    "metrics": [
                        {
                            "name": "step_count",
                            "units": "count",
                            "data": [
                                {"date": "2026-08-02 00:00:00 +0000", "qty": 5678}
                            ],
                        }
                    ]
                }
            }
        ).encode("utf-8")

        for body in (earlier, earlier, later):
            manager.accept(
                profile.name, validate_upload(_headers("metrics"), body), body
            )

        archived = self._archived(profile, "metrics")
        assert len(archived) == 1
        assert gzip.decompress(archived[0].read_bytes()) == later

    def test_archive_failure_still_accepts_and_imports_the_upload(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        profile = _profile(tmp_path)
        manager = self._manager(profile)

        real_write = http_ingest._atomic_write

        def fail_only_the_archive(path: Path, data: bytes, **kwargs: object) -> None:
            """Break archive writes while leaving the ingest path working."""
            if profile.import_archive in path.parents:
                raise OSError("archive volume full")
            real_write(path, data, **kwargs)  # type: ignore[arg-type]

        with caplog.at_level(logging.ERROR):
            with patch("http_ingest._atomic_write", side_effect=fail_only_the_archive):
                accepted = manager.accept(
                    profile.name,
                    validate_upload(_headers("metrics"), _body("metrics")),
                    _body("metrics"),
                )

        assert accepted.kind == "metrics"
        assert self._archived(profile, "metrics") == []
        assert (profile.http_cache / "Metrics" / "latest.json").read_bytes() == _body(
            "metrics"
        )
        assert "will not be replayable" in caplog.text


_PAIR_WINDOW_S = 60 * 60
"""Production pairing window, so health messages are asserted against it."""

_NOW = datetime.now(timezone.utc)
"""Reference moment for deriving stored-data dates relative to today."""


class TestIngestHealth:
    def _state(self, profile: Profile, payload: dict) -> None:
        """Write an ingest state file for the profile."""
        path = profile.http_cache / ".ingest_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, **payload}), encoding="utf-8")

    def _at(self, hours_ago: float) -> str:
        """Return an ISO timestamp *hours_ago* in the past."""
        return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()

    def _current(self, days_behind: int = 0) -> str:
        """Return a stored-data date *days_behind* the newest day owed."""
        return (newest_expected_day(_NOW) - timedelta(days=days_behind)).isoformat()

    def _assess(self, profile: Profile, **kwargs):
        """Assess a profile, defaulting every knob the case does not exercise."""
        kwargs.setdefault("newest_data_date", self._current())
        kwargs.setdefault("stale_after_days", 1)
        kwargs.setdefault("split_after_h", 6)
        kwargs.setdefault("pair_window_s", _PAIR_WINDOW_S)
        return assess_ingest_health(profile, **kwargs)

    def test_a_profile_that_never_uploaded_is_not_nagged(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        self._state(profile, {"uploads": {}, "receipts": []})

        # Mid-setup, not broken — and no stored data yet, which must not read as
        # a fault while onboarding is still in progress.
        health = self._assess(profile, newest_data_date=None)

        assert health.is_alerting is False

    def test_a_first_upload_awaiting_its_first_import_is_not_nagged(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {
                    "metrics": {
                        "received_at": self._at(0.05),
                        "first_seen_at": self._at(0.05),
                    }
                }
            },
        )

        # Minutes into onboarding: an upload has landed, no pair has completed a
        # cycle, and nothing is stored yet. Reporting "no health data has ever
        # been stored" here would greet a new user with a fault. Waiting past the
        # split threshold is a real fault, and that check owns the call.
        assert self._assess(profile, newest_data_date=None).is_alerting is False

    def test_recent_import_is_healthy(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {
                    "metrics": {"received_at": self._at(0.2)},
                    "workouts": {"received_at": self._at(0.2)},
                },
                "last_imported_at": self._at(0.2),
            },
        )

        assert self._assess(profile).is_alerting is False

    @pytest.mark.parametrize("hours_quiet", [9, 16, 24, 35])
    def test_silence_with_current_data_is_never_an_alert(
        self, tmp_path: Path, hours_quiet: float
    ) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {
                    "metrics": {"received_at": self._at(hours_quiet)},
                    "workouts": {"received_at": self._at(hours_quiet)},
                },
                "last_imported_at": self._at(hours_quiet),
            },
        )

        # Auto Export batches unpredictably: measured over twelve days the gap
        # between imports hit 34.9h without ever losing a day. Hours of quiet
        # are not evidence of anything while the days themselves are all here.
        assert self._assess(profile).is_alerting is False

    @pytest.mark.parametrize("hours_quiet", [7, 12, 23])
    def test_quiet_after_a_clean_import_is_never_blamed_on_pairing(
        self, tmp_path: Path, hours_quiet: int
    ) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {
                    "metrics": {"received_at": self._at(hours_quiet)},
                    "workouts": {"received_at": self._at(hours_quiet)},
                },
                "last_imported_at": self._at(hours_quiet - 0.01),
            },
        )

        # Everything that arrived did import. Telling someone to realign two
        # automations that fired simultaneously sends them chasing a non-bug.
        assert self._assess(profile).is_alerting is False

    def test_a_missing_day_alerts_even_though_uploads_look_fine(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {
                    "metrics": {"received_at": self._at(0.2)},
                    "workouts": {"received_at": self._at(0.2)},
                },
                "last_imported_at": self._at(0.2),
            },
        )

        # The exact case the old hours-based check reported as healthy: uploads
        # arriving and importing on schedule, carrying nothing for yesterday.
        health = self._assess(profile, newest_data_date=self._current(1))

        assert health.status == "stale"
        assert "Auto Export" in health.detail
        assert health.missing_from == newest_expected_day(_NOW).isoformat()
        assert health.missing_to == newest_expected_day(_NOW).isoformat()

    def test_a_multi_day_gap_names_its_range(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {"metrics": {"received_at": self._at(0.2)}},
                "last_imported_at": self._at(0.1),
            },
        )

        health = self._assess(profile, newest_data_date=self._current(3))

        assert health.status == "stale"
        assert "(3 days)" in health.detail
        assert health.missing_to == newest_expected_day(_NOW).isoformat()

    def test_the_two_day_threshold_tolerates_a_single_missing_day(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {"metrics": {"received_at": self._at(0.2)}},
                "last_imported_at": self._at(0.1),
            },
        )

        assert (
            self._assess(
                profile, newest_data_date=self._current(1), stale_after_days=2
            ).is_alerting
            is False
        )

    @pytest.mark.parametrize(
        ("hour", "alerting"),
        [(9, False), (11, True)],
    )
    def test_yesterday_is_only_owed_once_the_morning_window_closes(
        self, tmp_path: Path, hour: int, alerting: bool
    ) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {"metrics": {"received_at": self._at(0.2)}},
                "last_imported_at": self._at(0.1),
            },
        )
        now = datetime(2026, 8, 14, hour, 0).astimezone()

        # Data runs through the 12th. Before the cutoff the 13th is merely late.
        health = assess_ingest_health(
            profile,
            newest_data_date="2026-08-12",
            stale_after_days=1,
            split_after_h=6,
            pair_window_s=_PAIR_WINDOW_S,
            now=now,
        )

        assert health.is_alerting is alerting

    def test_today_is_never_counted_as_missing(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {"metrics": {"received_at": self._at(0.2)}},
                "last_imported_at": self._at(0.1),
            },
        )
        now = datetime(2026, 8, 14, 23, 0).astimezone()

        # A day still in progress owes nothing, even late in the evening.
        health = assess_ingest_health(
            profile,
            newest_data_date="2026-08-13",
            stale_after_days=1,
            split_after_h=6,
            pair_window_s=_PAIR_WINDOW_S,
            now=now,
        )

        assert health.is_alerting is False

    def test_importing_uploads_that_store_nothing_is_a_fault(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {
                    "metrics": {"received_at": self._at(0.2)},
                    "workouts": {"received_at": self._at(0.2)},
                },
                "last_imported_at": self._at(0.1),
            },
        )

        # Pairs import cleanly and the database stays empty: a parser problem
        # the old check could not see, because it only watched the pipe.
        health = self._assess(profile, newest_data_date=None)

        assert health.status == "stale"
        assert "no daily health metrics have ever been stored" in health.detail

    def test_halves_outside_the_window_are_blamed_on_the_schedules(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {
                    "metrics": {"received_at": self._at(0.1)},
                    "workouts": {"received_at": self._at(1.8)},
                },
                "last_imported_at": self._at(9),
            },
        )

        health = self._assess(profile)

        assert health.status == "split"
        assert "same schedule" in health.detail

    def test_halves_inside_the_window_are_not_blamed_on_the_schedules(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {
                    "metrics": {"received_at": self._at(0.1)},
                    "workouts": {"received_at": self._at(0.45)},
                },
                "last_imported_at": self._at(9),
            },
        )

        health = self._assess(profile)

        # 21m apart inside a 60m window pairs fine. Sending someone to realign
        # automations that are already aligned is how this alert wasted a day.
        assert health.status == "split"
        assert "same schedule" not in health.detail
        assert "the import on this end is stuck" in health.detail

    def test_a_stalled_pipe_outranks_the_staleness_it_causes(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {
                    "metrics": {"received_at": self._at(0.1)},
                    "workouts": {"received_at": self._at(1.8)},
                },
                "last_imported_at": self._at(30),
            },
        )

        # Both are true, but only one names the fix. "Set both automations to
        # the same schedule" beats "days are missing" when the schedules are
        # demonstrably why they are missing.
        health = self._assess(profile, newest_data_date=self._current(2))

        assert health.status == "split"

    def test_one_half_never_configured_is_reported_as_split(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        # Metrics keeps uploading every few minutes; Workouts was never set up.
        # Only the latest upload is stored, so the age of the problem comes from
        # first_seen_at, not received_at.
        self._state(
            profile,
            {
                "uploads": {
                    "metrics": {
                        "received_at": self._at(0.1),
                        "first_seen_at": self._at(30),
                    }
                }
            },
        )

        health = self._assess(profile)

        assert health.status == "split"
        assert "Workouts" in health.detail

    def test_failed_import_outranks_the_generic_split_message(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        self._state(
            profile,
            {
                "uploads": {
                    "metrics": {"received_at": self._at(0.1)},
                    "workouts": {"received_at": self._at(0.1)},
                },
                "last_imported_at": self._at(9),
                "last_error": {
                    "failed_at": self._at(1),
                    "message": "parser rejected the payload",
                },
            },
        )

        health = self._assess(profile)

        assert health.status == "error"
        assert "parser rejected the payload" in health.detail

    def test_unreadable_state_reports_rather_than_raising(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        path = profile.http_cache / ".ingest_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")

        health = self._assess(profile)

        assert health.status == "error"
        assert str(path) in health.detail


class TestNewestExpectedDay:
    def test_before_the_morning_cutoff_yesterday_is_still_allowed_to_arrive(
        self,
    ) -> None:
        now = datetime(2026, 8, 14, 7, 30).astimezone()

        assert newest_expected_day(now).isoformat() == "2026-08-12"

    def test_after_the_morning_cutoff_yesterday_is_owed(self) -> None:
        now = datetime(2026, 8, 14, 10, 0).astimezone()

        assert newest_expected_day(now).isoformat() == "2026-08-13"


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

    def _import_one_pair(self, manager: HttpIngestManager) -> None:
        """Drive a first Workouts + Metrics pair all the way through import."""
        for kind in ("workouts", "metrics"):
            body = _body(kind)
            accepted = manager.accept(
                "adam", validate_upload(_headers(kind), body), body
            )
        assert accepted.pair_ready is True
        digest = manager.pending_pairs()[0][1]
        assert manager.begin_import("adam", digest) is not None
        manager.finish_import("adam", digest, success=True)

    def test_a_refreshed_half_imports_without_waiting_for_its_partner(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=_PAIR_WINDOW_S,
            on_pair_ready=lambda name, digest: None,
        )
        self._import_one_pair(manager)

        # Workouts fires minutes after Metrics, as two automations normally do.
        # Requiring both halves to be new stranded this payload until the next
        # Metrics upload, which is how a whole day of data went missing.
        fresh = _body("workouts", variant=1)
        accepted = manager.accept(
            "adam", validate_upload(_headers("workouts"), fresh), fresh
        )

        assert accepted.pair_ready is True
        assert manager.status()["adam"]["pair_state"] == "ready"

    def test_an_unchanged_re_upload_does_not_import_again(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=_PAIR_WINDOW_S,
            on_pair_ready=lambda name, digest: None,
        )
        self._import_one_pair(manager)

        # Auto Export resends a rolling window; an identical pair is a no-op.
        again = _body("workouts")
        accepted = manager.accept(
            "adam", validate_upload(_headers("workouts"), again), again
        )

        assert accepted.pair_ready is False
        assert manager.status()["adam"]["pair_state"] == "imported"

    def test_a_pair_awaiting_import_is_not_reported_as_up_to_date(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=_PAIR_WINDOW_S,
            on_pair_ready=lambda name, digest: None,
        )
        for kind in ("workouts", "metrics"):
            body = _body(kind)
            manager.accept("adam", validate_upload(_headers(kind), body), body)
        digest = manager.pending_pairs()[0][1]
        manager.begin_import("adam", digest)

        state = manager.status()["adam"]

        # Nothing has been imported yet. Calling this "up to date" is what hid
        # the stall behind a reassuring `ingest status`.
        assert state["pair_state"] == "pending"
        assert state["last_imported_at"] is None


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


class TestBatchCaptureEndpoint:
    """The batch path stores raw requests and imports nothing."""

    def _serve(self, tmp_path: Path):
        """Start a receiver and return (server, profile, token)."""
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
        return server, profile, token

    def _post(self, server, token: str, body: bytes, headers: dict | None = None):
        """POST one request to the batch capture path."""
        host, port = server.address
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request(
            "POST",
            "/v1/auto-export-batch",
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, payload

    def test_captures_body_verbatim_without_importing(self, tmp_path: Path) -> None:
        """An arbitrary body is stored and nothing is staged for import."""
        server, profile, token = self._serve(tmp_path)
        # Deliberately not a valid Auto Export payload: the endpoint must not
        # care, since its whole purpose is capturing an unknown format.
        body = b'{"batch": 3, "of": 12, "data": {"metrics": []}}'
        try:
            status, raw = self._post(server, token, body)
        finally:
            server.stop()

        assert status == 200
        result = json.loads(raw)
        assert result["captured"] is True
        assert result["bytes"] == len(body)

        captures = list(profile.batch_capture.glob("*.json"))
        assert len(captures) == 1
        record = json.loads(captures[0].read_text())
        assert base64.b64decode(record["body_base64"]) == body
        assert record["sha256"] == hashlib.sha256(body).hexdigest()
        # Nothing entered the import pipeline.
        assert not profile.http_cache.exists()
        assert not profile.import_archive.exists()

    def test_authorization_header_is_not_written_to_disk(self, tmp_path: Path) -> None:
        """The capture file is a plain-text artefact and must not hold the token."""
        server, profile, token = self._serve(tmp_path)
        try:
            self._post(server, token, b'{"a": 1}', {"Automation-Name": "Metrics"})
        finally:
            server.stop()

        record = json.loads(next(profile.batch_capture.glob("*.json")).read_text())
        assert "authorization" not in record["headers"]
        assert token not in json.dumps(record)
        # Non-secret headers are kept, since they are what we came to inspect.
        assert record["headers"]["automation-name"] == "Metrics"

    def test_multiple_requests_are_each_kept(self, tmp_path: Path) -> None:
        """Batch mode sends many requests; none may overwrite another."""
        server, profile, token = self._serve(tmp_path)
        try:
            for index in range(5):
                status, _ = self._post(
                    server, token, json.dumps({"batch": index}).encode()
                )
                assert status == 200
        finally:
            server.stop()

        assert len(list(profile.batch_capture.glob("*.json"))) == 5

    def test_rejects_invalid_token_before_writing(self, tmp_path: Path) -> None:
        """Auth runs before any disk write, as on the real upload path."""
        server, profile, _token = self._serve(tmp_path)
        host, port = server.address
        try:
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request(
                "POST",
                "/v1/auto-export-batch",
                body=b'{"a": 1}',
                headers={
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
        assert not profile.batch_capture.exists()

    def test_prunes_to_the_retention_ceiling(self, tmp_path: Path) -> None:
        """The directory is bounded so a stray automation cannot fill the disk."""
        profile = _profile(tmp_path)
        manager = HttpIngestManager(
            {"adam": profile},
            pair_window_s=600,
            on_pair_ready=lambda _name, _digest: None,
        )
        for index in range(HTTP_INGEST_BATCH_CAPTURE_MAX_FILES + 5):
            manager.capture_batch(
                "adam", {"automation-name": "Metrics"}, f'{{"n":{index}}}'.encode()
            )

        kept = list(profile.batch_capture.glob("*.json"))
        assert len(kept) == HTTP_INGEST_BATCH_CAPTURE_MAX_FILES


class TestFunnelDnsHealth:
    """Detect the outage where the Funnel's public DNS record disappears.

    Every local signal stays green through this: the receiver answers on
    loopback, `tailscale funnel status` says the Funnel is on, and MagicDNS
    resolves the hostname on the host. Four occurrences between 2026-08-02 and
    2026-08-16 each ran 26-35 hours and were only noticed by hand.
    """

    def _state(self, profile: Profile, payload: dict) -> None:
        path = profile.http_cache / ".ingest_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, **payload}), encoding="utf-8")

    def _quiet_for(self, profile: Profile, hours: float) -> None:
        """Write a state where the last upload landed and imported *hours* ago."""
        moment = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        self._state(
            profile,
            {
                "uploads": {
                    "metrics": {"received_at": moment, "sha256": "a"},
                    "workouts": {"received_at": moment, "sha256": "b"},
                },
                "last_imported_at": moment,
                "receipts": [],
            },
        )

    def _assess(self, profile: Profile, resolver, **kwargs):
        kwargs.setdefault("newest_data_date", newest_expected_day(_NOW).isoformat())
        kwargs.setdefault("stale_after_days", 30)
        kwargs.setdefault("split_after_h", 6)
        kwargs.setdefault("pair_window_s", _PAIR_WINDOW_S)
        return assess_ingest_health(profile, resolve_funnel_dns=resolver, **kwargs)

    def test_silence_plus_a_missing_record_is_reported_as_funnel(
        self, tmp_path: Path
    ) -> None:
        profile = _profile(tmp_path)
        self._quiet_for(profile, 30)

        health = self._assess(profile, lambda: (False, "no public DNS record for x"))

        assert health.status == "funnel"
        assert health.is_alerting is True
        assert "no public DNS record" in health.detail
        # The alert says when uploads stopped, not when the check noticed.
        assert health.since is not None

    def test_an_unreachable_resolver_is_never_a_fault(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        self._quiet_for(profile, 30)

        # A host that is itself offline cannot distinguish a missing record from
        # its own broken network. Reporting that as a Funnel outage would train
        # the owner to ignore the alert on the day it is right.
        health = self._assess(profile, lambda: (None, "could not reach the resolver"))

        assert health.status != "funnel"

    def test_a_resolving_record_is_not_a_fault(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        self._quiet_for(profile, 30)

        health = self._assess(profile, lambda: (True, "resolves to 1.2.3.4"))

        assert health.status != "funnel"

    def test_recent_uploads_skip_the_lookup_entirely(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        self._quiet_for(profile, 0.5)
        calls: list[int] = []

        def resolver() -> tuple[bool | None, str]:
            calls.append(1)
            return False, "no public DNS record for x"

        health = self._assess(profile, resolver)

        # An upload that just landed has already proved the record resolves, so
        # spending a network call to ask again is waste on every healthy cycle.
        assert calls == []
        assert health.status != "funnel"

    def test_omitting_the_resolver_disables_the_check(self, tmp_path: Path) -> None:
        profile = _profile(tmp_path)
        self._quiet_for(profile, 30)

        # Drive and local transports have no Funnel to be missing.
        health = assess_ingest_health(
            profile,
            newest_data_date=newest_expected_day(_NOW).isoformat(),
            stale_after_days=30,
            split_after_h=6,
            pair_window_s=_PAIR_WINDOW_S,
        )

        assert health.status != "funnel"
