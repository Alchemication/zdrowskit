"""Authenticated HTTP ingestion for Auto Export Metrics and Workouts payloads."""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable

from config import (
    DATA_HEALTH_STALE_CUTOFF_HOUR,
    FUNNEL_ALLOWED_HTTPS_PORTS,
    FUNNEL_HTTPS_PORT,
    FUNNEL_DNS_CHECK_AFTER_H,
    HTTP_INGEST_BATCH_CAPTURE_MAX_FILES,
    HTTP_INGEST_MAX_JSON_DEPTH,
    HTTP_INGEST_MAX_JSON_NODES,
    HTTP_INGEST_MAX_METRIC_ENTRIES,
    HTTP_INGEST_MAX_METRICS,
    HTTP_INGEST_MAX_RECEIPTS,
    HTTP_INGEST_MAX_ROUTE_POINTS,
    INSTANCE_NAME,
    HTTP_INGEST_MAX_WORKOUTS,
    PUBLIC_DNS_RESOLVER_URL,
    PUBLIC_DNS_TIMEOUT_S,
    TAILSCALE_BINARY,
    TAILSCALE_STATUS_TIMEOUT_S,
)
from parsers.metrics import parse_metrics_payload
from parsers.workouts import parse_workouts_payload
from profiles import Profile

logger = logging.getLogger(__name__)

# The port the documented setup uses, and the one a bare https:// URL means.
_SHARED_FUNNEL_PORT = 443

UPLOAD_PATH = "/v1/auto-export"
BATCH_CAPTURE_PATH = "/v1/auto-export-batch"
HEALTH_PATH = "/healthz"
TOKEN_VERSION = 1
STATE_VERSION = 1
_TOKEN_RE = re.compile(r"^zdr_([a-f0-9]{16})_([A-Za-z0-9_-]{32,64})$")
_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
_APPLE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T].*)?$")


class IngestError(ValueError):
    """An upload error safe to return to the Auto Export client."""

    def __init__(self, status: int, message: str) -> None:
        """Initialize an error with an HTTP status and actionable message."""
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ValidatedUpload:
    """Validated request metadata used to persist and pair one upload."""

    kind: str
    automation_id: str
    session_id: str
    sha256: str
    size: int


@dataclass(frozen=True)
class IngestHealth:
    """One profile's ingest condition, ready to report to its owner."""

    status: str
    detail: str
    since: str | None = None
    missing_from: str | None = None
    missing_to: str | None = None

    @property
    def is_alerting(self) -> bool:
        """Return whether this condition is worth telling someone about."""
        return self.status != "ok"


@dataclass(frozen=True)
class AcceptedUpload:
    """Result returned after an upload is durably staged."""

    profile: str
    kind: str
    duplicate: bool
    pair_ready: bool


def _utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 form."""
    return datetime.now(timezone.utc).isoformat()


def _format_gap(seconds: float) -> str:
    """Format a duration in the largest unit that stays readable."""
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Atomically replace a file and fsync its contents before publication."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, mode)
    finally:
        temp_path.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically write compact UTF-8 JSON with private permissions."""
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(path, data)


def _load_json_object(path: Path, *, missing: dict[str, Any]) -> dict[str, Any]:
    """Load a JSON object or return the supplied value when the file is absent."""
    if not path.exists():
        return missing
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return value


class TokenRegistry:
    """Hash-only registry mapping bearer-token key IDs to profiles."""

    def __init__(self, path: Path) -> None:
        """Initialize the registry at a private local path."""
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        """Load and minimally validate the current registry."""
        registry = _load_json_object(
            self.path,
            missing={"version": TOKEN_VERSION, "tokens": {}},
        )
        if registry.get("version") != TOKEN_VERSION:
            raise ValueError(
                f"Unsupported token registry version in {self.path}; run ingest setup."
            )
        if not isinstance(registry.get("tokens"), dict):
            raise ValueError(f"{self.path} has an invalid tokens object.")
        return registry

    def create_token(
        self,
        profile: str,
        *,
        label: str = "Auto Export",
        revoke_existing: bool = False,
    ) -> str:
        """Create a token, optionally revoking all active tokens for the profile."""
        with self._lock:
            registry = self._load()
            tokens = registry["tokens"]
            if revoke_existing:
                for entry in tokens.values():
                    if isinstance(entry, dict) and entry.get("profile") == profile:
                        entry["enabled"] = False
                        entry["revoked_at"] = _utc_now()
            kid = secrets.token_hex(8)
            secret = secrets.token_urlsafe(32)
            token = f"zdr_{kid}_{secret}"
            tokens[kid] = {
                "profile": profile,
                "label": label,
                "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "created_at": _utc_now(),
                "enabled": True,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self.path, registry)
            return token

    def active_profiles(self) -> set[str]:
        """Return profiles with at least one active upload token."""
        with self._lock:
            registry = self._load()
        return {
            entry["profile"]
            for entry in registry["tokens"].values()
            if isinstance(entry, dict)
            and entry.get("enabled") is True
            and isinstance(entry.get("profile"), str)
        }

    def authenticate(self, token: str) -> str | None:
        """Return the trusted profile mapped to a valid bearer token."""
        match = _TOKEN_RE.fullmatch(token)
        if match is None:
            return None
        kid = match.group(1)
        with self._lock:
            try:
                registry = self._load()
            except ValueError:
                logger.exception("HTTP ingest token registry is invalid")
                return None
        entry = registry["tokens"].get(kid)
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            return None
        expected = entry.get("token_hash")
        profile = entry.get("profile")
        actual = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not isinstance(expected, str) or not hmac.compare_digest(actual, expected):
            return None
        return profile if isinstance(profile, str) else None


def funnel_conflict_reason() -> str | None:
    """Return why this installation must not assert the Funnel, or None.

    The Funnel mapping is machine-wide and per public port, so two
    installations claiming the same port silently take it from each other —
    the loser's phone uploads reach the winner's receiver and are rejected as
    an unknown token, which reads as an outage rather than a misconfiguration.

    Returns:
        A sentence naming the conflict and its fix, or None when asserting the
        Funnel is safe.
    """
    if INSTANCE_NAME and FUNNEL_HTTPS_PORT == _SHARED_FUNNEL_PORT:
        return (
            f"instance {INSTANCE_NAME!r} would claim public port "
            f"{_SHARED_FUNNEL_PORT}, which belongs to the default "
            "installation. Set ZDROWSKIT_FUNNEL_HTTPS_PORT to one of "
            f"{', '.join(str(port) for port in FUNNEL_ALLOWED_HTTPS_PORTS[1:])} "
            "for this instance, or import from a local directory instead"
        )
    return None


def _strict_json_loads(body: bytes) -> dict[str, Any]:
    """Parse a JSON object while rejecting non-standard numeric constants."""

    def _reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value}")

    try:
        parsed = json.loads(body, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY, f"Invalid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "The request body must be a JSON object.",
        )
    return parsed


def _validate_tree(value: Any, *, depth: int = 0, counter: list[int]) -> None:
    """Reject pathological nesting, excessive nodes, and non-finite numbers."""
    counter[0] += 1
    if counter[0] > HTTP_INGEST_MAX_JSON_NODES:
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "JSON payload contains too many values.",
        )
    if depth > HTTP_INGEST_MAX_JSON_DEPTH:
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "JSON payload is nested too deeply.",
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "JSON payload contains a non-finite number.",
        )
    if isinstance(value, dict):
        for child in value.values():
            _validate_tree(child, depth=depth + 1, counter=counter)
    elif isinstance(value, list):
        for child in value:
            _validate_tree(child, depth=depth + 1, counter=counter)


def _require_apple_date(value: Any, field: str) -> None:
    """Require an Apple-style timestamp or date string."""
    if not isinstance(value, str) or _APPLE_DATE_RE.fullmatch(value) is None:
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"{field} must be an Apple Health date string.",
        )


def _validate_metrics(items: list[Any]) -> None:
    """Validate the daily-summary Metrics envelope."""
    if len(items) > HTTP_INGEST_MAX_METRICS:
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"Metrics payload exceeds the {HTTP_INGEST_MAX_METRICS}-metric limit.",
        )
    for index, metric in enumerate(items):
        if not isinstance(metric, dict):
            raise IngestError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"metrics[{index}] must be an object.",
            )
        if not isinstance(metric.get("name"), str) or not metric["name"]:
            raise IngestError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"metrics[{index}].name must be a non-empty string.",
            )
        entries = metric.get("data")
        if not isinstance(entries, list):
            raise IngestError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"metrics[{index}].data must be an array.",
            )
        if len(entries) > HTTP_INGEST_MAX_METRIC_ENTRIES:
            raise IngestError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"metrics[{index}] carries more than {HTTP_INGEST_MAX_METRIC_ENTRIES} daily "
                "entries. Use local or Google Drive import for a backfill this "
                "large.",
            )
        for entry_index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise IngestError(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    f"metrics[{index}].data[{entry_index}] must be an object.",
                )
            _require_apple_date(
                entry.get("date"),
                f"metrics[{index}].data[{entry_index}].date",
            )


def _validate_workouts(items: list[Any]) -> None:
    """Validate workout summaries and route coordinates."""
    if len(items) > HTTP_INGEST_MAX_WORKOUTS:
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"Workouts payload exceeds the {HTTP_INGEST_MAX_WORKOUTS}-workout limit.",
        )
    for index, workout in enumerate(items):
        if not isinstance(workout, dict):
            raise IngestError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"workouts[{index}] must be an object.",
            )
        if not isinstance(workout.get("name"), str) or not workout["name"]:
            raise IngestError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"workouts[{index}].name must be a non-empty string.",
            )
        _require_apple_date(workout.get("start"), f"workouts[{index}].start")
        duration = workout.get("duration")
        if duration is not None and (
            isinstance(duration, bool) or not isinstance(duration, int | float)
        ):
            raise IngestError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"workouts[{index}].duration must be numeric.",
            )
        route = workout.get("route", [])
        if not isinstance(route, list):
            raise IngestError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"workouts[{index}].route must be an array.",
            )
        if len(route) > HTTP_INGEST_MAX_ROUTE_POINTS:
            raise IngestError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"A workout route exceeds the {HTTP_INGEST_MAX_ROUTE_POINTS}-point limit.",
            )
        for point_index, point in enumerate(route):
            if not isinstance(point, dict):
                raise IngestError(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    f"workouts[{index}].route[{point_index}] must be an object.",
                )
            latitude = point.get("latitude")
            longitude = point.get("longitude")
            if (
                isinstance(latitude, bool)
                or not isinstance(latitude, int | float)
                or not -90 <= latitude <= 90
                or isinstance(longitude, bool)
                or not isinstance(longitude, int | float)
                or not -180 <= longitude <= 180
            ):
                raise IngestError(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    f"workouts[{index}].route[{point_index}] has invalid coordinates.",
                )
            _require_apple_date(
                point.get("timestamp"),
                f"workouts[{index}].route[{point_index}].timestamp",
            )


def _parser_dry_run(payload: dict[str, Any], kind: str) -> None:
    """Run the production parser against a validated payload before accepting it.

    Parsing the decoded payload in memory keeps health data out of the shared
    system temp directory and avoids decoding the request body a second time.
    """
    try:
        if kind == "metrics":
            parse_metrics_payload(payload)
        else:
            parse_workouts_payload(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"Payload is not compatible with the zdrowskit {kind} parser: {exc}",
        ) from exc


def validate_upload(headers: dict[str, str], body: bytes) -> ValidatedUpload:
    """Validate Auto Export headers, envelope, schema, and parser compatibility."""
    automation_name = headers.get("automation-name", "").strip().lower()
    if automation_name not in {"metrics", "workouts"}:
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "automation-name must be Metrics or Workouts.",
        )
    # Metrics accepts Hours or Days; Workouts must be Minutes.
    #
    # Hours is what the setup guide asks for, because sleeping wrist temperature
    # and respiratory rate are filed under the night twelve hours before the
    # reading: at Hours a reading keeps its real 23:00 clock time and lands on
    # the right night, while Days zeroes it to 00:00 and files it a night early.
    # Days is still accepted rather than rejected — it was the documented
    # setting until 2026-08-21, every daily total parses identically either way,
    # and refusing it would strand an existing profile mid-upload over a
    # one-night skew in two metrics.
    #
    # Workouts is not negotiable. Per-kilometre heart rate and cadence come from
    # the per-minute series inside each workout, and a coarser bin is credited
    # with at most a minute of a split, so every split silently falls below the
    # sample-coverage floor and stores nothing.
    aggregation = headers.get("automation-aggregation", "").strip().lower()
    if automation_name == "metrics":
        if aggregation not in {"hours", "days"}:
            raise IngestError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "Metrics automation aggregation must be Hours (preferred) or Days.",
            )
    elif aggregation != "minutes":
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Workouts automation aggregation must be Minutes.",
        )
    # Any Date Range is accepted. A wider Metrics window makes an outage
    # recoverable instead of permanently lost, and the real protection against
    # an oversized export is the per-metric and per-workout entry caps below,
    # which reject with an actionable message. Pinning this to "Default" only
    # blocked a setting worth using.
    period = headers.get("automation-period", "").strip()
    if not period:
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "automation-period must be supplied by Auto Export.",
        )
    automation_id = headers.get("automation-id", "").strip()
    session_id = headers.get("session-id", "").strip()
    if _UUID_RE.fullmatch(automation_id) is None:
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "automation-id must be a UUID supplied by Auto Export.",
        )
    if _UUID_RE.fullmatch(session_id) is None:
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "session-id must be a UUID supplied by Auto Export.",
        )

    payload = _strict_json_loads(body)
    _validate_tree(payload, counter=[0])
    data = payload.get("data")
    if not isinstance(data, dict):
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Payload must contain a data object.",
        )
    if set(data) != {automation_name}:
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"data must contain only the {automation_name} array.",
        )
    items = data[automation_name]
    if not isinstance(items, list):
        raise IngestError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"data.{automation_name} must be an array.",
        )
    if automation_name == "metrics":
        _validate_metrics(items)
    else:
        _validate_workouts(items)
    _parser_dry_run(payload, automation_name)
    logger.debug("Validated %s upload over Date Range %r", automation_name, period)
    return ValidatedUpload(
        kind=automation_name,
        automation_id=automation_id,
        session_id=session_id,
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )


def _parse_iso(value: Any) -> datetime | None:
    """Parse a stored ISO-8601 timestamp, or return None when unusable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def newest_expected_day(now: datetime) -> date:
    """Return the most recent day that should already hold a full day of data.

    Today never qualifies: it is still happening, so an absence of data says
    nothing. Yesterday only qualifies once the morning sync window has closed,
    because until then a missing night is late rather than lost.

    Args:
        now: The current moment, in any timezone.

    Returns:
        The newest local date whose data is legitimately owed.
    """
    local = now.astimezone()
    behind = 1 if local.hour >= DATA_HEALTH_STALE_CUTOFF_HOUR else 2
    return local.date() - timedelta(days=behind)


def _assess_data_freshness(
    newest_data_date: str | None,
    *,
    stale_after_days: int,
    now: datetime,
) -> IngestHealth:
    """Judge whether stored data has kept up, given a healthy-looking pipe.

    Args:
        newest_data_date: Newest ISO date holding a metric, or None when the
            profile has stored none at all.
        stale_after_days: Owed days that may go missing before reporting.
        now: The current moment.

    Returns:
        Either ``ok`` or ``stale``.
    """
    expected = newest_expected_day(now)
    if newest_data_date is None:
        return IngestHealth(
            status="stale",
            detail=(
                "Uploads are arriving and importing, but no daily health metrics "
                "have ever been stored from them. Check the daemon log for parser "
                "errors."
            ),
        )

    newest = date.fromisoformat(newest_data_date)
    days_behind = (expected - newest).days
    if days_behind < stale_after_days:
        return IngestHealth(
            status="ok",
            detail=f"Daily health metrics are current through {newest_data_date}.",
        )

    first_missing = newest + timedelta(days=1)
    if days_behind == 1:
        gap = f"for {first_missing.isoformat()}"
    else:
        gap = (
            f"for {first_missing.isoformat()} to {expected.isoformat()} "
            f"({days_behind} days)"
        )
    return IngestHealth(
        status="stale",
        detail=(
            f"No daily health metrics have arrived {gap}. Check that Auto Export "
            "is still running on the phone and that its automations are enabled. "
            "The rolling Metrics export may still backfill the gap once sync "
            "resumes."
        ),
        since=newest_data_date,
        missing_from=first_missing.isoformat(),
        missing_to=expected.isoformat(),
    )


def tailscale_node_health() -> tuple[bool | None, str]:
    """Check whether this node still holds its Tailscale control connection.

    The distinction this draws is the one that cost two days of data on
    2026-08-29. Uploads had stopped and the public DNS record was gone, which
    looks exactly like the recurring Tailscale-side outage — but this node was
    reporting ``Online: false`` with no network map, while ``curl`` reached the
    coordination server from the same machine in two seconds. The control plane
    does not publish a public address for a node it cannot see, so the missing
    record was a symptom of a wedged local daemon, not an outage to wait out.

    Reported separately from ``public_dns_health`` because the two have
    opposite fixes: this one is repairable here in seconds, and the other is
    nobody's to fix.

    Returns:
        Whether the node is connected, and a detail line. ``None`` means the
        question could not be answered — no CLI, a timeout, unreadable output —
        and must never be reported as a disconnection, because an absent CLI is
        not a broken tailnet.
    """
    if not TAILSCALE_BINARY.exists():
        return None, f"the Tailscale CLI is not at {TAILSCALE_BINARY}"
    try:
        completed = subprocess.run(
            [str(TAILSCALE_BINARY), "status", "--json"],
            capture_output=True,
            text=True,
            timeout=TAILSCALE_STATUS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"the Tailscale CLI could not be run ({exc})"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:200]
        return None, f"tailscale status exited {completed.returncode}: {detail}"
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, "tailscale status returned unreadable JSON"
    self_info = payload.get("Self")
    if not isinstance(self_info, dict) or not isinstance(self_info.get("Online"), bool):
        return None, "tailscale status did not report this node's state"
    if self_info["Online"]:
        return True, "this Mac is connected to Tailscale"
    warnings = [w for w in payload.get("Health") or [] if isinstance(w, str)]
    reason = warnings[0] if warnings else "no reason was reported"
    return False, (
        "This Mac has lost its connection to Tailscale, so Tailscale has "
        "stopped publishing the address your phone uploads to and exports "
        f"cannot get through. Tailscale says: {reason}"
    )


def public_dns_health(dns_name: str) -> tuple[bool | None, str]:
    """Check that the Funnel hostname resolves for anything outside the tailnet.

    Every local signal can look perfect while uploads are impossible: the
    receiver answers on loopback, ``tailscale funnel status`` says the Funnel
    is on, and MagicDNS resolves the hostname on this machine. None of that
    involves the public DNS record the phone actually needs, so when that
    record disappeared the loss stayed invisible for a day and the sync alert
    blamed the phone instead.

    Re-asserting the Funnel config does not republish the record; that was
    measured against a live occurrence on 2026-08-16 and changed nothing over
    several minutes. Whether ``tailscale funnel reset`` plus re-enable helps is
    genuinely unresolved: that outage ended roughly fifteen minutes after a
    reset, but at 28 hours old it was already inside the 26-35 hour band the
    four observed occurrences cleared in on their own. One sample cannot
    separate the two, so the message promises neither.

    The detail lives in ``docs/http-ingest.md`` rather than in a status line
    nobody reads to the end.

    Args:
        dns_name: Tailscale DNS name serving the Funnel.

    Returns:
        Whether the name resolves publicly and a detail line. The flag is
        ``None`` when the check could not run at all, which must not be
        reported as a failure — an offline laptop is not a missing record.
    """
    unknown: str | None = None
    # A then AAAA, because a resolver can hold a stale negative answer for one
    # while already serving the other. Observed on 2026-08-16: minutes after the
    # record was republished, Cloudflare still returned NXDOMAIN for A while
    # answering AAAA, and the phone was uploading throughout. Asking only about
    # A would have raised an outage alert against a working pipe, which is the
    # fastest way to teach someone to ignore this check.
    for record_type, type_code in (("A", 1), ("AAAA", 28)):
        query = urllib.parse.urlencode({"name": dns_name, "type": record_type})
        request = urllib.request.Request(
            f"{PUBLIC_DNS_RESOLVER_URL}?{query}",
            headers={"accept": "application/dns-json"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=PUBLIC_DNS_TIMEOUT_S
            ) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            unknown = f"could not reach the public resolver ({exc})"
            continue
        if not isinstance(payload, dict):
            unknown = "the public resolver returned an unreadable answer"
            continue
        addresses = [
            answer.get("data")
            for answer in payload.get("Answer") or []
            if isinstance(answer, dict) and answer.get("type") == type_code
        ]
        if addresses:
            return True, f"resolves to {', '.join(addresses)}"

    if unknown is not None:
        return None, unknown
    return False, (
        f"Tailscale has stopped publishing the address your phone uploads to "
        f"({dns_name}), so exports cannot get through. This Mac is still "
        "connected, so this is Tailscale's side and nothing to do here. Past "
        "outages cleared themselves within about 36 hours."
    )


def assess_ingest_health(
    profile: Profile,
    *,
    newest_data_date: str | None,
    stale_after_days: int,
    split_after_h: float,
    pair_window_s: float,
    resolve_funnel_dns: Callable[[], tuple[bool | None, str]] | None = None,
    resolve_node_health: Callable[[], tuple[bool | None, str]] | None = None,
    now: datetime | None = None,
) -> IngestHealth:
    """Judge whether a profile's phone is still successfully feeding the system.

    Five conditions are worth a message. ``node`` and ``funnel`` are settled
    first whenever uploads have gone quiet, because silence invalidates the
    premise the upload-timing conditions rest on; the rest follow in order of
    how specific they are about the cause:

    ``node``
        This Mac has lost its Tailscale control connection. Checked before
        ``funnel`` because it produces the same missing DNS record from a
        completely different cause: the control plane does not publish a public
        address for a node it cannot see. Repairable here in seconds, which is
        the whole reason it must not be reported as the outage below.
    ``funnel``
        Uploads have gone quiet and the Funnel's public DNS record is missing,
        so the phone cannot reach the receiver however healthy it looks here.
        Only checked after ``FUNNEL_DNS_CHECK_AFTER_H`` of silence, since an
        arriving upload has already proved the record resolves. Reported
        separately from ``stale`` because the cause is known, external, and
        nobody's to fix: measured occurrences cleared themselves in 26-35
        hours, and no local command shortened one.
    ``error``
        The last import failed and none has succeeded since, or the state file
        itself is unreadable.
    ``split``
        Uploads arrived and no pair has imported. A strong signal while the
        phone is demonstrably talking to us, so it is detected in hours. Since
        a half now imports on its own once the pairing window lapses, a stall
        this long is a fault on this end whatever the arrival gap was — the
        window is still passed in so the message can say which, and so a pair
        that has not reached its import deadline yet is not mistaken for one
        that missed it. Measured from when the un-imported upload landed, not
        from the last import: the hours in between are hours in which there
        was nothing to import.

    ``stale``
        The pipe looks fine and days of data are missing anyway. The catch-all,
        and the only one measured against stored data rather than upload
        timing: Auto Export batches unpredictably, so hours of silence prove
        nothing, while a day that never arrived is a fault however it was
        scheduled. Cause-agnostic — phone off, token revoked, Funnel down, app
        deleted, parser rejecting every payload.

    A profile that has never uploaded is reported as ``ok``: it is mid-setup,
    not broken, and nagging someone who has not finished onboarding is worse
    than saying nothing.

    Args:
        profile: Profile whose ingest state should be examined.
        newest_data_date: Newest ISO date holding a stored metric, from
            ``store.latest_metric_date``, or None when there is none.
        stale_after_days: Owed days that may go missing before reporting
            ``stale``.
        split_after_h: Hours without a successful import, while uploads keep
            arriving, before reporting ``split``.
        resolve_funnel_dns: Returns whether the Funnel hostname resolves
            publicly, as ``public_dns_health`` does. Injected so this stays a
            pure function over the state file, and omitted for transports with
            no Funnel. A ``None`` result means the check could not run and is
            never treated as a fault.
        resolve_node_health: Returns whether this node still holds its Tailscale
            control connection, as ``tailscale_node_health`` does. Injected and
            optional on the same terms. A ``None`` result is never a fault.
        pair_window_s: Arrival gap the importer tolerates, so a stall can be
            blamed on drifting schedules only when the halves really missed it.
        now: Override for the current time.

    Returns:
        The current condition, with a detail line naming the fix.
    """
    now = now or datetime.now(timezone.utc)
    state_path = profile.http_cache / ".ingest_state.json"
    try:
        state = _load_json_object(
            state_path,
            missing={"version": STATE_VERSION, "uploads": {}, "receipts": []},
        )
    except ValueError:
        return IngestHealth(
            status="error",
            detail=(
                f"The ingest state file for {profile.name} is unreadable "
                f"({state_path}). Uploads are still being accepted but nothing "
                "can import until it is repaired."
            ),
        )

    uploads = state.get("uploads")
    uploads = uploads if isinstance(uploads, dict) else {}
    arrivals = {
        kind: _parse_iso(entry.get("received_at"))
        for kind, entry in uploads.items()
        if isinstance(entry, dict)
    }
    arrivals = {kind: seen for kind, seen in arrivals.items() if seen is not None}
    if not arrivals:
        return IngestHealth(status="ok", detail="No upload has ever arrived.")

    last_upload = max(arrivals.values())
    last_import = _parse_iso(state.get("last_imported_at"))
    everything_imported = last_import is not None and last_upload <= last_import

    # Silence is the only state where the public DNS record can say something
    # the last upload has not already proved — and it is also the state in which
    # every upload-timing diagnosis below stops meaning anything, because they
    # all rest on the phone demonstrably reaching us. Settling the record first
    # is what keeps a stalled pair from being blamed on the automations while
    # the real fault is that nothing can arrive at all: an unimportable pair
    # never clears itself during an outage, so the stall would mask the outage
    # for as long as the outage lasted. A live pipe still reports its local
    # faults first, since those are the owner's to repair and this is not.
    quiet_long_enough = (now - last_upload).total_seconds() >= (
        FUNNEL_DNS_CHECK_AFTER_H * 3600
    )
    if quiet_long_enough:
        # Asked before the DNS record, because a disconnected node explains a
        # missing record outright and the two want opposite responses: this one
        # is repaired here, that one is waited out. Getting the order wrong
        # tells the owner to wait on something they could have fixed.
        if resolve_node_health is not None:
            connected, node_detail = resolve_node_health()
            if connected is False:
                return IngestHealth(
                    status="node",
                    detail=node_detail,
                    since=last_upload.isoformat(),
                )
        if resolve_funnel_dns is not None:
            resolves, dns_detail = resolve_funnel_dns()
            if resolves is False:
                return IngestHealth(
                    status="funnel",
                    detail=dns_detail,
                    since=last_upload.isoformat(),
                )

    if not everything_imported:
        pipe = _assess_pipe_fault(
            state,
            uploads=uploads,
            arrivals=arrivals,
            last_import=last_import,
            split_after_h=split_after_h,
            pair_window_s=pair_window_s,
            now=now,
        )
        if pipe is not None:
            return pipe

    if last_import is None:
        # An upload has landed but no pair has completed a cycle yet, so there is
        # nothing stored and nothing wrong with that: this is the first minutes
        # of onboarding. Waiting past the split threshold is a fault, and the
        # check above already owns that call.
        return IngestHealth(status="ok", detail="No pair has imported yet.")

    # The uploads that arrived have been consumed, so the mechanism is sound and
    # the only remaining question is whether they carried the days they owed.
    return _assess_data_freshness(
        newest_data_date,
        stale_after_days=stale_after_days,
        now=now,
    )


def _staged_arrivals(state: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """Return the oldest and newest arrival of a complete staged pair.

    Args:
        state: Parsed ingest state file.

    Returns:
        ``(oldest, newest)``, or None unless both halves are staged and
        carry a readable arrival time.
    """
    uploads = state.get("uploads")
    uploads = uploads if isinstance(uploads, dict) else {}
    arrivals = []
    for kind in ("metrics", "workouts"):
        upload = uploads.get(kind)
        if not isinstance(upload, dict):
            return None
        seen = _parse_iso(upload.get("received_at"))
        if seen is None:
            return None
        arrivals.append(seen)
    return min(arrivals), max(arrivals)


def _pair_due_at(state: dict[str, Any], pair_window_s: float) -> datetime | None:
    """Return when a complete staged pair may import, or None when it cannot.

    Halves that arrived within the window are due the moment the later one
    landed. Halves that missed each other are due one window after the pair
    first had something new to import — ``pending_since``, not the latest
    arrival.

    The distinction is the whole point. Anchoring on the latest arrival made
    the deadline renewable: Auto Export re-sends Metrics every 15-30 minutes,
    each re-send moved the deadline a full hour further out, and on 2026-08-30
    a pair only imported because the phone happened to go quiet for an hour.
    Under a steadier cadence it would have waited forever — the same "delayed
    means never" failure the window was rewritten to end, one layer up.

    Args:
        state: Parsed ingest state file.
        pair_window_s: How long a staged half waits for its partner.

    Returns:
        The moment the pair may import, or None when no complete pair is
        staged.
    """
    arrivals = _staged_arrivals(state)
    if arrivals is None:
        return None
    oldest, newest = arrivals
    if (newest - oldest).total_seconds() <= pair_window_s:
        return newest
    # Absent only in state files written before pending_since existed. Falling
    # back to the newest arrival reproduces the old deadline for one cycle,
    # which the next import replaces.
    pending_since = _parse_iso(state.get("pending_since")) or newest
    return pending_since + timedelta(seconds=pair_window_s)


def _assess_pipe_fault(
    state: dict[str, Any],
    *,
    uploads: dict[str, Any],
    arrivals: dict[str, datetime],
    last_import: datetime | None,
    split_after_h: float,
    pair_window_s: float,
    now: datetime,
) -> IngestHealth | None:
    """Report an upload that arrived and never imported, if one has stalled.

    Args:
        state: Parsed ingest state file.
        uploads: Raw per-kind upload records.
        arrivals: Parsed arrival times per kind.
        last_import: Time of the last successful import, if any.
        split_after_h: Hours a stall must persist before it is reported.
        pair_window_s: Arrival gap the importer tolerates.
        now: The current moment.

    Returns:
        The fault, or None when nothing has stalled long enough to report.
    """
    # A pair inside its pairing window is waiting by design, not stuck. The
    # health check used to know nothing about that deadline and reported the
    # wait as a stall, telling the operator to restart an import that was doing
    # exactly what it was asked to.
    due_at = _pair_due_at(state, pair_window_s)
    if due_at is not None and now < due_at:
        return None

    pending_since = _parse_iso(state.get("pending_since"))
    if pending_since is not None:
        # The stall began when the un-imported data landed. Measuring from the
        # last import instead charged it for every hour the phone was asleep,
        # so the first upload of the morning arrived already past a threshold
        # meant to describe hours of failing to import something.
        stalled_for = (now - pending_since).total_seconds() / 3600
    elif last_import is not None:
        stalled_for = (now - last_import).total_seconds() / 3600
    else:
        # Nothing has ever imported, so the condition has run since the first
        # upload ever seen — not since the most recent one.
        first_seen = [
            seen
            for entry in uploads.values()
            if isinstance(entry, dict)
            and (seen := _parse_iso(entry.get("first_seen_at"))) is not None
        ]
        started = min(first_seen) if first_seen else min(arrivals.values())
        stalled_for = (now - started).total_seconds() / 3600
    if stalled_for < split_after_h:
        return None

    last_error = state.get("last_error")
    if isinstance(last_error, dict):
        failed_at = _parse_iso(last_error.get("failed_at"))
        if failed_at is not None and (last_import is None or failed_at > last_import):
            return IngestHealth(
                status="error",
                detail=(
                    "The last import failed and none has succeeded since: "
                    f"{last_error.get('message', 'check the daemon logs')}"
                ),
                since=failed_at.isoformat(),
            )

    if len(arrivals) < 2:
        missing = "Workouts" if "metrics" in arrivals else "Metrics"
        return IngestHealth(
            status="split",
            detail=(
                f"{missing} uploads have never arrived, so nothing can import. "
                f"Check that the {missing} automation exists in Auto Export and "
                "points at the same URL and token as the other one."
            ),
            since=min(arrivals.values()).isoformat(),
        )

    gap = abs((arrivals["metrics"] - arrivals["workouts"]).total_seconds())
    stalled = _format_gap(stalled_for * 3600)
    # Neither branch blames the phone any more. A wide gap used to block the
    # import outright and was worth telling the user to fix; now it only delays
    # one by the pairing window, so a stall outlasting that window is this end's
    # fault either way, and sending someone after their automation schedules
    # would send them after the one thing that is demonstrably working.
    if gap > pair_window_s:
        detail = (
            f"An upload has been waiting {stalled} to import and none has run. "
            f"Metrics and Workouts are {_format_gap(gap)} apart, so the pair was "
            f"due {_format_gap(pair_window_s)} after the wait began and is long "
            "past that. The import on this end is stuck: check the daemon log "
            "and restart it."
        )
    else:
        detail = (
            f"An upload has been waiting {stalled} to import and none has run. "
            f"Metrics and Workouts are only {_format_gap(gap)} apart, well "
            f"inside the {_format_gap(pair_window_s)} pairing window, so the "
            "phone is fine and the import on this end is stuck. Check the "
            "daemon log and restart it."
        )
    return IngestHealth(
        status="split",
        detail=detail,
        since=last_import.isoformat() if last_import else None,
    )


class HttpIngestManager:
    """Archive raw payloads, persist bounded latest ones, and pair imports."""

    def __init__(
        self,
        profiles: dict[str, Profile],
        *,
        pair_window_s: int,
        on_pair_ready: Callable[[str, str], None],
    ) -> None:
        """Initialize profile caches and the complete-pair callback."""
        self.profiles = profiles
        self.pair_window_s = pair_window_s
        self.on_pair_ready = on_pair_ready
        self._lock = threading.RLock()
        self._inflight: dict[tuple[str, str], dict[str, str]] = {}

    def _state_path(self, profile: Profile) -> Path:
        """Return the private state file for a profile's HTTP cache."""
        return profile.http_cache / ".ingest_state.json"

    def _load_state(self, profile: Profile) -> dict[str, Any]:
        """Load one profile's HTTP state."""
        state = _load_json_object(
            self._state_path(profile),
            missing={"version": STATE_VERSION, "uploads": {}, "receipts": []},
        )
        if state.get("version") != STATE_VERSION:
            raise ValueError(f"Unsupported HTTP ingest state for {profile.name}.")
        if not isinstance(state.get("uploads"), dict):
            raise ValueError(f"Invalid HTTP ingest uploads state for {profile.name}.")
        if not isinstance(state.get("receipts"), list):
            state["receipts"] = []
        return state

    def _payload_path(self, profile: Profile, kind: str) -> Path:
        """Return the bounded latest payload path for a kind."""
        directory = "Metrics" if kind == "metrics" else "Workouts"
        return profile.http_cache / directory / "latest.json"

    def _archive_path(self, profile: Profile, kind: str) -> Path:
        """Return today's archive path for one payload kind."""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return profile.import_archive / kind / f"{day}.json.gz"

    def _archive_payload(
        self,
        profile: Profile,
        upload: ValidatedUpload,
        body: bytes,
    ) -> None:
        """Keep one gzipped raw payload per kind per day, last upload winning.

        The bounded ``latest.json`` cache is overwritten by every upload, so it
        cannot answer "what did the phone actually send?" after the fact. This
        daily archive can, which is what makes a later parser fix or a widened
        metric map replayable without asking someone to re-export from their
        phone.

        One snapshot a day is enough because Auto Export sends a rolling
        multi-day window: each date is covered both by its own day's snapshot
        and by the next day's, which is the more complete of the two once Apple
        Health has backfilled past midnight. Keeping every upload instead would
        store roughly twenty near-identical copies a day — and would not even
        deduplicate, since Auto Export does not serialize JSON keys in a stable
        order, so unchanged content still hashes differently on every send.

        Archiving never blocks ingestion: a failure here is logged loudly and
        the payload still imports.
        """
        try:
            _atomic_write(
                self._archive_path(profile, upload.kind),
                gzip.compress(body, mtime=0),
            )
        except OSError:
            logger.exception(
                "Could not archive the %s payload for %s. The upload still "
                "imports, but this payload will not be replayable later; check "
                "permissions and free space under %s",
                upload.kind,
                profile.name,
                profile.import_archive,
            )

    def _pair_gap_s(self, state: dict[str, Any]) -> float | None:
        """Return the arrival gap between the latest Metrics and Workouts uploads."""
        uploads = state["uploads"]
        metrics = uploads.get("metrics")
        workouts = uploads.get("workouts")
        if not isinstance(metrics, dict) or not isinstance(workouts, dict):
            return None
        try:
            metrics_time = datetime.fromisoformat(metrics["received_at"])
            workouts_time = datetime.fromisoformat(workouts["received_at"])
        except (KeyError, TypeError, ValueError):
            return None
        return abs((metrics_time - workouts_time).total_seconds())

    def _pair_digest(self, state: dict[str, Any]) -> str | None:
        """Return a digest identifying the currently staged Metrics/Workouts pair."""
        uploads = state["uploads"]
        try:
            material = (
                f"{uploads['metrics']['sha256']}:{uploads['workouts']['sha256']}"
            ).encode("ascii")
        except (KeyError, TypeError):
            return None
        return hashlib.sha256(material).hexdigest()

    def _pair_due_at(self, state: dict[str, Any]) -> datetime | None:
        """Return when the staged pair may import, or None when it cannot."""
        return _pair_due_at(state, self.pair_window_s)

    def _pending_arrivals(self, state: dict[str, Any]) -> list[datetime]:
        """Return arrival times of staged halves the last import does not cover."""
        imported = state.get("last_import_uploads")
        imported = imported if isinstance(imported, dict) else {}
        pending: list[datetime] = []
        for kind in ("metrics", "workouts"):
            upload = state["uploads"].get(kind)
            if not isinstance(upload, dict):
                continue
            sha256 = upload.get("sha256")
            session_id = upload.get("session_id")
            if not isinstance(sha256, str) or not isinstance(session_id, str):
                continue
            if imported.get(kind) == f"{sha256}:{session_id}":
                continue
            seen = _parse_iso(upload.get("received_at"))
            if seen is not None:
                pending.append(seen)
        return pending

    def _has_pending_half(self, state: dict[str, Any]) -> bool:
        """Whether a staged half is not covered by the last successful import."""
        return bool(self._pending_arrivals(state))

    def _pair_state(
        self,
        state: dict[str, Any],
        *,
        profile_name: str,
    ) -> tuple[str, str]:
        """Return why this profile is or is not about to import a complete pair."""
        uploads = state["uploads"]
        missing = [
            label
            for kind, label in (("metrics", "Metrics"), ("workouts", "Workouts"))
            if not isinstance(uploads.get(kind), dict)
        ]
        if missing:
            return "waiting", f"no {' or '.join(missing)} upload has arrived yet"
        gap = self._pair_gap_s(state)
        if gap is None:
            return "waiting", "an upload record is unreadable; re-run both automations"
        if self._pair_candidate(state, profile_name=profile_name) is not None:
            return "ready", "both halves staged and queued for import"
        if self._pair_digest(state) == state.get("last_import_pair"):
            return "imported", "both halves already imported"
        due_at = self._pair_due_at(state)
        if due_at is not None and datetime.now(timezone.utc) < due_at:
            return "split", (
                f"Metrics and Workouts arrived {_format_gap(gap)} apart, outside the "
                f"{_format_gap(self.pair_window_s)} pairing window; importing anyway "
                f"at {due_at.isoformat(timespec='seconds')}"
            )
        # Staged payloads the last receipt does not cover. Reporting these as
        # imported is how a stalled pair stayed invisible in `ingest status`.
        return "pending", (
            "both halves are staged but no import has recorded them yet; "
            "an import may still be running"
        )

    def _upload_markers(self, state: dict[str, Any]) -> dict[str, str] | None:
        """Return request identities for the latest Metrics and Workouts uploads."""
        uploads = state["uploads"]
        markers: dict[str, str] = {}
        for kind in ("metrics", "workouts"):
            upload = uploads.get(kind)
            if not isinstance(upload, dict):
                return None
            sha256 = upload.get("sha256")
            session_id = upload.get("session_id")
            if not isinstance(sha256, str) or not isinstance(session_id, str):
                return None
            markers[kind] = f"{sha256}:{session_id}"
        return markers

    def _pair_candidate(
        self,
        state: dict[str, Any],
        *,
        profile_name: str,
        now: datetime | None = None,
    ) -> tuple[str, dict[str, str]] | None:
        """Return a complete pair once at least one half advanced after import.

        Requiring *both* halves to be new deadlocks the common case: the two
        automations rarely fire simultaneously, so whichever half lands second
        pairs with an already-imported partner and would be refused until its
        own partner uploaded again. The digest check above already rejects a
        pair that is identical to the last imported one, so re-reading an
        unchanged half is the harmless price of importing the fresh one.

        Args:
            state: Parsed ingest state file.
            profile_name: Profile the pair belongs to, for the in-flight check.
            now: Override for the current time, used to decide whether a pair
                whose halves missed each other has waited long enough.
        """
        now = now or datetime.now(timezone.utc)
        pair_digest = self._pair_digest(state)
        markers = self._upload_markers(state)
        if pair_digest is None or markers is None:
            return None
        due_at = self._pair_due_at(state)
        if due_at is None or now < due_at:
            return None
        if pair_digest == state.get("last_import_pair"):
            return None
        previous = state.get("last_import_uploads")
        if isinstance(previous, dict) and all(
            previous.get(kind) == marker for kind, marker in markers.items()
        ):
            return None
        # Overlap with a running import stays ``any``: this only defers the new
        # pair until that import finishes and rewrites ``last_import_uploads``,
        # so it cannot deadlock the way the check above did.
        for (inflight_profile, _digest), inflight_markers in self._inflight.items():
            if inflight_profile == profile_name and any(
                inflight_markers.get(kind) == marker for kind, marker in markers.items()
            ):
                return None
        return pair_digest, markers

    def accept(
        self,
        profile_name: str,
        upload: ValidatedUpload,
        body: bytes,
    ) -> AcceptedUpload:
        """Archive the raw payload, replace the latest one, and pair if ready."""
        profile = self.profiles[profile_name]
        self._archive_payload(profile, upload, body)
        callback_digest: str | None = None
        pair_state = "ready"
        pair_detail = ""
        with self._lock:
            state = self._load_state(profile)
            previous = state["uploads"].get(upload.kind)
            duplicate = (
                isinstance(previous, dict) and previous.get("sha256") == upload.sha256
            )
            _atomic_write(self._payload_path(profile, upload.kind), body)
            received_at = _utc_now()
            # Stamped before this upload is recorded, so it marks when the
            # pipeline first had something new to import rather than when it
            # most recently did. A refresh of an already-pending half must not
            # move it; that is what made the import deadline renewable.
            had_pending = self._has_pending_half(state)
            # Only the latest upload per kind is kept, so without this a profile
            # that configured just one automation would look freshly active
            # forever and never trip the health check.
            first_seen_at = (
                previous.get("first_seen_at")
                if isinstance(previous, dict) and previous.get("first_seen_at")
                else received_at
            )
            state["uploads"][upload.kind] = {
                "sha256": upload.sha256,
                "received_at": received_at,
                "first_seen_at": first_seen_at,
                "automation_id": upload.automation_id,
                "session_id": upload.session_id,
                "bytes": upload.size,
            }
            if not had_pending:
                state["pending_since"] = received_at
            candidate = self._pair_candidate(state, profile_name=profile_name)
            pair_ready = candidate is not None
            _atomic_write_json(self._state_path(profile), state)
            if pair_ready:
                callback_digest = candidate[0]
            else:
                pair_state, pair_detail = self._pair_state(
                    state, profile_name=profile_name
                )
        logger.info(
            "Accepted %s upload for %s (%d bytes%s)",
            upload.kind,
            profile_name,
            upload.size,
            ", identical to the previous one" if duplicate else "",
        )
        if pair_ready:
            logger.info("Complete pair staged for %s; queuing import", profile_name)
        elif pair_state == "split":
            logger.info(
                "%s is waiting out the pairing window: %s. Giving both Auto Export "
                "automations the same schedule imports it sooner.",
                profile_name,
                pair_detail,
            )
        else:
            logger.info("%s not importing yet: %s", profile_name, pair_detail)
        if callback_digest is not None:
            self.on_pair_ready(profile_name, callback_digest)
        return AcceptedUpload(
            profile=profile_name,
            kind=upload.kind,
            duplicate=duplicate,
            pair_ready=pair_ready,
        )

    def due_pairs(self, *, now: datetime | None = None) -> list[tuple[str, str]]:
        """Return staged pairs that may import now and have not yet done so.

        Called on daemon start to recover work an unclean shutdown left staged,
        and on a timer thereafter. The timer is what rescues a pair whose halves
        missed each other: no further upload is coming to re-evaluate it, so
        nothing but a clock will notice that its wait is over.

        Args:
            now: Override for the current time.

        Returns:
            ``(profile name, pair digest)`` for every pair ready to import.
        """
        now = now or datetime.now(timezone.utc)
        due: list[tuple[str, str]] = []
        with self._lock:
            for name, profile in self.profiles.items():
                try:
                    state = self._load_state(profile)
                except ValueError:
                    logger.exception("Invalid HTTP ingest state for %s", name)
                    continue
                candidate = self._pair_candidate(state, profile_name=name, now=now)
                if candidate is not None:
                    due.append((name, candidate[0]))
        return due

    def begin_import(self, profile_name: str, pair_digest: str) -> Path | None:
        """Snapshot the current pair into an immutable import generation."""
        profile = self.profiles[profile_name]
        key = (profile_name, pair_digest)
        with self._lock:
            state = self._load_state(profile)
            candidate = self._pair_candidate(state, profile_name=profile_name)
            if (
                candidate is None
                or candidate[0] != pair_digest
                or key in self._inflight
            ):
                return None
            generation = profile.http_cache / ".staging" / pair_digest
            if generation.exists():
                shutil.rmtree(generation)
            for kind, directory in (("metrics", "Metrics"), ("workouts", "Workouts")):
                destination = generation / directory / "latest.json"
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(self._payload_path(profile, kind), destination)
            self._inflight[key] = candidate[1]
            return generation

    def finish_import(
        self,
        profile_name: str,
        pair_digest: str,
        *,
        success: bool,
        error: str | None = None,
    ) -> None:
        """Record an import receipt or failure and remove its transient snapshot."""
        profile = self.profiles[profile_name]
        key = (profile_name, pair_digest)
        with self._lock:
            imported_uploads = self._inflight.get(key)
            if success and imported_uploads is None:
                raise ValueError(
                    f"Cannot finish unknown HTTP import pair for {profile_name}."
                )
            state = self._load_state(profile)
            now = _utc_now()
            if success:
                state["last_import_pair"] = pair_digest
                state["last_import_uploads"] = imported_uploads
                state["last_imported_at"] = now
                state["last_error"] = None
                receipts = state["receipts"]
                receipts.append({"pair": pair_digest, "imported_at": now})
                state["receipts"] = receipts[-HTTP_INGEST_MAX_RECEIPTS:]
                # A half that landed while this import ran is still pending, and
                # its wait started when it arrived rather than when the previous
                # one did. Re-deriving from what the receipt leaves uncovered
                # keeps the stamp honest and repairs a state file written before
                # it existed.
                still_pending = self._pending_arrivals(state)
                if still_pending:
                    state["pending_since"] = min(still_pending).isoformat()
                else:
                    state.pop("pending_since", None)
            else:
                state["last_error"] = {
                    "pair": pair_digest,
                    "failed_at": now,
                    "message": error or "Import failed; check daemon logs.",
                }
            _atomic_write_json(self._state_path(profile), state)
            self._inflight.pop(key, None)
            generation = profile.http_cache / ".staging" / pair_digest
            if generation.exists():
                shutil.rmtree(generation)
            staging_root = profile.http_cache / ".staging"
            if staging_root.exists() and not any(staging_root.iterdir()):
                staging_root.rmdir()

    def capture_batch(
        self,
        profile_name: str,
        headers: dict[str, str],
        body: bytes,
    ) -> dict[str, Any]:
        """Store one raw batch request verbatim without importing anything.

        A deliberate dead end. Nothing here is validated, staged, paired, or
        parsed, and no database is touched: the endpoint exists only to learn
        what Auto Export puts on the wire once batch export is switched on, so
        the real receiver can be built against an observed format rather than an
        assumed one.

        Headers and body are written as one JSON file per request, named by
        arrival order and time so a multi-request batch can be replayed in the
        order the phone sent it. The body is stored base64-encoded so a
        malformed or non-UTF-8 payload is captured exactly as received rather
        than being lost to a decode error — the whole point is to see what
        actually arrived.

        Args:
            profile_name: Authenticated profile the request arrived for.
            headers: Lowercased request headers.
            body: Raw request body.

        Returns:
            A dict describing what was captured, for the response and the log.
        """
        profile = self.profiles[profile_name]
        directory = profile.batch_capture
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

        received_at = _utc_now()
        stamp = received_at.replace(":", "").replace("-", "")
        digest = hashlib.sha256(body).hexdigest()
        record = {
            "received_at": received_at,
            "profile": profile_name,
            # Authorization is dropped rather than captured: it is the bearer
            # token, and this file is a plain-text inspection artefact.
            "headers": {k: v for k, v in headers.items() if k != "authorization"},
            "bytes": len(body),
            "sha256": digest,
            "body_base64": base64.b64encode(body).decode("ascii"),
        }
        path = directory / f"{stamp}_{digest[:12]}.json"
        _atomic_write(path, json.dumps(record, indent=2).encode())

        # Bound the directory so a misconfigured automation cannot fill the disk.
        existing = sorted(directory.glob("*.json"))
        for stale in existing[:-HTTP_INGEST_BATCH_CAPTURE_MAX_FILES]:
            try:
                stale.unlink()
            except OSError:
                logger.warning("Could not remove old batch capture %s", stale)

        logger.info(
            "Captured batch request for %s (%d bytes) at %s — inspection only, "
            "nothing imported",
            profile_name,
            len(body),
            path,
        )
        return {
            "captured": True,
            "bytes": len(body),
            "sha256": digest,
            "stored_files": min(len(existing), HTTP_INGEST_BATCH_CAPTURE_MAX_FILES),
        }

    def status(self) -> dict[str, dict[str, Any]]:
        """Return secret-free upload and import state for every HTTP profile."""
        result: dict[str, dict[str, Any]] = {}
        with self._lock:
            for name, profile in self.profiles.items():
                state = self._load_state(profile)
                uploads = state["uploads"]
                pair_state, pair_detail = self._pair_state(state, profile_name=name)
                result[name] = {
                    "metrics_received_at": (
                        uploads.get("metrics", {}).get("received_at")
                        if isinstance(uploads.get("metrics"), dict)
                        else None
                    ),
                    "workouts_received_at": (
                        uploads.get("workouts", {}).get("received_at")
                        if isinstance(uploads.get("workouts"), dict)
                        else None
                    ),
                    "last_imported_at": state.get("last_imported_at"),
                    "last_error": state.get("last_error"),
                    "pair_state": pair_state,
                    "pair_detail": pair_detail,
                }
        return result
