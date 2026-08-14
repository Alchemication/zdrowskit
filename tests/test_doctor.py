"""Tests for source-aware local readiness checks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import commands as commands_module
import profiles as profiles_module
from commands import cmd_doctor
from http_ingest import TokenRegistry
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
        assert "All checks passed" in output

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
        monkeypatch.setattr(cmd_ingest_module, "_tailscale_dns_name", lambda: None)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

        with pytest.raises(SystemExit):
            cmd_doctor(SimpleNamespace())

        output = capsys.readouterr().out
        assert "[!!] HTTP receiver" in output
        assert "is the daemon running?" in output
        assert "All checks passed" not in output

    def test_a_funnel_that_stopped_resolving_publicly_fails_doctor(
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
        token_file = app_home / "ingest_tokens.json"
        TokenRegistry(token_file).create_token("adam")
        monkeypatch.setattr(commands_module, "HTTP_INGEST_TOKEN_FILE", token_file)
        profile.http_cache.mkdir(parents=True, exist_ok=True)
        # Everything local is healthy; only the public record is gone. This is
        # the exact shape of the outage that went unnoticed for a day.
        monkeypatch.setattr(
            cmd_ingest_module, "receiver_health", lambda: (True, "HTTP 200")
        )
        monkeypatch.setattr(
            cmd_ingest_module, "_tailscale_dns_name", lambda: "host.ts.net"
        )
        monkeypatch.setattr(
            cmd_ingest_module,
            "public_dns_health",
            lambda name: (False, f"{name} does not resolve outside the tailnet"),
        )
        monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

        with pytest.raises(SystemExit):
            cmd_doctor(SimpleNamespace())

        output = capsys.readouterr().out
        # The public DNS check must be the *only* thing failing, or this test
        # would still pass with the check removed entirely.
        assert "[ok] HTTP receiver" in output
        assert "[ok] adam HTTP token" in output
        assert output.count("[!!]") == 1
        assert "[!!] Funnel public DNS" in output
        assert "All checks passed" not in output

    def test_an_unreachable_resolver_does_not_fail_doctor(
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
        token_file = app_home / "ingest_tokens.json"

        monkeypatch.setattr(commands_module, "REPO_ROOT", repo)
        monkeypatch.setattr(commands_module, "APP_HOME", app_home)
        profile = Profile("adam", 11, profile_root, operator=True, import_source="http")
        monkeypatch.setattr(profiles_module, "load_profiles", lambda: {"adam": profile})
        monkeypatch.setattr(
            profiles_module, "PROFILES_FILE", app_home / "profiles.toml"
        )
        monkeypatch.setattr(commands_module.shutil, "which", lambda name: "/bin/uv")
        TokenRegistry(token_file).create_token("adam")
        monkeypatch.setattr(commands_module, "HTTP_INGEST_TOKEN_FILE", token_file)
        profile.http_cache.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            cmd_ingest_module, "receiver_health", lambda: (True, "HTTP 200")
        )
        monkeypatch.setattr(
            cmd_ingest_module, "_tailscale_dns_name", lambda: "host.ts.net"
        )
        monkeypatch.setattr(
            cmd_ingest_module,
            "public_dns_health",
            lambda name: (None, "could not reach the public resolver"),
        )
        monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")

        cmd_doctor(SimpleNamespace())

        output = capsys.readouterr().out
        # A laptop on a plane is not a broken Funnel. Reporting this as a
        # failure would train the operator to ignore the check that matters.
        assert "[--] Funnel public DNS" in output
        assert "All checks passed" in output
