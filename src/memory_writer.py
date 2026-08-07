"""Weekly memory extraction as a call of its own.

Memory used to be a `<memory>` section the report writer produced alongside the
report. That coupled the two: the writer had to hold the report's contract and
the memory contract at once, and the memory rules were a fifth of the insights
prompt. It also made the block hard to evaluate — a case could only fail memory
by running a full report first.

Splitting it out buys three things: the insights prompt gets shorter, the block
runs on a cheap model because summarising a finished 1024-character report is a
small job, and `memory` becomes an eval feature that can be scored on its own.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from config import MAX_TOKENS_MEMORY, PROMPTS_DIR
from llm import call_llm, extract_memory

logger = logging.getLogger(__name__)

MEMORY_PROMPT = "memory_prompt.md"


def build_memory_messages(
    *,
    report: str,
    week_label: str | None,
    review_facts: str | None,
    log: str | None,
    history: str | None,
    prompts_dir: Path = PROMPTS_DIR,
) -> list[dict[str, str]]:
    """Render the memory prompt into a single-message payload.

    There is no system/user split and no soul: this call makes no judgement
    about the person, it decides what to carry forward from text already
    written and approved.

    Args:
        report: The final visible report, post-verification.
        week_label: ISO week label the report covers, e.g. "2026-W31".
        review_facts: Shared review facts handed to the report writer.
        log: Recent user notes.
        history: Existing memory entries, so the call can avoid repeating one.
        prompts_dir: Directory holding the prompt file.

    Returns:
        Messages ready for ``call_llm``.
    """
    template = (prompts_dir / MEMORY_PROMPT).read_text(encoding="utf-8")
    content = template.format(
        report=report.strip() or "(empty)",
        week_label=week_label or "this week",
        review_facts=review_facts or "(not provided)",
        log=log or "(not provided)",
        history=history or "(nothing yet)",
    )
    return [{"role": "user", "content": content}]


def write_memory(
    *,
    report: str,
    week_label: str | None,
    review_facts: str | None,
    log: str | None,
    history: str | None,
    conn: sqlite3.Connection | None = None,
    trace_id: int | None = None,
    model_prefs_path: Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """Extract this week's memory block from the finished report.

    Failure here is deliberately non-fatal. The report has already been saved
    and sent by the time this runs, so an exception must not take it down —
    the cost of a missing memory entry is one week of thinner continuity.

    Args:
        report: The final visible report, post-verification.
        week_label: ISO week label the report covers.
        review_facts: Shared review facts handed to the report writer.
        log: Recent user notes.
        history: Existing memory entries.
        conn: Open database connection for call logging.
        trace_id: Trace to attach this call to, so it groups with the report.
        model_prefs_path: Profile model preferences.
        metadata: Extra metadata recorded on the logged call.

    Returns:
        The memory block's contents, or None when there is nothing to carry
        forward or the call failed.
    """
    from model_prefs import resolve_model_route

    messages = build_memory_messages(
        report=report,
        week_label=week_label,
        review_facts=review_facts,
        log=log,
        history=history,
    )

    try:
        route = resolve_model_route("memory", path=model_prefs_path).call_kwargs()
        result = call_llm(
            messages,
            **route,
            max_tokens=MAX_TOKENS_MEMORY,
            conn=conn,
            request_type="memory",
            trace_id=trace_id,
            metadata=metadata,
        )
    except Exception as e:  # noqa: BLE001 - the report is already out the door
        logger.error("Memory extraction failed: %s", e)
        return None

    memory = extract_memory(result.text)
    if not memory:
        logger.info("Memory call returned no entries; history.md unchanged")
        return None
    return memory
