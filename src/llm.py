"""LLM integration for personalized health insights.

Loads user context files, assembles prompts, calls an LLM via litellm,
and manages the memory/history feedback loop.

Public API:
    load_context    — read markdown context files from a directory (prompt_file selects template)
    build_messages  — assemble system + user messages for the LLM
    call_llm        — call litellm and return an LLMResult with text + metadata
    extract_memory  — pull <memory> block from LLM response
    append_history  — append a timestamped memory entry to history.md
    build_llm_data  — build current-week + history JSON for LLM consumption
    LLMResult       — dataclass holding response text and usage metadata

Example:
    ctx = load_context(CONTEXT_DIR)
    msgs = build_messages(ctx, health_data_json="...")
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
from config import CHART_THEME, MAX_HISTORY_ENTRIES, MAX_LOG_ENTRIES, PROMPTS_DIR
from report import group_by_week, to_dict
from store import load_date_range, load_snapshots, log_llm_call

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "anthropic/claude-opus-4-6"
FALLBACK_MODEL = "anthropic/claude-sonnet-4-6"

# Exponential backoff delays (seconds) between retries on overloaded errors.
_RETRY_DELAYS = [10, 30, 90]

CONTEXT_FILES = ["me", "goals", "plan", "log", "history"]

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


DEFAULT_SOUL = (
    "You are a knowledgeable, no-nonsense health and fitness coach. "
    "You speak directly, use data to support your observations, "
    "and never pad your reports with filler. When something looks off "
    "you say so. When progress is real you acknowledge it briefly and move on. "
    "Use markdown formatting with headers, bullet points, and bold for key numbers."
)


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
) -> dict[str, str]:
    """Read prompt templates and user context files.

    Prompt templates (prompt.md, nudge_prompt.md, chat_prompt.md, soul.md)
    are loaded from *prompts_dir* (shipped with the repo in ``src/prompts/``).
    User context files (me.md, goals.md, plan.md, log.md, history.md) are
    loaded from *context_dir*.

    Args:
        context_dir: Directory containing user context files.
        prompt_file: Stem of the prompt template file to load (default
            ``"prompt"``). Use ``"chat_prompt"`` for interactive chat
            or ``"nudge_prompt"`` for nudges.
        prompts_dir: Directory containing prompt template files.

    Returns:
        A dict mapping file stems (e.g. "soul", "prompt") to their
        text content, or "(not provided)" for missing optional files.
        The prompt template is always stored under the key ``"prompt"``
        regardless of which file was loaded.

    Raises:
        FileNotFoundError: If the prompt file is missing.
    """
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
                content = _recent_history(content, MAX_HISTORY_ENTRIES)
            elif name == "log":
                content = _recent_history(content, MAX_LOG_ENTRIES)
            result[name] = content
            logger.debug("Loaded context: %s", path)
        else:
            logger.info("Optional context file not found: %s", path)
            result[name] = "(not provided)"

    return result


def build_messages(
    context: dict[str, str],
    health_data_json: str,
    baselines: str | None = None,
    week_complete: bool = True,
    today: date | None = None,
) -> list[dict[str, str]]:
    """Assemble system and user messages for the LLM call.

    The system message comes from soul.md (or a hardcoded fallback).
    The user message is the prompt.md template rendered with context
    file contents and health data JSON.

    Args:
        context: Dict from load_context() with file stems as keys.
        health_data_json: JSON string of current week + history data.
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
    placeholders.update({
        "me": context.get("me", "(not provided)"),
        "goals": context.get("goals", "(not provided)"),
        "plan": context.get("plan", "(not provided)"),
        "log": context.get("log", "(not provided)"),
        "history": context.get("history", "(not provided)"),
        "health_data": health_data_json,
        "baselines": baselines or "(not computed)",
        "today": today.isoformat(),
        "weekday": today.strftime("%A"),
        "week_status": week_status,
        "chart_theme": CHART_THEME,
    })
    # Forward any extra keys (e.g. recent_nudges) from context into placeholders.
    for key, value in context.items():
        if key not in placeholders and key not in ("soul", "prompt"):
            placeholders[key] = value
    user_content = template.format_map(placeholders)

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def context_update_tool() -> list[dict]:
    """Tool definition for context file updates, used in chat calls.

    Returns:
        A list with a single tool definition dict for litellm.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "update_context",
                "description": "Update a user context file (me, goals, plan, or log).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "enum": ["me", "goals", "plan", "log"],
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
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
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
        params = {"max_tokens": max_tokens, "temperature": temperature}
        if reasoning_effort is not None:
            params["reasoning_effort"] = reasoning_effort
        try:
            log_llm_call(
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


def build_llm_data(
    conn: sqlite3.Connection, months: int, week: str = "current"
) -> dict:
    """Build the combined current-week + history JSON structure for LLM consumption.

    Args:
        conn: Open SQLite database connection.
        months: Number of months of history to include.
        week: Which week to report on — "current" for the ISO week containing
              today, "last" for the previous full Mon–Sun week.

    Returns:
        A dict with 'current_week', 'history', and 'week_complete' keys,
        JSON-serialisable.  Returns empty structure if the database has no data.
    """
    dr = load_date_range(conn)
    if dr is None:
        return {
            "current_week": {"summary": None, "days": []},
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

    # Shift sleep forward by one day.  Apple Health stores sleep under the
    # night-start date, but for the LLM each day's sleep should be "the night
    # before this day" — the sleep that affected this day's recovery.  We
    # fetched one extra day before the week to supply Monday's sleep.
    _SLEEP_KEYS = {
        "sleep_total_h",
        "sleep_in_bed_h",
        "sleep_efficiency_pct",
        "sleep_deep_h",
        "sleep_core_h",
        "sleep_rem_h",
        "sleep_awake_h",
    }
    for i in range(len(all_days) - 1, 0, -1):
        prev_day, cur_day = all_days[i - 1], all_days[i]
        # Move previous day's sleep into current day.
        for k in _SLEEP_KEYS:
            cur_day[k] = prev_day.get(k)
            prev_day.pop(k, None)
    # First day in all_days is the extra pre-week day — strip its remaining
    # sleep (already moved forward) and then drop it from the output.
    if all_days and all_days[0].get("date", "") < week_start:
        for k in _SLEEP_KEYS:
            all_days[0].pop(k, None)
        all_days = all_days[1:]
    days = all_days

    # Mark days with no sleep data.  After the shift, null sleep means the
    # watch wasn't worn the night before — except for today (last night may
    # not have synced yet) and yesterday before the sync cutoff.
    today_iso = date.today().isoformat()
    yesterday_iso = (date.today() - timedelta(days=1)).isoformat()
    before_sync_cutoff = datetime.now().hour < SLEEP_SYNC_CUTOFF_HOUR
    for day in days:
        if isinstance(day, dict) and all(day.get(k) is None for k in _SLEEP_KEYS):
            for k in _SLEEP_KEYS:
                day.pop(k, None)

            day_date = day.get("date")
            if day_date == today_iso:
                pass  # last night may not have synced yet
            elif day_date == yesterday_iso and before_sync_cutoff:
                pass  # watch data likely hasn't synced yet
            else:
                day["sleep"] = "not_tracked"

    # Exclude the extra pre-week day from the summary.
    week_snaps = [s for s in current_snaps if s.date >= week_start]

    return {
        "current_week": {
            "summary": to_dict(summarise(week_snaps)) if week_snaps else None,
            "days": days,
        },
        "history": [{"summary": to_dict(summarise(w))} for w in history_weeks],
        "week_complete": today > date.fromisoformat(week_end),
        "week_label": week_label,
    }
