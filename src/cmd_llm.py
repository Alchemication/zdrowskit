"""LLM-powered subcommand handlers: insights, nudge, coach.

Extracted from commands.py to keep individual modules under ~1000 lines.
Public API re-exported from commands.py for backward compatibility.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from baselines import compute_baselines
from charts import (
    ChartResult,
    chart_figure_caption,
    extract_charts,
    render_chart,
    strip_charts,
)
from config import (
    CONTEXT_DIR,
    MAX_TOOL_ITERATIONS_COACH,
    MAX_TOOL_ITERATIONS_INSIGHTS,
    MAX_TOOL_ITERATIONS_NUDGE,
    NUDGES_DIR,
    NOTIFICATION_PREFS_PATH,
    REPORTS_DIR,
)
from context_edit import ContextEdit, EditPreviewError, build_edit_preview
from llm import DEFAULT_MODEL, LLMResult, call_llm, extract_memory
from llm_context import append_history, build_messages, load_context
from llm_health import (
    build_llm_data,
    build_review_facts,
    format_recent_nudges,
    render_health_data,
)
from notification_prefs import (
    DEFAULT_NOTIFICATION_PREFS,
    active_temporary_mutes,
    effective_notification_prefs,
    validate_notification_changes,
)
from notify import send_email, send_telegram, send_telegram_photo, send_telegram_report
from store import open_db

logger = logging.getLogger(__name__)

_NUDGE_TOOL_FOLLOWUP = (
    "Use the tool results above to write the final nudge now. Output only one "
    "short user-facing message (maximum 80 words) or SKIP. Do not mention "
    "checking data, reviewing notifications, or that something is genuinely "
    "new. No headers; the app adds them."
)
_NUDGE_NONFINAL_RETRY = (
    "That was internal reasoning, not a finished nudge. Rewrite it as the "
    "final user-facing nudge now: one short message (maximum 80 words) or "
    "SKIP. Do not mention checking, reviewing, or deciding whether to send."
)
NOTIFY_MODEL = "anthropic/claude-haiku-4-5"
LOG_FLOW_MODEL = "anthropic/claude-haiku-4-5"

# Hard caps must match log_flow_prompt.md and log-flow handler expectations.
# The initial call returns exactly 1 step (the state check); a second step
# may be appended later by build_log_step_followup, giving 2 steps total.
MAX_LOG_FLOW_INITIAL_STEPS = 1
MAX_LOG_FLOW_STEPS = 2
MAX_LOG_FLOW_OPTIONS_PER_STEP = 8


@dataclass
class LogFlowStep:
    """One step in the /log tap-through flow."""

    id: str
    question: str
    options: list[str]
    multi_select: bool = False
    optional: bool = False
    ask_end_date_if_selected: list[str] | None = None


@dataclass
class LogFlow:
    """A complete /log interview returned by the LLM."""

    steps: list[LogFlowStep]
    llm_call_id: int | None = None
    model: str | None = None


@dataclass
class CommandResult:
    """Return value from LLM-powered commands that send via Telegram.

    Attributes:
        text: The LLM response text (or None if skipped).
        llm_call_id: Database row id of the logged LLM call.
        telegram_message_id: Message id of the last Telegram message sent.
    """

    text: str | None = None
    llm_call_id: int | None = None
    telegram_message_id: int | None = None


@dataclass
class CoachProposal:
    """A validated coach edit ready to be presented to the user.

    Attributes:
        edit: The proposed ContextEdit.
        preview: Pre-rendered unified-diff preview string for the edit.
    """

    edit: ContextEdit
    preview: str


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _normalize_reasoning_effort(value: str | None) -> str | None:
    """Normalize a CLI reasoning-effort value to what call_llm expects."""
    if value is None or value == "none":
        return None
    return value


def _print_explain(
    context: dict[str, str],
    context_dir: Path,
    messages: list[dict[str, str]],
    result: LLMResult,
    memory: str | None,
    baselines: str | None = None,
    report_path: Path | None = None,
) -> None:
    """Print LLM call diagnostics to stderr using rich formatting.

    Args:
        context: Dict from load_context() with file stems as keys.
        context_dir: Path to the context files directory.
        messages: The system + user messages sent to the LLM.
        result: LLMResult from call_llm().
        memory: Extracted memory string, or None.
        baselines: Auto-computed baselines markdown, or None if skipped.
        report_path: Path where the report was saved, or None.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    stderr = Console(stderr=True)

    # Context files
    all_names = [
        "soul",
        "me",
        "strategy",
        "log",
        "history",
        "coach_feedback",
        "prompt",
    ]
    ctx_table = Table(title="Context Files", show_lines=False)
    ctx_table.add_column("File", style="cyan")
    ctx_table.add_column("Status", style="green")
    ctx_table.add_column("Size (chars)", justify="right")
    for name in all_names:
        if name in context and context[name] != "(not provided)":
            ctx_table.add_row(f"{name}.md", "loaded", f"{len(context[name]):,}")
        else:
            ctx_table.add_row(f"{name}.md", "[red]missing[/red]", "—")
    stderr.print(ctx_table)

    # Prompt assembly
    sys_len = len(messages[0]["content"])
    user_len = len(messages[1]["content"])
    total_chars = sys_len + user_len
    prompt_table = Table(title="Prompt Assembly", show_lines=False)
    prompt_table.add_column("Component", style="cyan")
    prompt_table.add_column("Chars", justify="right")
    prompt_table.add_column("~Tokens", justify="right")
    prompt_table.add_row("System message", f"{sys_len:,}", f"{sys_len // 4:,}")
    prompt_table.add_row("User message", f"{user_len:,}", f"{user_len // 4:,}")
    prompt_table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{total_chars:,}[/bold]",
        f"[bold]{total_chars // 4:,}[/bold]",
    )
    stderr.print(prompt_table)

    # LLM call params
    params_table = Table(title="LLM Call", show_lines=False)
    params_table.add_column("Parameter", style="cyan")
    params_table.add_column("Value")
    params_table.add_row("Model", result.model)
    params_table.add_row("Temperature", "0.7")
    params_table.add_row("Max tokens", "4,096")
    stderr.print(params_table)

    # Response stats
    stats_table = Table(title="Response Stats", show_lines=False)
    stats_table.add_column("Metric", style="cyan")
    stats_table.add_column("Value", justify="right")
    stats_table.add_row("Input tokens", f"{result.input_tokens:,}")
    stats_table.add_row("Output tokens", f"{result.output_tokens:,}")
    stats_table.add_row("Total tokens", f"{result.total_tokens:,}")
    stats_table.add_row("Latency", f"{result.latency_s:.1f}s")
    if result.cost is not None:
        stats_table.add_row("Cost", f"${result.cost:.4f}")
    else:
        stats_table.add_row("Cost", "unavailable")
    stderr.print(stats_table)

    # Baselines
    if baselines:
        stderr.print(
            Panel(baselines, title="Auto-computed Baselines", border_style="cyan")
        )
    else:
        stderr.print(
            Panel(
                "[yellow]Skipped[/yellow] (--no-update-baselines)",
                title="Auto-computed Baselines",
            )
        )

    # Memory extraction
    if memory:
        stderr.print(
            Panel(
                f"Extracted: [green]yes[/green] ({len(memory):,} chars)",
                title="Memory",
            )
        )
    else:
        stderr.print(
            Panel(
                "Extracted: [yellow]no[/yellow] (no <memory> block found)",
                title="Memory",
            )
        )

    # Saved report path
    if report_path:
        stderr.print(f"\n[dim]Report saved to:[/dim] [cyan]{report_path}[/cyan]")


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse a top-level JSON object from model output.

    Accepts plain JSON or a fenced code block containing JSON.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError("Notify interpreter must return a JSON object")
    return data


def interpret_notify_request(
    request_text: str,
    *,
    db: str | Path,
    prefs: dict[str, Any],
    now: datetime | None = None,
    clarification_answer: str | None = None,
    model: str = NOTIFY_MODEL,
) -> dict[str, Any]:
    """Interpret a Telegram /notify request into a structured payload."""
    now = now or datetime.now().astimezone()

    try:
        context = load_context(
            CONTEXT_DIR,
            prompt_file="notify_prompt",
            max_history=0,
            max_log=0,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        raise

    context["current_settings"] = json.dumps(
        effective_notification_prefs(prefs), indent=2, sort_keys=True
    )
    context["default_settings"] = json.dumps(
        effective_notification_prefs(DEFAULT_NOTIFICATION_PREFS),
        indent=2,
        sort_keys=True,
    )
    context["active_mutes"] = json.dumps(
        active_temporary_mutes(prefs, now=now), indent=2, sort_keys=True
    )
    context["notify_request"] = request_text
    context["clarification_answer"] = clarification_answer or "(none)"
    context["timezone"] = now.tzname() or str(now.tzinfo) or "local"

    messages = build_messages(
        context,
        health_data_text="{}",
        baselines=None,
        week_complete=False,
        today=now.date(),
    )

    conn = open_db(Path(db))
    try:
        result = call_llm(
            messages,
            model=model,
            max_tokens=512,
            temperature=0,
            conn=conn,
            request_type="notify",
            metadata={
                "request_text": request_text,
                "clarification_answer": clarification_answer,
                "prefs_path": str(NOTIFICATION_PREFS_PATH),
            },
        )
    finally:
        conn.close()

    payload = _extract_json_object(result.text)
    status = payload.get("status")
    intent = payload.get("intent")
    if status not in {"proposal", "needs_clarification", "unsupported"}:
        raise ValueError(f"Unsupported notify status: {status}")
    if intent not in {
        "show",
        "set",
        "enable",
        "disable",
        "reset",
        "reset_all",
        "mute_until",
    }:
        raise ValueError(f"Unsupported notify intent: {intent}")

    payload["changes"] = validate_notification_changes(payload.get("changes", []))
    if status == "needs_clarification":
        question = payload.get("clarification_question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("Notify clarification must include a question")
        payload["clarification_question"] = question.strip()
    else:
        payload["clarification_question"] = None

    summary = payload.get("summary", "")
    payload["summary"] = summary.strip() if isinstance(summary, str) else ""
    reason = payload.get("reason", "")
    payload["reason"] = reason.strip() if isinstance(reason, str) else ""
    payload["llm_call_id"] = result.llm_call_id
    payload["model"] = result.model
    return payload


def _query_today_snapshot(conn, today: date) -> str:
    """Build a short human-readable snapshot of today's signals for /log.

    Pulls today's `daily` row, last night's `sleep_all`, and today's
    `workout_all` entries. Kept tiny so the prompt stays focused.
    """
    lines: list[str] = [f"Date: {today.isoformat()}"]

    row = conn.execute(
        "SELECT hrv_ms, resting_hr, recovery_index, steps FROM daily WHERE date = ?",
        (today.isoformat(),),
    ).fetchone()
    if row:
        parts = []
        if row[0] is not None:
            parts.append(f"HRV {row[0]:.1f} ms")
        if row[1] is not None:
            parts.append(f"RHR {int(row[1])}")
        if row[2] is not None:
            parts.append(f"recovery_index {row[2]:.2f}")
        if row[3] is not None:
            parts.append(f"steps {int(row[3])}")
        if parts:
            lines.append("Today daily: " + ", ".join(parts))
        else:
            lines.append("Today daily: row present, all nulls")
    else:
        lines.append("Today daily: (no row yet)")

    sleep_date = today - timedelta(days=1)
    sleep_row = conn.execute(
        "SELECT sleep_total_h, sleep_efficiency_pct FROM sleep_all WHERE date = ?",
        (sleep_date.isoformat(),),
    ).fetchone()
    if sleep_row and sleep_row[0] is not None:
        effpart = (
            f", {sleep_row[1]:.0f}% efficiency" if sleep_row[1] is not None else ""
        )
        lines.append(f"Last night: {sleep_row[0]:.2f}h{effpart}")
    else:
        lines.append("Last night: (no sleep data)")

    workouts = conn.execute(
        "SELECT category, duration_min FROM workout_all "
        "WHERE date = ? ORDER BY start_utc, category, duration_min",
        (today.isoformat(),),
    ).fetchall()
    if workouts:
        summary = ", ".join(
            f"{cat or 'other'} {int(dur or 0)}m" for cat, dur in workouts
        )
        lines.append(f"Today workouts: {summary}")
    else:
        lines.append("Today workouts: (none logged)")

    return "\n".join(lines)


def _coerce_log_flow_step(raw: Any, index: int) -> LogFlowStep:
    """Validate and coerce a single raw step dict into a LogFlowStep."""
    if not isinstance(raw, dict):
        raise ValueError(f"Step {index} is not an object")
    step_id = raw.get("id")
    question = raw.get("question")
    options = raw.get("options")
    if not isinstance(step_id, str) or not step_id.strip():
        raise ValueError(f"Step {index} missing 'id'")
    if not isinstance(question, str) or not question.strip():
        raise ValueError(f"Step {index} missing 'question'")
    if not isinstance(options, list) or not options:
        raise ValueError(f"Step {index} missing non-empty 'options'")
    if len(options) > MAX_LOG_FLOW_OPTIONS_PER_STEP:
        raise ValueError(
            f"Step {index} has {len(options)} options "
            f"(max {MAX_LOG_FLOW_OPTIONS_PER_STEP})"
        )
    clean_options: list[str] = []
    for j, opt in enumerate(options):
        if not isinstance(opt, str) or not opt.strip():
            raise ValueError(f"Step {index} option {j} is not a non-empty string")
        clean_options.append(opt.strip())
    ask_end = raw.get("ask_end_date_if_selected")
    if ask_end is not None:
        if not isinstance(ask_end, list) or not all(
            isinstance(x, str) for x in ask_end
        ):
            raise ValueError(
                f"Step {index} 'ask_end_date_if_selected' must be a list of strings"
            )
        unknown = [x for x in ask_end if x not in clean_options]
        if unknown:
            raise ValueError(
                f"Step {index} 'ask_end_date_if_selected' references unknown "
                f"options: {unknown}"
            )
    return LogFlowStep(
        id=step_id.strip(),
        question=question.strip(),
        options=clean_options,
        multi_select=bool(raw.get("multi_select", False)),
        optional=bool(raw.get("optional", False)),
        ask_end_date_if_selected=[x.strip() for x in ask_end] if ask_end else None,
    )


def build_log_flow(
    *,
    db: str | Path,
    now: datetime | None = None,
    model: str = LOG_FLOW_MODEL,
) -> LogFlow:
    """Ask the LLM to design an adaptive /log interview for today.

    Reads me.md, strategy.md, the entire log.md (self-trimmed by history.md
    pipeline), and a lean snapshot of today's DB signals, then returns a
    validated 1–3 step flow.
    """
    now = now or datetime.now().astimezone()
    today = now.date()

    try:
        context = load_context(
            CONTEXT_DIR,
            prompt_file="log_flow_prompt",
            max_history=0,
            max_log=0,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        raise

    conn = open_db(Path(db))
    try:
        context["today_snapshot"] = _query_today_snapshot(conn, today)

        messages = build_messages(
            context,
            health_data_text="{}",
            baselines=None,
            week_complete=False,
            today=today,
        )

        result = call_llm(
            messages,
            model=model,
            max_tokens=512,
            temperature=0,
            conn=conn,
            request_type="log_flow",
            metadata={"date": today.isoformat()},
        )
    finally:
        conn.close()

    payload = _extract_json_object(result.text)
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("Log flow must include a non-empty 'steps' array")
    if len(raw_steps) > MAX_LOG_FLOW_INITIAL_STEPS:
        raise ValueError(
            f"Log flow returned {len(raw_steps)} steps "
            f"(max {MAX_LOG_FLOW_INITIAL_STEPS} for the initial call)"
        )
    steps = [_coerce_log_flow_step(raw, i) for i, raw in enumerate(raw_steps)]
    return LogFlow(steps=steps, llm_call_id=result.llm_call_id, model=result.model)


def build_log_step_followup(
    *,
    prior_step: LogFlowStep,
    prior_answer: list[str],
    db: str | Path,
    now: datetime | None = None,
    model: str = LOG_FLOW_MODEL,
) -> LogFlowStep | None:
    """Design a reactive follow-up step given the user's step-1 answer.

    Returns a tailored ``LogFlowStep`` whose options react to
    ``prior_answer`` (affirmative tags after a positive state, disruption
    tags after a negative state), or ``None`` when step 1 already
    captured everything worth knowing and the bullet should commit now.
    """
    now = now or datetime.now().astimezone()
    today = now.date()

    try:
        context = load_context(
            CONTEXT_DIR,
            prompt_file="log_followup_prompt",
            max_history=0,
            max_log=0,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        raise

    conn = open_db(Path(db))
    try:
        context["today_snapshot"] = _query_today_snapshot(conn, today)
        context["prior_question"] = prior_step.question
        context["prior_options"] = ", ".join(prior_step.options)
        context["prior_answer"] = ", ".join(prior_answer) if prior_answer else "(none)"

        messages = build_messages(
            context,
            health_data_text="{}",
            baselines=None,
            week_complete=False,
            today=today,
        )

        result = call_llm(
            messages,
            model=model,
            max_tokens=512,
            temperature=0,
            conn=conn,
            request_type="log_flow_followup",
            metadata={
                "date": today.isoformat(),
                "prior_step_id": prior_step.id,
                "prior_answer": prior_answer,
            },
        )
    finally:
        conn.close()

    payload = _extract_json_object(result.text)
    raw_step = payload.get("step")
    if raw_step is None:
        return None
    return _coerce_log_flow_step(raw_step, 1)


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


def _extract_strategy_sections(strategy_md: str) -> list[str]:
    """Return the ordered list of `## ` headings inside strategy.md."""
    if not strategy_md or strategy_md == "(not provided)":
        return []
    headings: list[str] = []
    for line in strategy_md.splitlines():
        stripped = line.rstrip()
        if stripped.startswith("## ") and not stripped.startswith("### "):
            headings.append(stripped)
    return headings


def _save_baselines(context_dir: Path, baselines: str) -> None:
    """Write auto-computed baselines to a dedicated baselines.md file."""
    path = context_dir / "baselines.md"
    path.write_text(baselines.rstrip() + "\n", encoding="utf-8")
    logger.info("Saved baselines to %s", path)


def _save_report(report: str, week: str) -> Path:
    """Save the report to a timestamped markdown file."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    target_date = date.today()
    if week == "last":
        target_date = target_date - timedelta(days=7)
    iso_week = f"{target_date.isocalendar().year}-W{target_date.isocalendar().week:02d}"
    suffix = "midweek" if week == "current" else "weekly"
    filename = f"{iso_week}-{suffix}.md"

    path = REPORTS_DIR / filename
    path.write_text(report, encoding="utf-8")
    logger.info("Report saved to %s", path)
    return path


def _save_nudge(text: str, trigger: str) -> Path:
    """Save a nudge to a timestamped markdown file."""
    NUDGES_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    filename = f"nudge_{timestamp}_{trigger}.md"

    path = NUDGES_DIR / filename
    path.write_text(text, encoding="utf-8")
    logger.info("Nudge saved to %s", path)
    return path


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_insights(
    args: argparse.Namespace,
    reply_markup: dict | None = None,
) -> CommandResult:
    """Handle the 'insights' subcommand: generate LLM-driven health report.

    Args:
        args: Parsed CLI arguments with db, data_dir, months, model,
              no_update_history, and explain attributes.
        reply_markup: Optional Telegram reply markup (e.g. feedback keyboard)
            attached to the last message chunk.

    Returns:
        A CommandResult with text, llm_call_id, and telegram_message_id.
    """
    try:
        context = load_context(CONTEXT_DIR)
    except FileNotFoundError as e:
        logger.error("%s", e)
        sys.exit(1)

    conn = open_db(Path(args.db))
    health_data = build_llm_data(conn, args.months, week=args.week)
    if health_data["current_week"]["summary"] is None:
        logger.error("Database is empty. Run 'import' first.")
        sys.exit(1)

    baselines = None
    if not args.no_update_baselines and args.week != "current":
        baselines = compute_baselines(conn)
        _save_baselines(CONTEXT_DIR, baselines)

    week_complete = health_data.get("week_complete", False)
    week_label = health_data.get("week_label")
    context["review_facts"] = build_review_facts(
        {**health_data, "week_label": week_label},
        context,
        week_complete=week_complete,
    )
    health_data_text = render_health_data(
        health_data,
        prompt_kind="report",
        week=args.week,
    )

    try:
        messages = build_messages(
            context,
            health_data_text,
            baselines=baselines,
            week_complete=week_complete,
        )
    except (KeyError, ValueError) as e:
        logger.error("Failed to render prompt.md template: %s", e)
        sys.exit(1)

    from tools import execute_run_sql, run_sql_tool

    tools = run_sql_tool()
    max_iterations = MAX_TOOL_ITERATIONS_INSIGHTS
    reasoning_effort = _normalize_reasoning_effort(
        getattr(args, "reasoning_effort", "medium")
    )

    logger.info("Calling %s (reasoning=%s) ...", args.model, reasoning_effort or "off")
    for iteration in range(max_iterations):
        try:
            result = call_llm(
                messages,
                model=args.model,
                tools=tools,
                reasoning_effort=reasoning_effort,
                conn=conn,
                request_type="insights",
                metadata={
                    "week": args.week,
                    "months": args.months,
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
                logger.info("Insights SQL: %s", args_dict.get("query", "")[:200])
                tool_result = execute_run_sql(Path(args.db), args_dict)
            else:
                tool_result = json.dumps({"error": f"Unknown tool: {fn_name}"})

            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": tool_result}
            )

    # If the loop exited with an empty response (e.g. iteration cap reached
    # while the model still wanted to call tools), force one final synthesis
    # pass without tools so we never ship a blank report.
    if not result.text.strip():
        logger.warning(
            "Insights loop exited with empty text (tool_calls=%s); forcing final synthesis",
            bool(result.tool_calls),
        )
        if result.tool_calls:
            messages.append(result.raw_message)
            for tc in result.tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(
                            {"error": "tool budget exhausted, synthesize now"}
                        ),
                    }
                )
        try:
            result = call_llm(
                messages,
                model=args.model,
                tools=None,
                reasoning_effort=reasoning_effort,
                conn=conn,
                request_type="insights",
                metadata={
                    "week": args.week,
                    "months": args.months,
                    "iteration": "final_synthesis",
                    "reasoning_effort": reasoning_effort,
                },
            )
        except Exception as e:
            logger.error("Final synthesis call failed: %s", e)
            sys.exit(1)

    # Extract and render charts before stripping them from the response.
    chart_blocks = extract_charts(result.text)
    chart_results: list[ChartResult] = []
    for block in chart_blocks:
        png = render_chart(block.code, health_data)
        if png:
            chart_results.append(
                ChartResult(title=block.title, section=block.section, image_bytes=png)
            )
        else:
            logger.warning("Chart '%s' failed to render, skipping", block.title)

    # Strip chart and memory blocks from the visible text.
    visible_report = strip_charts(result.text)
    memory = extract_memory(visible_report)
    if memory:
        visible_report = re.sub(
            r"\s*<memory>.*?</memory>\s*", "", visible_report, flags=re.DOTALL
        ).strip()

    report_path = _save_report(visible_report, args.week)

    if args.explain:
        _print_explain(
            context, CONTEXT_DIR, messages, result, memory, baselines, report_path
        )

    print(visible_report)

    if memory and not args.no_update_history:
        append_history(CONTEXT_DIR, memory, week_label=week_label)
    elif not memory:
        logger.info("No <memory> block in response; history.md unchanged")

    # Push notifications
    report_type = "Review" if args.week == "last" else "Progress"
    notify_subject = f"Week {week_label} {report_type}" if week_label else "Report"

    telegram_message_id: int | None = None
    if args.email:
        send_email(visible_report, notify_subject)
    if args.telegram:
        telegram_message_id = send_telegram_report(
            visible_report,
            notify_subject,
            charts=chart_results,
            reply_markup=reply_markup,
        )

    return CommandResult(
        text=visible_report,
        llm_call_id=result.llm_call_id,
        telegram_message_id=telegram_message_id,
    )


def cmd_nudge(
    args: argparse.Namespace,
    trigger_type: str | None = None,
    reply_markup: dict | None = None,
) -> CommandResult:
    """Handle the 'nudge' subcommand: send a short context-aware notification.

    Args:
        args: Parsed CLI arguments with db, model, email, telegram, trigger,
              months, and optional recent_nudges attributes.
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

    model = getattr(args, "model", DEFAULT_MODEL)
    tools = run_sql_tool()
    max_iterations = MAX_TOOL_ITERATIONS_NUDGE

    logger.info("Calling %s for nudge (trigger: %s) ...", model, _trigger)
    for iteration in range(max_iterations):
        try:
            result = call_llm(
                messages,
                model=model,
                tools=tools,
                conn=conn,
                request_type="nudge",
                metadata={"trigger_type": _trigger, "iteration": iteration},
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
                        {"error": "tool budget exhausted, answer or SKIP now"}
                    ),
                }
            )
        try:
            result = call_llm(
                messages,
                model=model,
                tools=None,
                conn=conn,
                request_type="nudge",
                metadata={"trigger_type": _trigger, "iteration": "final_synthesis"},
            )
        except Exception as e:
            logger.error("Nudge final synthesis call failed: %s", e)
            sys.exit(1)

    raw_text = result.text.strip()

    # Check for SKIP as the entire response OR as a standalone line.
    if raw_text.upper() == "SKIP" or "\nSKIP\n" in f"\n{raw_text}\n":
        logger.info("Nudge skipped by LLM — nothing new to say (trigger: %s)", _trigger)
        return CommandResult(llm_call_id=result.llm_call_id)

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

    nudge_text = strip_charts(raw_text)

    # Trigger-specific emoji header for visual distinction in Telegram.
    _TRIGGER_HEADERS: dict[str, str] = {
        "new_data": "\U0001f4ca Data Sync",
        "missed_session": "\U0001f3cb\ufe0f Missed Session",
        "log_update": "\U0001f4dd Log Update",
        "strategy_updated": "\U0001f9ed Strategy Update",
    }
    header = _TRIGGER_HEADERS.get(
        _trigger, f"\U0001f514 {_trigger.replace('_', ' ').title()}"
    )
    nudge_text = f"**{header}**\n\n{nudge_text}"

    _save_nudge(nudge_text, _trigger)
    print(nudge_text)

    use_email = getattr(args, "email", False)
    use_telegram = getattr(args, "telegram", False)
    if not use_email and not use_telegram:
        use_telegram = True  # Default channel

    telegram_message_id: int | None = None
    subject = f"zdrowskit — {_trigger.replace('_', ' ')}"
    if use_email:
        send_email(nudge_text, subject)
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


def cmd_coach(
    args: argparse.Namespace,
) -> tuple[CommandResult, list[CoachProposal]]:
    """Generate coaching proposals for strategy.md updates.

    Reviews the week's data against the current strategy (goals + weekly plan
    + diet + sleep) and proposes concrete edits via the ``update_context``
    tool. Returns the validated proposals so the daemon can present them as
    Approve/Reject buttons inside a single bundled Telegram message.

    Args:
        args: Parsed CLI arguments with db, model, week, months, and
            (optionally) recent_nudges and reasoning_effort.

    Returns:
        A tuple of (CommandResult, list_of_proposals). For SKIP, text is
        None and proposals is empty. Otherwise text holds the bundled
        narrative + proposal summaries (also `print()`ed to stdout) and
        proposals contains one CoachProposal per validated edit.
    """
    from context_edit import context_edit_from_tool_call
    from llm_context import context_update_tool
    from tools import execute_run_sql, run_sql_tool

    try:
        context = load_context(CONTEXT_DIR, prompt_file="coach_prompt")
    except FileNotFoundError as e:
        logger.error("%s", e)
        sys.exit(1)

    conn = open_db(Path(args.db))
    week = getattr(args, "week", "current")
    health_data = build_llm_data(conn, getattr(args, "months", 3), week=week)
    if health_data["current_week"]["summary"] is None:
        logger.error("Database is empty. Run 'import' first.")
        sys.exit(1)

    baselines = compute_baselines(conn)
    _save_baselines(CONTEXT_DIR, baselines)

    week_complete = health_data.get("week_complete", False)
    week_label = health_data.get("week_label")
    context["review_facts"] = build_review_facts(
        {**health_data, "week_label": week_label},
        context,
        week_complete=week_complete,
    )

    # Inject the live list of strategy.md section headings so the model only
    # proposes replace_section edits against headings that actually exist.
    strategy_sections = _extract_strategy_sections(context.get("strategy", ""))
    if strategy_sections:
        context["strategy_sections"] = "\n".join(f"- `{h}`" for h in strategy_sections)
    else:
        context["strategy_sections"] = "(strategy.md has no level-2 sections)"

    # Cross-message awareness: inject recent nudges
    recent_nudge_entries: list[dict] = getattr(args, "recent_nudges", [])
    context["recent_nudges"] = format_recent_nudges(
        recent_nudge_entries,
        empty_text="(none)",
    )

    health_data_text = render_health_data(
        health_data,
        prompt_kind="coach",
        week=week,
    )

    try:
        messages = build_messages(
            context,
            health_data_text,
            baselines=baselines,
            week_complete=week_complete,
        )
    except (KeyError, ValueError) as e:
        logger.error("Failed to render coach_prompt.md template: %s", e)
        sys.exit(1)

    model = getattr(args, "model", DEFAULT_MODEL)
    tools = run_sql_tool() + context_update_tool(allowed_files=["strategy"])
    raw_edits: list[ContextEdit] = []
    narrative_parts: list[str] = []
    max_iterations = MAX_TOOL_ITERATIONS_COACH
    reasoning_effort = _normalize_reasoning_effort(
        getattr(args, "reasoning_effort", "medium")
    )

    logger.info(
        "Calling %s for coaching review (reasoning=%s) ...",
        model,
        reasoning_effort or "off",
    )
    for iteration in range(max_iterations):
        try:
            result = call_llm(
                messages,
                model=model,
                tools=tools,
                reasoning_effort=reasoning_effort,
                conn=conn,
                request_type="coach",
                metadata={
                    "week": week,
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

        # Capture this iteration's narrative text before we move on.
        iter_text = (result.text or "").strip()
        if iter_text and iter_text != "SKIP":
            narrative_parts.append(iter_text)

        if not result.tool_calls:
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

            if fn_name == "update_context":
                edit = context_edit_from_tool_call(tc)
                if edit:
                    raw_edits.append(edit)
                tool_result = "Proposed. User will be asked to confirm."
            elif fn_name == "run_sql":
                logger.info("Coach SQL: %s", args_dict.get("query", "")[:200])
                tool_result = execute_run_sql(Path(args.db), args_dict)
            else:
                tool_result = json.dumps({"error": f"Unknown tool: {fn_name}"})

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                }
            )

    # If the iteration cap was reached while the model still wanted to call
    # tools, force one final tool-less synthesis call.
    if not (result.text or "").strip() and result.tool_calls:
        logger.warning(
            "Coach loop hit iteration cap with pending tool_calls; forcing final synthesis"
        )
        messages.append(result.raw_message)
        for tc in result.tool_calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(
                        {"error": "tool budget exhausted, synthesize now"}
                    ),
                }
            )
        try:
            result = call_llm(
                messages,
                model=model,
                tools=None,
                reasoning_effort=reasoning_effort,
                conn=conn,
                request_type="coach",
                metadata={
                    "week": week,
                    "iteration": "final_synthesis",
                    "reasoning_effort": reasoning_effort,
                },
            )
        except Exception as e:
            logger.error("Coach final synthesis call failed: %s", e)
            sys.exit(1)
        synthesis_text = (result.text or "").strip()
        if synthesis_text and synthesis_text != "SKIP":
            narrative_parts.append(synthesis_text)

    narrative = "\n\n".join(part for part in narrative_parts if part).strip()

    # Validate edits against the current strategy.md.
    proposals: list[CoachProposal] = []
    for edit in raw_edits:
        try:
            preview = build_edit_preview(CONTEXT_DIR, edit, strict=True)
        except EditPreviewError as exc:
            logger.warning(
                "Dropping invalid coach edit for %s.md (section=%r): %s",
                edit.file,
                edit.section,
                exc,
            )
            continue
        proposals.append(CoachProposal(edit=edit, preview=preview))

    if not proposals and not narrative:
        logger.info("Coach returned SKIP — no strategy changes warranted")
        return (
            CommandResult(text=None, llm_call_id=result.llm_call_id),
            [],
        )

    # Protocol violation fallback: edits but no narrative.
    if not narrative and proposals:
        logger.warning(
            "Coach returned %d edit(s) with empty narrative — "
            "prompt compliance failure; sending fallback wrapper",
            len(proposals),
        )
        narrative = (
            f"Proposing {len(proposals)} strategy update"
            f"{'s' if len(proposals) != 1 else ''} from this week's data "
            "(rationale missing — review the diffs carefully)."
        )

    bundled_text = _format_coach_bundle(narrative, proposals)
    print(bundled_text)

    cmd_result = CommandResult(
        text=bundled_text,
        llm_call_id=result.llm_call_id,
    )
    return cmd_result, proposals


def _format_coach_bundle(narrative: str, proposals: list[CoachProposal]) -> str:
    """Render the consolidated coach review for stdout / Telegram."""
    if not proposals:
        return narrative
    parts: list[str] = []
    if narrative:
        parts.append(narrative)
    parts.append("──────────────")
    for i, proposal in enumerate(proposals, start=1):
        block = (
            f"📋 **Proposed change {i}** — {proposal.edit.file}.md "
            f"_({proposal.edit.section or proposal.edit.action})_\n"
            f"{proposal.edit.summary}\n\n"
            f"```diff\n{proposal.preview}\n```"
        )
        parts.append(block)
    return "\n\n".join(parts)
