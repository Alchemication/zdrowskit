"""Tests for the operator-facing HTTP ingest setup commands."""

from __future__ import annotations

import argparse
import json
import urllib.error
from pathlib import Path

import pytest

import cmd_ingest
from cmd_ingest import cmd_ingest_setup, cmd_ingest_token
from http_ingest import TokenRegistry, public_dns_health
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

    def test_a_named_instance_refuses_to_take_the_shared_funnel_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Taking public 443 would break the default installation's ingestion."""
        monkeypatch.setattr("http_ingest.INSTANCE_NAME", "lab")
        monkeypatch.setattr("http_ingest.FUNNEL_HTTPS_PORT", 443)

        with pytest.raises(ProfileConfigError, match="Refusing to start the Funnel"):
            cmd_ingest._start_funnel()

    def test_a_non_default_funnel_port_appears_in_the_phone_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A phone posting to a bare https:// URL would reach the wrong instance."""
        monkeypatch.setattr("cmd_ingest.FUNNEL_HTTPS_PORT", 8443)

        assert (
            cmd_ingest._public_funnel_url("host.ts.net") == "https://host.ts.net:8443"
        )

    def test_the_default_funnel_port_stays_out_of_the_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cmd_ingest.FUNNEL_HTTPS_PORT", 443)

        assert cmd_ingest._public_funnel_url("host.ts.net") == "https://host.ts.net"


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

        monkeypatch.setattr("http_ingest.urllib.request.urlopen", fake_urlopen)

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

    def test_a_missing_record_points_at_diagnosis_not_a_false_fix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # NXDOMAIN: MagicDNS still answers on this machine, so nothing local
        # looks wrong, but no phone in the world can reach the Funnel.
        self._resolver(monkeypatch, {"Status": 3})

        ok, detail = public_dns_health("host.ts.net")

        assert ok is False
        # Plain language: this is read on a phone, not in a terminal. Naming
        # Tailscale as the party that stopped publishing is what stops the
        # reader chasing a local repair, which is how the false fix reached the
        # docs the first time.
        assert "Tailscale has stopped publishing" in detail
        # States the verified fact rather than a disclaimer: the node's own
        # connection is checked before this condition is ever reported, because
        # a disconnected Mac produces an identical missing record from a cause
        # that *is* repairable here.
        assert "This Mac is still connected" in detail
        # Jargon check: the operator should never meet the code's vocabulary.
        for jargon in ("Funnel mapping", "re-assert", "DNS record", "receiver"):
            assert jargon not in detail, f"jargon leaked into the alert: {jargon}"
        # A status line is skimmed, not read. Keep it short enough that the
        # last clause still lands; the detail belongs in the docs.
        assert len(detail) < 260, f"status detail too long to be read: {detail}"

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


class TestPublicDnsRecordTypes:
    """A negative answer for one record type must not declare an outage."""

    def test_aaaa_alone_counts_as_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Observed 2026-08-16: minutes after the record was republished,
        # Cloudflare still returned NXDOMAIN for A while answering AAAA, and the
        # phone was uploading the whole time. Reporting that as an outage would
        # alert against a working pipe.
        def fake_urlopen(request: object, timeout: float = 0) -> object:
            url = request.full_url  # type: ignore[attr-defined]
            if "type=AAAA" in url:
                return _FakeResolverResponse(
                    {"Status": 0, "Answer": [{"type": 28, "data": "2a00:dd80:3a::131"}]}
                )
            return _FakeResolverResponse({"Status": 3})

        monkeypatch.setattr("http_ingest.urllib.request.urlopen", fake_urlopen)

        ok, detail = public_dns_health("host.ts.net")

        assert ok is True
        assert "2a00:dd80:3a::131" in detail

    def test_both_types_missing_is_an_outage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_urlopen(request: object, timeout: float = 0) -> object:
            return _FakeResolverResponse({"Status": 3})

        monkeypatch.setattr("http_ingest.urllib.request.urlopen", fake_urlopen)

        assert public_dns_health("host.ts.net")[0] is False

    def test_resolver_failure_on_both_types_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_urlopen(request: object, timeout: float = 0) -> object:
            raise urllib.error.URLError("offline")

        monkeypatch.setattr("http_ingest.urllib.request.urlopen", fake_urlopen)

        # Still None, not False: an offline host has proved nothing.
        assert public_dns_health("host.ts.net")[0] is None
