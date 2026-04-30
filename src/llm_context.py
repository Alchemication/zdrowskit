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

DEFAULT_SOUL_PROMPT = "default_soul.md"
SCHEMA_REFERENCE_PROMPT = "schema_reference.md"
WEEK_STATUS_FULL_PROMPT = "week_status_full.md"
WEEK_STATUS_PARTIAL_PROMPT = "week_status_partial.md"


def load_prompt_text(
    prompt_name: str,
    prompts_dir: Path = PROMPTS_DIR,
) -> str:
    """Read a prompt text file from ``src/prompts``.

    Args:
        prompt_name: Prompt filename or stem. Stems get a ``.md`` suffix.
        prompts_dir: Directory containing prompt files.

    Returns:
        Prompt file contents.

    Raises:
        FileNotFoundError: If the prompt file is missing.
    """
    filename = prompt_name if prompt_name.endswith(".md") else f"{prompt_name}.md"
    path = prompts_dir / filename
    if not path.exists():
        raise FileNotFoundError(f"Required prompt template missing: {path}")
    return path.read_text(encoding="utf-8")


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
    prompt_file: str = "insights_prompt",
    prompts_dir: Path = PROMPTS_DIR,
    *,
    max_history: int | None = None,
    max_log: int | None = None,
) -> dict[str, str]:
    """Read prompt templates and user context files.

    Prompt templates (insights_prompt.md, nudge_prompt.md, chat_prompt.md,
    soul.md) are loaded from *prompts_dir* (shipped with the repo in
    ``src/prompts/``).
    User context files (me.md, strategy.md, log.md, history.md) are
    loaded from *context_dir*.

    Args:
        context_dir: Directory containing user context files.
        prompt_file: Stem of the prompt template file to load (default
            ``"insights_prompt"``). Use ``"chat_prompt"`` for interactive
            chat or ``"nudge_prompt"`` for nudges.
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
    result["prompt"] = load_prompt_text(prompt_file, prompts_dir)

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
    milestones: str | None = None,
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
        milestones: Auto-computed milestone markdown, or None to skip.
        week_complete: Whether the reported week has fully elapsed.
        today: Override for the current date (defaults to today).
            Useful for evals with pinned dates.

    Returns:
        A list of message dicts ready for litellm.completion().
    """
    system_content = context.get("soul")
    if system_content == "(not provided)":
        system_content = None
    if system_content is None:
        system_content = load_prompt_text(DEFAULT_SOUL_PROMPT)

    if today is None:
        today = date.today()
    if week_complete:
        week_status = load_prompt_text(WEEK_STATUS_FULL_PROMPT)
    else:
        weekday = today.strftime("%A")
        week_status = load_prompt_text(WEEK_STATUS_PARTIAL_PROMPT).format(
            weekday=weekday
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
            "milestones": milestones or "(not computed)",
            "review_facts": context.get("review_facts", "(not provided)"),
            "schema_reference": load_prompt_text(SCHEMA_REFERENCE_PROMPT),
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
