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
    cmd_daemon_stop    — stop the launchd daemon service.
    cmd_coach  — generate coaching proposals for plan/goal updates.
    cmd_telegram_setup — register bot commands for Telegram autocomplete.

Example:
    from commands import cmd_import
    cmd_import(args)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from aggregator import summarise
from assembler import assemble
from baselines import compute_baselines
from db.migrations import (
    apply_migrations,
    discover_migrations,
    get_live_schema,
    list_migrations,
)
from config import (
    CONTEXT_DIR,
    NUDGES_DIR,
    NOTIFICATION_PREFS_PATH,
    PROMPTS_DIR,
    REPORTS_DIR,
    resolve_data_dir,
)
from llm import (
    DEFAULT_MODEL,
    LLMResult,
    append_history,
    build_llm_data,
    build_messages,
    build_review_facts,
    call_llm,
    extract_memory,
    load_context,
    slim_for_prompt,
)
from charts import ChartResult, extract_charts, render_chart, strip_charts
from context_edit import ContextEdit
from notify import send_email, send_telegram, send_telegram_photo, send_telegram_report
from notification_prefs import (
    DEFAULT_NOTIFICATION_PREFS,
    active_temporary_mutes,
    effective_notification_prefs,
    validate_notification_changes,
)
from report import (
    current_week_bounds,
    group_by_week,
    print_daily,
    print_summary,
    to_dict,
)
from store import (
    connect_db,
    load_date_range,
    load_feedback_entries,
    load_feedback_for_call,
    load_snapshots,
    open_db,
    store_snapshots,
)

logger = logging.getLogger(__name__)

_LLM_LOG_NEARBY_WINDOW_S = 120
_LLM_LOG_MAX_PANEL_CHARS = 20000
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
NOTIFY_MODEL = "anthropic/claude-3-5-haiku-latest"


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
    all_names = [
        "soul",
        "me",
        "goals",
        "plan",
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
        health_data_json="{}",
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


def _save_baselines(context_dir: Path, baselines: str) -> None:
    """Write auto-computed baselines to a dedicated baselines.md file.

    Args:
        context_dir: Directory containing the context files.
        baselines: Markdown string produced by compute_baselines().
    """
    path = context_dir / "baselines.md"
    path.write_text(baselines.rstrip() + "\n", encoding="utf-8")
    logger.info("Saved baselines to %s", path)


def _try_parse_json_text(content: str) -> str | None:
    """Pretty-print JSON content when possible.

    Args:
        content: Candidate JSON string.

    Returns:
        Pretty-printed JSON text, or None if parsing fails.
    """
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return json.dumps(parsed, indent=2, sort_keys=True)


def _format_llm_log_content(content: object) -> str:
    """Normalize logged message content for display.

    Args:
        content: Raw message content from the DB payload.

    Returns:
        Display-ready text.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        pretty = _try_parse_json_text(content)
        return pretty if pretty is not None else content
    return json.dumps(content, indent=2, sort_keys=True)


def _clip_llm_log_text(content: str, limit: int = _LLM_LOG_MAX_PANEL_CHARS) -> str:
    """Clip extremely large content for terminal display.

    Args:
        content: Full text to render.
        limit: Maximum characters to keep before clipping.

    Returns:
        Original text when short enough, otherwise a clipped preview with size note.
    """
    if len(content) <= limit:
        return content
    return content[:limit] + f"\n\n… [truncated, {len(content):,} chars total]"


def _normalize_llm_log_transcript(
    messages: list[dict],
    response_text: str,
) -> list[dict[str, object]]:
    """Build a normalized transcript for llm-log detail mode.

    Args:
        messages: Parsed messages_json payload.
        response_text: Final assistant text stored in llm_call.

    Returns:
        A normalized transcript list suitable for JSON output and rich rendering.
    """
    transcript: list[dict[str, object]] = []
    for index, msg in enumerate(messages, start=1):
        role = str(msg.get("role", "unknown"))
        entry: dict[str, object] = {
            "index": index,
            "role": role,
            "content": msg.get("content", ""),
        }
        if role == "assistant" and msg.get("tool_calls"):
            tool_calls: list[dict[str, object]] = []
            for tool_index, tc in enumerate(msg.get("tool_calls", []), start=1):
                fn = tc.get("function") or {}
                tool_calls.append(
                    {
                        "index": tool_index,
                        "id": tc.get("id"),
                        "type": tc.get("type", "function"),
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments", ""),
                    }
                )
            entry["tool_calls"] = tool_calls
        if role == "tool":
            entry["tool_call_id"] = msg.get("tool_call_id")
        transcript.append(entry)

    transcript.append(
        {
            "index": len(messages) + 1,
            "role": "assistant_final",
            "content": response_text,
            "highlighted": True,
        }
    )
    return transcript


def _load_nearby_llm_calls(
    conn,
    target_row,
    window_s: int = _LLM_LOG_NEARBY_WINDOW_S,
) -> list[dict[str, object]]:
    """Load nearby same-type LLM calls around a selected row.

    Args:
        conn: Open SQLite connection.
        target_row: Selected llm_call row.
        window_s: Absolute time window for inclusion.

    Returns:
        Nearby calls, including the selected row, ordered by timestamp then id.
    """
    target_ts = datetime.fromisoformat(target_row["timestamp"])
    rows = conn.execute(
        """
        SELECT id, timestamp, request_type, model, input_tokens, output_tokens,
               total_tokens, latency_s
        FROM llm_call
        WHERE request_type = ?
        ORDER BY timestamp ASC, id ASC
        """,
        (target_row["request_type"],),
    ).fetchall()

    nearby: list[dict[str, object]] = []
    for row in rows:
        row_ts = datetime.fromisoformat(row["timestamp"])
        delta_s = abs((row_ts - target_ts).total_seconds())
        if delta_s > window_s:
            continue
        nearby.append(
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "request_type": row["request_type"],
                "model": row["model"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "total_tokens": row["total_tokens"],
                "latency_s": row["latency_s"],
                "delta_s": round(delta_s, 3),
                "selected": row["id"] == target_row["id"],
            }
        )
    return nearby


def _save_report(report: str, week: str) -> Path:
    """Save the report to a timestamped markdown file.

    Args:
        report: The visible report text (memory block already stripped).
        week: Which week was reported on ("current" or "last").

    Returns:
        The path to the saved file.
    """
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
        args: Parsed CLI arguments with data_dir, source, and db attributes.
    """
    source = getattr(args, "source", "autoexport")
    data_dir = resolve_data_dir(args.data_dir, source=source)
    if not data_dir.exists():
        logger.error("data directory not found: %s", data_dir)
        sys.exit(1)

    logger.info("Loading data from: %s (source=%s)", data_dir, source)
    snapshots = assemble(data_dir, source=source)
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


def cmd_db(args: argparse.Namespace) -> None:
    """Handle the 'db' subcommand family for migration and schema admin."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table

    console = Console(width=140)
    db_path = Path(args.db).expanduser().resolve()

    if args.db_cmd == "migrate":
        conn = connect_db(db_path, migrate=False)
        changes = apply_migrations(conn)
        if not changes:
            console.print(
                Panel(
                    "Database schema is already up to date.",
                    title="DB Migrate",
                    border_style="green",
                )
            )
            return

        table = Table(title="Applied Migrations", show_lines=False)
        table.add_column("Status", style="cyan", no_wrap=True)
        table.add_column("Key", style="magenta", overflow="fold")
        table.add_column("Name", style="green")
        table.add_column("Applied At", style="dim", no_wrap=True)
        for change in changes:
            ts = change.applied_at[:19] if change.applied_at else "—"
            table.add_row(change.status, change.key, change.name, ts)
        console.print(
            Panel(
                f"Applied {len(changes)} migration(s) to [bold]{db_path}[/bold].",
                title="DB Migrate",
                border_style="green",
            )
        )
        console.print(table)
        return

    if not db_path.exists():
        if args.db_cmd == "status":
            console.print(
                Panel(
                    f"Database file does not exist:\n[bold]{db_path}[/bold]",
                    title="DB Status",
                    border_style="yellow",
                )
            )
            table = Table(title="Available Migrations", show_lines=False)
            table.add_column("Status", style="cyan", no_wrap=True)
            table.add_column("Key", style="magenta", overflow="fold")
            table.add_column("Name", style="green")
            for migration in discover_migrations():
                table.add_row("pending", migration.key, migration.name)
            console.print(table)
            return

        if args.db_cmd == "schema":
            console.print(
                Panel(
                    f"Database file does not exist:\n[bold]{db_path}[/bold]",
                    title="DB Schema",
                    border_style="yellow",
                )
            )
            return

    conn = connect_db(db_path, migrate=False)

    if args.db_cmd == "status":
        statuses = list_migrations(conn)
        current = next(
            (status for status in reversed(statuses) if status.status == "applied"),
            None,
        )
        current_label = current.key if current else "(none recorded yet)"
        file_size = db_path.stat().st_size if db_path.exists() else 0
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        used_bytes = (page_count - freelist_count) * page_size
        tables = [
            row[0]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
        ]
        row_counts: dict[str, int] = {}
        for table_name in tables:
            quoted = table_name.replace('"', '""')
            row_counts[table_name] = int(
                conn.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
            )

        table_sizes: dict[str, int] = {}
        dbstat_available = True
        try:
            rows = conn.execute(
                """
                SELECT name, SUM(pgsize) AS size_bytes
                FROM dbstat
                WHERE aggregate = TRUE
                  AND name NOT LIKE 'sqlite_%'
                GROUP BY name
                ORDER BY name
                """
            ).fetchall()
            table_sizes = {
                str(row["name"]): int(row["size_bytes"] or 0)
                for row in rows
                if str(row["name"]) in row_counts
            }
        except sqlite3.DatabaseError:
            dbstat_available = False

        summary = (
            f"[cyan]Database:[/cyan] {db_path}\n"
            f"[cyan]Current migration:[/cyan] {current_label}"
            f"\n[cyan]File size:[/cyan] {_format_bytes(file_size)}"
            f"\n[cyan]Tables:[/cyan] {len(tables)}"
            f"\n[cyan]SQLite pages:[/cyan] {page_count:,} × {page_size:,} B"
            f"\n[cyan]Used / free:[/cyan] {_format_bytes(used_bytes)} / {_format_bytes(freelist_count * page_size)}"
        )
        console.print(Panel(summary, title="DB Status", border_style="blue"))

        object_table = Table(title="Table Stats", show_lines=False)
        object_table.add_column("Table", style="magenta")
        object_table.add_column("Rows", justify="right", style="cyan")
        if dbstat_available:
            object_table.add_column("Approx Size", justify="right", style="green")
            object_table.add_column("Share", justify="right", style="dim")

        total_sized_bytes = sum(table_sizes.values())
        for table_name in tables:
            cells = [table_name, f"{row_counts[table_name]:,}"]
            if dbstat_available:
                size_bytes = table_sizes.get(table_name, 0)
                share = (
                    f"{(size_bytes / total_sized_bytes) * 100:.1f}%"
                    if total_sized_bytes
                    else "0.0%"
                )
                cells.extend([_format_bytes(size_bytes), share])
            object_table.add_row(*cells)
        console.print(object_table)
        if not dbstat_available:
            console.print(
                Panel(
                    "Per-table size estimates are unavailable because SQLite dbstat is not enabled in this runtime.",
                    title="DB Status Note",
                    border_style="yellow",
                )
            )

        table = Table(title="Migration Status", show_lines=False)
        table.add_column("Status", style="cyan", no_wrap=True)
        table.add_column("Key", style="magenta", overflow="fold")
        table.add_column("Name", style="green")
        table.add_column("Applied At", style="dim", no_wrap=True)
        for status in statuses:
            ts = status.applied_at[:19] if status.applied_at else "—"
            table.add_row(status.status, status.key, status.name, ts)
        console.print(table)
        return

    if args.db_cmd == "schema":
        schema = get_live_schema(conn)
        if schema:
            console.print(
                Panel(
                    f"[bold]Database:[/bold] {db_path}",
                    title="DB Schema",
                    border_style="blue",
                )
            )
            console.print(Syntax(schema, "sql", line_numbers=True, word_wrap=False))
        else:
            console.print(
                Panel(
                    "No schema objects found in the database.",
                    title="DB Schema",
                    border_style="yellow",
                )
            )
        return

    raise ValueError(f"Unknown db subcommand: {args.db_cmd}")


def _format_bytes(size_bytes: int) -> str:
    """Format a byte count into a compact human-readable string."""
    value = float(size_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024


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
        ("me.md", "you", "Your physical profile — age, weight, injuries, pace zones"),
        ("goals.md", "you", "Your fitness goals with timelines"),
        ("plan.md", "you", "Weekly training schedule, diet, sleep targets"),
        ("log.md", "you", "Weekly journal — why things happened"),
        ("baselines.md", "auto", "Auto-computed rolling averages from DB"),
        ("history.md", "auto", "LLM memory — appended after each insights run"),
        (
            "coach_feedback.md",
            "auto",
            "Accept/reject history for coach and chat context edit proposals",
        ),
    ]

    owner_styles = {
        "you": "[green]you edit[/green]",
        "auto": "[blue]auto-managed[/blue]",
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
    console.print(f"[dim]Prompts directory:[/dim] [cyan]{PROMPTS_DIR}[/cyan]")

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
    prompt_data = slim_for_prompt(health_data)
    prompt_data.pop("week_complete", None)
    prompt_data.pop("week_label", None)
    health_data_json = json.dumps(prompt_data, indent=2)

    try:
        messages = build_messages(
            context,
            health_data_json,
            baselines=baselines,
            week_complete=week_complete,
        )
    except (KeyError, ValueError) as e:
        logger.error("Failed to render prompt.md template: %s", e)
        sys.exit(1)

    from tools import execute_run_sql, run_sql_tool

    tools = run_sql_tool()
    max_iterations = 3

    logger.info("Calling %s ...", args.model)
    for iteration in range(max_iterations):
        try:
            result = call_llm(
                messages,
                model=args.model,
                tools=tools,
                conn=conn,
                request_type="insights",
                metadata={
                    "week": args.week,
                    "months": args.months,
                    "iteration": iteration,
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

    # Append model signature
    visible_report += f"\n\n---\n_Generated by {result.model}_"

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

    Nudge does not update history.md or baselines. It is designed for short
    reactive notifications triggered by file changes or missed sessions.
    Sent nudges are saved to the Reports directory for debugging and review.

    Delivery defaults to Telegram. Pass --email or --telegram to override.

    If the LLM determines there is nothing new worth saying, it responds with
    "SKIP" and this function returns an empty CommandResult.

    Args:
        args: Parsed CLI arguments with db, model, email, telegram, trigger,
              months, and optional recent_nudges attributes.
        trigger_type: What triggered the nudge — overrides args.trigger when
            called programmatically (e.g. from the daemon). One of:
            "new_data", "log_update", "goal_updated", "plan_updated",
            "missed_session".
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
    health_data_json = json.dumps(slim_for_prompt(health_data), indent=2)

    recent_nudge_entries: list[dict] = getattr(args, "recent_nudges", [])
    if recent_nudge_entries:
        context["recent_nudges"] = "\n".join(
            f"{i + 1}. [{e['ts'][:16]} / {e['trigger']}] {e['text']}"
            for i, e in enumerate(recent_nudge_entries)
        )
    else:
        context["recent_nudges"] = "(none yet)"
    context["trigger_type"] = _trigger

    # Cross-message awareness: inject last coach review
    coach_summary = getattr(args, "last_coach_summary", "")
    coach_date = getattr(args, "last_coach_summary_date", "")
    if coach_summary:
        context["last_coach_summary"] = f"[{coach_date}] {coach_summary}"
    else:
        context["last_coach_summary"] = "(no recent coach review)"

    messages = build_messages(context, health_data_json)

    from tools import execute_run_sql, run_sql_tool

    model = getattr(args, "model", DEFAULT_MODEL)
    tools = run_sql_tool()
    max_iterations = 3

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

    raw_text = result.text.strip()

    # Check for SKIP as the entire response OR as a standalone line.
    # LLMs sometimes reason before/after the SKIP directive.
    if raw_text.upper() == "SKIP" or "\nSKIP\n" in f"\n{raw_text}\n":
        logger.info("Nudge skipped by LLM — nothing new to say (trigger: %s)", _trigger)
        return CommandResult()

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
        "goal_updated": "\U0001f3af Goal Update",
        "plan_updated": "\U0001f4cb Plan Update",
    }
    header = _TRIGGER_HEADERS.get(
        _trigger, f"\U0001f514 {_trigger.replace('_', ' ').title()}"
    )
    nudge_text = f"**{header}**\n\n{nudge_text}"
    nudge_text += f"\n\n---\n_Generated by {result.model}_"

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
        for chart in nudge_charts:
            send_telegram_photo(chart.image_bytes, caption=f"**{chart.title}**")
        telegram_message_id = send_telegram(nudge_text, subject, reply_markup)

    return CommandResult(
        text=nudge_text,
        llm_call_id=result.llm_call_id,
        telegram_message_id=telegram_message_id,
    )


def cmd_coach(
    args: argparse.Namespace,
    reply_markup: dict | None = None,
) -> tuple[CommandResult, list[ContextEdit]]:
    """Generate coaching proposals for plan/goal updates.

    Reviews the week's data against current plan and goals, and proposes
    concrete edits via the ``update_context`` tool. Proposals are returned
    so the daemon can present them as Approve/Reject buttons in Telegram.

    Args:
        args: Parsed CLI arguments with db, model, email, telegram,
              and months attributes.
        reply_markup: Optional Telegram reply markup (e.g. feedback keyboard)
            attached to the last message chunk.

    Returns:
        A tuple of (CommandResult, list_of_edits). The CommandResult contains
        the coaching text, llm_call_id, and telegram_message_id.
    """
    from context_edit import context_edit_from_tool_call
    from llm import context_update_tool
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

    # Cross-message awareness: inject recent nudges
    recent_nudge_entries: list[dict] = getattr(args, "recent_nudges", [])
    if recent_nudge_entries:
        context["recent_nudges"] = "\n".join(
            f"- [{e['ts'][:16]} / {e['trigger']}] {e['text']}"
            for e in recent_nudge_entries
        )
    else:
        context["recent_nudges"] = "(none)"

    prompt_data = slim_for_prompt(health_data)
    prompt_data.pop("week_complete", None)
    prompt_data.pop("week_label", None)
    health_data_json = json.dumps(prompt_data, indent=2)

    try:
        messages = build_messages(
            context,
            health_data_json,
            baselines=baselines,
            week_complete=week_complete,
        )
    except (KeyError, ValueError) as e:
        logger.error("Failed to render coach_prompt.md template: %s", e)
        sys.exit(1)

    model = getattr(args, "model", DEFAULT_MODEL)
    tools = run_sql_tool() + context_update_tool(allowed_files=["plan", "goals"])
    edits: list[ContextEdit] = []
    max_iterations = 3

    logger.info("Calling %s for coaching review ...", model)
    for iteration in range(max_iterations):
        try:
            result = call_llm(
                messages,
                model=model,
                tools=tools,
                conn=conn,
                request_type="coach",
                metadata={"week": week, "iteration": iteration},
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

            if fn_name == "update_context":
                edit = context_edit_from_tool_call(tc)
                if edit:
                    edits.append(edit)
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

    visible_text = (result.text or "").strip()
    visible_text += f"\n\n---\n_Generated by {result.model}_"

    print(visible_text)

    use_email = getattr(args, "email", False)
    use_telegram = getattr(args, "telegram", False)

    telegram_message_id: int | None = None
    if use_email:
        send_email(visible_text, "Coaching Review")
    if use_telegram:
        telegram_message_id = send_telegram(
            visible_text, "Coaching Review", reply_markup
        )

    cmd_result = CommandResult(
        text=visible_text,
        llm_call_id=result.llm_call_id,
        telegram_message_id=telegram_message_id,
    )
    return cmd_result, edits


def cmd_llm_log(args: argparse.Namespace) -> None:
    """Handle the 'llm-log' subcommand: query LLM call history from the database.

    Three modes:
      default   — list recent calls with summary info (last N, default 10).
      --stats   — aggregate usage summary by request type and model.
      --id N    — show full detail for a specific call.
      --feedback — list recent thumbs-down feedback joined to LLM calls.

    Args:
        args: Parsed CLI arguments with db, last, stats, id, feedback,
            and json attributes.
    """
    conn = open_db(Path(args.db))

    # --- Detail mode ---
    if args.id:
        row = conn.execute("SELECT * FROM llm_call WHERE id = ?", (args.id,)).fetchone()
        if row is None:
            print(f"No LLM call found with id={args.id}")
            sys.exit(1)

        messages = json.loads(row["messages_json"])
        transcript = _normalize_llm_log_transcript(messages, row["response_text"])
        nearby_calls = _load_nearby_llm_calls(conn, row)

        if args.json:
            detail = {k: row[k] for k in row.keys()}
            detail["messages"] = messages
            detail["transcript"] = transcript
            detail["nearby_calls"] = nearby_calls
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

        if nearby_calls:
            nearby_table = Table(
                title="Nearby Calls (~2 min, same type)", show_lines=False
            )
            nearby_table.add_column("ID", justify="right", style="dim")
            nearby_table.add_column("When")
            nearby_table.add_column("Model", style="dim")
            nearby_table.add_column("Latency", justify="right")
            nearby_table.add_column("In tok", justify="right")
            nearby_table.add_column("Out tok", justify="right")
            nearby_table.add_column("Delta", justify="right")
            for nearby in nearby_calls:
                row_style = "bold green" if nearby["selected"] else ""
                marker = ">" if nearby["selected"] else ""
                nearby_table.add_row(
                    f"{marker}{nearby['id']}",
                    str(nearby["timestamp"])[:19],
                    str(nearby["model"]).split("/")[-1],
                    f"{float(nearby['latency_s']):.1f}s",
                    f"{int(nearby['input_tokens']):,}",
                    f"{int(nearby['output_tokens']):,}",
                    f"{float(nearby['delta_s']):.0f}s",
                    style=row_style,
                )
            console.print(nearby_table)

        feedback_rows = load_feedback_for_call(conn, args.id)
        if feedback_rows:
            feedback_table = Table(title="Feedback", show_lines=False)
            feedback_table.add_column("ID", justify="right", style="dim")
            feedback_table.add_column("When")
            feedback_table.add_column("Category", style="cyan")
            feedback_table.add_column("Message", style="dim")
            feedback_table.add_column("Reason")
            for feedback in feedback_rows:
                feedback_table.add_row(
                    str(feedback["id"]),
                    feedback["created_at"][:16],
                    feedback["category"],
                    feedback["message_type"],
                    feedback["reason"] or "—",
                )
            console.print(feedback_table)

        for entry in transcript[:-1]:
            role = str(entry["role"])
            content = _clip_llm_log_text(
                _format_llm_log_content(entry.get("content", ""))
            )

            if role == "assistant" and entry.get("tool_calls"):
                sections: list[str] = []
                if content.strip():
                    sections.append(content)
                for tool_call in entry["tool_calls"]:
                    tool_parts = [
                        f"Tool call #{tool_call['index']}",
                        f"Name: {tool_call['name'] or '(unknown)'}",
                    ]
                    if tool_call.get("id"):
                        tool_parts.append(f"ID: {tool_call['id']}")
                    args_text = _clip_llm_log_text(
                        _format_llm_log_content(tool_call.get("arguments", ""))
                    )
                    if args_text.strip():
                        tool_parts.append(f"Arguments:\n{args_text}")
                    sections.append("\n".join(tool_parts))
                content = "\n\n".join(sections).strip()
            elif role == "tool":
                tool_id = entry.get("tool_call_id")
                if tool_id:
                    content = f"Tool call ID: {tool_id}\n\n{content}".strip()

            title = role.replace("_", " ").title()
            border_style = {
                "system": "blue",
                "user": "cyan",
                "assistant": "magenta",
                "tool": "yellow",
            }.get(role, "dim")
            console.print(
                Panel(
                    content or "[dim](empty)[/dim]",
                    title=f"[bold]{title}[/bold]",
                    border_style=border_style,
                )
            )

        final_response = _clip_llm_log_text(
            _format_llm_log_content(transcript[-1]["content"])
        )
        console.print(
            Panel(
                final_response or "[dim](empty)[/dim]",
                title=f"[bold green]Final Response for Call #{row['id']}[/bold green]",
                border_style="green",
            )
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

    # --- Feedback mode ---
    if getattr(args, "feedback", False):
        rows = load_feedback_entries(conn, limit=args.last)
        if not rows:
            print("No feedback logged yet.")
            return

        if args.json:
            output = [{k: row[k] for k in row.keys()} for row in rows]
            print(json.dumps(output, indent=2))
            return

        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title=f"Recent Feedback (last {args.last})", show_lines=False)
        table.add_column("Feedback", justify="right", style="dim")
        table.add_column("When")
        table.add_column("Category", style="cyan")
        table.add_column("Message", style="dim")
        table.add_column("Call", justify="right")
        table.add_column("Type")
        table.add_column("Model", style="dim")
        table.add_column("Reason")

        for row in rows:
            reason = row["reason"] or "—"
            if len(reason) > 80:
                reason = reason[:77] + "..."
            table.add_row(
                str(row["feedback_id"]),
                row["created_at"][:16],
                row["category"],
                row["message_type"],
                str(row["llm_call_id"]),
                row["request_type"],
                row["model"].split("/")[-1],
                reason,
            )

        console.print(table)
        return

    # --- List mode (default) ---
    rows = conn.execute(
        """
        SELECT
            c.id,
            c.timestamp,
            c.request_type,
            c.model,
            c.input_tokens,
            c.output_tokens,
            c.total_tokens,
            c.latency_s,
            c.cost,
            c.metadata_json,
            COUNT(f.id) AS feedback_count
        FROM llm_call AS c
        LEFT JOIN llm_feedback AS f
          ON f.llm_call_id = c.id
        GROUP BY
            c.id, c.timestamp, c.request_type, c.model,
            c.input_tokens, c.output_tokens, c.total_tokens,
            c.latency_s, c.cost, c.metadata_json
        ORDER BY c.id DESC
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
    table.add_column("Feedback", justify="right")
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
            str(r["feedback_count"] or 0),
            meta,
        )

    console.print(table)


def cmd_daemon_restart(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Restart the launchd daemon service.

    If the service is loaded, uses ``kickstart -k`` to restart it.
    If it was previously stopped with ``daemon-stop`` (unloaded),
    re-loads the plist via ``bootstrap``.

    Args:
        args: Parsed CLI arguments (unused).
    """
    import subprocess

    label = "com.zdrowskit.daemon"
    plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    uid = subprocess.check_output(["id", "-u"]).decode().strip()
    target = f"gui/{uid}/{label}"
    domain = f"gui/{uid}"

    # Check if the service is currently loaded.
    info = subprocess.run(
        ["launchctl", "print", target],
        capture_output=True,
        text=True,
    )

    if info.returncode == 0:
        # Service is loaded — kickstart to restart it.
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"Daemon restarted ({target})")
        else:
            print(f"Failed to restart daemon: {result.stderr.strip()}")
            sys.exit(1)
    else:
        # Service was unloaded (e.g. after daemon-stop) — bootstrap it.
        if not plist.exists():
            print(f"Plist not found: {plist}")
            sys.exit(1)
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"Daemon loaded and started ({target})")
        else:
            print(f"Failed to load daemon: {result.stderr.strip()}")
            sys.exit(1)


def cmd_daemon_stop(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Stop the launchd daemon service.

    Uses ``launchctl bootout`` to fully unload the service so launchd
    does not respawn it. Use ``daemon-restart`` to bring it back.

    Args:
        args: Parsed CLI arguments (unused).
    """
    import subprocess

    label = "com.zdrowskit.daemon"
    uid = subprocess.check_output(["id", "-u"]).decode().strip()
    target = f"gui/{uid}/{label}"

    # Check if the service is loaded first.
    info = subprocess.run(
        ["launchctl", "print", target],
        capture_output=True,
        text=True,
    )
    if info.returncode != 0:
        print("Daemon is not loaded.")
        return

    result = subprocess.run(
        ["launchctl", "bootout", target],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"Daemon stopped and unloaded ({target})")
        print("Run 'uv run python main.py daemon-restart' to start it again.")
    else:
        print(f"Failed to stop daemon: {result.stderr.strip()}")
        sys.exit(1)


# Bot commands registered with Telegram for / autocomplete and menu button.
TELEGRAM_BOT_COMMANDS: list[dict[str, str]] = [
    {"command": "clear", "description": "Reset conversation buffer"},
    {"command": "coach", "description": "Run coaching review and propose plan updates"},
    {"command": "notify", "description": "Show or change notification preferences"},
    {"command": "status", "description": "Nudge count, buffer size, last nudge time"},
    {"command": "context", "description": "List context files (add name to view one)"},
    {"command": "help", "description": "Show available commands"},
]


def cmd_telegram_setup(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Register bot commands with Telegram for autocomplete and menu button.

    Calls the ``setMyCommands`` API so the bot's commands appear when the
    user types ``/`` or taps the menu button next to the text field.

    Args:
        args: Parsed CLI arguments (unused).
    """
    import os

    from telegram_bot import TelegramPoller

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
        sys.exit(1)

    poller = TelegramPoller(bot_token=bot_token, chat_id=chat_id)
    if poller.set_my_commands(TELEGRAM_BOT_COMMANDS):
        print("Telegram bot commands registered:")
        for cmd in TELEGRAM_BOT_COMMANDS:
            print(f"  /{cmd['command']} — {cmd['description']}")
        print("\nType / in the chat or tap the menu button to see them.")
    else:
        print("Failed to register commands. Check your bot token.")
        sys.exit(1)
