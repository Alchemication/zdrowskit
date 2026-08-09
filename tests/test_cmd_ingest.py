"""Tests for the operator-facing HTTP ingest setup commands."""

from __future__ import annotations

import argparse
import json
import urllib.error
from pathlib import Path

import pytest

from cmd_ingest import cmd_ingest_setup, cmd_ingest_token, public_dns_health
from http_ingest import TokenRegistry
from profiles import Profile, ProfileConfigError


class _FakeResolverResponse:
    """Minimal stand-in for the DoH resolver's HTTP response."""

    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResolverResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


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


class TestPublicDnsHealth:
    def _resolver(
        self, monkeypatch: pytest.MonkeyPatch, payload: dict | Exception
    ) -> None:
        """Stand in for the public DoH resolver."""

        def fake_urlopen(request: object, timeout: float = 0) -> object:
            if isinstance(payload, Exception):
                raise payload
            return _FakeResolverResponse(payload)

        monkeypatch.setattr("cmd_ingest.urllib.request.urlopen", fake_urlopen)

    def test_a_published_record_is_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._resolver(
            monkeypatch,
            {"Status": 0, "Answer": [{"type": 1, "data": "176.58.88.82"}]},
        )

        ok, detail = public_dns_health("host.ts.net")

        assert ok is True
        assert "176.58.88.82" in detail

    def test_a_missing_record_is_reported_with_the_fix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # NXDOMAIN: MagicDNS still answers on this machine, so nothing local
        # looks wrong, but no phone in the world can reach the Funnel.
        self._resolver(monkeypatch, {"Status": 3})

        ok, detail = public_dns_health("host.ts.net")

        assert ok is False
        assert "does not resolve outside the tailnet" in detail
        assert "tailscale funnel --bg --https=443" in detail

    def test_a_cname_without_an_address_is_not_treated_as_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._resolver(
            monkeypatch,
            {"Status": 0, "Answer": [{"type": 5, "data": "ingress.example."}]},
        )

        assert public_dns_health("host.ts.net")[0] is False

    def test_an_unreachable_resolver_is_unknown_rather_than_broken(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._resolver(monkeypatch, urllib.error.URLError("offline"))

        ok, detail = public_dns_health("host.ts.net")

        # None, not False. An offline machine reporting a missing record would
        # send the operator to re-create a Funnel that was never broken.
        assert ok is None
        assert "public resolver" in detail

    def test_unreadable_resolver_output_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._resolver(monkeypatch, ValueError("not json"))

        assert public_dns_health("host.ts.net")[0] is None
