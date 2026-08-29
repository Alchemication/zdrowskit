"""Tests for the daemon's complete HTTP-pair import orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from commands import ImportResult
from daemon_http import DaemonHttpIngestHandler
from http_ingest import validate_upload
from profiles import Profile


def _payload(kind: str) -> bytes:
    """Return a minimal parser-compatible Auto Export payload."""
    if kind == "metrics":
        data = {
            "metrics": [
                {
                    "name": "step_count",
                    "units": "count",
                    "data": [{"date": "2026-08-02 00:00:00 +0000", "qty": 1000}],
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
                }
            ]
        }
    return json.dumps({"data": data}).encode()


def _headers(kind: str) -> dict[str, str]:
    """Return required Auto Export headers."""
    return {
        "automation-name": kind.title(),
        "automation-aggregation": "Days" if kind == "metrics" else "Minutes",
        "automation-period": "Default",
        "automation-id": "788509F9-3AFA-42CE-850C-E4D2DDB02F12",
        "session-id": "2589AA62-C39F-4FF2-8BC7-2D11D5488A7F",
    }


class TestDaemonHttpIngestHandler:
    def test_complete_pair_imports_once_and_runs_new_data_flow(
        self, tmp_path: Path
    ) -> None:
        profile = Profile(
            "adam",
            11,
            tmp_path / "profiles/adam",
            operator=True,
            import_source="http",
        )
        runtime = MagicMock()
        runtime.name = "adam"
        runtime._state = {}
        runtime._runners._data_snapshot.side_effect = [
            {"daily_count": 1},
            {"daily_count": 2},
        ]
        runtime._runners._format_data_delta.return_value = "one new day"
        runtime._runners._run_import.return_value = ImportResult(
            source="http",
            drive_files_downloaded=0,
            drive_files_skipped=0,
            parsed_days=2,
            stored_days=2,
        )
        daemon = MagicMock()
        daemon.runtimes = {"adam": runtime}
        daemon._submit.side_effect = lambda queued_runtime, function, *args: function(
            *args
        )
        handler = DaemonHttpIngestHandler(daemon, {"adam": profile})
        assert handler._manager is not None

        for kind in ("metrics", "workouts"):
            body = _payload(kind)
            handler._manager.accept(
                "adam",
                validate_upload(_headers(kind), body),
                body,
            )

        runtime._runners._run_import.assert_called_once()
        generation = runtime._runners._run_import.call_args.kwargs["data_dir"]
        assert generation.exists() is False
        runtime._runners._run_nudge.assert_called_once_with(
            "new_data", trigger_context="one new day"
        )
        assert handler._manager.due_pairs() == []
