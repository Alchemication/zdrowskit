"""Tests for source-aware local readiness checks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import commands as commands_module
import profiles as profiles_module
from commands import cmd_doctor
from profiles import Profile


class TestDoctorImportSource:
    def test_google_drive_does_not_require_existing_cache(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo = tmp_path / "repo"
        app_home = tmp_path / "app"
        profile_root = app_home / "profiles" / "adam"
        context_dir = profile_root / "ContextFiles"
        repo.mkdir()
        context_dir.mkdir(parents=True)
        (profile_root / "health.db").write_bytes(b"db")
        for name in ("me.md", "strategy.md", "log.md", "history.md"):
            (context_dir / name).write_text("test\n", encoding="utf-8")
        (repo / ".env").write_text("", encoding="utf-8")
        service_account = tmp_path / "service-account.json"
        service_account.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(commands_module, "REPO_ROOT", repo)
        monkeypatch.setattr(commands_module, "APP_HOME", app_home)
        profile = Profile(
            "adam",
            11,
            profile_root,
            operator=True,
            import_source="google-drive",
            drive_metrics_folder_id="metrics-folder",
            drive_workouts_folder_id="workouts-folder",
        )
        monkeypatch.setattr(profiles_module, "load_profiles", lambda: {"adam": profile})
        monkeypatch.setattr(
            profiles_module, "PROFILES_FILE", app_home / "profiles.toml"
        )
        monkeypatch.setattr(
            commands_module,
            "GOOGLE_DRIVE_SERVICE_ACCOUNT",
            str(service_account),
        )
        monkeypatch.setattr(commands_module, "GOOGLE_DRIVE_POLL_INTERVAL_S", 300)
        monkeypatch.setattr(commands_module.shutil, "which", lambda name: "/bin/uv")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

        cmd_doctor(SimpleNamespace())

        output = capsys.readouterr().out
        assert "[ok] profile roster" in output
        assert "[ok] Drive service account" in output
        assert "[--] adam Drive cache" in output
        assert "All local checks passed" in output

    def test_http_receiver_check_fails_when_the_daemon_is_not_answering(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import cmd_ingest as cmd_ingest_module

        repo = tmp_path / "repo"
        app_home = tmp_path / "app"
        profile_root = app_home / "profiles" / "adam"
        context_dir = profile_root / "ContextFiles"
        repo.mkdir()
        context_dir.mkdir(parents=True)
        (profile_root / "health.db").write_bytes(b"db")
        for name in ("me.md", "strategy.md", "log.md", "history.md"):
            (context_dir / name).write_text("test\n", encoding="utf-8")
        (repo / ".env").write_text("", encoding="utf-8")

        monkeypatch.setattr(commands_module, "REPO_ROOT", repo)
        monkeypatch.setattr(commands_module, "APP_HOME", app_home)
        profile = Profile("adam", 11, profile_root, operator=True, import_source="http")
        monkeypatch.setattr(profiles_module, "load_profiles", lambda: {"adam": profile})
        monkeypatch.setattr(
            profiles_module, "PROFILES_FILE", app_home / "profiles.toml"
        )
        monkeypatch.setattr(commands_module.shutil, "which", lambda name: "/bin/uv")
        monkeypatch.setattr(
            commands_module, "HTTP_INGEST_TOKEN_FILE", app_home / "ingest_tokens.json"
        )
        monkeypatch.setattr(
            cmd_ingest_module,
            "receiver_health",
            lambda: (False, "not answering; is the daemon running?"),
        )
        monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

        with pytest.raises(SystemExit):
            cmd_doctor(SimpleNamespace())

        output = capsys.readouterr().out
        assert "[!!] HTTP receiver" in output
        assert "is the daemon running?" in output
        assert "All local checks passed" not in output
