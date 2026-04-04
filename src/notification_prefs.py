"""Notification preference storage and evaluation helpers.

Public API:
    DEFAULT_NOTIFICATION_PREFS  — built-in notification defaults.
    load_notification_prefs     — read prefs JSON and prune expired mutes.
    save_notification_prefs     — persist prefs JSON.
    effective_notification_prefs — merge defaults with overrides.
    evaluate_nudge_delivery     — decide whether a nudge may send now.
    evaluate_report_delivery    — decide whether a scheduled report may send now.
    apply_notification_changes  — apply a validated proposal to prefs data.
    format_notification_summary — render a human-readable prefs summary.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, time
from pathlib import Path
from typing import Any

from config import NOTIFICATION_PREFS_PATH

logger = logging.getLogger(__name__)

PREFS_VERSION = 1
DAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
SETTABLE_PATHS = {
    "nudges.enabled",
    "nudges.earliest_time",
    "weekly_insights.enabled",
    "weekly_insights.weekday",
    "weekly_insights.time",
    "midweek_report.enabled",
    "midweek_report.weekday",
    "midweek_report.time",
}
RESETTABLE_PATHS = SETTABLE_PATHS | {
    "nudges",
    "weekly_insights",
    "midweek_report",
    "all",
}
MUTE_TARGETS = {"all", "nudges", "weekly_insights", "midweek_report"}

DEFAULT_NOTIFICATION_PREFS: dict[str, Any] = {
    "version": PREFS_VERSION,
    "overrides": {},
    "temporary_mutes": [],
}

_DEFAULT_EFFECTIVE: dict[str, dict[str, Any]] = {
    "nudges": {
        "enabled": True,
        "earliest_time": "10:00",
    },
    "weekly_insights": {
        "enabled": True,
        "weekday": "monday",
        "time": "08:00",
    },
    "midweek_report": {
        "enabled": True,
        "weekday": "thursday",
        "time": "09:00",
    },
}


def _deep_copy_defaults() -> dict[str, Any]:
    """Return a mutable deep copy of the default prefs payload."""
    return copy.deepcopy(DEFAULT_NOTIFICATION_PREFS)


def _parse_timestamp(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp or return None."""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _parse_hhmm(value: str) -> time:
    """Parse a 24-hour HH:MM string."""
    return datetime.strptime(value, "%H:%M").time()


def _normalise_overrides(overrides: object) -> dict[str, dict[str, Any]]:
    """Keep only recognised override keys and value shapes."""
    if not isinstance(overrides, dict):
        return {}

    clean: dict[str, dict[str, Any]] = {}
    for section, values in overrides.items():
        if section not in _DEFAULT_EFFECTIVE or not isinstance(values, dict):
            continue
        clean_section: dict[str, Any] = {}
        for key, value in values.items():
            path = f"{section}.{key}"
            if path not in SETTABLE_PATHS:
                continue
            if path.endswith(".enabled") and isinstance(value, bool):
                clean_section[key] = value
            elif path.endswith(".earliest_time") or path.endswith(".time"):
                if isinstance(value, str):
                    try:
                        _parse_hhmm(value)
                    except ValueError:
                        continue
                    clean_section[key] = value
            elif path.endswith(".weekday") and value in DAY_NAMES:
                clean_section[key] = value
        if clean_section:
            clean[section] = clean_section
    return clean


def prune_expired_mutes(
    prefs: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Remove expired or malformed temporary mutes.

    Returns:
        A tuple of (cleaned_prefs, changed).
    """
    now = now or datetime.now().astimezone()
    cleaned = copy.deepcopy(prefs)
    mutes = cleaned.get("temporary_mutes")
    if not isinstance(mutes, list):
        cleaned["temporary_mutes"] = []
        return cleaned, True

    fresh: list[dict[str, str]] = []
    changed = False
    for entry in mutes:
        if not isinstance(entry, dict):
            changed = True
            continue
        target = entry.get("target")
        expires_at = entry.get("expires_at")
        source_text = entry.get("source_text")
        if target not in MUTE_TARGETS or not isinstance(source_text, str):
            changed = True
            continue
        parsed = _parse_timestamp(expires_at)
        if parsed is None:
            changed = True
            continue
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        if parsed <= now:
            changed = True
            continue
        fresh.append(
            {
                "target": target,
                "expires_at": parsed.isoformat(),
                "source_text": source_text,
            }
        )
    cleaned["temporary_mutes"] = fresh
    return cleaned, changed


def load_notification_prefs(
    path: Path = NOTIFICATION_PREFS_PATH,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load notification prefs from JSON, falling back safely to defaults."""
    if not path.exists():
        return _deep_copy_defaults()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read notification prefs %s: %s", path, exc)
        return _deep_copy_defaults()

    prefs = _deep_copy_defaults()
    if isinstance(raw, dict):
        prefs["version"] = PREFS_VERSION
        prefs["overrides"] = _normalise_overrides(raw.get("overrides"))
        prefs["temporary_mutes"] = raw.get("temporary_mutes", [])

    cleaned, changed = prune_expired_mutes(prefs, now=now)
    if changed:
        save_notification_prefs(cleaned, path=path)
    return cleaned


def save_notification_prefs(
    prefs: dict[str, Any],
    path: Path = NOTIFICATION_PREFS_PATH,
) -> None:
    """Persist notification prefs JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs, indent=2, sort_keys=True), encoding="utf-8")


def effective_notification_prefs(prefs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Merge defaults with overrides into an effective settings map."""
    effective = copy.deepcopy(_DEFAULT_EFFECTIVE)
    overrides = prefs.get("overrides", {})
    if not isinstance(overrides, dict):
        return effective
    for section, values in overrides.items():
        if section not in effective or not isinstance(values, dict):
            continue
        effective[section].update(values)
    return effective


def active_temporary_mutes(
    prefs: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Return active temporary mutes after pruning expired entries."""
    cleaned, _ = prune_expired_mutes(prefs, now=now)
    return cleaned.get("temporary_mutes", [])


def _target_is_muted(target: str, mutes: list[dict[str, str]]) -> dict[str, str] | None:
    """Return the active mute that suppresses a target, if any."""
    for entry in mutes:
        if entry["target"] in {"all", target}:
            return entry
    return None


def evaluate_nudge_delivery(
    prefs: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Decide whether nudges may send now."""
    now = now or datetime.now().astimezone()
    effective = effective_notification_prefs(prefs)
    mutes = active_temporary_mutes(prefs, now=now)
    muted = _target_is_muted("nudges", mutes)
    if muted:
        return {
            "status": "suppressed",
            "reason": "temporary_mute",
            "until": muted["expires_at"],
        }
    if not effective["nudges"]["enabled"]:
        return {"status": "suppressed", "reason": "disabled"}

    earliest = _parse_hhmm(effective["nudges"]["earliest_time"])
    if now.time().replace(second=0, microsecond=0) < earliest:
        until_dt = now.replace(
            hour=earliest.hour,
            minute=earliest.minute,
            second=0,
            microsecond=0,
        )
        return {
            "status": "deferred",
            "reason": "earliest_time",
            "until": until_dt.isoformat(),
        }
    return {"status": "allowed", "reason": "enabled"}


def evaluate_report_delivery(
    prefs: dict[str, Any],
    report_type: str,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Decide whether a scheduled report may send now."""
    if report_type not in {"weekly_insights", "midweek_report"}:
        raise ValueError(f"Unsupported report type: {report_type}")
    now = now or datetime.now().astimezone()
    effective = effective_notification_prefs(prefs)
    mutes = active_temporary_mutes(prefs, now=now)
    muted = _target_is_muted(report_type, mutes)
    if muted:
        return {
            "status": "suppressed",
            "reason": "temporary_mute",
            "until": muted["expires_at"],
        }
    if not effective[report_type]["enabled"]:
        return {"status": "suppressed", "reason": "disabled"}
    return {"status": "allowed", "reason": "enabled"}


def scheduled_report_due(
    prefs: dict[str, Any],
    report_type: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Return True when the configured weekday/time has passed today."""
    if report_type not in {"weekly_insights", "midweek_report"}:
        raise ValueError(f"Unsupported report type: {report_type}")
    now = now or datetime.now().astimezone()
    effective = effective_notification_prefs(prefs)
    report = effective[report_type]
    if DAY_NAMES[now.weekday()] != report["weekday"]:
        return False
    scheduled = _parse_hhmm(report["time"])
    return now.time().replace(second=0, microsecond=0) >= scheduled


def _set_nested_override(overrides: dict[str, Any], path: str, value: Any) -> None:
    """Set a nested override by dotted path."""
    section, field = path.split(".", 1)
    section_map = overrides.setdefault(section, {})
    if not isinstance(section_map, dict):
        section_map = {}
        overrides[section] = section_map
    section_map[field] = value


def _reset_override(overrides: dict[str, Any], path: str) -> None:
    """Clear a specific override path or section."""
    if path == "all":
        overrides.clear()
        return
    if "." not in path:
        overrides.pop(path, None)
        return
    section, field = path.split(".", 1)
    section_map = overrides.get(section)
    if not isinstance(section_map, dict):
        return
    section_map.pop(field, None)
    if not section_map:
        overrides.pop(section, None)


def validate_notification_changes(changes: object) -> list[dict[str, Any]]:
    """Validate notify proposal changes and return a normalised list."""
    if not isinstance(changes, list):
        raise ValueError("changes must be a list")

    validated: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("each change must be an object")
        action = change.get("action")
        if action == "set":
            path = change.get("path")
            value = change.get("value")
            if path not in SETTABLE_PATHS:
                raise ValueError(f"unsupported set path: {path}")
            if path.endswith(".enabled") and not isinstance(value, bool):
                raise ValueError(f"{path} requires a boolean")
            if path.endswith(".weekday") and value not in DAY_NAMES:
                raise ValueError(f"{path} requires a weekday name")
            if path.endswith(".time") or path.endswith(".earliest_time"):
                if not isinstance(value, str):
                    raise ValueError(f"{path} requires a HH:MM string")
                _parse_hhmm(value)
            validated.append({"action": "set", "path": path, "value": value})
        elif action == "reset":
            path = change.get("path")
            if path not in RESETTABLE_PATHS:
                raise ValueError(f"unsupported reset path: {path}")
            validated.append({"action": "reset", "path": path})
        elif action == "reset_all":
            validated.append({"action": "reset_all"})
        elif action == "mute_until":
            target = change.get("target")
            expires_at = change.get("expires_at")
            source_text = change.get("source_text", "")
            if target not in MUTE_TARGETS:
                raise ValueError(f"unsupported mute target: {target}")
            parsed = _parse_timestamp(expires_at)
            if parsed is None:
                raise ValueError("mute_until requires a valid expires_at")
            if not isinstance(source_text, str):
                raise ValueError("mute_until source_text must be a string")
            validated.append(
                {
                    "action": "mute_until",
                    "target": target,
                    "expires_at": parsed.isoformat(),
                    "source_text": source_text,
                }
            )
        else:
            raise ValueError(f"unsupported change action: {action}")
    return validated


def apply_notification_changes(
    prefs: dict[str, Any],
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply validated changes to the prefs payload and return a new dict."""
    updated = copy.deepcopy(prefs)
    updated["version"] = PREFS_VERSION
    updated["overrides"] = _normalise_overrides(updated.get("overrides", {}))
    overrides = updated["overrides"]
    if not isinstance(overrides, dict):
        overrides = {}
        updated["overrides"] = overrides

    mutes = active_temporary_mutes(updated)
    updated["temporary_mutes"] = mutes

    for change in validate_notification_changes(changes):
        action = change["action"]
        if action == "set":
            _set_nested_override(overrides, change["path"], change["value"])
        elif action == "reset":
            _reset_override(overrides, change["path"])
        elif action == "reset_all":
            overrides.clear()
            updated["temporary_mutes"] = []
        elif action == "mute_until":
            updated["temporary_mutes"].append(
                {
                    "target": change["target"],
                    "expires_at": change["expires_at"],
                    "source_text": change["source_text"],
                }
            )

    cleaned, _ = prune_expired_mutes(updated)
    cleaned["overrides"] = _normalise_overrides(cleaned.get("overrides", {}))
    return cleaned


def _mute_label(target: str) -> str:
    """Return a friendly label for a mute target."""
    labels = {
        "all": "All notifications",
        "nudges": "Nudges",
        "weekly_insights": "Weekly insights",
        "midweek_report": "Midweek report",
    }
    return labels.get(target, target)


def format_notification_summary(
    prefs: dict[str, Any],
    *,
    now: datetime | None = None,
    include_examples: bool = False,
) -> str:
    """Render the current settings and active temporary mutes."""
    now = now or datetime.now().astimezone()
    effective = effective_notification_prefs(prefs)
    mutes = active_temporary_mutes(prefs, now=now)

    lines = [
        "Current notification settings:",
        f"- Nudges: {'on' if effective['nudges']['enabled'] else 'off'}",
        f"- Nudges not before: {effective['nudges']['earliest_time']}",
        (
            "- Weekly insights: "
            f"{'on' if effective['weekly_insights']['enabled'] else 'off'}"
            f" ({effective['weekly_insights']['weekday'].title()} "
            f"{effective['weekly_insights']['time']})"
        ),
        (
            "- Midweek report: "
            f"{'on' if effective['midweek_report']['enabled'] else 'off'}"
            f" ({effective['midweek_report']['weekday'].title()} "
            f"{effective['midweek_report']['time']})"
        ),
    ]

    if mutes:
        lines.append("")
        lines.append("Active temporary mutes:")
        for entry in mutes:
            lines.append(
                f"- {_mute_label(entry['target'])}: muted until {entry['expires_at']}"
            )

    if include_examples:
        lines.extend(
            [
                "",
                "Examples:",
                "- /notify no nudges before 11am",
                "- /notify send weekly insights on Tuesday at 8",
                "- /notify turn off midweek report",
                "- /notify mute nudges today",
                "- /notify mute all notifications until tomorrow 11am",
                "- /notify bring weekly insights back to default",
                "- /notify set all as default",
            ]
        )

    return "\n".join(lines)


def format_proposed_changes(
    prefs: dict[str, Any],
    changes: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> str:
    """Render a human-readable before/after proposal summary."""
    now = now or datetime.now().astimezone()
    updated = apply_notification_changes(prefs, changes)
    before = effective_notification_prefs(prefs)
    after = effective_notification_prefs(updated)
    before_mutes = active_temporary_mutes(prefs, now=now)
    after_mutes = active_temporary_mutes(updated, now=now)

    lines = ["Proposed notification changes:"]

    def _section_line(name: str, before_value: str, after_value: str) -> str:
        if before_value == after_value:
            return f"- {name}: unchanged ({after_value})"
        return f"- {name}: {before_value} -> {after_value}"

    lines.append(
        _section_line(
            "Nudges",
            "on" if before["nudges"]["enabled"] else "off",
            "on" if after["nudges"]["enabled"] else "off",
        )
    )
    lines.append(
        _section_line(
            "Nudge earliest time",
            before["nudges"]["earliest_time"],
            after["nudges"]["earliest_time"],
        )
    )
    lines.append(
        _section_line(
            "Weekly insights",
            (
                f"{before['weekly_insights']['weekday'].title()} "
                f"{before['weekly_insights']['time']} "
                f"({'on' if before['weekly_insights']['enabled'] else 'off'})"
            ),
            (
                f"{after['weekly_insights']['weekday'].title()} "
                f"{after['weekly_insights']['time']} "
                f"({'on' if after['weekly_insights']['enabled'] else 'off'})"
            ),
        )
    )
    lines.append(
        _section_line(
            "Midweek report",
            (
                f"{before['midweek_report']['weekday'].title()} "
                f"{before['midweek_report']['time']} "
                f"({'on' if before['midweek_report']['enabled'] else 'off'})"
            ),
            (
                f"{after['midweek_report']['weekday'].title()} "
                f"{after['midweek_report']['time']} "
                f"({'on' if after['midweek_report']['enabled'] else 'off'})"
            ),
        )
    )

    if after_mutes != before_mutes:
        if after_mutes:
            latest = after_mutes[-1]
            lines.append(
                f"- Temporary mute: {_mute_label(latest['target'])} until {latest['expires_at']}"
            )
        else:
            lines.append("- Temporary mute: cleared")

    return "\n".join(lines)
