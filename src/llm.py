"""LLM integration for personalized health insights.

Loads user context files, assembles prompts, calls an LLM via litellm,
and manages the memory/history feedback loop.

Public API:
    load_context         — read markdown context files from a directory
    build_messages       — assemble system + user messages for the LLM
    render_health_data   — render compact markdown health context per prompt kind
    format_recent_nudges — clean and format recent delivered nudges for prompts
    call_llm             — call litellm and return an LLMResult with text + metadata
    extract_memory       — pull <memory> block from LLM response
    append_history       — append a timestamped memory entry to history.md
    build_llm_data       — build current-week + history canonical data
    LLMResult            — dataclass holding response text and usage metadata

Example:
    ctx = load_context(CONTEXT_DIR)
    rendered = render_health_data(health_data, prompt_kind="report")
    msgs = build_messages(ctx, health_data_text=rendered)
    result = call_llm(msgs)
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import litellm

from aggregator import summarise
from config import (
    CHART_THEME,
    MAX_COACH_FEEDBACK_ENTRIES,
    MAX_HISTORY_ENTRIES,
    MAX_LOG_ENTRIES,
    PROMPTS_DIR,
)
from report import group_by_week, to_dict
from store import load_date_range, load_snapshots, log_llm_call

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "anthropic/claude-opus-4-6"
FALLBACK_MODEL = "anthropic/claude-sonnet-4-6"

# Exponential backoff delays (seconds) between retries on overloaded errors.
_RETRY_DELAYS = [10, 30, 90]

CONTEXT_FILES = ["me", "strategy", "log", "history", "coach_feedback"]

# Before this hour, yesterday's null sleep is marked "sync_pending" instead of
# "not_tracked" — the data likely hasn't synced from the watch yet.
SLEEP_SYNC_CUTOFF_HOUR = 10


@dataclass
class LLMResult:
    """Container for LLM response text and call metadata.

    Attributes:
        text: The LLM's response text.
        model: The model string used for the call.
        input_tokens: Number of input tokens reported by the API.
        output_tokens: Number of output tokens reported by the API.
        total_tokens: Total tokens (input + output).
        latency_s: Wall-clock time for the LLM call in seconds.
        cost: Actual cost in USD as reported by litellm, or None if unavailable.
    """

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_s: float
    cost: float | None = None
    tool_calls: list | None = None
    raw_message: dict | None = None
    """The assistant message dict suitable for appending back to the messages
    list in a tool-calling loop (includes ``tool_calls`` when present)."""
    llm_call_id: int | None = None
    """Database row id from ``llm_call`` table, set when the call is logged."""


DEFAULT_SOUL = (
    "You are a knowledgeable, no-nonsense health and fitness coach. "
    "You speak directly, use data to support your observations, "
    "and never pad your reports with filler. When something looks off "
    "you say so. When progress is real you acknowledge it briefly and move on. "
    "Use markdown formatting with headers, bullet points, and bold for key numbers."
)

SCHEMA_REFERENCE = """### Database schema (for run_sql)

**daily** — one row per calendar day, PK: `date` (YYYY-MM-DD)

- Activity: `steps`, `distance_km`, `active_energy_kj`, `exercise_min`, `stand_hours`, `flights_climbed`
- Cardiac: `resting_hr`, `hrv_ms`, `walking_hr_avg`, `hr_day_min`, `hr_day_max`, `vo2max`, `recovery_index`
- Mobility: `walking_speed_kmh`, `walking_step_length_cm`, `walking_asymmetry_pct`, `walking_double_support_pct`, `stair_speed_up_ms`, `stair_speed_down_ms`, `running_stride_length_m`, `running_power_w`, `running_speed_kmh`

**workout_all** — one row per session, FK: `date`, with `source` (`'import'` or `'manual'`)

- Identity: `type`, `category` (`run` / `lift` / `walk` / `cycle` / `other`)
- Core fields: `duration_min`, `hr_min`, `hr_avg`, `hr_max`, `active_energy_kj`, `intensity_kcal_per_hr_kg`
- Environment: `temperature_c`, `humidity_pct`
- GPX fields: `gpx_distance_km`, `gpx_elevation_gain_m`, `gpx_avg_speed_ms`, `gpx_max_speed_p95_ms`
- Pace tip: `duration_min / gpx_distance_km` = min/km when `gpx_distance_km IS NOT NULL`

**sleep_all** — one row per night, keyed by `date`, with `source` (`'import'` or `'manual'`)

- Columns: `sleep_total_h`, `sleep_in_bed_h`, `sleep_efficiency_pct`, `sleep_deep_h`, `sleep_core_h`, `sleep_rem_h`, `sleep_awake_h`
- Stored under **night-start date**
- Stage columns are NULL for manual entries"""


def _recent_history(content: str, n: int) -> str:
    """Return only the last n entries from history.md content.

    Args:
        content: Full history.md text with '## ' delimited entries.
        n: Number of most recent entries to keep.

    Returns:
        The last n entries joined as text, or the original if fewer than n.
    """
    parts = re.split(r"(?m)(?=^## )", content)
    entries = [p.strip() for p in parts if p.strip() and p.strip().startswith("## ")]
    if len(entries) <= n:
        return content
    return "\n\n".join(entries[-n:]) + "\n"


def load_context(
    context_dir: Path,
    prompt_file: str = "prompt",
    prompts_dir: Path = PROMPTS_DIR,
    *,
    max_history: int | None = None,
    max_log: int | None = None,
) -> dict[str, str]:
    """Read prompt templates and user context files.

    Prompt templates (prompt.md, nudge_prompt.md, chat_prompt.md, soul.md)
    are loaded from *prompts_dir* (shipped with the repo in ``src/prompts/``).
    User context files (me.md, strategy.md, log.md, history.md) are
    loaded from *context_dir*.

    Args:
        context_dir: Directory containing user context files.
        prompt_file: Stem of the prompt template file to load (default
            ``"prompt"``). Use ``"chat_prompt"`` for interactive chat
            or ``"nudge_prompt"`` for nudges.
        prompts_dir: Directory containing prompt template files.
        max_history: Override MAX_HISTORY_ENTRIES for this call.
        max_log: Override MAX_LOG_ENTRIES for this call.

    Returns:
        A dict mapping file stems (e.g. "soul", "prompt") to their
        text content, or "(not provided)" for missing optional files.
        The prompt template is always stored under the key ``"prompt"``
        regardless of which file was loaded.

    Raises:
        FileNotFoundError: If the prompt file is missing.
    """
    history_limit = max_history if max_history is not None else MAX_HISTORY_ENTRIES
    log_limit = max_log if max_log is not None else MAX_LOG_ENTRIES

    result: dict[str, str] = {}

    # Load prompt template from prompts_dir
    prompt_path = prompts_dir / f"{prompt_file}.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Required prompt template missing: {prompt_path}")
    result["prompt"] = prompt_path.read_text(encoding="utf-8")

    # Load soul.md from prompts_dir
    soul_path = prompts_dir / "soul.md"
    if soul_path.exists():
        result["soul"] = soul_path.read_text(encoding="utf-8")
        logger.debug("Loaded prompt: %s", soul_path)

    # Load user context files from context_dir
    for name in CONTEXT_FILES:
        path = context_dir / f"{name}.md"
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if name == "history":
                content = _recent_history(content, history_limit)
            elif name == "coach_feedback":
                content = _recent_history(content, MAX_COACH_FEEDBACK_ENTRIES)
            elif name == "log":
                content = _recent_history(content, log_limit)
            result[name] = content
            logger.debug("Loaded context: %s", path)
        else:
            logger.info("Optional context file not found: %s", path)
            result[name] = "(not provided)"

    return result


def build_messages(
    context: dict[str, str],
    health_data_text: str | None = None,
    *,
    health_data_json: str | None = None,
    baselines: str | None = None,
    week_complete: bool = True,
    today: date | None = None,
) -> list[dict[str, str]]:
    """Assemble system and user messages for the LLM call.

    The system message comes from soul.md (or a hardcoded fallback).
    The user message is the selected prompt template rendered with context
    file contents and a prompt-specific health-data markdown section.

    Args:
        context: Dict from load_context() with file stems as keys.
        health_data_text: Rendered health-data markdown for the prompt.
        health_data_json: Backward-compatible alias for ``health_data_text``.
        baselines: Auto-computed baselines markdown, or None to skip.
        week_complete: Whether the reported week has fully elapsed.
        today: Override for the current date (defaults to today).
            Useful for evals with pinned dates.

    Returns:
        A list of message dicts ready for litellm.completion().
    """
    if health_data_text is None:
        health_data_text = health_data_json or "(not provided)"

    system_content = context.get("soul", DEFAULT_SOUL)
    if system_content == "(not provided)":
        system_content = DEFAULT_SOUL

    if today is None:
        today = date.today()
    if week_complete:
        week_status = "This is a full week review (Mon–Sun complete)."
    else:
        weekday = today.strftime("%A")
        week_status = (
            f"This is a mid-week progress check (Mon–{weekday}). "
            "The week is not over — do not flag missing sessions for days "
            "that haven't happened yet."
        )

    template = context["prompt"]
    placeholders: dict[str, str] = defaultdict(lambda: "(not provided)")
    placeholders.update(
        {
            "me": context.get("me", "(not provided)"),
            "strategy": context.get("strategy", "(not provided)"),
            "log": context.get("log", "(not provided)"),
            "history": context.get("history", "(not provided)"),
            "coach_feedback": context.get("coach_feedback", "(not provided)"),
            "health_data": health_data_text,
            "baselines": baselines or "(not computed)",
            "review_facts": context.get("review_facts", "(not provided)"),
            "schema_reference": SCHEMA_REFERENCE,
            "today": today.isoformat(),
            "weekday": today.strftime("%A"),
            "week_status": week_status,
            "chart_theme": CHART_THEME,
        }
    )
    # Forward any extra keys (e.g. recent_nudges) from context into placeholders.
    for key, value in context.items():
        if key not in placeholders and key not in ("soul", "prompt"):
            placeholders[key] = value
    user_content = template.format_map(placeholders)

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


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
    """Format a metric with units or return None when unavailable."""
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

    if parts:
        return f"{name} ({'; '.join(parts)})"
    return name


def _render_day_block(day: dict) -> str:
    """Render one day card for the prompt."""
    lines = [f"#### {_format_day_label(str(day['date']))}"]

    activity_parts: list[str] = []
    steps = _format_metric(day.get("steps"), "steps", decimals=0)
    if steps:
        activity_parts.append(steps)
    exercise = _format_metric(day.get("exercise_min"), "exercise min", decimals=0)
    if exercise:
        activity_parts.append(exercise)
    distance = _format_metric(day.get("distance_km"), "km", decimals=1)
    if distance:
        activity_parts.append(distance)
    if activity_parts:
        lines.append(f"- Activity: {', '.join(activity_parts)}.")

    recovery_parts: list[str] = []
    hrv = _format_metric(day.get("hrv_ms"), "ms", decimals=1)
    if hrv:
        recovery_parts.append(f"HRV {hrv}")
    resting_hr = _format_metric(day.get("resting_hr"), "bpm", decimals=0)
    if resting_hr:
        recovery_parts.append(f"resting HR {resting_hr}")
    recovery_index = _format_decimal(day.get("recovery_index"), decimals=2)
    if recovery_index:
        recovery_parts.append(f"recovery index {recovery_index}")
    if recovery_parts:
        lines.append(f"- Recovery: {', '.join(recovery_parts)}.")
    else:
        lines.append("- Recovery: unavailable.")

    sleep_status = day.get("sleep_status")
    if sleep_status == "tracked":
        sleep_parts: list[str] = []
        sleep_total = _format_metric(day.get("sleep_total_h"), "h", decimals=1)
        if sleep_total:
            sleep_parts.append(sleep_total)
        efficiency = _format_percent(day.get("sleep_efficiency_pct"))
        if efficiency:
            sleep_parts.append(f"efficiency {efficiency}")
        stage_parts: list[str] = []
        for key, label in (
            ("sleep_deep_h", "deep"),
            ("sleep_core_h", "core"),
            ("sleep_rem_h", "REM"),
            ("sleep_awake_h", "awake"),
        ):
            value = _format_metric(day.get(key), "h", decimals=1)
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
    """Render the current target week's summary with semantic missing-data rules."""
    title = "### Target Week Summary"
    if prompt_kind == "chat":
        title = "### This Week So Far"
    lines = [title]

    week_label = summary.get("week_label")
    if week_label:
        lines.append(f"- Week: {week_label}.")

    run_count = int(summary.get("run_count", 0) or 0)
    lift_count = int(summary.get("lift_count", 0) or 0)
    walk_count = int(summary.get("walk_count", 0) or 0)
    if prompt_kind == "chat":
        training_line = (
            "- Logged so far: "
            f"{run_count} {_pluralize(run_count, 'run')}, "
            f"{lift_count} {_pluralize(lift_count, 'lift')}, "
            f"{walk_count} {_pluralize(walk_count, 'walk')}."
        )
    else:
        training_line = (
            "- Logged so far: "
            f"{run_count} {_pluralize(run_count, 'run')}, "
            f"{lift_count} {_pluralize(lift_count, 'lift')}, "
            f"{walk_count} {_pluralize(walk_count, 'walk')}."
        )
    lines.append(training_line)

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
    if total_lift and float(summary.get("total_lift_min", 0) or 0) > 0:
        lift_parts.append(total_lift)
    avg_lift_hr = _format_metric(summary.get("avg_lift_hr"), "bpm", decimals=0)
    if avg_lift_hr:
        lift_parts.append(f"avg lift HR {avg_lift_hr}")
    if lift_parts:
        lines.append(f"- Strength: {', '.join(lift_parts)}.")

    activity_parts: list[str] = []
    avg_steps = _format_metric(summary.get("avg_steps"), "steps/day", decimals=0)
    if avg_steps and int(summary.get("avg_steps", 0) or 0) > 0:
        activity_parts.append(avg_steps)
    avg_exercise = _format_metric(summary.get("avg_exercise_min"), "exercise min/day")
    if avg_exercise and float(summary.get("avg_exercise_min", 0) or 0) > 0:
        activity_parts.append(avg_exercise)
    avg_energy = _format_metric(summary.get("avg_active_energy_kj"), "kJ/day")
    if avg_energy and float(summary.get("avg_active_energy_kj", 0) or 0) > 0:
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
    if total_run and float(summary.get("total_run_km", 0) or 0) > 0:
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


def _select_day_blocks(
    days: list[dict],
    *,
    prompt_kind: str,
    week: str,
    today: date,
) -> tuple[list[dict], list[dict]]:
    """Choose current and recent day blocks for prompt rendering."""
    if not days:
        return ([], [])

    if prompt_kind == "nudge":
        today_iso = today.isoformat()
        today_blocks = [day for day in days if day.get("date") == today_iso]
        if today_blocks:
            recent = [day for day in days if day.get("date") != today_iso][-2:]
        else:
            today_blocks = []
            recent = days[-3:]
        return (today_blocks, recent)
    if prompt_kind == "chat":
        return (days, [])

    # Report/coach views track the requested target week.
    if week in {"current", "last"}:
        return (days, [])
    return (days, [])


def render_health_data(
    health_data: dict,
    *,
    prompt_kind: str,
    week: str = "current",
    today: date | None = None,
) -> str:
    """Render canonical health data into compact markdown for LLM prompts."""
    if today is None:
        today = date.today()

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

    primary_days, secondary_days = _select_day_blocks(
        days,
        prompt_kind=prompt_kind,
        week=week,
        today=today,
    )
    if prompt_kind == "nudge":
        if primary_days:
            sections.append(
                "### Today\n\n"
                + "\n\n".join(_render_day_block(day) for day in primary_days)
            )
        if secondary_days:
            sections.append(
                "### Recent Days\n\n"
                + "\n\n".join(_render_day_block(day) for day in secondary_days)
            )
    elif prompt_kind == "chat":
        chat_days = primary_days + secondary_days
        if chat_days:
            sections.append(
                "### This Week Days (Mon to today)\n\n"
                + "\n\n".join(_render_day_block(day) for day in chat_days)
            )
    elif primary_days:
        title = "### Target Week Days"
        if week == "current":
            title = "### Target Week Days (Mon to today)"
        elif week == "last":
            title = "### Target Week Days (Mon to Sun)"
        sections.append(
            title + "\n\n" + "\n\n".join(_render_day_block(day) for day in primary_days)
        )

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


def _recovery_verdict(summary: dict, previous_summary: dict | None) -> str:
    """Derive a compact recovery verdict from weekly summary fields."""
    recovery = summary.get("avg_recovery_index")
    sleep_h = summary.get("avg_sleep_total_h")
    sleep_eff = summary.get("avg_sleep_efficiency_pct")
    hrv_trend = summary.get("hrv_trend")
    resting_hr = summary.get("avg_resting_hr")
    prev_resting_hr = (
        previous_summary.get("avg_resting_hr") if previous_summary else None
    )

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
) -> str:
    """Build a compact shared summary for insights and coach prompts."""
    summary = health_data.get("current_week", {}).get("summary") or {}
    history = health_data.get("history", [])
    previous_summary = history[-1].get("summary") if history else None
    week_label = summary.get("week_label") or health_data.get("week_label") or "unknown"

    verdict = _recovery_verdict(summary, previous_summary)
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


def context_update_tool(
    allowed_files: list[str] | None = None,
) -> list[dict]:
    """Tool definition for context file updates.

    Args:
        allowed_files: Restrict which files the LLM can target.
            Defaults to all editable files.

    Returns:
        A list with a single tool definition dict for litellm.
    """
    files = allowed_files or ["me", "strategy", "log"]
    return [
        {
            "type": "function",
            "function": {
                "name": "update_context",
                "description": "Update a user context file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "enum": files,
                            "description": "Which context file to update.",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["append", "replace_section"],
                            "description": "append: add to end of file. replace_section: replace a ## heading section.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Exact markdown to write.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "One-sentence description of the change.",
                        },
                        "section": {
                            "type": "string",
                            "description": "The exact ## heading to replace. Required when action is replace_section.",
                        },
                    },
                    "required": ["file", "action", "content", "summary"],
                },
            },
        }
    ]


def _is_overloaded(exc: Exception) -> bool:
    """Return True if *exc* is an Anthropic overloaded error."""
    return "overloaded_error" in str(exc) or "Overloaded" in str(exc)


def _call_with_retry(
    kwargs: dict,
    model: str,
) -> tuple:
    """Call litellm.completion with retries and model fallback.

    Retries on overloaded errors using exponential backoff.  After exhausting
    retries on the primary model, switches to FALLBACK_MODEL and retries once
    more.  Re-raises the last exception if all attempts fail.

    Args:
        kwargs: litellm.completion keyword arguments (may be mutated for fallback).
        model: Primary model string.

    Returns:
        A (response, effective_model) tuple.
    """
    for attempt, delay in enumerate(_RETRY_DELAYS + [None]):
        try:
            response = litellm.completion(**{**kwargs, "model": model})
            return response, model
        except Exception as exc:
            if not _is_overloaded(exc):
                raise
            if delay is not None:
                logger.warning(
                    "Anthropic overloaded (attempt %d/%d), retrying in %ds ...",
                    attempt + 1,
                    len(_RETRY_DELAYS),
                    delay,
                )
                time.sleep(delay)
            else:
                logger.warning(
                    "All retries exhausted on %s, switching to fallback %s",
                    model,
                    FALLBACK_MODEL,
                )

    # Fallback model — same retry schedule.
    model = FALLBACK_MODEL
    last_exc: Exception | None = None
    for attempt, delay in enumerate(_RETRY_DELAYS + [None]):
        try:
            response = litellm.completion(**{**kwargs, "model": model})
            logger.info("Fallback model %s succeeded", model)
            return response, model
        except Exception as exc:
            if not _is_overloaded(exc):
                raise
            last_exc = exc
            if delay is not None:
                logger.warning(
                    "Fallback %s also overloaded (attempt %d/%d), retrying in %ds ...",
                    model,
                    attempt + 1,
                    len(_RETRY_DELAYS),
                    delay,
                )
                time.sleep(delay)

    raise last_exc  # type: ignore[misc]


def call_llm(
    messages: list[dict[str, str]],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    reasoning_effort: str | None = None,
    tools: list[dict] | None = None,
    conn: sqlite3.Connection | None = None,
    request_type: str = "",
    metadata: dict | None = None,
) -> LLMResult:
    """Call the LLM via litellm and return the response with metadata.

    All calls are logged to the database when *conn* and *request_type* are
    provided. A logging failure is never propagated — it is logged as a
    warning and the result is returned normally.

    Args:
        messages: System + user messages for the LLM.
        model: litellm model string.
        max_tokens: Maximum tokens in the response.
        temperature: Sampling temperature.
        reasoning_effort: Optional reasoning effort hint (model-dependent).
        tools: Optional list of tool definitions for function calling.
        conn: Open DB connection for logging. None to skip logging.
        request_type: Product-level call type, e.g. "insights" or "nudge".
        metadata: Product context dict stored alongside the call.

    Returns:
        An LLMResult containing the response text and usage metadata.

    Raises:
        litellm.AuthenticationError: If the API key is missing or invalid.
        litellm.APIError: On network or API failures.
    """
    # Anthropic's extended thinking requires temperature=1; any other value
    # is rejected with a BadRequestError. Force it here so callers can keep
    # passing their preferred sampling temperature without having to know
    # about this constraint.
    effective_temperature = 1.0 if reasoning_effort is not None else temperature

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": effective_temperature,
    }
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    if tools is not None:
        kwargs["tools"] = tools

    t0 = time.perf_counter()
    response, model = _call_with_retry(kwargs, model)
    latency = time.perf_counter() - t0
    usage = response.usage

    try:
        cost = litellm.completion_cost(completion_response=response)
    except Exception:
        cost = None

    message = response.choices[0].message
    raw_tool_calls = getattr(message, "tool_calls", None)

    # Build a raw message dict for tool-calling loops.
    raw_msg: dict = {"role": "assistant", "content": message.content or ""}
    if raw_tool_calls:
        raw_msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in raw_tool_calls
        ]

    result = LLMResult(
        text=message.content or "",
        model=model,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        latency_s=latency,
        cost=cost,
        tool_calls=raw_tool_calls if raw_tool_calls else None,
        raw_message=raw_msg,
    )

    if conn and request_type:
        params = {"max_tokens": max_tokens, "temperature": effective_temperature}
        if reasoning_effort is not None:
            params["reasoning_effort"] = reasoning_effort
        try:
            row_id = log_llm_call(
                conn,
                request_type=request_type,
                model=model,
                messages=messages,
                response_text=result.text,
                params=params,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=result.total_tokens,
                latency_s=result.latency_s,
                cost=result.cost,
                metadata=metadata,
            )
            result.llm_call_id = row_id
        except Exception:
            logger.warning("Failed to log LLM call to DB", exc_info=True)

    return result


def extract_memory(response: str) -> str | None:
    """Extract the <memory> block from the LLM response.

    Args:
        response: Full LLM response text.

    Returns:
        The memory content (stripped), or None if no block found.
    """
    match = re.search(r"<memory>(.*?)</memory>", response, re.DOTALL)
    return match.group(1).strip() if match else None


def append_history(
    context_dir: Path, memory_block: str, week_label: str | None = None
) -> None:
    """Append a memory entry to history.md, keyed by ISO week label.

    Splits the file on '## ' headings, appends the new entry, and trims
    to the most recent MAX_HISTORY_ENTRIES entries so the file stays bounded.

    Args:
        context_dir: Directory containing history.md.
        memory_block: Text to append as this week's memory.
        week_label: ISO week label (e.g. "2026-W12") used as the entry
            heading.  Falls back to today's date if not provided.
    """
    history_path = context_dir / "history.md"
    heading = week_label or date.today().isoformat()
    new_entry = f"## {heading}\n\n{memory_block}"

    if history_path.exists():
        content = history_path.read_text(encoding="utf-8")
    else:
        content = ""

    # Split into entries on '## ' at start of line
    parts = re.split(r"(?m)(?=^## )", content)
    # Filter out empty/whitespace-only parts (e.g. preamble before first heading)
    entries = [p.strip() for p in parts if p.strip() and p.strip().startswith("## ")]
    # Replace existing entry for the same week instead of duplicating
    replaced = False
    for i, entry in enumerate(entries):
        if entry.startswith(f"## {heading}"):
            entries[i] = new_entry
            replaced = True
            logger.info("Replaced existing %s entry in %s", heading, history_path)
            break
    if not replaced:
        entries.append(new_entry)

    history_path.write_text("\n\n".join(entries) + "\n", encoding="utf-8")
    logger.info("History %s now has %d entries", history_path, len(entries))


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


def build_llm_data(
    conn: sqlite3.Connection, months: int, week: str = "current"
) -> dict:
    """Build a compact summary + history JSON for LLM consumption.

    The output keeps a canonical summary + day-level view: pre-computed
    compliance stats, a today snapshot, target-week days, and weekly
    history summaries. Prompt rendering may choose to show only a subset of
    that data, while charts can still consume the full day arrays.

    Args:
        conn: Open SQLite database connection.
        months: Number of months of history to include.
        week: Which week to report on — "current" for the ISO week containing
              today, "last" for the previous full Mon–Sun week.

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

    all_days = [to_dict(s) for s in current_snaps]

    # --- Sleep shift ---
    # Apple Health stores sleep under the night-start date, but for the LLM
    # each day's sleep should be "the night before this day" — the sleep that
    # affected this day's recovery.  We fetched one extra day before the week
    # to supply Monday's sleep.
    for i in range(len(all_days) - 1, 0, -1):
        prev_day, cur_day = all_days[i - 1], all_days[i]
        for k in _SLEEP_KEYS:
            cur_day[k] = prev_day.get(k)
            prev_day.pop(k, None)
    # Drop the extra pre-week day.
    if all_days and all_days[0].get("date", "") < week_start:
        all_days = all_days[1:]
    days = all_days

    # --- Classify sleep status per day and compute compliance ---
    today_iso = today.isoformat()
    yesterday_iso = (today - timedelta(days=1)).isoformat()
    before_sync_cutoff = datetime.now().hour < SLEEP_SYNC_CUTOFF_HOUR

    sleep_tracked = 0
    sleep_total_eligible = 0
    not_tracked_dates: list[str] = []

    for day in days:
        if not isinstance(day, dict):
            continue
        day_date = day.get("date")
        has_sleep = any(day.get(k) is not None for k in _SLEEP_KEYS)

        if has_sleep:
            day["sleep_status"] = "tracked"
            sleep_tracked += 1
            sleep_total_eligible += 1
        elif day_date == today_iso:
            day["sleep_status"] = "pending"
            # Don't count today against compliance — data may not have synced.
        elif day_date == yesterday_iso and before_sync_cutoff:
            day["sleep_status"] = "pending"
            # Before sync cutoff, yesterday is also pending.
        else:
            day["sleep_status"] = "not_tracked"
            sleep_total_eligible += 1
            not_tracked_dates.append(day_date or "")

    # --- Build the today snapshot ---
    today_snapshot = _build_today_snapshot(days, today_iso)

    # --- Assemble the summary ---
    week_snaps = [s for s in current_snaps if s.date >= week_start]
    summary = to_dict(summarise(week_snaps)) if week_snaps else None

    if summary:
        # Inject pre-computed compliance fields.
        summary["sleep_nights_tracked"] = sleep_tracked
        summary["sleep_nights_total"] = sleep_total_eligible
        summary["sleep_not_tracked_dates"] = not_tracked_dates
        if today_snapshot:
            summary["today"] = today_snapshot

    return {
        "current_week": {
            "summary": summary,
            # Per-day data is kept for chart rendering and prompt-specific
            # markdown rendering.
            "days": days,
        },
        "history": [{"summary": to_dict(summarise(w))} for w in history_weeks],
        "week_complete": today > date.fromisoformat(week_end),
        "week_label": week_label,
    }


def slim_for_prompt(health_data: dict) -> dict:
    """Return a copy of health_data with per-day arrays stripped.

    The compact version contains only weekly summaries, pre-computed
    compliance stats, and the today snapshot. It is still useful for
    charts, debugging, or tests that want a summary-only view.

    The original dict (with ``days``) should still be passed to
    ``render_chart()`` so chart code can access daily values.
    """
    import copy

    slim = copy.deepcopy(health_data)
    cw = slim.get("current_week")
    if isinstance(cw, dict):
        cw.pop("days", None)
    return slim


def _build_today_snapshot(days: list[dict], today_iso: str) -> dict | None:
    """Extract a compact snapshot for today from the shifted day list.

    Returns a dict with key vitals and minimal workout info, or None if
    today is not in the data.
    """
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
    # Include sleep metrics only if tracked.
    if snapshot["sleep_status"] == "tracked":
        snapshot["sleep_total_h"] = today_day.get("sleep_total_h")
        snapshot["sleep_efficiency_pct"] = today_day.get("sleep_efficiency_pct")

    if workouts:
        snapshot["workouts"] = workouts

    return snapshot
