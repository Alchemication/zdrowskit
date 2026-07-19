"""Tests for source-aware local readiness checks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import commands as commands_module
from commands import cmd_doctor


class TestDoctorImportSource:
    def test_google_drive_does_not_require_existing_cache(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo = tmp_path / "repo"
        app_home = tmp_path / "app"
        context_dir = app_home / "ContextFiles"
        repo.mkdir()
        app_home.mkdir()
        context_dir.mkdir()
        for name in ("me.md", "strategy.md", "log.md", "history.md"):
            (context_dir / name).write_text("test\n", encoding="utf-8")
        (repo / ".env").write_text("", encoding="utf-8")
        service_account = tmp_path / "service-account.json"
        service_account.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(commands_module, "REPO_ROOT", repo)
        monkeypatch.setattr(commands_module, "APP_HOME", app_home)
        monkeypatch.setattr(commands_module, "CONTEXT_DIR", context_dir)
        monkeypatch.setattr(commands_module, "IMPORT_SOURCE", "google-drive")
        monkeypatch.setattr(
            commands_module,
            "GOOGLE_DRIVE_SERVICE_ACCOUNT",
            str(service_account),
        )
        monkeypatch.setattr(
            commands_module, "GOOGLE_DRIVE_METRICS_FOLDER_ID", "metrics-folder"
        )
        monkeypatch.setattr(
            commands_module, "GOOGLE_DRIVE_WORKOUTS_FOLDER_ID", "workouts-folder"
        )
        monkeypatch.setattr(commands_module, "GOOGLE_DRIVE_POLL_INTERVAL_S", 300)
        monkeypatch.setattr(
            commands_module,
            "resolve_google_drive_data_dir",
            lambda arg: tmp_path / "missing-cache",
        )
        monkeypatch.setattr(commands_module.shutil, "which", lambda name: "/bin/uv")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

        cmd_doctor(SimpleNamespace())

        output = capsys.readouterr().out
        assert "[ok] import source" in output
        assert "[ok] Drive service account" in output
        assert "[--] Drive data cache" in output
        assert "All local checks passed" in output
