"""Non-LLM subcommand handlers for the zdrowskit CLI.

LLM-powered commands live in dedicated modules:
    cmd_insights.py — cmd_insights
    cmd_nudge.py    — cmd_nudge
    cmd_coach.py    — cmd_coach
    cmd_llm_log.py  — cmd_llm_log
    cmd_db.py       — cmd_db

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
import os
import platform
import shutil
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from aggregator import summarise
from assembler import assemble
from config import (
    APP_HOME,
    CONTEXT_DIR,
    resolve_data_dir,
)
from llm_context import load_context
from llm_health import build_llm_data
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

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHD_LABEL = "com.zdrowskit.daemon"
LAUNCHD_PLIST = f"{LAUNCHD_LABEL}.plist"


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


def _copy_file_if_needed(src: Path, dst: Path, *, force: bool = False) -> str:
    """Copy *src* to *dst* when missing, returning a short status label."""
    existed = dst.exists()
    if existed and not force:
        return "exists"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return "updated" if existed else "created"


def _env_has_any_model_key() -> bool:
    """Return True when at least one supported LLM provider key is configured."""
    return any(
        os.environ.get(name)
        for name in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    )


def cmd_setup(args: argparse.Namespace) -> None:
    """Create first-run files and print the remaining manual setup steps.

    Args:
        args: Parsed CLI arguments with force and skip_env attributes.
    """
    force = bool(getattr(args, "force", False))
    skip_env = bool(getattr(args, "skip_env", False))

    print("Setting up zdrowskit")
    APP_HOME.mkdir(parents=True, exist_ok=True)
    (APP_HOME / "Reports").mkdir(parents=True, exist_ok=True)
    (APP_HOME / "Nudges").mkdir(parents=True, exist_ok=True)
    print(f"  app home: {APP_HOME}")

    examples_dir = REPO_ROOT / "examples" / "context"
    if not examples_dir.exists():
        print(f"  context: missing bundled examples at {examples_dir}")
        sys.exit(1)

    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    for src in sorted(examples_dir.glob("*.md")):
        status = _copy_file_if_needed(src, CONTEXT_DIR / src.name, force=force)
        print(f"  {status:7} {CONTEXT_DIR / src.name}")

    if not skip_env:
        env_example = REPO_ROOT / ".env_example"
        env_path = REPO_ROOT / ".env"
        if env_example.exists():
            status = _copy_file_if_needed(env_example, env_path, force=False)
            print(f"  {status:7} {env_path}")
        else:
            print(f"  skipped .env: .env_example not found at {env_example}")

    print("\nNext steps:")
    print(f"  1. Edit {CONTEXT_DIR / 'me.md'}")
    print(f"  2. Edit {CONTEXT_DIR / 'strategy.md'}")
    print("  3. Add at least one LLM API key to .env")
    print("  4. Set up Auto Export on iPhone, then run: uv run python main.py import")
    print("\nCheck readiness any time with: uv run python main.py doctor")


def cmd_doctor(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Print a local readiness checklist without calling external services."""
    checks: list[tuple[str, bool, str, bool]] = []
    checks.append(
        ("Python 3.12+", sys.version_info >= (3, 12), platform.python_version(), True)
    )
    checks.append(
        (
            "uv on PATH",
            shutil.which("uv") is not None,
            shutil.which("uv") or "missing",
            True,
        )
    )

    env_path = REPO_ROOT / ".env"
    checks.append((".env file", env_path.exists(), str(env_path), True))
    checks.append(("app home", APP_HOME.exists(), str(APP_HOME), True))
    checks.append(("context dir", CONTEXT_DIR.exists(), str(CONTEXT_DIR), True))

    for name in ("me.md", "strategy.md", "log.md", "history.md"):
        path = CONTEXT_DIR / name
        checks.append((f"context {name}", path.exists(), str(path), True))

    data_dir = resolve_data_dir(None)
    checks.append(("Auto Export data dir", data_dir.exists(), str(data_dir), True))
    checks.append(
        ("LLM API key", _env_has_any_model_key(), "DEEPSEEK/ANTHROPIC/OPENAI", True)
    )

    telegram_ready = bool(
        os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")
    )
    checks.append(
        ("Telegram config", telegram_ready, "optional but needed for bot", False)
    )

    print("zdrowskit doctor")
    failed = 0
    for label, ok, detail, required in checks:
        marker = "ok" if ok else ("!!" if required else "--")
        if required and not ok:
            failed += 1
        print(f"  [{marker}] {label:22} {detail}")

    if failed:
        print("\nSome checks need attention. Run `uv run python main.py setup` first.")
        sys.exit(1)
    print("\nAll local checks passed.")


def _render_launchd_plist(*, uv_path: Path, project_dir: Path, home: Path) -> str:
    """Return a launchd plist for this checkout and user."""
    daemon_path = project_dir / "src" / "daemon.py"
    log_file = home / "Library" / "Logs" / "zdrowskit.daemon.log"
    path_value = (
        f"{uv_path.parent}:{home}/.local/bin:/opt/homebrew/bin:"
        "/usr/local/bin:/usr/bin:/bin"
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{xml_escape(str(uv_path))}</string>
        <string>run</string>
        <string>python</string>
        <string>{xml_escape(str(daemon_path))}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{xml_escape(str(project_dir))}</string>

    <key>KeepAlive</key>
    <true/>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{xml_escape(str(log_file))}</string>

    <key>StandardErrorPath</key>
    <string>{xml_escape(str(log_file))}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{xml_escape(path_value)}</string>
        <key>HOME</key>
        <string>{xml_escape(str(home))}</string>
    </dict>
</dict>
</plist>
"""


def cmd_daemon_install(args: argparse.Namespace) -> None:
    """Generate and optionally load the per-user launchd plist.

    Args:
        args: Parsed CLI arguments with no_start attribute.
    """
    import subprocess

    uv = shutil.which("uv")
    if uv is None:
        print("uv not found on PATH. Install uv first, then retry daemon-install.")
        sys.exit(1)

    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist = launch_agents / LAUNCHD_PLIST
    plist.write_text(
        _render_launchd_plist(
            uv_path=Path(uv),
            project_dir=REPO_ROOT,
            home=Path.home(),
        ),
        encoding="utf-8",
    )
    print(f"Wrote {plist}")

    if getattr(args, "no_start", False):
        print("Not starting daemon because --no-start was provided.")
        return

    uid = subprocess.check_output(["id", "-u"]).decode().strip()
    domain = f"gui/{uid}"
    target = f"{domain}/{LAUNCHD_LABEL}"
    subprocess.run(["launchctl", "bootout", target], capture_output=True, text=True)
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Failed to start daemon: {result.stderr.strip()}")
        print(
            f"Plist is installed at {plist}; try `uv run python main.py daemon-restart`."
        )
        sys.exit(1)
    print(f"Daemon installed and started ({target})")


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
    {"command": "log", "description": "Log today's context"},
    {"command": "add", "description": "Add workout or sleep"},
    {"command": "clear", "description": "Reset chat memory"},
    {"command": "status", "description": "Show bot/data status"},
    {"command": "advanced", "description": "Show advanced commands"},
]

# Hidden-but-supported commands shown by /advanced.
ADVANCED_TELEGRAM_BOT_COMMANDS: list[dict[str, str]] = [
    {"command": "notify", "description": "Tune notifications"},
    {"command": "review", "description": "Run weekly report"},
    {"command": "coach", "description": "Suggest plan changes"},
    {"command": "models", "description": "Model routing settings"},
    {"command": "context", "description": "View context files"},
    {"command": "events", "description": "Recent system events (nudges, imports, …)"},
    {"command": "tutorial", "description": "Guided tour of zdrowskit"},
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
