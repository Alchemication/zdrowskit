"""Subcommand handlers for the zdrowskit CLI.

Public API:
    cmd_import   — parse export dir and upsert into DB.
    cmd_report   — load from DB and print summary.
    cmd_status   — show DB row counts and date range.
    cmd_context  — show context files and their status.
    cmd_insights — generate LLM-driven health report.
    cmd_nudge    — send a short context-aware notification.
    cmd_llm_log  — query LLM call history from the database.
    cmd_daemon_restart — restart the launchd daemon service.

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
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from aggregator import summarise
from assembler import assemble
from baselines import compute_baselines
from config import CONTEXT_DIR, NUDGES_DIR, REPORTS_DIR, resolve_data_dir
from llm import (
    DEFAULT_MODEL,
    DEFAULT_SOUL,
    LLMResult,
    append_history,
    build_llm_data,
    build_messages,
    call_llm,
    extract_memory,
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


def _update_me_baselines(me_path: Path, baselines: str) -> None:
    """Replace or append the auto-computed baselines section in me.md.

    Looks for a line starting with '## Baselines' and replaces everything
    from that heading to the next '## ' heading (or EOF). If no such
    section exists, appends the baselines at the end of the file.

    Args:
        me_path: Path to the me.md context file.
        baselines: Markdown string produced by compute_baselines().
    """
    if not me_path.exists():
        logger.warning("me.md not found at %s — skipping baselines update", me_path)
        return

    content = me_path.read_text(encoding="utf-8")

    # Match from '## Baselines' to next '## ' heading or EOF
    pattern = r"(?m)^## Baselines.*?(?=^## |\Z)"
    if re.search(pattern, content, flags=re.DOTALL):
        updated = re.sub(pattern, baselines.rstrip() + "\n\n", content, flags=re.DOTALL)
    else:
        updated = content.rstrip() + "\n\n" + baselines.rstrip() + "\n"

    me_path.write_text(updated, encoding="utf-8")
    logger.info("Updated baselines in %s", me_path)


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


def _save_nudge(text: str, trigger: str) -> Path:
    """Save a nudge to a timestamped markdown file.

    Args:
        text: The nudge text (including model signature).
        trigger: The trigger type that prompted the nudge.

    Returns:
        The path to the saved file.
    """
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
        _update_me_baselines(CONTEXT_DIR / "me.md", baselines)

    health_data_json = json.dumps(health_data, indent=2)

    try:
        messages = build_messages(context, health_data_json, baselines=baselines)
    except (KeyError, ValueError) as e:
        logger.error("Failed to render prompt.md template: %s", e)
        sys.exit(1)

    logger.info("Calling %s ...", args.model)
    try:
        result = call_llm(
            messages,
            model=args.model,
            conn=conn,
            request_type="insights",
            metadata={"week": args.week, "months": args.months},
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

    memory = extract_memory(result.text)
    visible_report = result.text
    if memory:
        visible_report = re.sub(
            r"\s*<memory>.*?</memory>\s*", "", result.text, flags=re.DOTALL
        ).strip()

    # Append model signature
    visible_report += f"\n\n---\n*Generated by {result.model}*"

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


def cmd_nudge(
    args: argparse.Namespace,
    trigger_type: str | None = None,
) -> str | None:
    """Handle the 'nudge' subcommand: send a short context-aware notification.

    Nudge does not update history.md or baselines. It is designed for short
    reactive notifications triggered by file changes or missed sessions.
    Sent nudges are saved to the Reports directory for debugging and review.

    Delivery defaults to Telegram. Pass --email or --telegram to override.

    If the LLM determines there is nothing new worth saying, it responds with
    "SKIP" and this function returns False without sending anything.

    Args:
        args: Parsed CLI arguments with db, model, email, telegram, trigger,
              months, and optional recent_nudges attributes.
        trigger_type: What triggered the nudge — overrides args.trigger when
            called programmatically (e.g. from the daemon). One of:
            "new_data", "log_update", "goal_updated", "plan_updated",
            "missed_session".

    Returns:
        The nudge text if sent, None if the LLM chose to SKIP or an error
        occurred.
    """
    _trigger = trigger_type or getattr(args, "trigger", "new_data")

    try:
        context = load_context(CONTEXT_DIR)
    except FileNotFoundError as e:
        logger.error("%s", e)
        sys.exit(1)

    nudge_prompt_path = CONTEXT_DIR / "nudge_prompt.md"
    if not nudge_prompt_path.exists():
        logger.error(
            "nudge_prompt.md not found at %s. "
            "Copy examples/context/nudge_prompt.md to %s/ to get started.",
            nudge_prompt_path,
            CONTEXT_DIR,
        )
        sys.exit(1)
    nudge_prompt = nudge_prompt_path.read_text(encoding="utf-8")

    conn = open_db(Path(args.db))
    health_data = build_llm_data(conn, getattr(args, "months", 1))
    health_data_json = json.dumps(health_data, indent=2)

    soul = context.get("soul", "")
    if not soul or soul == "(not provided)":
        soul = DEFAULT_SOUL

    recent_nudge_entries: list[dict] = getattr(args, "recent_nudges", [])
    if recent_nudge_entries:
        recent_nudges_text = "\n".join(
            f"{i + 1}. [{e['ts'][:16]} / {e['trigger']}] {e['text']}"
            for i, e in enumerate(recent_nudge_entries)
        )
    else:
        recent_nudges_text = "(none yet)"

    placeholders: dict = defaultdict(lambda: "(not provided)")
    placeholders.update(
        {
            "me": context.get("me", "(not provided)"),
            "goals": context.get("goals", "(not provided)"),
            "plan": context.get("plan", "(not provided)"),
            "log": context.get("log", "(not provided)"),
            "history": context.get("history", "(not provided)"),
            "health_data": health_data_json,
            "today": date.today().isoformat(),
            "weekday": date.today().strftime("%A"),
            "trigger_type": _trigger,
            "recent_nudges": recent_nudges_text,
        }
    )
    user_content = nudge_prompt.format_map(placeholders)
    messages = [
        {"role": "system", "content": soul},
        {"role": "user", "content": user_content},
    ]

    model = getattr(args, "model", DEFAULT_MODEL)
    logger.info("Calling %s for nudge (trigger: %s) ...", model, _trigger)
    try:
        result = call_llm(
            messages,
            model=model,
            conn=conn,
            request_type="nudge",
            metadata={"trigger_type": _trigger},
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

    nudge_text = result.text.strip()

    if nudge_text.upper() == "SKIP":
        logger.info("Nudge skipped by LLM — nothing new to say (trigger: %s)", _trigger)
        return None

    nudge_text += f"\n\n---\n*Generated by {result.model} — trigger: {_trigger}*"

    _save_nudge(nudge_text, _trigger)
    print(nudge_text)

    use_email = getattr(args, "email", False)
    use_telegram = getattr(args, "telegram", False)
    if not use_email and not use_telegram:
        use_telegram = True  # Default channel

    subject = f"zdrowskit — {_trigger.replace('_', ' ')}"
    if use_email:
        send_email(nudge_text, subject)
    if use_telegram:
        send_telegram(nudge_text, subject)

    return nudge_text


def cmd_llm_log(args: argparse.Namespace) -> None:
    """Handle the 'llm-log' subcommand: query LLM call history from the database.

    Three modes:
      default   — list recent calls with summary info (last N, default 10).
      --stats   — aggregate usage summary by request type and model.
      --id N    — show full detail for a specific call.

    Args:
        args: Parsed CLI arguments with db, last, stats, id, and json attributes.
    """
    conn = open_db(Path(args.db))

    # --- Detail mode ---
    if args.id:
        row = conn.execute("SELECT * FROM llm_call WHERE id = ?", (args.id,)).fetchone()
        if row is None:
            print(f"No LLM call found with id={args.id}")
            sys.exit(1)

        if args.json:
            detail = {k: row[k] for k in row.keys()}
            print(json.dumps(detail, indent=2))
            return

        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table

        console = Console()

        meta_table = Table(title=f"LLM Call #{row['id']}", show_lines=False)
        meta_table.add_column("Field", style="cyan")
        meta_table.add_column("Value")
        meta_table.add_row("Timestamp", row["timestamp"])
        meta_table.add_row("Request type", row["request_type"])
        meta_table.add_row("Model", row["model"])
        meta_table.add_row("Input tokens", f"{row['input_tokens']:,}")
        meta_table.add_row("Output tokens", f"{row['output_tokens']:,}")
        meta_table.add_row("Total tokens", f"{row['total_tokens']:,}")
        meta_table.add_row("Latency", f"{row['latency_s']:.1f}s")
        if row["cost"] is not None:
            meta_table.add_row("Cost", f"${row['cost']:.4f}")
        else:
            meta_table.add_row("Cost", "unavailable")

        if row["params_json"]:
            meta_table.add_row("Params", row["params_json"])
        if row["metadata_json"]:
            meta_table.add_row("Metadata", row["metadata_json"])
        console.print(meta_table)

        messages = json.loads(row["messages_json"])
        for msg in messages:
            content = msg["content"]
            max_chars = 2000
            if len(content) > max_chars:
                content = (
                    content[:max_chars] + f"\n\n… ({len(msg['content']):,} chars total)"
                )
            console.print(
                Panel(content, title=f"[bold]{msg['role']}[/bold]", border_style="dim")
            )

        response = row["response_text"]
        if len(response) > 3000:
            response = (
                response[:3000] + f"\n\n… ({len(row['response_text']):,} chars total)"
            )
        console.print(
            Panel(response, title="[bold]Response[/bold]", border_style="green")
        )
        return

    # --- Stats mode ---
    if args.stats:
        rows = conn.execute(
            """
            SELECT
                request_type,
                model,
                COUNT(*)           AS calls,
                SUM(input_tokens)  AS total_input,
                SUM(output_tokens) AS total_output,
                SUM(total_tokens)  AS total_tokens,
                AVG(latency_s)     AS avg_latency,
                SUM(cost)          AS total_cost,
                MIN(timestamp)     AS first_call,
                MAX(timestamp)     AS last_call
            FROM llm_call
            GROUP BY request_type, model
            ORDER BY last_call DESC
            """
        ).fetchall()

        if not rows:
            print("No LLM calls logged yet.")
            return

        if args.json:
            output = [{k: r[k] for k in r.keys()} for r in rows]
            print(json.dumps(output, indent=2))
            return

        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title="LLM Usage Summary", show_lines=False)
        table.add_column("Type", style="cyan")
        table.add_column("Model", style="dim")
        table.add_column("Calls", justify="right")
        table.add_column("Input tok", justify="right")
        table.add_column("Output tok", justify="right")
        table.add_column("Avg latency", justify="right")
        table.add_column("Cost", justify="right", style="green")
        table.add_column("Last call")

        grand_cost = 0.0
        grand_calls = 0
        for r in rows:
            cost = r["total_cost"] or 0.0
            grand_cost += cost
            grand_calls += r["calls"]
            table.add_row(
                r["request_type"],
                r["model"],
                f"{r['calls']:,}",
                f"{r['total_input']:,}",
                f"{r['total_output']:,}",
                f"{r['avg_latency']:.1f}s",
                f"${cost:.4f}" if r["total_cost"] is not None else "—",
                r["last_call"][:16],
            )

        console.print(table)
        console.print(f"\n[bold]Total:[/bold] {grand_calls} calls, ${grand_cost:.4f}")
        return

    # --- List mode (default) ---
    rows = conn.execute(
        """
        SELECT id, timestamp, request_type, model,
               input_tokens, output_tokens, total_tokens, latency_s,
               cost, metadata_json
        FROM llm_call
        ORDER BY id DESC
        LIMIT ?
        """,
        (args.last,),
    ).fetchall()

    if not rows:
        print("No LLM calls logged yet.")
        return

    if args.json:
        output = [{k: r[k] for k in r.keys()} for r in rows]
        print(json.dumps(output, indent=2))
        return

    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title=f"Recent LLM Calls (last {args.last})", show_lines=False)
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Timestamp")
    table.add_column("Type", style="cyan")
    table.add_column("Model", style="dim")
    table.add_column("In tok", justify="right")
    table.add_column("Out tok", justify="right")
    table.add_column("Latency", justify="right")
    table.add_column("Cost", justify="right", style="green")
    table.add_column("Metadata", style="dim")

    for r in rows:
        meta = r["metadata_json"] or ""
        table.add_row(
            str(r["id"]),
            r["timestamp"][:16],
            r["request_type"],
            r["model"].split("/")[-1],
            f"{r['input_tokens']:,}",
            f"{r['output_tokens']:,}",
            f"{r['latency_s']:.1f}s",
            f"${r['cost']:.4f}" if r["cost"] is not None else "—",
            meta,
        )

    console.print(table)


def cmd_daemon_restart(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Restart the launchd daemon service.

    Args:
        args: Parsed CLI arguments (unused).
    """
    import subprocess

    label = "com.zdrowskit.daemon"
    uid = subprocess.check_output(["id", "-u"]).decode().strip()
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"Daemon restarted (gui/{uid}/{label})")
    else:
        print(f"Failed to restart daemon: {result.stderr.strip()}")
        sys.exit(1)
