"""Subcommand handlers for the zdrowskit CLI.

Public API:
    cmd_import   — parse export dir and upsert into DB.
    cmd_report   — load from DB and print summary.
    cmd_status   — show DB row counts and date range.
    cmd_context  — show context files and their status.
    cmd_insights — generate LLM-driven health report.

Example:
    from commands import cmd_import
    cmd_import(args)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from aggregator import summarise
from assembler import assemble
from baselines import compute_baselines
from config import CONTEXT_DIR, REPORTS_DIR, resolve_data_dir
from llm import (
    ReportResult,
    append_history,
    build_llm_data,
    build_messages,
    extract_memory,
    generate_report,
    load_context,
)
from notify import send_email, send_telegram
from report import (
    current_week_bounds,
    group_by_week,
    print_daily,
    print_summary,
    to_dict,
)
from store import (
    load_date_range,
    load_snapshots,
    open_db,
    store_snapshots,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _print_explain(
    context: dict[str, str],
    context_dir: Path,
    messages: list[dict[str, str]],
    result: ReportResult,
    memory: str | None,
    baselines: str | None = None,
    report_path: Path | None = None,
) -> None:
    """Print LLM call diagnostics to stderr using rich formatting.

    Args:
        context: Dict from load_context() with file stems as keys.
        context_dir: Path to the context files directory.
        messages: The system + user messages sent to the LLM.
        result: ReportResult from generate_report().
        memory: Extracted memory string, or None.
        baselines: Auto-computed baselines markdown, or None if skipped.
        report_path: Path where the report was saved, or None.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    stderr = Console(stderr=True)

    # Context files
    all_names = ["soul", "me", "goals", "plan", "log", "history", "prompt"]
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
    cost_input = result.input_tokens * 0.80 / 1_000_000
    cost_output = result.output_tokens * 4.00 / 1_000_000
    total_cost = cost_input + cost_output
    stats_table = Table(title="Response Stats", show_lines=False)
    stats_table.add_column("Metric", style="cyan")
    stats_table.add_column("Value", justify="right")
    stats_table.add_row("Input tokens", f"{result.input_tokens:,}")
    stats_table.add_row("Output tokens", f"{result.output_tokens:,}")
    stats_table.add_row("Total tokens", f"{result.total_tokens:,}")
    stats_table.add_row("Latency", f"{result.latency_s:.1f}s")
    stats_table.add_row("Est. cost", f"${total_cost:.4f}")
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


def _save_report(report: str, week: str) -> Path:
    """Save the report to a timestamped markdown file.

    Args:
        report: The visible report text (memory block already stripped).
        week: Which week was reported on ("current" or "last").

    Returns:
        The path to the saved file.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    target_date = date.today()
    if week == "last":
        target_date = target_date - timedelta(days=7)
    iso_week = f"{target_date.isocalendar().year}-W{target_date.isocalendar().week:02d}"
    timestamp = now.strftime("%Y-%m-%d_%H%M")
    filename = f"{iso_week}_{timestamp}.md"

    path = REPORTS_DIR / filename
    path.write_text(report, encoding="utf-8")
    logger.info("Report saved to %s", path)
    return path


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_import(args: argparse.Namespace) -> None:
    """Handle the 'import' subcommand: parse export dir and upsert into DB.

    Args:
        args: Parsed CLI arguments with data_dir and db attributes.
    """
    data_dir = resolve_data_dir(args.data_dir)
    if not data_dir.exists():
        logger.error("data directory not found: %s", data_dir)
        sys.exit(1)

    logger.info("Loading data from: %s", data_dir)
    snapshots = assemble(data_dir)
    if not snapshots:
        logger.warning("No snapshots parsed from %s", data_dir)
        return

    conn = open_db(Path(args.db))
    n = store_snapshots(conn, snapshots)
    dr = load_date_range(conn)
    date_info = f"{dr[0]} – {dr[1]}" if dr else "unknown"
    print(f"Stored {n} day(s) from {snapshots[0].date} – {snapshots[-1].date}")
    print(f"Database now covers: {date_info}")


def cmd_report(args: argparse.Namespace) -> None:
    """Handle the 'report' subcommand: load from DB and print summary.

    Three modes, selected by flags:
      default   — current ISO week (or --since/--until range): summary + daily breakdown.
      --history — all weeks (or scoped): one summary block per ISO week, no daily detail.
      --llm     — JSON only: current week detailed + N months of weekly history.

    Args:
        args: Parsed CLI arguments with db, since, until, history, llm, months,
              and json attributes.
    """
    conn = open_db(Path(args.db))
    dr = load_date_range(conn)
    if dr is None:
        print("Database is empty. Run 'import' first.")
        sys.exit(1)

    # --- LLM mode ---
    if args.llm:
        output = build_llm_data(conn, args.months)
        print(json.dumps(output, indent=2))
        return

    # --- History mode ---
    if args.history:
        snapshots = load_snapshots(conn, start=args.since, end=args.until)
        if not snapshots:
            print(f"No data in range {args.since or dr[0]} – {args.until or dr[1]}")
            sys.exit(1)
        weeks = group_by_week(snapshots)
        if args.json:
            output = [
                {"summary": to_dict(summarise(w)), "days": [to_dict(s) for s in w]}
                for w in weeks
            ]
            print(json.dumps(output, indent=2))
        else:
            for week_snapshots in weeks:
                print_summary(week_snapshots, summarise(week_snapshots))
        return

    # --- Default mode: current week (or explicit range) ---
    if not args.since and not args.until:
        args.since, args.until = current_week_bounds(dr[1])

    snapshots = load_snapshots(conn, start=args.since, end=args.until)
    if not snapshots:
        print(f"No data in range {args.since} – {args.until}")
        sys.exit(1)

    summary = summarise(snapshots)
    if args.json:
        output = {
            "summary": to_dict(summary),
            "days": [to_dict(s) for s in snapshots],
        }
        print(json.dumps(output, indent=2))
    else:
        print_summary(snapshots, summary)
        print_daily(snapshots)


def cmd_status(args: argparse.Namespace) -> None:
    """Handle the 'status' subcommand: show DB row counts and date range.

    Args:
        args: Parsed CLI arguments with a db attribute.
    """
    conn = open_db(Path(args.db))
    dr = load_date_range(conn)
    if dr is None:
        print("Database is empty.")
        return

    day_count = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    workout_count = conn.execute("SELECT COUNT(*) FROM workout").fetchone()[0]
    print(f"Days stored:   {day_count}  ({dr[0]} – {dr[1]})")
    print(f"Workouts:      {workout_count}")


def cmd_context(args: argparse.Namespace) -> None:
    """Handle the 'context' subcommand: show context files and their status.

    Args:
        args: Parsed CLI arguments (unused, but required by dispatcher).
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()

    # (name, owner, purpose)
    # owner: "you" = user-edited, "auto" = system-managed, "you + auto" = both
    context_files = [
        ("me.md", "you + auto", "Your profile + auto-computed baselines"),
        ("goals.md", "you", "Your fitness goals with timelines"),
        ("plan.md", "you", "Weekly training schedule, diet, sleep targets"),
        ("log.md", "you", "Weekly journal — why things happened"),
        ("soul.md", "you", "AI coach persona — tone, style, philosophy"),
        ("prompt.md", "you", "Prompt template — controls report structure"),
        ("history.md", "auto", "LLM memory — appended after each insights run"),
    ]

    owner_styles = {
        "you": "[green]you edit[/green]",
        "auto": "[blue]auto-managed[/blue]",
        "you + auto": "[green]you edit[/green] + [blue]auto baselines[/blue]",
    }

    table = Table(title="Context Files", show_lines=False)
    table.add_column("File", style="cyan")
    table.add_column("Managed by")
    table.add_column("Size", justify="right")
    table.add_column("Updated", justify="right")
    table.add_column("Purpose", style="dim")

    for name, owner, purpose in context_files:
        path = CONTEXT_DIR / name
        if path.exists():
            size = len(path.read_text(encoding="utf-8"))
            mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
            table.add_row(name, owner_styles[owner], f"{size:,}", mtime, purpose)
        else:
            table.add_row(name, owner_styles[owner], "[red]missing[/red]", "—", purpose)

    console.print(table)
    console.print(f"\n[dim]Context directory:[/dim] [cyan]{CONTEXT_DIR}[/cyan]")

    max_preview_lines = 20

    # Show preview of each file
    for name, _, _ in context_files:
        path = CONTEXT_DIR / name
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            lines = content.splitlines()
            preview = "\n".join(lines[:max_preview_lines])
            if len(lines) > max_preview_lines:
                preview += (
                    f"\n[dim]… ({len(lines) - max_preview_lines} more lines)[/dim]"
                )
            console.print(Panel(preview, title=name, border_style="dim", width=80))


def cmd_insights(args: argparse.Namespace) -> None:
    """Handle the 'insights' subcommand: generate LLM-driven health report.

    Args:
        args: Parsed CLI arguments with db, data_dir, months, model,
              no_update_history, and explain attributes.
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
    if not args.no_update_baselines:
        baselines = compute_baselines(conn)

    health_data_json = json.dumps(health_data, indent=2)

    try:
        messages = build_messages(context, health_data_json, baselines=baselines)
    except (KeyError, ValueError) as e:
        logger.error("Failed to render prompt.md template: %s", e)
        sys.exit(1)

    logger.info("Calling %s ...", args.model)
    try:
        result = generate_report(messages, model=args.model)
    except Exception as e:
        err_name = type(e).__name__
        if "authentication" in err_name.lower() or "auth" in str(e).lower():
            logger.error(
                "Authentication failed. Set ANTHROPIC_API_KEY in your .env file."
            )
        else:
            logger.error("LLM call failed: %s: %s", err_name, e)
        sys.exit(1)

    memory = extract_memory(result.text)
    visible_report = result.text
    if memory:
        visible_report = re.sub(
            r"\s*<memory>.*?</memory>\s*", "", result.text, flags=re.DOTALL
        ).strip()

    report_path = _save_report(visible_report, args.week)

    if args.explain:
        _print_explain(
            context, CONTEXT_DIR, messages, result, memory, baselines, report_path
        )

    print(visible_report)

    if memory and not args.no_update_history:
        append_history(CONTEXT_DIR, memory)
    elif not memory:
        logger.info("No <memory> block in response; history.md unchanged")

    # Push notifications
    target_date = date.today()
    if args.week == "last":
        target_date = target_date - timedelta(days=7)
    iso_week = f"{target_date.isocalendar().year}-W{target_date.isocalendar().week:02d}"
    week_label = f"Week {iso_week} {'Review' if args.week == 'last' else 'Progress'}"

    if args.email:
        send_email(visible_report, week_label)
    if args.telegram:
        send_telegram(visible_report, week_label)
