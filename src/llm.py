"""LLM integration for personalized health insights.

Loads user context files, assembles prompts, calls an LLM via litellm,
and manages the memory/history feedback loop.

Public API:
    load_context    — read markdown context files from a directory
    build_messages  — assemble system + user messages for the LLM
    generate_report — call litellm and return a ReportResult with text + metadata
    extract_memory  — pull <memory> block from LLM response
    append_history  — append a timestamped memory entry to history.md
    ReportResult    — dataclass holding response text and usage metadata

Example:
    ctx = load_context(Path("~/Documents/zdrowskit/ContextFiles"))
    msgs = build_messages(ctx, health_data_json="...")
    report = generate_report(msgs)
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import litellm

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "anthropic/claude-haiku-4-5-20251001"

CONTEXT_FILES = ["soul", "me", "goals", "plan", "log", "history"]
REQUIRED_FILES = ["prompt"]


@dataclass
class ReportResult:
    """Container for LLM response text and call metadata.

    Attributes:
        text: The LLM's response text.
        model: The model string used for the call.
        input_tokens: Number of input tokens reported by the API.
        output_tokens: Number of output tokens reported by the API.
        total_tokens: Total tokens (input + output).
        latency_s: Wall-clock time for the LLM call in seconds.
    """

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_s: float


DEFAULT_SOUL = (
    "You are a knowledgeable, no-nonsense health and fitness coach. "
    "You speak directly, use data to support your observations, "
    "and never pad your reports with filler. When something looks off "
    "you say so. When progress is real you acknowledge it briefly and move on. "
    "Use markdown formatting with headers, bullet points, and bold for key numbers."
)


def load_context(context_dir: Path) -> dict[str, str]:
    """Read all markdown context files from the given directory.

    Args:
        context_dir: Directory containing soul.md, me.md, goals.md,
            plan.md, log.md, history.md, and prompt.md.

    Returns:
        A dict mapping file stems (e.g. "soul", "prompt") to their
        text content, or "(not provided)" for missing optional files.

    Raises:
        FileNotFoundError: If prompt.md is missing.
    """
    result: dict[str, str] = {}

    for name in REQUIRED_FILES:
        path = context_dir / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(
                f"Required context file missing: {path}\n"
                f"Copy examples/context/{name}.md to {context_dir}/ to get started."
            )
        result[name] = path.read_text(encoding="utf-8")

    for name in CONTEXT_FILES:
        path = context_dir / f"{name}.md"
        if path.exists():
            result[name] = path.read_text(encoding="utf-8")
            logger.debug("Loaded context: %s", path)
        else:
            logger.info("Optional context file not found: %s", path)
            result[name] = "(not provided)"

    return result


def build_messages(
    context: dict[str, str],
    health_data_json: str,
    baselines: str | None = None,
) -> list[dict[str, str]]:
    """Assemble system and user messages for the LLM call.

    The system message comes from soul.md (or a hardcoded fallback).
    The user message is the prompt.md template rendered with context
    file contents and health data JSON.

    Args:
        context: Dict from load_context() with file stems as keys.
        health_data_json: JSON string of current week + history data.
        baselines: Auto-computed baselines markdown, or None to skip.

    Returns:
        A list of message dicts ready for litellm.completion().
    """
    system_content = context.get("soul", DEFAULT_SOUL)
    if system_content == "(not provided)":
        system_content = DEFAULT_SOUL

    template = context["prompt"]
    placeholders: dict[str, str] = defaultdict(lambda: "(not provided)")
    placeholders.update(
        {
            "me": context.get("me", "(not provided)"),
            "goals": context.get("goals", "(not provided)"),
            "plan": context.get("plan", "(not provided)"),
            "log": context.get("log", "(not provided)"),
            "history": context.get("history", "(not provided)"),
            "health_data": health_data_json,
            "baselines": baselines or "(not computed)",
            "today": date.today().isoformat(),
            "weekday": date.today().strftime("%A"),
        }
    )
    user_content = template.format_map(placeholders)

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def generate_report(
    messages: list[dict[str, str]], model: str = DEFAULT_MODEL
) -> ReportResult:
    """Call the LLM via litellm and return the response with metadata.

    Args:
        messages: System + user messages for the LLM.
        model: litellm model string (default: claude-haiku-4-5).

    Returns:
        A ReportResult containing the response text and usage metadata.

    Raises:
        litellm.AuthenticationError: If the API key is missing or invalid.
        litellm.APIError: On network or API failures.
    """
    t0 = time.perf_counter()
    response = litellm.completion(
        model=model,
        messages=messages,
        max_tokens=4096,
        temperature=0.7,
    )
    latency = time.perf_counter() - t0
    usage = response.usage
    return ReportResult(
        text=response.choices[0].message.content,
        model=model,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        latency_s=latency,
    )


def extract_memory(response: str) -> str | None:
    """Extract the <memory> block from the LLM response.

    Args:
        response: Full LLM response text.

    Returns:
        The memory content (stripped), or None if no block found.
    """
    match = re.search(r"<memory>(.*?)</memory>", response, re.DOTALL)
    return match.group(1).strip() if match else None


def append_history(context_dir: Path, memory_block: str) -> None:
    """Append a timestamped memory entry to history.md.

    Args:
        context_dir: Directory containing history.md.
        memory_block: Text to append as this week's memory.
    """
    history_path = context_dir / "history.md"
    today = date.today().isoformat()
    entry = f"\n\n## {today}\n\n{memory_block}\n"
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(entry)
    logger.info("Appended memory to %s", history_path)
