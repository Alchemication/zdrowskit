"""Non-LLM subcommand handlers for the zdrowskit CLI.

LLM-powered commands live in dedicated modules:
    cmd_llm.py     — cmd_insights, cmd_nudge, cmd_coach
    cmd_llm_log.py — cmd_llm_log
    cmd_db.py      — cmd_db

This module contains:
    cmd_import   — parse export dir and upsert into DB.
    cmd_report   — load from DB and print summary.
    cmd_status   — show DB row counts and date range.
    cmd_context  — show context files and their status.
    cmd_daemon_restart — restart the launchd daemon service.
    cmd_daemon_stop    — stop the launchd daemon service.
    cmd_telegram_setup — register bot commands for Telegram autocomplete.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from aggregator import summarise
from assembler import assemble
from config import (
    CONTEXT_DIR,
    resolve_data_dir,
)
from llm import (
    build_llm_data,
    load_context,
)
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


def cmd_context(args: argparse.Namespace) -> None:
    """Handle the 'context' subcommand: show context files and their status.

    Args:
        args: Parsed CLI arguments with a db attribute.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()

    # Context directory listing
    context_dir = CONTEXT_DIR
    if not context_dir.exists():
        console.print(
            Panel(
                f"Context directory not found:\n[bold]{context_dir}[/bold]",
                title="Context Files",
                border_style="yellow",
            )
        )
        return

    try:
        context = load_context(context_dir)
    except FileNotFoundError as e:
        logger.error("%s", e)
        sys.exit(1)

    all_names = [
        "soul",
        "me",
        "strategy",
        "log",
        "history",
        "baselines",
        "coach_feedback",
    ]
    table = Table(title="Context Files", show_lines=False)
    table.add_column("File", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Size", justify="right")
    table.add_column("Path", style="dim")

    for name in all_names:
        file_path = context_dir / f"{name}.md"
        if name in context and context[name] != "(not provided)":
            table.add_row(
                f"{name}.md",
                "loaded",
                f"{len(context[name]):,} chars",
                str(file_path),
            )
        else:
            table.add_row(
                f"{name}.md",
                "[red]missing[/red]",
                "—",
                str(file_path),
            )

    console.print(table)
    console.print(f"\n[dim]Context directory:[/dim] {context_dir}")


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
    {"command": "review", "description": "Weekly report"},
    {"command": "coach", "description": "Coaching review (strategy proposals)"},
    {"command": "add", "description": "Log a workout or sleep"},
    {"command": "status", "description": "Bot and data status"},
    {"command": "notify", "description": "Notification settings"},
    {"command": "context", "description": "View context files"},
    {"command": "clear", "description": "Reset chat memory"},
    {"command": "tutorial", "description": "Guided tour of zdrowskit"},
    {"command": "help", "description": "Command list"},
]


def cmd_telegram_setup(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Register bot commands with Telegram for autocomplete and menu button.

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
