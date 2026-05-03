"""LLM-powered weekly and midweek report command."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from baselines import compute_baselines
from charts import ChartResult, extract_charts, render_chart, strip_charts
from cmd_llm_common import (
    CommandResult,
    _apply_verification,
    _hit_token_ceiling,
    _normalize_reasoning_effort,
    _route_kwargs,
    _save_baselines,
)
from config import (
    CONTEXT_DIR,
    MAX_TOKENS_INSIGHTS,
    MAX_TOOL_ITERATIONS_INSIGHTS,
    REPORTS_DIR,
)
from llm import LLMResult, _reasoning_engaged, call_llm, extract_memory
from llm_context import append_history, build_messages, load_context, load_prompt_text
from llm_health import build_llm_data, build_review_facts, render_health_data
from llm_verify import extract_tool_evidence, slim_source_messages
from milestones import compute_milestones
from notify import send_telegram_report
from store import open_db

logger = logging.getLogger(__name__)


def _print_explain(
    context: dict[str, str],
    context_dir: Path,
    messages: list[dict[str, str]],
    result: LLMResult,
    memory: str | None,
    baselines: str | None = None,
    milestones: str | None = None,
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
        milestones: Auto-computed milestones markdown, or None if skipped.
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
    max_tokens = result.max_tokens or MAX_TOKENS_INSIGHTS
    params_table.add_row("Max tokens", f"{max_tokens:,}")
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

    if milestones:
        stderr.print(Panel(milestones, title="Milestones", border_style="cyan"))

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
    milestones = None
    if not args.no_update_baselines and args.week != "current":
        baselines = compute_baselines(conn)
        _save_baselines(CONTEXT_DIR, baselines)
    milestones = compute_milestones(conn)

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
            milestones=milestones,
            week_complete=week_complete,
        )
    except (KeyError, ValueError) as e:
        logger.error("Failed to render insights_prompt.md template: %s", e)
        sys.exit(1)

    from tools import execute_run_sql, run_sql_tool

    tools = run_sql_tool()
    max_iterations = MAX_TOOL_ITERATIONS_INSIGHTS
    reasoning_effort = _normalize_reasoning_effort(
        getattr(args, "reasoning_effort", "medium")
    )
    route = _route_kwargs("insights", getattr(args, "model", None))
    model = route["model"]
    fallback_models = route.get("fallback_models")
    if "reasoning_effort" in route:
        reasoning_effort = route["reasoning_effort"]
    temperature = route.get("temperature", 0.7)

    logger.info("Calling %s (reasoning=%s) ...", model, reasoning_effort or "off")
    for iteration in range(max_iterations):
        try:
            result = call_llm(
                messages,
                model=model,
                max_tokens=MAX_TOKENS_INSIGHTS,
                temperature=temperature,
                tools=tools,
                reasoning_effort=reasoning_effort,
                fallback_models=fallback_models,
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
                            {"error": load_prompt_text("tool_budget_synthesize")}
                        ),
                    }
                )
        synthesis_attempts = [("final_synthesis", reasoning_effort)]
        # The "no-reasoning" retry only helps when reasoning was actually
        # engaged for this model+effort combination. Skipping the no-op retry
        # avoids doubling cost on routes where reasoning was already off
        # (e.g. DeepSeek with low/medium, or non-reasoning providers).
        if _reasoning_engaged(model, reasoning_effort):
            synthesis_attempts.append(("final_synthesis_no_reasoning", None))
        for label, effort in synthesis_attempts:
            try:
                result = call_llm(
                    messages,
                    model=model,
                    max_tokens=MAX_TOKENS_INSIGHTS,
                    temperature=temperature,
                    tools=None,
                    reasoning_effort=effort,
                    fallback_models=fallback_models,
                    conn=conn,
                    request_type="insights",
                    metadata={
                        "week": args.week,
                        "months": args.months,
                        "iteration": label,
                        "reasoning_effort": effort,
                    },
                )
            except Exception as e:
                logger.error("Final synthesis call failed: %s", e)
                sys.exit(1)
            if result.text.strip():
                break
            if effort is not None:
                logger.warning(
                    "Insights final synthesis returned empty text; retrying with reasoning disabled"
                )

    if not result.text.strip():
        logger.error(
            "Insights returned an empty report after fallback synthesis; refusing to save a blank report"
        )
        sys.exit(1)

    if _hit_token_ceiling(result):
        logger.warning(
            "Insights response hit max_tokens=%d; retrying concise synthesis",
            MAX_TOKENS_INSIGHTS,
        )
        concise_messages = [
            *messages,
            {
                "role": "user",
                "content": load_prompt_text("insights_truncation_retry"),
            },
        ]
        try:
            result = call_llm(
                concise_messages,
                model=model,
                max_tokens=MAX_TOKENS_INSIGHTS,
                temperature=temperature,
                tools=None,
                reasoning_effort=None,
                fallback_models=fallback_models,
                conn=conn,
                request_type="insights",
                metadata={
                    "week": args.week,
                    "months": args.months,
                    "iteration": "truncation_retry",
                    "reasoning_effort": None,
                },
            )
        except Exception as e:
            logger.error("Concise synthesis retry failed: %s", e)
            sys.exit(1)
        if not result.text.strip():
            logger.error(
                "Insights concise synthesis returned empty text; refusing to save a blank report"
            )
            sys.exit(1)
        if _hit_token_ceiling(result):
            logger.error(
                "Insights concise synthesis still hit max_tokens=%d; refusing to save a truncated report",
                MAX_TOKENS_INSIGHTS,
            )
            sys.exit(1)

    verified_text = _apply_verification(
        kind="insights",
        draft=result.text,
        evidence={
            "health_data_text": health_data_text,
            "review_facts": context.get("review_facts"),
            "baselines": baselines,
            "milestones": milestones,
            "week_complete": week_complete,
            "week_label": week_label,
            "tool_calls": extract_tool_evidence(messages),
        },
        source_messages=slim_source_messages(messages, result.text),
        conn=conn,
        metadata={
            "source_llm_call_id": result.llm_call_id,
            "week": args.week,
            "months": args.months,
            "week_label": week_label,
        },
    )
    if verified_text is None:
        logger.error("Insights verification failed; refusing to save/send report")
        sys.exit(1)
    result.text = verified_text

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
            context,
            CONTEXT_DIR,
            messages,
            result,
            memory,
            baselines,
            milestones,
            report_path,
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
