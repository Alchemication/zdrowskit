"""LLM-powered reactive nudge command."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from charts import strip_charts
from cmd_llm_common import (
    CommandResult,
    apply_verification,
    route_kwargs,
    telegram_chat_id,
)
from baselines import unestablished_metrics
from config import (
    CONTEXT_DIR,
    MAX_TOKENS_NUDGE,
    MAX_TOOL_ITERATIONS_NUDGE,
    METRIC_TRUST_WINDOW_DAYS,
    NUDGES_DIR,
)
from data_maturity import build_data_maturity
from llm import call_llm
from llm_context import build_messages, load_context, load_prompt_text
from llm_health import build_llm_data, format_recent_nudges, render_health_data
from llm_verify import extract_tool_evidence, slim_source_messages
from notify import send_telegram
from standouts import effect_for, find_standout, record_standout_announced
from store import create_llm_trace, open_db
from plan_frame import resolve_plan_frame
from weekly_progress import (
    record_progress_line_shown,
    weekly_progress_nudge_line,
)

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


def _save_nudge(text: str, trigger: str, nudges_dir: Path = NUDGES_DIR) -> Path:
    """Save a nudge to a timestamped markdown file."""
    nudges_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    filename = f"nudge_{timestamp}_{trigger}.md"

    path = nudges_dir / filename
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
        context_dir = Path(getattr(args, "context_dir", CONTEXT_DIR))
        context = load_context(
            context_dir, prompt_file="nudge_prompt", max_history=3, max_log=3
        )
    except FileNotFoundError as e:
        logger.error("%s", e)
        sys.exit(1)

    conn = open_db(Path(args.db))
    health_data = build_llm_data(conn, getattr(args, "months", 1))
    health_data_text = render_health_data(
        health_data,
        prompt_kind="nudge",
        unestablished=unestablished_metrics(conn, METRIC_TRUST_WINDOW_DAYS),
    )

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

    trace_id = create_llm_trace(
        conn,
        "nudge",
        metadata={"trigger_type": _trigger},
    )

    # Resolved before the nudge is written, for two reasons. The writer needs
    # to know a header is coming so it does not spend its eighty words
    # restating it, and a nudge that would otherwise skip still has to ship the
    # one rare thing worth interrupting for. The sentence itself is computed
    # and gated in `standouts`; nothing here lets a model phrase it.
    #
    # Arriving data only. A record is news because it came in with this sync,
    # and the recency window is wide enough to absorb an import landing a day
    # or two late — but not wide enough to make it honest on a journal or
    # strategy edit, where announcing a workout from two days ago is a non
    # sequitur about something the person did not just do.
    standout = (
        find_standout(
            conn,
            me=context.get("me"),
            log=context.get("log"),
            history=context.get("history"),
            today=datetime.now().date(),
            trace_id=trace_id,
            model_prefs_path=getattr(args, "model_prefs_path", None),
        )
        if _trigger == "new_data"
        else None
    )
    context["standout"] = (
        standout.headline if standout else "(none — do not invent one)"
    )

    messages = build_messages(
        context,
        health_data_text,
        data_maturity=build_data_maturity(conn, context),
    )

    from tools import execute_run_sql, run_sql_tool

    route = route_kwargs(
        "nudge",
        getattr(args, "model", None),
        prefs_path=getattr(args, "model_prefs_path", None),
    )
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
                trace_id=trace_id,
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
                trace_id=trace_id,
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
                    trace_id=trace_id,
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

    # A skip normally ends the run here. It does not when a standout is
    # waiting: that headline is a complete sentence computed from the person's
    # own history, so the rare message still ships even when the model had
    # nothing to add underneath it.
    skipped = (
        not raw_text or raw_text.upper() == "SKIP" or "\nSKIP\n" in f"\n{raw_text}\n"
    )
    if skipped and standout is None:
        logger.info("Nudge skipped — nothing new to say (trigger: %s)", _trigger)
        return CommandResult(llm_call_id=result.llm_call_id)

    verified_text: str | None = None
    if not skipped:
        verified_text = apply_verification(
            kind="nudge",
            draft=raw_text,
            evidence={
                "health_data_text": health_data_text,
                "recent_nudges_text": context.get("recent_nudges"),
                "last_coach_summary": context.get("last_coach_summary"),
                "trigger_type": _trigger,
                "trigger_context": trigger_context_text,
                "tool_calls": extract_tool_evidence(messages),
                # The verifier scores the body, but the body was written to sit
                # underneath this. Without it, a sentence that only means
                # something beside the header reads as unsupported, and a body
                # restating the header reads as fine.
                "standout_headline": standout.headline if standout else None,
            },
            source_messages=slim_source_messages(messages, raw_text),
            conn=conn,
            metadata={
                "source_llm_call_id": result.llm_call_id,
                "trigger_type": _trigger,
            },
            trace_id=trace_id,
            model_prefs_path=getattr(args, "model_prefs_path", None),
        )
    # Nudges no longer offer charts: the block cost a fifth of the prompt and
    # produced one in 652 messages. Any chart the model still emits is stripped
    # rather than rendered, so a stray block cannot reach the user as code.
    nudge_body = ""
    if verified_text is None or verified_text.strip().upper() == "SKIP":
        if not skipped:
            logger.info("Nudge body dropped by verifier (trigger: %s)", _trigger)
    else:
        nudge_body = strip_charts(verified_text.strip()).strip()

    if not nudge_body and standout is None:
        logger.warning("Nudge produced no deliverable text (trigger: %s)", _trigger)
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

    # One weekly ring as the header, and only when it has visibly moved since
    # the last nudge that carried one. Nudges fire up to twice a day while the
    # rings move three or four times a week, so an ungated line would be the
    # same sentence a dozen times a week — the part the reader learns to skip,
    # sitting immediately above the part that matters.
    #
    # Called here, past verification and every SKIP path, for two reasons: the
    # nudge is by now committed to being sent, so recording the line as shown
    # is truthful, and measured numbers never pass through a model that could
    # reword them.
    # Resolved on the standout path too. The session that sets a record is
    # almost always the session that moved a ring, so this is exactly the
    # message where the week's state would otherwise go missing.
    frame = resolve_plan_frame(
        conn,
        me=context.get("me"),
        log=context.get("log"),
        history=context.get("history"),
        today=datetime.now().date().isoformat(),
        trace_id=trace_id,
        model_prefs_path=getattr(args, "model_prefs_path", None),
    )
    shown = weekly_progress_nudge_line(
        conn,
        strategy_md=context.get("strategy"),
        trace_id=trace_id,
        model_prefs_path=getattr(args, "model_prefs_path", None),
        frame=frame,
    )
    progress, progress_fingerprint = shown if shown else (None, None)
    # The ring replaces the trigger label rather than sitting beside it.
    # "Data Sync" names the plumbing that woke the bot up, reads identically on
    # every nudge, and took two thirds of the line on a watch — enough to wrap
    # the state it was meant to make room for onto a second row. "Lifts ●●"
    # says where the week stands and implies the trigger anyway. The label
    # returns whenever there is no ring to show, because a nudge with no
    # header at all loses the one line that says why the phone buzzed.
    #
    # A standout outranks both and takes the header. The ring then moves to the
    # foot of the message rather than being dropped: stacked under the standout
    # it would be a second header, and the wrapped header is the problem the
    # dots were introduced to solve, but below the body it costs one short line
    # and the week is still visible on the message read most closely.
    #
    # The standout sits in a quote block, which Telegram draws as a coloured
    # bar down its left edge. It is a statement about years of history wedged
    # into a message about today, and undivided the two read as one paragraph.
    # The bar does that job without a horizontal rule, which would cost a line
    # and split a message this short into three visible pieces.
    footer = ""
    if standout is not None:
        heading = f"> {standout.headline}"
        footer = progress or ""
    elif progress:
        heading = f"**{progress}**"
    else:
        heading = f"**{header}**"
    nudge_text = "\n\n".join(part for part in (heading, nudge_body, footer) if part)

    _save_nudge(
        nudge_text,
        _trigger,
        Path(getattr(args, "nudges_dir", NUDGES_DIR)),
    )
    print(nudge_text)

    use_telegram = getattr(args, "telegram", False)
    if not use_telegram:
        use_telegram = True  # Default channel

    telegram_message_id: int | None = None
    subject = f"zdrowskit — {_trigger.replace('_', ' ')}"
    if use_telegram:
        telegram_message_id = send_telegram(
            nudge_text,
            subject,
            reply_markup,
            chat_id=telegram_chat_id(args),
            message_effect_id=(effect_for(standout) if standout is not None else None),
        )

    # Only now is the line something the person has actually seen. Recording
    # it at composition time would let a failed send suppress it from the next
    # nudge on the strength of a message that never arrived.
    delivered = telegram_message_id is not None or not use_telegram
    if progress_fingerprint and delivered:
        record_progress_line_shown(conn, progress_fingerprint, progress or "")
    # Spending a month of budget on a message that never arrived would silence
    # the next four weeks on the strength of nothing.
    if standout is not None and delivered:
        record_standout_announced(conn, standout)

    return CommandResult(
        text=nudge_text,
        llm_call_id=result.llm_call_id,
        telegram_message_id=telegram_message_id,
    )
