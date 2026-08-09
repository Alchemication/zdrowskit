"""CLI setup, token rotation, status, and Tailscale helpers for HTTP ingest."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from config import (
    HTTP_INGEST_HOST,
    HTTP_INGEST_PAIR_WINDOW_S,
    HTTP_INGEST_PORT,
    HTTP_INGEST_TOKEN_FILE,
    PUBLIC_DNS_RESOLVER_URL,
    PUBLIC_DNS_TIMEOUT_S,
)
from http_ingest import HttpIngestManager, TokenRegistry, UPLOAD_PATH
from profiles import Profile, ProfileConfigError, load_profiles

PAIR_STATE_LABELS = {
    "ready": "queued for import",
    "waiting": "waiting for the other half",
    "split": "halves arrived too far apart",
    "pending": "staged but not imported",
    "imported": "up to date",
}


def _ensure_private_gitignore(app_home: Path) -> bool:
    """Ensure the hash-only token registry cannot enter the state repository."""
    path = app_home / ".gitignore"
    entry = "/ingest_tokens.json"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = {line.strip() for line in existing.splitlines()}
    if entry in lines or "ingest_tokens.json" in lines:
        return False
    separator = "" if not existing or existing.endswith("\n") else "\n"
    addition = f"{separator}\n# HTTP ingest credentials\n{entry}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(addition)
    return True


def _http_profiles(profiles: dict[str, Profile]) -> dict[str, Profile]:
    """Return enabled profiles configured for HTTP ingestion."""
    return {
        name: profile
        for name, profile in profiles.items()
        if profile.enabled and profile.import_source == "http"
    }


def _tailscale_dns_name() -> str | None:
    """Return this device's Tailscale DNS name when the CLI is connected."""
    executable = shutil.which("tailscale")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "status", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    self_info = payload.get("Self")
    if not isinstance(self_info, dict):
        return None
    dns_name = self_info.get("DNSName")
    if not isinstance(dns_name, str) or not dns_name:
        return None
    return dns_name.rstrip(".")


def _start_funnel() -> None:
    """Start the stable HTTPS Funnel for the configured loopback receiver."""
    executable = shutil.which("tailscale")
    if executable is None:
        raise ProfileConfigError(
            "Tailscale CLI is not installed. In Tailscale Settings, enable CLI "
            "integration, then retry ingest setup --funnel."
        )
    target = f"http://{HTTP_INGEST_HOST}:{HTTP_INGEST_PORT}"
    result = subprocess.run(
        [executable, "funnel", "--bg", "--https=443", target],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ProfileConfigError(f"Could not start Tailscale Funnel: {detail}")
    print(f"Tailscale Funnel points HTTPS port 443 to {target}")


def _print_token(profile: str, token: str, public_url: str | None) -> None:
    """Print a newly issued token and the matching Auto Export settings."""
    print(f"\n{profile} Auto Export configuration")
    if public_url:
        print(f"URL: {public_url}{UPLOAD_PATH}")
    else:
        print(f"URL path: {UPLOAD_PATH}")
    print("Method: POST")
    print("Content type: application/json")
    print(f"Authorization: Bearer {token}")
    print("Store this token in Auto Export now; zdrowskit cannot display it again.")


def cmd_ingest_setup(args: argparse.Namespace) -> None:
    """Initialize caches and missing profile tokens, optionally starting Funnel."""
    profiles = load_profiles()
    http_profiles = _http_profiles(profiles)
    if not http_profiles:
        raise ProfileConfigError(
            "No enabled profile uses HTTP. Run 'profile source NAME http' first."
        )
    token_home = HTTP_INGEST_TOKEN_FILE.parent
    if _ensure_private_gitignore(token_home):
        print(f"Added /ingest_tokens.json to {token_home / '.gitignore'}")
    if args.funnel:
        _start_funnel()
    registry = TokenRegistry(HTTP_INGEST_TOKEN_FILE)
    active = registry.active_profiles()
    issued: dict[str, str] = {}
    for name, profile in http_profiles.items():
        (profile.http_cache / "Metrics").mkdir(parents=True, exist_ok=True, mode=0o700)
        (profile.http_cache / "Workouts").mkdir(parents=True, exist_ok=True, mode=0o700)
        if name not in active:
            issued[name] = registry.create_token(name)
    dns_name = _tailscale_dns_name()
    public_url = f"https://{dns_name}" if dns_name else None
    print(f"Receiver: http://{HTTP_INGEST_HOST}:{HTTP_INGEST_PORT}{UPLOAD_PATH}")
    if public_url:
        print(f"Public URL: {public_url}{UPLOAD_PATH}")
    else:
        print(
            "Tailscale is offline or has no DNS name. Connect it, then run:\n"
            f"  tailscale funnel --bg --https=443 http://{HTTP_INGEST_HOST}:{HTTP_INGEST_PORT}"
        )
    if issued:
        for name, token in issued.items():
            _print_token(name, token, public_url)
    else:
        print("Every HTTP profile already has a token; existing tokens are not stored.")
    print("Run 'uv run python main.py daemon-install' to install/start the receiver.")


def cmd_ingest_token(args: argparse.Namespace) -> None:
    """Issue a new profile token, revoking older tokens when requested."""
    profiles = load_profiles()
    profile = profiles.get(args.name)
    if profile is None:
        raise ProfileConfigError(f"Unknown profile {args.name!r}.")
    if not profile.enabled or profile.import_source != "http":
        raise ProfileConfigError(
            f"Profile {profile.name!r} must be enabled with import_source = 'http'."
        )
    _ensure_private_gitignore(HTTP_INGEST_TOKEN_FILE.parent)
    registry = TokenRegistry(HTTP_INGEST_TOKEN_FILE)
    active = registry.active_profiles()
    if profile.name in active and not args.rotate:
        raise ProfileConfigError(
            f"{profile.name} already has an active token. Pass --rotate to revoke it "
            "and issue a replacement."
        )
    token = registry.create_token(profile.name, revoke_existing=args.rotate)
    dns_name = _tailscale_dns_name()
    public_url = f"https://{dns_name}" if dns_name else None
    _print_token(profile.name, token, public_url)


def receiver_health() -> tuple[bool, str]:
    """Check the loopback receiver without exposing it beyond the host."""
    url = f"http://{HTTP_INGEST_HOST}:{HTTP_INGEST_PORT}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200, f"HTTP {response.status} at {url}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"not answering at {url} ({exc}); is the daemon running?"


def public_dns_health(dns_name: str) -> tuple[bool | None, str]:
    """Check that the Funnel hostname resolves for anything outside the tailnet.

    Every local signal can look perfect while uploads are impossible: the
    receiver answers on loopback, ``tailscale funnel status`` says the Funnel
    is on, and MagicDNS resolves the hostname on this machine. None of that
    involves the public DNS record the phone actually needs, so when that
    record disappeared the loss stayed invisible for a day and the sync alert
    blamed the phone instead.

    Args:
        dns_name: Tailscale DNS name serving the Funnel.

    Returns:
        Whether the name resolves publicly and a detail line. The flag is
        ``None`` when the check could not run at all, which must not be
        reported as a failure — an offline laptop is not a missing record.
    """
    query = urllib.parse.urlencode({"name": dns_name, "type": "A"})
    request = urllib.request.Request(
        f"{PUBLIC_DNS_RESOLVER_URL}?{query}",
        headers={"accept": "application/dns-json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=PUBLIC_DNS_TIMEOUT_S) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return None, f"could not reach the public resolver ({exc})"
    if not isinstance(payload, dict):
        return None, "the public resolver returned an unreadable answer"
    addresses = [
        answer.get("data")
        for answer in payload.get("Answer") or []
        if isinstance(answer, dict) and answer.get("type") == 1
    ]
    if addresses:
        return True, f"resolves to {', '.join(addresses)}"
    return False, (
        f"{dns_name} does not resolve outside the tailnet, so Auto Export "
        "cannot reach it no matter how healthy the receiver looks here. "
        "Re-publish the record with: tailscale funnel --bg --https=443 "
        "http://127.0.0.1:8787"
    )


def cmd_ingest_status(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Print receiver, token, pair, and Funnel status without secrets."""
    profiles = _http_profiles(load_profiles())
    if not profiles:
        raise ProfileConfigError("No enabled profile uses HTTP ingestion.")
    registry = TokenRegistry(HTTP_INGEST_TOKEN_FILE)
    active = registry.active_profiles()
    manager = HttpIngestManager(
        profiles,
        pair_window_s=HTTP_INGEST_PAIR_WINDOW_S,
        on_pair_ready=lambda _profile, _digest: None,
    )
    receiver_ok, receiver_detail = receiver_health()
    dns_name = _tailscale_dns_name()
    print(f"Receiver: {'ready' if receiver_ok else 'not ready'} ({receiver_detail})")
    if dns_name:
        print(f"Funnel URL: https://{dns_name}{UPLOAD_PATH}")
        public_ok, public_detail = public_dns_health(dns_name)
        label = {True: "reachable", False: "NOT REACHABLE", None: "unknown"}[public_ok]
        print(f"Public DNS: {label} ({public_detail})")
    else:
        print("Funnel URL: unavailable (Tailscale is offline or not connected)")
    states = manager.status()
    for name in profiles:
        state = states[name]
        print(f"\n{name}")
        print(f"  token: {'configured' if name in active else 'missing'}")
        print(f"  metrics received: {state['metrics_received_at'] or 'never'}")
        print(f"  workouts received: {state['workouts_received_at'] or 'never'}")
        print(f"  last imported: {state['last_imported_at'] or 'never'}")
        print(f"  pairing: {PAIR_STATE_LABELS[state['pair_state']]}")
        if state["pair_state"] in {"waiting", "split", "pending"}:
            print(f"    {state['pair_detail']}")
        if state["last_error"]:
            print(f"  last error: {state['last_error']['message']}")


def cmd_ingest(args: argparse.Namespace) -> None:
    """Dispatch the HTTP ingest CLI command group."""
    if args.ingest_cmd == "setup":
        cmd_ingest_setup(args)
    elif args.ingest_cmd == "token":
        cmd_ingest_token(args)
    else:
        cmd_ingest_status(args)
