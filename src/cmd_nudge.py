"""LLM-powered reactive nudge command."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from charts import (
    ChartResult,
    chart_figure_caption,
    extract_charts,
    render_chart,
    strip_charts,
)
from cmd_llm_common import CommandResult, _apply_verification, _route_kwargs
from config import CONTEXT_DIR, MAX_TOKENS_NUDGE, MAX_TOOL_ITERATIONS_NUDGE, NUDGES_DIR
from llm import call_llm
from llm_context import build_messages, load_context, load_prompt_text
from llm_health import build_llm_data, format_recent_nudges, render_health_data
from llm_verify import extract_tool_evidence, slim_source_messages
from notify import send_telegram, send_telegram_photo
from store import open_db

logger = logging.getLogger(__name__)

_NUDGE_TOOL_FOLLOWUP = load_prompt_text("nudge_tool_followup")
_NUDGE_NONFINAL_RETRY = load_prompt_text("nudge_nonfinal_retry")
_NUDGE_EMPTY_RETRY = load_prompt_text("nudge_empty_retry")


def _looks_like_nonfinal_nudge(text: str) -> bool:
    """Return True when a nudge reply looks like internal reasoning.

    Args:
        text: Raw assistant text returned by the model.

    Returns:
        True when the text looks like planning or meta-commentary rather than a
        user-facing nudge.
    """
    normalized = " ".join(text.strip().split()).lower()
    if not normalized:
        return False

    meta_patterns = (
        r"^(let me|i(?:'ll| will))\b",
        r"^the \d{1,2}:\d{2}\s?(?:am|pm) notification prescribed\b",
        r"\bgenuinely new data worth (?:a quick response|saying)\b",
        r"\bwhat(?:'s| is) actually new\b",
    )
    return any(re.search(pattern, normalized) for pattern in meta_patterns)


def _save_nudge(text: str, trigger: str) -> Path:
    """Save a nudge to a timestamped markdown file."""
    NUDGES_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    filename = f"nudge_{timestamp}_{trigger}.md"

    path = NUDGES_DIR / filename
    path.write_text(text, encoding="utf-8")
    logger.info("Nudge saved to %s", path)
    return path


def cmd_nudge(
    args: argparse.Namespace,
    trigger_type: str | None = None,
    reply_markup: dict | None = None,
) -> CommandResult:
    """Handle the 'nudge' subcommand: send a short context-aware notification.

    Args:
        args: Parsed CLI arguments with db, model, telegram, trigger, months,
              and optional recent_nudges attributes.
        trigger_type: What triggered the nudge — overrides args.trigger when
            called programmatically (e.g. from the daemon).
        reply_markup: Optional Telegram reply markup (e.g. feedback keyboard)
            attached to the last message chunk.

    Returns:
        A CommandResult with text, llm_call_id, and telegram_message_id.
    """
    _trigger = trigger_type or getattr(args, "trigger", "new_data")

    try:
        context = load_context(
            CONTEXT_DIR, prompt_file="nudge_prompt", max_history=3, max_log=3
        )
    except FileNotFoundError as e:
        logger.error("%s", e)
        sys.exit(1)

    conn = open_db(Path(args.db))
    health_data = build_llm_data(conn, getattr(args, "months", 1))
    health_data_text = render_health_data(health_data, prompt_kind="nudge")

    recent_nudge_entries: list[dict] = getattr(args, "recent_nudges", [])
    context["recent_nudges"] = format_recent_nudges(
        recent_nudge_entries,
        empty_text="(none yet)",
    )
    context["trigger_type"] = _trigger
    trigger_context_text = (getattr(args, "trigger_context", "") or "").strip()
    context["trigger_context"] = trigger_context_text or "(no additional detail)"

    # Cross-message awareness: inject last coach review
    coach_summary = getattr(args, "last_coach_summary", "")
    coach_date = getattr(args, "last_coach_summary_date", "")
    if coach_summary:
        context["last_coach_summary"] = f"[{coach_date}] {coach_summary}"
    else:
        context["last_coach_summary"] = "(no recent coach review)"

    messages = build_messages(context, health_data_text)

    from tools import execute_run_sql, run_sql_tool

    route = _route_kwargs("nudge", getattr(args, "model", None))
    model = route["model"]
    fallback_models = route.get("fallback_models")
    temperature = route.get("temperature", 0.7)
    reasoning_effort = route.get("reasoning_effort")
    tools = run_sql_tool()
    max_iterations = MAX_TOOL_ITERATIONS_NUDGE

    logger.info(
        "Calling %s for nudge (trigger: %s, reasoning=%s) ...",
        model,
        _trigger,
        reasoning_effort or "off",
    )
    for iteration in range(max_iterations):
        try:
            result = call_llm(
                messages,
                model=model,
                max_tokens=MAX_TOKENS_NUDGE,
                temperature=temperature,
                tools=tools,
                reasoning_effort=reasoning_effort,
                fallback_models=fallback_models,
                conn=conn,
                request_type="nudge",
                metadata={
                    "trigger_type": _trigger,
                    "iteration": iteration,
                    "reasoning_effort": reasoning_effort,
                },
            )
        except Exception as e:
            err_name = type(e).__name__
            if "authentication" in err_name.lower() or "auth" in str(e).lower():
                logger.error(
                    "Authentication failed. Set ANTHROPIC_API_KEY in your .env file."
                )
            else:
                logger.error("LLM call failed: %s: %s", err_name, e)
            sys.exit(1)

        if not result.tool_calls:
            raw_text = result.text.strip()
            if _looks_like_nonfinal_nudge(raw_text) and iteration < max_iterations - 1:
                logger.warning(
                    "Nudge returned non-final meta text on iteration %d; retrying",
                    iteration,
                )
                messages.append(
                    result.raw_message or {"role": "assistant", "content": result.text}
                )
                messages.append({"role": "user", "content": _NUDGE_NONFINAL_RETRY})
                continue
            break

        messages.append(result.raw_message)
        for tc in result.tool_calls:
            fn_name = tc.function.name
            raw_args = tc.function.arguments
            try:
                args_dict = (
                    json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                )
            except (ValueError, json.JSONDecodeError):
                args_dict = {}

            if fn_name == "run_sql":
                logger.info("Nudge SQL: %s", args_dict.get("query", "")[:200])
                tool_result = execute_run_sql(Path(args.db), args_dict)
            else:
                tool_result = json.dumps({"error": f"Unknown tool: {fn_name}"})

            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": tool_result}
            )
        messages.append({"role": "user", "content": _NUDGE_TOOL_FOLLOWUP})

    # If the loop exited with an empty response (iteration cap reached while
    # the model still wanted tools), force one final tool-less synthesis call.
    if not (result.text or "").strip() and result.tool_calls:
        logger.warning(
            "Nudge loop exited with empty text + pending tool_calls; forcing final synthesis"
        )
        messages.append(result.raw_message)
        for tc in result.tool_calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(
                        {"error": load_prompt_text("tool_budget_nudge")}
                    ),
                }
            )
        try:
            result = call_llm(
                messages,
                model=model,
                max_tokens=MAX_TOKENS_NUDGE,
                temperature=temperature,
                tools=None,
                reasoning_effort=reasoning_effort,
                fallback_models=fallback_models,
                conn=conn,
                request_type="nudge",
                metadata={
                    "trigger_type": _trigger,
                    "iteration": "final_synthesis",
                    "reasoning_effort": reasoning_effort,
                },
            )
        except Exception as e:
            logger.error("Nudge final synthesis call failed: %s", e)
            sys.exit(1)

    raw_text = result.text.strip()
    if not raw_text:
        retry_models: list[str] = []
        seen_models = {model}
        for fallback_model in fallback_models or []:
            if not isinstance(fallback_model, str) or fallback_model in seen_models:
                continue
            seen_models.add(fallback_model)
            retry_models.append(fallback_model)

        for retry_model in retry_models:
            logger.warning(
                "Nudge returned empty final text; retrying with fallback %s",
                retry_model,
            )
            source_llm_call_id = result.llm_call_id
            try:
                result = call_llm(
                    [*messages, {"role": "user", "content": _NUDGE_EMPTY_RETRY}],
                    model=retry_model,
                    max_tokens=MAX_TOKENS_NUDGE,
                    temperature=temperature,
                    tools=None,
                    reasoning_effort=reasoning_effort,
                    fallback_models=[],
                    conn=conn,
                    request_type="nudge",
                    metadata={
                        "trigger_type": _trigger,
                        "iteration": "empty_retry",
                        "reasoning_effort": reasoning_effort,
                        "retry_after_llm_call_id": source_llm_call_id,
                    },
                )
            except Exception as e:
                logger.error("Nudge empty-response retry failed: %s", e)
                continue
            raw_text = result.text.strip()
            if raw_text:
                break

        if not raw_text:
            logger.warning(
                "Nudge returned empty final text; treating as SKIP (trigger: %s)",
                _trigger,
            )
            return CommandResult(llm_call_id=result.llm_call_id)

    # Check for SKIP as the entire response OR as a standalone line.
    if raw_text.upper() == "SKIP" or "\nSKIP\n" in f"\n{raw_text}\n":
        logger.info("Nudge skipped by LLM — nothing new to say (trigger: %s)", _trigger)
        return CommandResult(llm_call_id=result.llm_call_id)

    verified_text = _apply_verification(
        kind="nudge",
        draft=raw_text,
        evidence={
            "health_data_text": health_data_text,
            "recent_nudges_text": context.get("recent_nudges"),
            "last_coach_summary": context.get("last_coach_summary"),
            "trigger_type": _trigger,
            "trigger_context": trigger_context_text,
            "tool_calls": extract_tool_evidence(messages),
        },
        source_messages=slim_source_messages(messages, raw_text),
        conn=conn,
        metadata={
            "source_llm_call_id": result.llm_call_id,
            "trigger_type": _trigger,
        },
    )
    if verified_text is None or verified_text.strip().upper() == "SKIP":
        logger.info("Nudge skipped by verifier (trigger: %s)", _trigger)
        return CommandResult(llm_call_id=result.llm_call_id)
    raw_text = verified_text.strip()

    # Extract and render optional chart(s).
    chart_blocks = extract_charts(raw_text)
    nudge_charts: list[ChartResult] = []
    for block in chart_blocks:
        png = render_chart(block.code, health_data)
        if png:
            nudge_charts.append(
                ChartResult(title=block.title, section=block.section, image_bytes=png)
            )
        else:
            logger.warning("Nudge chart '%s' failed to render, skipping", block.title)

    nudge_text = strip_charts(raw_text).strip()
    if not nudge_text:
        logger.warning(
            "Nudge final text was empty after chart stripping; treating as SKIP "
            "(trigger: %s)",
            _trigger,
        )
        return CommandResult(llm_call_id=result.llm_call_id)

    # Trigger-specific emoji header for visual distinction in Telegram.
    _TRIGGER_HEADERS: dict[str, str] = {
        "new_data": "\U0001f4ca Data Sync",
        "log_update": "\U0001f4dd Log Update",
        "strategy_updated": "\U0001f9ed Strategy Update",
    }
    header = _TRIGGER_HEADERS.get(
        _trigger, f"\U0001f514 {_trigger.replace('_', ' ').title()}"
    )
    nudge_text = f"**{header}**\n\n{nudge_text}"

    _save_nudge(nudge_text, _trigger)
    print(nudge_text)

    use_telegram = getattr(args, "telegram", False)
    if not use_telegram:
        use_telegram = True  # Default channel

    telegram_message_id: int | None = None
    subject = f"zdrowskit — {_trigger.replace('_', ' ')}"
    if use_telegram:
        # Send chart photos before the text nudge.
        for index, chart in enumerate(nudge_charts, start=1):
            send_telegram_photo(
                chart.image_bytes,
                caption=chart_figure_caption(index, chart.title),
            )
        telegram_message_id = send_telegram(nudge_text, subject, reply_markup)

    return CommandResult(
        text=nudge_text,
        llm_call_id=result.llm_call_id,
        telegram_message_id=telegram_message_id,
    )
