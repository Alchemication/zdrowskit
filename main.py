"""zdrowskit — Apple Health data pipeline.

Subcommands:
    import    Parse a MyHealth export directory and upsert into the database.
    report    Load stored data and print a summary report.
    status    Show the date range and row counts in the database.
    context   Show context files used by insights and their status.
    insights  Generate a personalized LLM-driven health report.
    telegram-setup  Register bot /commands for Telegram autocomplete and menu.

Examples:
    uv run python main.py import --data-dir MyHealth/
        Parse a weekly export and upsert its days into the default database.

    uv run python main.py import --data-dir MyHealth/ --db ./local.db
        Same, but write to a custom database file instead of the default.

    uv run python main.py report
        Current week: summary + per-day breakdown (auto-detects most recent ISO week).

    uv run python main.py report --since 2026-01-01
        Custom date range: summary + per-day breakdown for all matching days.

    uv run python main.py report --history
        All weeks in the DB: one compact summary block per ISO week, no day detail.

    uv run python main.py report --history --since 2026-02-01
        History scoped to a start date.

    uv run python main.py report --json
        Current week as JSON: {"summary": ..., "days": [...]}.

    uv run python main.py report --history --json
        All weeks as a JSON array of {"summary": ..., "days": [...]} objects.

    uv run python main.py report --llm
        LLM mode: current week (detailed) + 3 months of weekly history, as JSON.

    uv run python main.py report --llm --months 6
        LLM mode with 6 months of history.

    uv run python main.py status
        Show how many days and workouts are stored and what date range they cover.

    uv run python main.py insights
        Generate this week's personalized report using Claude Haiku.

    uv run python main.py insights --months 6
        Same, with 6 months of historical context.

    uv run python main.py insights --no-update-history
        Generate report without appending memory to history.md.

    uv run python main.py insights --no-update-baselines
        Generate report without auto-computed baselines from DB.

    uv run python main.py insights --week last
        Report on the previous ISO week (Monday morning flow after Sunday export).

    uv run python main.py insights --explain
        Show context files, assembled prompt, and token usage diagnostics on stderr.

    uv run python main.py insights --email
        Send the report via email (requires RESEND_API_KEY and EMAIL_TO in .env).

    uv run python main.py insights --telegram
        Send the report via Telegram (requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID).

    uv run python main.py insights --model openai/gpt-4o
        Use a different litellm model instead of the default Claude Haiku.

    uv run python main.py context
        Show context files used by insights and whether each exists.

    uv run python main.py nudge --trigger new_data
        Send a short nudge via Telegram (default) for new health data.

    uv run python main.py nudge --trigger log_update --email
        Send a nudge via email responding to a log.md update.

    uv run python main.py nudge --trigger missed_session
        Send a missed-session reminder via Telegram.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Ensure src/ is on the path when running from project root
sys.path.insert(0, str(Path(__file__).parent / "src"))

from commands import (
    cmd_coach,
    cmd_context,
    cmd_daemon_restart,
    cmd_daemon_stop,
    cmd_import,
    cmd_insights,
    cmd_llm_log,
    cmd_nudge,
    cmd_report,
    cmd_status,
    cmd_telegram_setup,
)
from llm import DEFAULT_MODEL
from log import setup_logging
from store import default_db_path


def main() -> None:
    """Entry point: parse CLI args and dispatch to the appropriate subcommand."""
    load_dotenv()
    setup_logging()

    db_default = str(default_db_path())
    parser = argparse.ArgumentParser(
        description="Apple Health data pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=db_default,
        help=f"Path to SQLite database (default: {db_default}, or zdrowskit_DB env var)",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_db(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--db",
            metavar="PATH",
            default=db_default,
            help=f"Path to SQLite database (default: {db_default})",
        )

    # import
    p_import = sub.add_parser(
        "import", help="Parse export directory and upsert into DB"
    )
    p_import.add_argument("--data-dir", metavar="PATH", help="Path to data folder")
    p_import.add_argument(
        "--source",
        choices=["shortcuts", "autoexport"],
        default="autoexport",
        help="Data source format (default: autoexport)",
    )
    _add_db(p_import)

    # report
    p_report = sub.add_parser("report", help="Load from DB and print summary")
    p_report.add_argument(
        "--since", metavar="DATE", help="Start date (inclusive), e.g. 2026-01-01"
    )
    p_report.add_argument(
        "--until", metavar="DATE", help="End date (inclusive), e.g. 2026-03-15"
    )
    p_report.add_argument(
        "--history",
        action="store_true",
        help="One summary per ISO week, no daily detail",
    )
    p_report.add_argument(
        "--llm",
        action="store_true",
        help="JSON: current week detailed + N months weekly history",
    )
    p_report.add_argument(
        "--months",
        type=int,
        default=3,
        metavar="N",
        help="History depth for --llm (default: 3)",
    )
    p_report.add_argument("--json", action="store_true", help="Output JSON")
    _add_db(p_report)

    # status
    p_status = sub.add_parser("status", help="Show date range and row counts in DB")
    _add_db(p_status)

    # context
    sub.add_parser(
        "context", help="Show context files used by insights and their status"
    )

    # insights
    p_insights = sub.add_parser(
        "insights", help="LLM-driven personalized health report"
    )
    p_insights.add_argument(
        "--data-dir", metavar="PATH", help="Path to MyHealth folder"
    )
    p_insights.add_argument(
        "--months",
        type=int,
        default=6,
        metavar="N",
        help="History depth in months (default: 6)",
    )
    p_insights.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        metavar="MODEL",
        help=f"litellm model string (default: {DEFAULT_MODEL})",
    )
    p_insights.add_argument(
        "--week",
        choices=["current", "last"],
        default="current",
        help=(
            "Which week to report on. "
            "'current' (default): this ISO week so far — use for mid-week "
            "progress checks. "
            "'last': the previous ISO week — use on Monday morning after "
            "exporting Sunday's data to get a full weekly review."
        ),
    )
    p_insights.add_argument(
        "--no-update-baselines",
        action="store_true",
        help="Skip auto-computed baselines (30/90-day rolling averages from DB)",
    )
    p_insights.add_argument(
        "--no-update-history",
        action="store_true",
        help="Do not append memory to history.md after generation",
    )
    p_insights.add_argument(
        "--explain",
        action="store_true",
        help="Show context, prompt, and LLM call diagnostics on stderr",
    )
    p_insights.add_argument(
        "--email",
        action="store_true",
        help="Send report via email (requires RESEND_API_KEY and EMAIL_TO in .env)",
    )
    p_insights.add_argument(
        "--telegram",
        action="store_true",
        help=(
            "Send report via Telegram "
            "(requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env)"
        ),
    )
    _add_db(p_insights)

    # daemon
    sub.add_parser("daemon-restart", help="Restart the background daemon service")
    sub.add_parser("daemon-stop", help="Stop the background daemon service")

    # telegram
    sub.add_parser(
        "telegram-setup",
        help="Register bot commands for Telegram autocomplete and menu button",
    )

    # nudge
    p_nudge = sub.add_parser(
        "nudge", help="Send a short context-aware notification (default: Telegram)"
    )
    p_nudge.add_argument(
        "--trigger",
        choices=[
            "new_data",
            "log_update",
            "goal_updated",
            "plan_updated",
            "missed_session",
        ],
        default="new_data",
        help="What triggered the nudge (default: new_data)",
    )
    p_nudge.add_argument(
        "--months",
        type=int,
        default=1,
        metavar="N",
        help="History depth in months (default: 1)",
    )
    p_nudge.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        metavar="MODEL",
        help=f"litellm model string (default: {DEFAULT_MODEL})",
    )
    p_nudge.add_argument(
        "--email",
        action="store_true",
        help="Send via email instead of Telegram",
    )
    p_nudge.add_argument(
        "--telegram",
        action="store_true",
        help="Send via Telegram (default when no flag given)",
    )
    _add_db(p_nudge)

    # coach
    p_coach = sub.add_parser(
        "coach", help="Generate coaching proposals for plan/goal updates"
    )
    p_coach.add_argument(
        "--week",
        choices=["current", "last"],
        default="last",
        help="Which week to review (default: last)",
    )
    p_coach.add_argument(
        "--months",
        type=int,
        default=3,
        metavar="N",
        help="History depth in months (default: 3)",
    )
    p_coach.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        metavar="MODEL",
        help=f"litellm model string (default: {DEFAULT_MODEL})",
    )
    p_coach.add_argument(
        "--email",
        action="store_true",
        help="Send coaching review via email",
    )
    p_coach.add_argument(
        "--telegram",
        action="store_true",
        help="Send coaching review via Telegram",
    )
    _add_db(p_coach)

    # llm-log
    p_llm_log = sub.add_parser(
        "llm-log", help="Query LLM call history from the database"
    )
    p_llm_log.add_argument(
        "--last",
        type=int,
        default=10,
        metavar="N",
        help="Number of recent calls to show (default: 10)",
    )
    p_llm_log.add_argument(
        "--id",
        type=int,
        metavar="ID",
        help="Show full detail for a specific call by row ID",
    )
    p_llm_log.add_argument(
        "--stats",
        action="store_true",
        help="Show aggregate usage summary by request type and model",
    )
    p_llm_log.add_argument("--json", action="store_true", help="Output JSON")
    _add_db(p_llm_log)

    args = parser.parse_args()

    dispatch = {
        "import": cmd_import,
        "report": cmd_report,
        "status": cmd_status,
        "context": cmd_context,
        "insights": cmd_insights,
        "nudge": cmd_nudge,
        "coach": cmd_coach,
        "llm-log": cmd_llm_log,
        "daemon-restart": cmd_daemon_restart,
        "daemon-stop": cmd_daemon_stop,
        "telegram-setup": cmd_telegram_setup,
    }
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
