"""Context loading, prompt assembly, and history management for LLM calls.

Reads user context files and prompt templates, assembles system + user
messages, manages the history feedback loop, and defines the context-update
tool schema.

Extracted from llm.py to separate app-domain context wiring from LLM
call infrastructure.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from config import (
    CHART_THEME,
    MAX_COACH_FEEDBACK_ENTRIES,
    MAX_HISTORY_ENTRIES,
    MAX_LOG_ENTRIES,
    PROMPTS_DIR,
)

logger = logging.getLogger(__name__)

CONTEXT_FILES = ["me", "strategy", "log", "history", "coach_feedback"]

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
- Note: `daily.running_speed_kmh` is a day-level Apple mobility metric. It is useful for broad daily movement context, but for run-session pace, distance, elevation, or running trends, prefer `workout_all`.

**workout_all** — one row per session, FK: `date`, with `source` (`'import'` or `'manual'`)

- Identity: `type`, `category` (`run` / `lift` / `walk` / `cycle` / `other`)
- Core fields: `duration_min`, `hr_min`, `hr_avg`, `hr_max`, `active_energy_kj`, `intensity_kcal_per_hr_kg`
- Environment: `temperature_c`, `humidity_pct`
- GPX fields: `gpx_distance_km`, `gpx_elevation_gain_m`, `gpx_avg_speed_ms`, `gpx_max_speed_p95_ms`
- Pace tip: `duration_min / gpx_distance_km` = min/km when `gpx_distance_km IS NOT NULL`
- Use `workout_all` as the canonical source for workout questions: runs, pace, splits/proxies, distance, elevation, workout HR, and session trends.

**sleep_all** — one row per night, keyed by `date`, with `source` (`'import'` or `'manual'`)

- Columns: `sleep_total_h`, `sleep_in_bed_h`, `sleep_efficiency_pct`, `sleep_deep_h`, `sleep_core_h`, `sleep_rem_h`, `sleep_awake_h`
- Stored under **night-start date**
- Stage columns are NULL for manual entries"""


def _recent_history(content: str, n: int) -> str:
    """Return only the last n entries from history.md content.

    Args:
        content: Full history.md text with '## ' delimited entries.
        n: Number of most recent entries to keep. ``0`` or negative means
            "do not trim".

    Returns:
        The last n entries joined as text, or the original if fewer than n.
    """
    if n <= 0:
        return content

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
    health_data_text: str,
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
        baselines: Auto-computed baselines markdown, or None to skip.
        week_complete: Whether the reported week has fully elapsed.
        today: Override for the current date (defaults to today).
            Useful for evals with pinned dates.

    Returns:
        A list of message dicts ready for litellm.completion().
    """
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
