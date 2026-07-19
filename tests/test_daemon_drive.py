"""Tests for Google Drive polling in the daemon."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import daemon as daemon_module
from commands import ImportResult
from daemon import ZdrowskitDaemon


def _make_drive_daemon(tmp_path: Path) -> ZdrowskitDaemon:
    """Create a daemon configured for Drive without real credentials."""
    daemon_module.STATE_FILE = tmp_path / "state.json"
    daemon = ZdrowskitDaemon(
        "test-model",
        tmp_path / "health.db",
        tmp_path / "context",
        health_dir=tmp_path / "import",
        import_source="google-drive",
        google_drive_service_account=tmp_path / "service-account.json",
        google_drive_metrics_folder_id="metrics-folder",
        google_drive_workouts_folder_id="workouts-folder",
        google_drive_poll_interval_s=300,
    )
    daemon._notification_prefs_path = tmp_path / "notification_prefs.json"
    return daemon


class TestDriveDaemonConfiguration:
    def test_rejects_missing_drive_settings(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="WORKOUTS_FOLDER_ID"):
            ZdrowskitDaemon(
                "test-model",
                tmp_path / "health.db",
                tmp_path,
                import_source="google-drive",
                google_drive_service_account=tmp_path / "service-account.json",
                google_drive_metrics_folder_id="metrics-folder",
            )

    def test_rejects_non_positive_poll_interval(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="greater than zero"):
            ZdrowskitDaemon(
                "test-model",
                tmp_path / "health.db",
                tmp_path,
                google_drive_poll_interval_s=0,
            )


class TestDrivePolling:
    def test_runner_passes_drive_configuration_to_import(self, tmp_path: Path) -> None:
        daemon = _make_drive_daemon(tmp_path)
        expected = ImportResult(
            source="google-drive",
            drive_files_downloaded=0,
            drive_files_skipped=4,
            parsed_days=0,
            stored_days=0,
            import_skipped=True,
        )

        with (
            patch("commands.cmd_import", return_value=expected) as cmd_import,
            patch.object(daemon._runners, "_data_snapshot", return_value={}),
        ):
            result = daemon._runners._run_import(
                skip_if_drive_unchanged=True,
                record_no_changes=False,
            )

        assert result == expected
        args = cmd_import.call_args.args[0]
        assert args.source == "google-drive"
        assert args.google_drive_metrics_folder_id == "metrics-folder"
        assert args.google_drive_workouts_folder_id == "workouts-folder"
        assert args.skip_if_drive_unchanged is True

    def test_changed_files_import_and_trigger_nudge(self, tmp_path: Path) -> None:
        daemon = _make_drive_daemon(tmp_path)
        result = ImportResult(
            source="google-drive",
            drive_files_downloaded=2,
            drive_files_skipped=8,
            parsed_days=14,
            stored_days=14,
        )
        before = {"daily_count": 10, "daily_max": "2026-07-18"}
        after = {"daily_count": 11, "daily_max": "2026-07-19"}

        with (
            patch.object(
                daemon._runners, "_data_snapshot", side_effect=[before, after]
            ),
            patch.object(
                daemon._runners, "_run_import", return_value=result
            ) as run_import,
            patch.object(
                daemon._runners,
                "_format_data_delta",
                return_value="1 new day",
            ),
            patch.object(daemon._runners, "_run_nudge") as run_nudge,
            patch.object(daemon, "_record_event") as record_event,
        ):
            success = daemon._poll_google_drive_once(force_import=False)

        assert success is True
        run_import.assert_called_once_with(
            skip_if_drive_unchanged=True,
            record_no_changes=False,
        )
        run_nudge.assert_called_once_with("new_data", trigger_context="1 new day")
        assert record_event.call_args.args[:3] == (
            "import",
            "drive_update",
            "Google Drive fetched 2 changed file(s)",
        )

    def test_unchanged_poll_does_not_nudge(self, tmp_path: Path) -> None:
        daemon = _make_drive_daemon(tmp_path)
        result = ImportResult(
            source="google-drive",
            drive_files_downloaded=0,
            drive_files_skipped=10,
            parsed_days=0,
            stored_days=0,
            import_skipped=True,
        )

        with (
            patch.object(daemon._runners, "_data_snapshot", return_value={}),
            patch.object(daemon._runners, "_run_import", return_value=result),
            patch.object(daemon._runners, "_run_nudge") as run_nudge,
            patch.object(daemon, "_record_event") as record_event,
        ):
            success = daemon._poll_google_drive_once(force_import=False)

        assert success is True
        run_nudge.assert_not_called()
        record_event.assert_not_called()

    def test_poll_failure_keeps_full_import_for_retry(self, tmp_path: Path) -> None:
        daemon = _make_drive_daemon(tmp_path)

        with (
            patch.object(
                daemon._drive,
                "poll_once",
                side_effect=[False, True],
            ) as poll,
            patch.object(daemon._stop_event, "is_set", return_value=False),
            patch.object(daemon._stop_event, "wait", side_effect=[False, True]),
        ):
            daemon._google_drive_poll_loop()

        assert [call.kwargs for call in poll.call_args_list] == [
            {"force_import": True},
            {"force_import": True},
        ]

    def test_successful_first_poll_enables_incremental_polls(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_drive_daemon(tmp_path)

        with (
            patch.object(
                daemon._drive,
                "poll_once",
                return_value=True,
            ) as poll,
            patch.object(daemon._stop_event, "is_set", return_value=False),
            patch.object(daemon._stop_event, "wait", side_effect=[False, True]),
        ):
            daemon._google_drive_poll_loop()

        assert [call.kwargs for call in poll.call_args_list] == [
            {"force_import": True},
            {"force_import": False},
        ]
