"""Tests for the operator-facing HTTP ingest setup commands."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from cmd_ingest import cmd_ingest_setup, cmd_ingest_token
from http_ingest import TokenRegistry
from profiles import Profile, ProfileConfigError


class TestIngestSetup:
    def test_creates_ignored_hash_only_token_registry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        profile = Profile(
            "adam",
            11,
            tmp_path / "profiles" / "adam",
            operator=True,
            import_source="http",
        )
        token_file = tmp_path / "ingest_tokens.json"
        monkeypatch.setattr("cmd_ingest.HTTP_INGEST_TOKEN_FILE", token_file)
        monkeypatch.setattr("cmd_ingest.load_profiles", lambda: {"adam": profile})
        monkeypatch.setattr("cmd_ingest._tailscale_dns_name", lambda: "host.ts.net")

        cmd_ingest_setup(argparse.Namespace(funnel=False))

        output = capsys.readouterr().out
        assert "/ingest_tokens.json" in (tmp_path / ".gitignore").read_text()
        assert "https://host.ts.net/v1/auto-export" in output
        assert "Authorization: Bearer zdr_" in output
        assert "zdr_" not in token_file.read_text(encoding="utf-8")
        assert profile.http_cache.joinpath("Metrics").is_dir()
        assert profile.http_cache.joinpath("Workouts").is_dir()

    def test_funnel_failure_does_not_create_an_unprinted_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        profile = Profile(
            "adam",
            11,
            tmp_path / "profiles" / "adam",
            operator=True,
            import_source="http",
        )
        token_file = tmp_path / "ingest_tokens.json"
        monkeypatch.setattr("cmd_ingest.HTTP_INGEST_TOKEN_FILE", token_file)
        monkeypatch.setattr("cmd_ingest.load_profiles", lambda: {"adam": profile})

        def fail_funnel() -> None:
            raise ProfileConfigError("Funnel approval failed")

        monkeypatch.setattr("cmd_ingest._start_funnel", fail_funnel)

        with pytest.raises(ProfileConfigError, match="Funnel approval failed"):
            cmd_ingest_setup(argparse.Namespace(funnel=True))

        assert token_file.exists() is False


class TestIngestToken:
    def test_requires_explicit_rotation_for_existing_profile_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        profile = Profile(
            "adam",
            11,
            tmp_path / "profiles" / "adam",
            operator=True,
            import_source="http",
        )
        token_file = tmp_path / "ingest_tokens.json"
        TokenRegistry(token_file).create_token("adam")
        monkeypatch.setattr("cmd_ingest.HTTP_INGEST_TOKEN_FILE", token_file)
        monkeypatch.setattr("cmd_ingest.load_profiles", lambda: {"adam": profile})

        with pytest.raises(ProfileConfigError, match="--rotate"):
            cmd_ingest_token(argparse.Namespace(name="adam", rotate=False))

        assert "/ingest_tokens.json" in (tmp_path / ".gitignore").read_text()
