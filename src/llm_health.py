"""Health data rendering and assembly for LLM prompts.

Pure formatting functions that turn raw health data dicts into compact
markdown for prompt injection, plus DB-backed data assembly.

Extracted from llm.py to separate app-domain logic from LLM infrastructure.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, datetime, timedelta

from aggregator import summarise
from report import group_by_week, to_dict
from store import load_date_range, load_snapshots

logger = logging.getLogger(__name__)

# Before this hour, yesterday's null sleep is marked "sync_pending" instead of
# "not_tracked" — the data likely hasn't synced from the watch yet.
SLEEP_SYNC_CUTOFF_HOUR = 10

_SLEEP_KEYS = frozenset(
    {
        "sleep_total_h",
        "sleep_in_bed_h",
        "sleep_efficiency_pct",
        "sleep_deep_h",
        "sleep_core_h",
        "sleep_rem_h",
        "sleep_awake_h",
    }
)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_decimal(value: float | int | None, decimals: int = 1) -> str | None:
    """Format a numeric value with stable prompt-friendly precision."""
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    return f"{value:.{decimals}f}"


def _format_metric(
    value: float | int | None,
    unit: str,
    *,
    decimals: int = 1,
) -> str | None:
    """Format a metric with units, or return None when missing or zero.

    Treats `None` and any value <= 0 as missing — weekly aggregates report
    `0` for unlogged categories (e.g. `total_lift_min` on a no-lift week)
    and we don't want to render them as `"0 min"`.
    """
    if value is None:
        return None
    try:
        if float(value) <= 0:
            return None
    except (TypeError, ValueError):
        return None
    rendered = _format_decimal(value, decimals=decimals)
    if rendered is None:
        return None
    return f"{rendered} {unit}".strip()


def _format_percent(value: float | int | None, decimals: int = 1) -> str | None:
    """Format a percentage without an extra space before the percent sign."""
    rendered = _format_decimal(value, decimals=decimals)
    if rendered is None:
        return None
    return f"{rendered}%"


def _format_pace(value: float | None) -> str | None:
    """Format minutes/km as mm:ss/km."""
    if value is None:
        return None
    total_seconds = int(round(value * 60))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}/km"


def _format_day_label(date_str: str) -> str:
    """Render a human-readable weekday/date label."""
    dt = date.fromisoformat(date_str)
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%b')}"


def _pluralize(count: int, singular: str, plural: str | None = None) -> str:
    """Return a simple singular/plural label for a count."""
    if count == 1:
        return singular
    return plural or f"{singular}s"


def _format_workout_line(workout: dict) -> str:
    """Render one workout compactly for prompt consumption."""
    name = str(workout.get("type") or workout.get("category") or "Workout")
    parts: list[str] = []

    distance = workout.get("gpx_distance_km") or workout.get("distance_km")
    duration = workout.get("duration_min")
    pace = workout.get("pace_min_per_km")
    if pace is None and distance and duration:
        try:
            if float(distance) > 0:
                pace = float(duration) / float(distance)
        except (TypeError, ValueError):
            pace = None

    distance_text = _format_metric(distance, "km", decimals=1)
    duration_text = _format_metric(duration, "min", decimals=0)
    if distance_text:
        parts.append(distance_text)
    if duration_text:
        parts.append(duration_text)
    pace_value = float(pace) if isinstance(pace, (int, float)) else None
    pace_text = _format_pace(pace_value)
    if pace_text:
        parts.append(pace_text)

    avg_hr = _format_metric(
        workout.get("avg_hr") or workout.get("hr_avg"),
        "bpm",
        decimals=0,
    )
    if avg_hr:
        parts.append(f"avg HR {avg_hr}")

    elevation = _format_metric(
        workout.get("elevation_gain_m") or workout.get("gpx_elevation_gain_m"),
        "m",
        decimals=0,
    )
    if elevation:
        parts.append(f"elev +{elevation}")

    location = workout.get("location_label") or workout.get("location_locality")
    country = workout.get("location_country")
    if location:
        location_text = str(location)
        if country and str(country).lower() not in location_text.lower():
            location_text = f"{location_text}, {country}"
        parts.append(location_text)

    splits_text = _format_splits(workout.get("splits"))
    if splits_text:
        parts.append(f"splits {splits_text}")

    if parts:
        return f"{name} ({'; '.join(parts)})"
    return name


def _format_splits(splits: object) -> str | None:
    """Format per-km splits as a compact slash-separated pace string.

    Returns None when splits are missing, empty, or unparseable, so the
    workout line stays clean for non-GPS sessions.
    """
    if not isinstance(splits, list) or not splits:
        return None
    rendered: list[str] = []
    for split in splits:
        if not isinstance(split, dict):
            continue
        pace = split.get("pace_min_km")
        if not isinstance(pace, (int, float)):
            continue
        minutes = int(pace)
        seconds = int(round((float(pace) - minutes) * 60))
        if seconds == 60:
            minutes += 1
            seconds = 0
        rendered.append(f"{minutes}:{seconds:02d}")
    if not rendered:
        return None
    return "/".join(rendered)


# ---------------------------------------------------------------------------
# Day / week block renderers
# ---------------------------------------------------------------------------


def _render_day_block(day: dict, withheld: frozenset[str] = frozenset()) -> str:
    """Render one day card for the prompt.

    Args:
        day: Canonical day dict.
        withheld: Metric columns with too few readings to show as a per-day
            series. Their latest values are reported once elsewhere instead.

    Returns:
        Markdown for one day card.
    """

    def value_of(column: str) -> object:
        """Return a day value unless the metric is being withheld."""
        return None if column in withheld else day.get(column)

    lines = [f"#### {_format_day_label(str(day['date']))}"]

    activity_parts: list[str] = []
    steps = _format_metric(value_of("steps"), "steps", decimals=0)
    if steps:
        activity_parts.append(steps)
    exercise = _format_metric(value_of("exercise_min"), "exercise min", decimals=0)
    if exercise:
        activity_parts.append(exercise)
    distance = _format_metric(value_of("distance_km"), "km", decimals=1)
    if distance:
        activity_parts.append(distance)
    if activity_parts:
        lines.append(f"- Activity: {', '.join(activity_parts)}.")

    recovery_parts: list[str] = []
    hrv = _format_metric(value_of("hrv_ms"), "ms", decimals=1)
    if hrv:
        recovery_parts.append(f"HRV {hrv}")
    resting_hr = _format_metric(value_of("resting_hr"), "bpm", decimals=0)
    if resting_hr:
        recovery_parts.append(f"resting HR {resting_hr}")
    recovery_index = _format_decimal(value_of("recovery_index"), decimals=2)
    if recovery_index:
        recovery_parts.append(f"recovery index {recovery_index}")
    if recovery_parts:
        lines.append(f"- Recovery: {', '.join(recovery_parts)}.")
    else:
        lines.append("- Recovery: unavailable.")

    sleep_status = day.get("sleep_status")
    if sleep_status == "tracked":
        sleep_parts: list[str] = []
        sleep_total = _format_metric(value_of("sleep_total_h"), "h", decimals=1)
        if sleep_total:
            sleep_parts.append(sleep_total)
        efficiency = _format_percent(value_of("sleep_efficiency_pct"))
        if efficiency:
            sleep_parts.append(f"efficiency {efficiency}")
        stage_parts: list[str] = []
        for key, label in (
            ("sleep_deep_h", "deep"),
            ("sleep_core_h", "core"),
            ("sleep_rem_h", "REM"),
            ("sleep_awake_h", "awake"),
        ):
            value = _format_metric(value_of(key), "h", decimals=1)
            if value:
                stage_parts.append(f"{label} {value}")
        if stage_parts:
            sleep_parts.append(", ".join(stage_parts))
        if sleep_parts:
            lines.append(f"- Sleep: {'; '.join(sleep_parts)}.")
        else:
            lines.append("- Sleep: tracked, details unavailable.")
    elif sleep_status == "pending":
        lines.append("- Sleep: pending sync.")
    elif sleep_status == "not_tracked":
        lines.append("- Sleep: not tracked.")
    else:
        lines.append("- Sleep: unavailable.")

    workouts = day.get("workouts") or []
    if workouts:
        rendered_workouts = " | ".join(_format_workout_line(w) for w in workouts)
        lines.append(f"- Workouts: {rendered_workouts}.")
    else:
        lines.append("- Workouts: none logged.")

    return "\n".join(lines)


def _render_week_summary_block(summary: dict, *, prompt_kind: str) -> str:
    """Render the current target week's summary with semantic missing-data rules.

    The totals here already include today, and the heading says so because a
    Today block follows immediately below and "logged so far" beside it is
    genuinely ambiguous.

    This is a clarity change, not a fix. Nudges overstated the week's run count
    by one in W21 and again in W27, and the obvious suspect was this wording —
    but an A/B over ten runs per variant scored 8/10 either way, so the phrasing
    is not what causes it. The defect is intermittent, reproduces on DeepSeek
    Flash, DeepSeek Pro and Opus 5 alike, and its mechanism is still unknown.
    See nudge_week_totals_match_logged_workouts_w21.
    """
    title = "### Target Week Summary"
    if prompt_kind == "chat":
        # "So Far" carried the same trap as "logged so far" below: day cards
        # for the week, today included, follow immediately after.
        title = "### This Week to Date (totals include today)"
    lines = [title]

    week_label = summary.get("week_label")
    if week_label:
        lines.append(f"- Week: {week_label}.")

    run_count = int(summary.get("run_count", 0) or 0)
    lift_count = int(summary.get("lift_count", 0) or 0)
    walk_count = int(summary.get("walk_count", 0) or 0)
    lines.append(
        "- Week to date, including today (do not add today's sessions again): "
        f"{run_count} {_pluralize(run_count, 'run')}, "
        f"{lift_count} {_pluralize(lift_count, 'lift')}, "
        f"{walk_count} {_pluralize(walk_count, 'walk')}."
    )

    running_parts: list[str] = []
    total_run_km = _format_metric(summary.get("total_run_km"), "km", decimals=1)
    if total_run_km:
        running_parts.append(f"{total_run_km} total")
    best_pace = _format_pace(summary.get("best_pace_min_per_km"))
    if best_pace:
        running_parts.append(f"best pace {best_pace}")
    avg_run_hr = _format_metric(summary.get("avg_run_hr"), "bpm", decimals=0)
    if avg_run_hr:
        running_parts.append(f"avg run HR {avg_run_hr}")
    avg_elev = _format_metric(summary.get("avg_elevation_gain_m"), "m", decimals=0)
    if avg_elev:
        running_parts.append(f"avg elev {avg_elev}")
    if running_parts:
        lines.append(f"- Running: {', '.join(running_parts)}.")

    lift_parts: list[str] = []
    total_lift = _format_metric(summary.get("total_lift_min"), "min", decimals=0)
    if total_lift:
        lift_parts.append(total_lift)
    avg_lift_hr = _format_metric(summary.get("avg_lift_hr"), "bpm", decimals=0)
    if avg_lift_hr:
        lift_parts.append(f"avg lift HR {avg_lift_hr}")
    if lift_parts:
        lines.append(f"- Strength: {', '.join(lift_parts)}.")

    activity_parts: list[str] = []
    avg_steps = _format_metric(summary.get("avg_steps"), "steps/day", decimals=0)
    if avg_steps:
        activity_parts.append(avg_steps)
    avg_exercise = _format_metric(
        summary.get("avg_exercise_min"), "exercise min/day", decimals=0
    )
    if avg_exercise:
        activity_parts.append(avg_exercise)
    avg_energy = _format_metric(
        summary.get("avg_active_energy_kj"), "kJ/day", decimals=0
    )
    if avg_energy:
        activity_parts.append(avg_energy)
    if activity_parts:
        lines.append(f"- Activity: {', '.join(activity_parts)}.")

    recovery_parts: list[str] = []
    avg_hrv = _format_metric(summary.get("avg_hrv_ms"), "ms")
    if avg_hrv:
        recovery_parts.append(f"avg HRV {avg_hrv}")
    avg_resting_hr = _format_metric(summary.get("avg_resting_hr"), "bpm", decimals=0)
    if avg_resting_hr:
        recovery_parts.append(f"avg resting HR {avg_resting_hr}")
    avg_recovery = _format_decimal(summary.get("avg_recovery_index"), decimals=2)
    if avg_recovery:
        recovery_parts.append(f"avg recovery index {avg_recovery}")
    if recovery_parts:
        lines.append(f"- Recovery: {', '.join(recovery_parts)}.")
    else:
        lines.append("- Recovery: unavailable this week.")

    hrv_trend = summary.get("hrv_trend")
    if hrv_trend:
        lines.append(f"- HRV trend: {hrv_trend}.")
    else:
        lines.append("- HRV trend: unavailable.")

    sleep_total = int(summary.get("sleep_nights_total", 0) or 0)
    sleep_tracked = summary.get("sleep_nights_tracked")
    if sleep_total == 0:
        lines.append("- Sleep: not tracked this week.")
    else:
        sleep_parts: list[str] = []
        if sleep_tracked is not None:
            sleep_parts.append(f"{int(sleep_tracked)}/{sleep_total} nights tracked")
        avg_sleep_total = _format_metric(summary.get("avg_sleep_total_h"), "h")
        if avg_sleep_total:
            sleep_parts.append(f"{avg_sleep_total} avg")
        avg_sleep_eff = _format_percent(summary.get("avg_sleep_efficiency_pct"))
        if avg_sleep_eff:
            sleep_parts.append(f"efficiency {avg_sleep_eff}")
        stage_parts: list[str] = []
        for key, label in (
            ("avg_sleep_deep_h", "deep"),
            ("avg_sleep_core_h", "core"),
            ("avg_sleep_rem_h", "REM"),
            ("avg_sleep_awake_h", "awake"),
        ):
            value = _format_metric(summary.get(key), "h")
            if value:
                stage_parts.append(f"{label} {value}")
        if stage_parts:
            sleep_parts.append(", ".join(stage_parts))
        if sleep_parts:
            lines.append(f"- Sleep: {'; '.join(sleep_parts)}.")
        else:
            lines.append("- Sleep: tracked this week, details unavailable.")

    return "\n".join(lines)


def _render_history_week(summary: dict) -> str:
    """Render one compact prior-week bullet."""
    label = str(summary.get("week_label", "Unknown week"))
    parts = [
        f"{int(summary.get('run_count', 0) or 0)} run",
        f"{int(summary.get('lift_count', 0) or 0)} lift",
    ]
    walk_count = int(summary.get("walk_count", 0) or 0)
    if walk_count:
        parts.append(f"{walk_count} walk")

    total_run = _format_metric(summary.get("total_run_km"), "km", decimals=1)
    if total_run:
        parts.append(total_run)

    avg_hrv = _format_metric(summary.get("avg_hrv_ms"), "ms")
    if avg_hrv:
        parts.append(f"HRV {avg_hrv}")
    avg_resting_hr = _format_metric(summary.get("avg_resting_hr"), "bpm", decimals=0)
    if avg_resting_hr:
        parts.append(f"RHR {avg_resting_hr}")

    sleep_total = int(summary.get("sleep_nights_total", 0) or 0)
    sleep_avg = _format_metric(summary.get("avg_sleep_total_h"), "h")
    if sleep_avg:
        parts.append(f"sleep {sleep_avg}")
    elif sleep_total == 0 and summary.get("avg_sleep_total_h") is None:
        parts.append("sleep not tracked")

    return f"- {label}: {', '.join(parts)}."


def _split_nudge_days(
    days: list[dict],
    today: date,
) -> tuple[list[dict], list[dict]]:
    """Split days into (today, recent) for nudge rendering."""
    today_iso = today.isoformat()
    today_blocks = [day for day in days if day.get("date") == today_iso]
    if today_blocks:
        recent = [day for day in days if day.get("date") != today_iso][-2:]
    else:
        recent = days[-3:]
    return (today_blocks, recent)


def _clean_nudge_text(text: str) -> str:
    """Strip saved-report chrome from a delivered nudge body."""
    cleaned_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            cleaned_lines.append("")
            continue
        if line == "---":
            continue
        if line.startswith("_Generated by "):
            continue
        if re.fullmatch(r"\*\*[^*]*Data Sync[^*]*\*\*", line):
            continue
        cleaned_lines.append(raw_line.rstrip())

    # Collapse repeated blank lines after removing prompt chrome.
    collapsed: list[str] = []
    previous_blank = False
    for line in cleaned_lines:
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        collapsed.append(line)
        previous_blank = is_blank

    return "\n".join(collapsed).strip()


def format_recent_nudges(
    entries: list[dict],
    *,
    empty_text: str = "(none yet)",
) -> str:
    """Return a compact prompt-friendly rendering of recent delivered nudges."""
    if not entries:
        return empty_text

    blocks: list[str] = []
    for index, entry in enumerate(entries, start=1):
        timestamp = str(entry.get("ts", ""))[:16]
        trigger = str(entry.get("trigger", "unknown"))
        body = _clean_nudge_text(str(entry.get("text", ""))) or "(empty)"
        blocks.append(f"{index}. [{timestamp} / {trigger}]\n{body}")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Top-level render orchestrator
# ---------------------------------------------------------------------------


_WITHHELD_LABELS = {
    "steps": ("Steps", "", 0),
    "exercise_min": ("Exercise minutes", "min", 0),
    "distance_km": ("Distance", "km", 1),
    "hrv_ms": ("HRV", "ms", 1),
    "resting_hr": ("Resting HR", "bpm", 0),
    "recovery_index": ("Recovery index", "", 2),
    "sleep_total_h": ("Sleep duration", "h", 1),
    "sleep_efficiency_pct": ("Sleep efficiency", "%", 1),
    "sleep_deep_h": ("Deep sleep", "h", 1),
    "sleep_core_h": ("Core sleep", "h", 1),
    "sleep_rem_h": ("REM sleep", "h", 1),
    "sleep_awake_h": ("Awake", "h", 1),
}


def _render_withheld_readings(days: list[dict], withheld: frozenset[str]) -> str | None:
    """Report the latest value of each metric kept out of the day cards.

    The reading itself is useful and stays. What is removed is its position in
    a dated sequence, because a handful of numbers in date order reads as a
    direction to any reader — including one that has just been told, correctly,
    that there are not enough of them to establish one.

    Args:
        days: Canonical day dicts, oldest first.
        withheld: Metric columns being kept out of the day cards.

    Returns:
        A markdown section, or None when nothing was withheld.
    """
    entries: list[str] = []
    for column in _WITHHELD_LABELS:
        if column not in withheld:
            continue
        latest: tuple[str, object] | None = None
        count = 0
        for day in days:
            value = day.get(column)
            if value is None:
                continue
            count += 1
            latest = (str(day.get("date")), value)
        if latest is None:
            continue
        label, unit, decimals = _WITHHELD_LABELS[column]
        rendered = _format_metric(latest[1], unit, decimals=decimals)
        entries.append(f"- {label}: {rendered} on {latest[0]} ({count} recorded).")

    if not entries:
        return None
    return (
        "### Readings Without a Baseline\n\n"
        "Too few readings exist for these to describe what is normal for this "
        "person, so they are deliberately not shown per day: a short run of "
        "values in date order would read as a trend that the data cannot "
        "support. The most recent value of each is below. Report these as "
        "single observations. Do not describe a direction, a slope, a decline "
        "or an improvement in them, and do not base a recommendation on one.\n\n"
        + "\n".join(entries)
    )


def render_health_data(
    health_data: dict,
    *,
    prompt_kind: str,
    week: str = "current",
    today: date | None = None,
    unestablished: set[str] | None = None,
) -> str:
    """Render canonical health data into compact markdown for LLM prompts.

    Args:
        health_data: Canonical health-data dict from ``build_llm_data``.
        prompt_kind: Which prompt the rendering is for.
        week: Which week the report targets.
        today: Override for the current date.
        unestablished: Metric columns with too few readings to show as a
            per-day series. Omit on a mature profile.

    Returns:
        Compact markdown for prompt injection.
    """
    if today is None:
        today = date.today()
    withheld = frozenset(unestablished or ())

    current_week = health_data.get("current_week", {})
    summary = current_week.get("summary") if isinstance(current_week, dict) else None
    days = current_week.get("days", []) if isinstance(current_week, dict) else []
    history = health_data.get("history", [])

    if not isinstance(days, list):
        days = []
    if not isinstance(history, list):
        history = []

    if not summary:
        return "No health data available."

    sections = [_render_week_summary_block(summary, prompt_kind=prompt_kind)]

    def render_day_section(title: str, day_blocks: list[dict]) -> None:
        if day_blocks:
            sections.append(
                f"{title}\n\n"
                + "\n\n".join(_render_day_block(day, withheld) for day in day_blocks)
            )

    if prompt_kind == "nudge":
        today_blocks, recent_blocks = _split_nudge_days(days, today)
        render_day_section("### Today", today_blocks)
        render_day_section("### Recent Days", recent_blocks)
    elif prompt_kind == "chat":
        render_day_section("### This Week Days (Mon to today)", days)
    else:
        title = {
            "current": "### Target Week Days (Mon to today)",
            "last": "### Target Week Days (Mon to Sun)",
        }.get(week, "### Target Week Days")
        render_day_section(title, days)
        since_days = health_data.get("since_days")
        if isinstance(since_days, list):
            render_day_section(
                "### Since That Week Ended (this week to date, not part of the "
                "reported week)",
                [day for day in since_days if isinstance(day, dict)],
            )

    if withheld:
        readings = _render_withheld_readings(days, withheld)
        if readings:
            sections.append(readings)

    if history:
        history_lines = []
        for entry in history:
            if not isinstance(entry, dict):
                continue
            summary_dict = entry.get("summary")
            if isinstance(summary_dict, dict):
                history_lines.append(_render_history_week(summary_dict))
        if history_lines:
            sections.append("### Previous Weeks\n\n" + "\n".join(history_lines))

    return "\n\n".join(sections).strip()


# ---------------------------------------------------------------------------
# Review facts
# ---------------------------------------------------------------------------


def _fmt_review_value(value: float | int | None, decimals: int = 1) -> str:
    """Format a numeric review value or em dash when unavailable."""
    if value is None:
        return "—"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{decimals}f}"


def _fmt_review_delta(current: float | int | None, previous: float | int | None) -> str:
    """Format a signed delta between two numeric values."""
    if current is None or previous is None:
        return "n/a"
    delta = float(current) - float(previous)
    return f"{delta:+.1f}"


def _recovery_verdict(
    summary: dict,
    previous_summary: dict | None,
    *,
    unestablished: frozenset[str] = frozenset(),
) -> str:
    """Derive a compact recovery verdict from weekly summary fields.

    The verdict is handed to the model as a fact, so it must not be derived
    from readings too sparse to support it. A declining HRV trend across three
    mornings is not a declining HRV trend, and a "back off" built on one lands
    in the report as an instruction the user is expected to follow.

    Args:
        summary: Weekly summary dict for the target week.
        previous_summary: Prior week's summary, when one exists.
        unestablished: Metric columns without enough readings to generalise
            from. Inputs drawn from these are ignored.

    Returns:
        One of "back off", "ready to push", "maintain", or "insufficient data".
    """

    def established(column: str, value: object) -> object:
        """Return a summary input unless its metric lacks the readings for it."""
        return None if column in unestablished else value

    recovery = established("recovery_index", summary.get("avg_recovery_index"))
    sleep_h = established("sleep_total_h", summary.get("avg_sleep_total_h"))
    sleep_eff = established(
        "sleep_efficiency_pct", summary.get("avg_sleep_efficiency_pct")
    )
    hrv_trend = established("hrv_ms", summary.get("hrv_trend"))
    resting_hr = established("resting_hr", summary.get("avg_resting_hr"))
    prev_resting_hr = (
        established("resting_hr", previous_summary.get("avg_resting_hr"))
        if previous_summary
        else None
    )

    if all(
        value is None for value in (recovery, sleep_h, sleep_eff, hrv_trend, resting_hr)
    ):
        return "insufficient data"

    if (
        (recovery is not None and recovery < 0.95)
        or hrv_trend == "declining"
        or (sleep_h is not None and sleep_h < 6.5)
        or (sleep_eff is not None and sleep_eff < 85)
        or (
            resting_hr is not None
            and prev_resting_hr is not None
            and resting_hr - prev_resting_hr >= 3
        )
    ):
        return "back off"

    if (
        (recovery is not None and recovery >= 1.15)
        and hrv_trend in {"improving", "stable", None}
        and (sleep_h is None or sleep_h >= 7.0)
        and (sleep_eff is None or sleep_eff >= 88)
    ):
        return "ready to push"

    return "maintain"


def build_review_facts(
    health_data: dict,
    context: dict[str, str] | None = None,
    *,
    week_complete: bool,
    unestablished: set[str] | None = None,
) -> str:
    """Build a compact shared summary for insights and coach prompts.

    Args:
        health_data: Canonical health-data dict from ``build_llm_data``.
        context: Loaded context files, when available.
        week_complete: Whether the target week has fully elapsed.
        unestablished: Metric columns with too few readings to generalise
            from. Omit on a mature profile.

    Returns:
        Compact markdown of shared review facts.
    """
    summary = health_data.get("current_week", {}).get("summary") or {}
    history = health_data.get("history", [])
    previous_summary = history[-1].get("summary") if history else None
    week_label = summary.get("week_label") or health_data.get("week_label") or "unknown"

    withheld = frozenset(unestablished or ())
    verdict = _recovery_verdict(summary, previous_summary, unestablished=withheld)
    run_count = summary.get("run_count")
    lift_count = summary.get("lift_count")
    total_run_km = summary.get("total_run_km")
    avg_hrv = summary.get("avg_hrv_ms")
    avg_resting_hr = summary.get("avg_resting_hr")
    avg_sleep = summary.get("avg_sleep_total_h")
    avg_sleep_eff = summary.get("avg_sleep_efficiency_pct")
    hrv_trend = summary.get("hrv_trend") or "n/a"
    prev_run_km = previous_summary.get("total_run_km") if previous_summary else None
    prev_hrv = previous_summary.get("avg_hrv_ms") if previous_summary else None
    prev_resting = previous_summary.get("avg_resting_hr") if previous_summary else None
    notes_present = (
        bool(context) and context.get("log", "(not provided)") != "(not provided)"
    )
    feedback_present = (
        bool(context)
        and context.get("coach_feedback", "(not provided)") != "(not provided)"
        and context.get("coach_feedback") != "(none)"
    )

    lines = [
        "## Shared Review Facts",
        (
            f"- Reviewed week: **{week_label}**. "
            f"Status: {'complete' if week_complete else 'in progress'}."
        ),
        (
            "- Training snapshot: "
            f"**{run_count or 0}** runs, "
            f"**{lift_count or 0}** lifts, "
            f"**{_fmt_review_value(total_run_km)} km** run volume."
        ),
        (
            "- Recovery verdict: "
            f"**{verdict}**. HRV {_fmt_review_value(avg_hrv)} ms "
            f"({hrv_trend}), resting HR {_fmt_review_value(avg_resting_hr)} bpm, "
            f"sleep {_fmt_review_value(avg_sleep)} h at "
            f"{_fmt_review_value(avg_sleep_eff)}% efficiency."
        ),
    ]

    if previous_summary:
        lines.append(
            "- vs previous week: "
            f"run volume {_fmt_review_delta(total_run_km, prev_run_km)} km, "
            f"HRV {_fmt_review_delta(avg_hrv, prev_hrv)} ms, "
            f"resting HR {_fmt_review_delta(avg_resting_hr, prev_resting)} bpm."
        )
    else:
        lines.append("- vs previous week: no prior weekly summary available.")

    if notes_present:
        lines.append(
            "- User notes are available in `log.md`; treat them as ground truth for constraints."
        )
    if feedback_present:
        lines.append(
            "- Recent accept/reject history is available in `coach_feedback.md`; avoid repeating recently rejected edits."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Data assembly from DB
# ---------------------------------------------------------------------------


def build_llm_data(
    conn: sqlite3.Connection, months: int, week: str = "current"
) -> dict:
    """Build a compact summary + history JSON for LLM consumption.

    Args:
        conn: Open SQLite database connection.
        months: Number of months of history to include.
        week: Which week to report on — "current" or "last".

    Returns:
        A dict with 'current_week', 'history', 'week_complete', and
        'week_label' keys, JSON-serialisable.
    """
    dr = load_date_range(conn)
    if dr is None:
        return {
            "current_week": {"summary": None},
            "history": [],
            "week_complete": False,
        }

    today = date.today()
    if week == "last":
        last_sunday = today - timedelta(days=today.weekday() + 1)
        week_start = (last_sunday - timedelta(days=6)).isoformat()
        week_end = last_sunday.isoformat()
    else:
        monday = today - timedelta(days=today.weekday())
        week_start = monday.isoformat()
        week_end = (monday + timedelta(days=6)).isoformat()

    logger.info(
        "Report dates: mode=%s, week=%s..%s, today=%s, db_range=%s..%s",
        week,
        week_start,
        week_end,
        today,
        dr[0],
        dr[1],
    )

    # Fetch one extra day before the week so we can shift sleep forward.
    sleep_start = (date.fromisoformat(week_start) - timedelta(days=1)).isoformat()
    current_snaps = load_snapshots(conn, start=sleep_start, end=week_end)

    history_end = (date.fromisoformat(week_start) - timedelta(days=1)).isoformat()
    history_start = (
        date.fromisoformat(week_start) - timedelta(days=30 * months)
    ).isoformat()
    history_snaps = load_snapshots(conn, start=history_start, end=history_end)
    history_weeks = group_by_week(history_snaps)

    ws = date.fromisoformat(week_start)
    iso = ws.isocalendar()
    week_label = f"{iso.year}-W{iso.week:02d}"

    days = _shift_sleep_forward([to_dict(s) for s in current_snaps], week_start)

    # --- Classify sleep status per day and compute compliance ---
    today_iso = today.isoformat()
    sleep_tracked, sleep_total_eligible, not_tracked_dates = _classify_sleep_status(
        days, today
    )

    # --- Build the today snapshot ---
    today_snapshot = _build_today_snapshot(days, today_iso)

    # --- Assemble the summary ---
    week_snaps = [s for s in current_snaps if s.date >= week_start]
    summary = to_dict(summarise(week_snaps)) if week_snaps else None

    if summary:
        summary["sleep_nights_tracked"] = sleep_tracked
        summary["sleep_nights_total"] = sleep_total_eligible
        summary["sleep_not_tracked_dates"] = not_tracked_dates
        if today_snapshot:
            summary["today"] = today_snapshot

    return {
        "current_week": {
            "summary": summary,
            "days": days,
        },
        "history": [{"summary": to_dict(summarise(w))} for w in history_weeks],
        "since_days": _build_since_days(conn, week_end, today),
        "week_complete": today > date.fromisoformat(week_end),
        "week_label": week_label,
    }


def _shift_sleep_forward(days: list[dict], boundary: str) -> list[dict]:
    """Move each night's sleep onto the morning it belongs to.

    Apple records a night's sleep against the day it started, but the user
    experiences it as the morning they woke up. Callers fetch one extra day
    before the window so the first in-window morning has a night to inherit;
    that extra day is dropped afterwards.

    Args:
        days: Day dicts in ascending date order, oldest first.
        boundary: First date that belongs to the window, ISO format. Days
            before it are dropped once their sleep has been shifted forward.

    Returns:
        The in-window days, sleep shifted.
    """
    for i in range(len(days) - 1, 0, -1):
        prev_day, cur_day = days[i - 1], days[i]
        for k in _SLEEP_KEYS:
            cur_day[k] = prev_day.get(k)
            prev_day.pop(k, None)
    if days and days[0].get("date", "") < boundary:
        days = days[1:]
    return days


def _classify_sleep_status(days: list[dict], today: date) -> tuple[int, int, list[str]]:
    """Tag each day with a sleep status and count tracking compliance.

    A night can be missing because the user did not wear the watch or because
    the export has not caught up yet. Only the first is a compliance miss, so
    today — and yesterday before the morning sync window closes — is "pending"
    rather than "not tracked" and does not count against the total.

    Args:
        days: Day dicts, mutated in place with a ``sleep_status`` key.
        today: The current date.

    Returns:
        Tuple of (nights tracked, nights eligible, dates not tracked).
    """
    today_iso = today.isoformat()
    yesterday_iso = (today - timedelta(days=1)).isoformat()
    before_sync_cutoff = datetime.now().hour < SLEEP_SYNC_CUTOFF_HOUR

    tracked = 0
    eligible = 0
    not_tracked_dates: list[str] = []

    for day in days:
        if not isinstance(day, dict):
            continue
        day_date = day.get("date")
        has_sleep = any(day.get(k) is not None for k in _SLEEP_KEYS)

        if has_sleep:
            day["sleep_status"] = "tracked"
            tracked += 1
            eligible += 1
        elif day_date == today_iso or (
            day_date == yesterday_iso and before_sync_cutoff
        ):
            day["sleep_status"] = "pending"
        else:
            day["sleep_status"] = "not_tracked"
            eligible += 1
            not_tracked_dates.append(day_date or "")

    return tracked, eligible, not_tracked_dates


def _build_since_days(
    conn: sqlite3.Connection, week_end: str, today: date
) -> list[dict]:
    """Build day cards for everything after the reported week, up to today.

    The weekly report runs on Monday morning but can be triggered by hand at
    any point in the following week. Without this the model is blind to the
    days between: it would talk about the finished week as if no time had
    passed, and recommend a session the user already did.

    Args:
        conn: Open SQLite database connection.
        week_end: Last date of the reported week, ISO format.
        today: The current date.

    Returns:
        Day dicts from the day after ``week_end`` through today, sleep
        shifted. Empty when the reported week is the current one.
    """
    first = date.fromisoformat(week_end) + timedelta(days=1)
    if first > today:
        return []
    # Fetch week_end too, so the first morning has a night to inherit.
    snaps = load_snapshots(conn, start=week_end, end=today.isoformat())
    days = _shift_sleep_forward([to_dict(s) for s in snaps], first.isoformat())
    _classify_sleep_status(days, today)
    return days


def _build_today_snapshot(days: list[dict], today_iso: str) -> dict | None:
    """Extract a compact snapshot for today from the shifted day list."""
    today_day = None
    for d in days:
        if isinstance(d, dict) and d.get("date") == today_iso:
            today_day = d
            break
    if today_day is None:
        return None

    workouts = []
    for w in today_day.get("workouts", []):
        distance_km = w.get("gpx_distance_km")
        duration_min = w.get("duration_min")
        pace_min_per_km: float | None = None
        if distance_km and duration_min and distance_km > 0:
            pace_min_per_km = round(duration_min / distance_km, 2)
        workouts.append(
            {
                "type": w.get("type"),
                "category": w.get("category"),
                "counts_as_lift": w.get("counts_as_lift"),
                "duration_min": duration_min,
                "distance_km": distance_km,
                "pace_min_per_km": pace_min_per_km,
                "avg_hr": w.get("hr_avg"),
                "elevation_gain_m": w.get("gpx_elevation_gain_m"),
                "location_label": w.get("location_label"),
                "location_locality": w.get("location_locality"),
                "location_country": w.get("location_country"),
                "location_country_code": w.get("location_country_code"),
            }
        )

    snapshot: dict = {
        "date": today_iso,
        "hrv_ms": today_day.get("hrv_ms"),
        "resting_hr": today_day.get("resting_hr"),
        "recovery_index": today_day.get("recovery_index"),
        "steps": today_day.get("steps"),
        "exercise_min": today_day.get("exercise_min"),
        "sleep_status": today_day.get("sleep_status", "pending"),
    }
    if snapshot["sleep_status"] == "tracked":
        snapshot["sleep_total_h"] = today_day.get("sleep_total_h")
        snapshot["sleep_efficiency_pct"] = today_day.get("sleep_efficiency_pct")

    if workouts:
        snapshot["workouts"] = workouts

    return snapshot
