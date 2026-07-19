"""Tests for Google Drive Auto Export fetching and import wiring."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import commands as commands_module
import google_drive
from commands import cmd_import
from google_drive import DriveFetchError, DriveItem, FetchStats, fetch_health_export


def _drive_item(file_id: str, name: str, content: bytes) -> DriveItem:
    """Build stable Drive metadata for a test blob."""
    return DriveItem(
        id=file_id,
        name=name,
        mime_type="text/json",
        modified_time="2026-07-19T10:00:00Z",
        md5_checksum=f"md5-{file_id}",
        size=str(len(content)),
    )


class TestFetchHealthExport:
    def test_writes_import_layout_and_skips_current_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service_account = tmp_path / "service-account.json"
        service_account.write_text("{}", encoding="utf-8")
        data_dir = tmp_path / "import"
        metrics_content = json.dumps({"data": {"metrics": []}}).encode()
        workouts_content = json.dumps({"data": {"workouts": []}}).encode()
        metrics_item = _drive_item(
            "metrics-file", "Metrics-2026-29.json", metrics_content
        )
        workouts_item = _drive_item(
            "workouts-file", "Workouts-2026-29.json", workouts_content
        )
        children = {
            "metrics-folder": [metrics_item],
            "workouts-folder": [workouts_item],
        }
        downloads = {
            "metrics-file": metrics_content,
            "workouts-file": workouts_content,
        }

        monkeypatch.setattr(google_drive, "_access_token", lambda path: "token")
        monkeypatch.setattr(
            google_drive,
            "_children",
            lambda folder_id, **kwargs: children[folder_id],
        )
        download = patch.object(
            google_drive,
            "_download_blob",
            side_effect=lambda file_id, **kwargs: downloads[file_id],
        )

        with download as download_mock:
            first = fetch_health_export(
                metrics_folder_id="metrics-folder",
                workouts_folder_id="workouts-folder",
                data_dir=data_dir,
                service_account_path=service_account,
            )
            second = fetch_health_export(
                metrics_folder_id="metrics-folder",
                workouts_folder_id="workouts-folder",
                data_dir=data_dir,
                service_account_path=service_account,
            )

        assert first.downloaded == 2
        assert first.skipped == 0
        assert second.downloaded == 0
        assert second.skipped == 2
        assert download_mock.call_count == 2
        assert (
            data_dir / "Metrics" / metrics_item.name
        ).read_bytes() == metrics_content
        assert (data_dir / "Workouts" / workouts_item.name).read_bytes() == (
            workouts_content
        )
        manifest = json.loads(
            (data_dir / google_drive.MANIFEST_NAME).read_text(encoding="utf-8")
        )
        assert manifest[metrics_item.id]["path"] == f"Metrics/{metrics_item.name}"
        assert manifest[workouts_item.id]["path"] == (f"Workouts/{workouts_item.name}")

    def test_rejects_swapped_folder_payloads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service_account = tmp_path / "service-account.json"
        service_account.write_text("{}", encoding="utf-8")
        wrong_content = json.dumps({"data": {"workouts": []}}).encode()
        item = _drive_item("wrong", "wrong.json", wrong_content)
        empty_metrics = json.dumps({"data": {"metrics": []}}).encode()
        workouts_item = _drive_item("valid", "valid.json", empty_metrics)

        monkeypatch.setattr(google_drive, "_access_token", lambda path: "token")
        monkeypatch.setattr(
            google_drive,
            "_children",
            lambda folder_id, **kwargs: {
                "metrics-folder": [item],
                "workouts-folder": [workouts_item],
            }[folder_id],
        )
        monkeypatch.setattr(
            google_drive,
            "_download_blob",
            lambda file_id, **kwargs: {
                "wrong": wrong_content,
                "valid": empty_metrics,
            }[file_id],
        )

        with pytest.raises(DriveFetchError, match="data.metrics"):
            fetch_health_export(
                metrics_folder_id="metrics-folder",
                workouts_folder_id="workouts-folder",
                data_dir=tmp_path / "import",
                service_account_path=service_account,
            )

        assert not (tmp_path / "import" / "Metrics" / item.name).exists()

    def test_rejects_same_folder_for_both_data_types(self, tmp_path: Path) -> None:
        service_account = tmp_path / "service-account.json"
        service_account.write_text("{}", encoding="utf-8")

        with pytest.raises(DriveFetchError, match="different Drive folder IDs"):
            fetch_health_export(
                metrics_folder_id="same",
                workouts_folder_id="same",
                data_dir=tmp_path / "import",
                service_account_path=service_account,
            )

    def test_rejects_empty_configured_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service_account = tmp_path / "service-account.json"
        service_account.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(google_drive, "_access_token", lambda path: "token")
        monkeypatch.setattr(google_drive, "_children", lambda folder_id, **kwargs: [])

        with pytest.raises(DriveFetchError, match="configured Metrics"):
            fetch_health_export(
                metrics_folder_id="metrics-folder",
                workouts_folder_id="workouts-folder",
                data_dir=tmp_path / "import",
                service_account_path=service_account,
            )


class TestGoogleDriveImportCommand:
    def test_fetches_before_assembling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / "import"
        data_dir.mkdir()
        args = SimpleNamespace(
            source="google-drive",
            data_dir=str(data_dir),
            google_drive_service_account=str(tmp_path / "service-account.json"),
            google_drive_metrics_folder_id="metrics-folder",
            google_drive_workouts_folder_id="workouts-folder",
            google_drive_force=False,
            google_drive_timeout=12.0,
            db=str(tmp_path / "health.db"),
        )
        fetch = patch.object(
            commands_module,
            "fetch_health_export",
            return_value=FetchStats(downloaded=2),
        )
        assemble = patch.object(commands_module, "assemble", return_value=[])

        with fetch as fetch_mock, assemble as assemble_mock:
            cmd_import(args)

        fetch_mock.assert_called_once_with(
            metrics_folder_id="metrics-folder",
            workouts_folder_id="workouts-folder",
            data_dir=data_dir,
            service_account_path=(tmp_path / "service-account.json").resolve(),
            force=False,
            timeout=12.0,
        )
        assemble_mock.assert_called_once_with(data_dir)

    def test_daemon_poll_can_skip_parse_when_drive_is_current(
        self, tmp_path: Path
    ) -> None:
        data_dir = tmp_path / "import"
        data_dir.mkdir()
        args = SimpleNamespace(
            source="google-drive",
            data_dir=str(data_dir),
            google_drive_service_account=str(tmp_path / "service-account.json"),
            google_drive_metrics_folder_id="metrics-folder",
            google_drive_workouts_folder_id="workouts-folder",
            google_drive_force=False,
            google_drive_timeout=30.0,
            skip_if_drive_unchanged=True,
            db=str(tmp_path / "health.db"),
        )

        with (
            patch.object(
                commands_module,
                "fetch_health_export",
                return_value=FetchStats(downloaded=0, skipped=4),
            ),
            patch.object(commands_module, "assemble") as assemble,
        ):
            result = cmd_import(args)

        assert result.import_skipped is True
        assert result.drive_files_skipped == 4
        assemble.assert_not_called()

    def test_reports_missing_drive_configuration(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        args = SimpleNamespace(
            source="google-drive",
            data_dir=str(tmp_path / "import"),
            google_drive_service_account=None,
            google_drive_metrics_folder_id="metrics-folder",
            google_drive_workouts_folder_id=None,
            db=str(tmp_path / "health.db"),
        )

        with pytest.raises(SystemExit):
            cmd_import(args)

        assert "ZDROWSKIT_GOOGLE_DRIVE_SERVICE_ACCOUNT" in caplog.text
        assert "ZDROWSKIT_GOOGLE_DRIVE_WORKOUTS_FOLDER_ID" in caplog.text
