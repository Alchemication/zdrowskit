"""zdrowskit — Apple Health data pipeline.

Subcommands:
    import    Parse a MyHealth export directory and upsert into the database.
    report    Load stored data and print a summary report.
    status    Show the date range and row counts in the database.
    context   Show context files used by insights and their status.
    insights  Generate a personalized LLM-driven health report.
    telegram-setup  Register bot /commands for Telegram autocomplete and menu.

Examples:
    uv run python main.py import --profile adam --data-dir MyHealth/
        Parse a weekly export into Adam's isolated database.

    uv run python main.py import --data-dir MyHealth/ --db ./local.db
        Same, but write to an existing custom database file.

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

    uv run python main.py db status
        Show migration status for the SQLite database.

    uv run python main.py db schema
        Print the live SQLite schema from the database.

    uv run python main.py insights
        Generate this week's personalized report using the default model.

    uv run python main.py insights --months 6
        Same, with 6 months of historical context.

    uv run python main.py insights --no-update-history
        Generate report without appending memory to history.md.

    uv run python main.py insights --no-update-baselines
        Generate report without auto-computed baselines from DB.

    uv run python main.py insights --explain
        Show context files, assembled prompt, and token usage diagnostics on stderr.

    uv run python main.py insights --telegram
        Send the report via Telegram (requires TELEGRAM_BOT_TOKEN and a profile).

    uv run python main.py insights --model openai/gpt-4o
        Use a different litellm model instead of the default.

    uv run python main.py context
        Show context files used by insights and whether each exists.

    uv run python main.py nudge --trigger new_data
        Send a short nudge via Telegram (default) for new health data.

    uv run python main.py nudge --trigger strategy_updated
        Send a nudge via Telegram acknowledging a strategy.md edit.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Ensure src/ is on the path when running from project root
sys.path.insert(0, str(Path(__file__).parent / "src"))

from cmd_db import cmd_db
from cmd_events import CATEGORIES as EVENT_CATEGORIES, cmd_events
from cmd_coach import cmd_coach
from cmd_llm_common import InsufficientWeekData
from cmd_insights import cmd_insights
from cmd_ingest import cmd_ingest
from cmd_llm_log import cmd_llm_log
from cmd_models import cmd_models
from cmd_nudge import cmd_nudge
from cmd_notify import RESET_TARGETS as NOTIFY_RESET_TARGETS
from cmd_notify import cmd_notify
from config import GOOGLE_DRIVE_SERVICE_ACCOUNT
from commands import (
    cmd_context,
    cmd_daemon_install,
    cmd_daemon_restart,
    cmd_daemon_stop,
    cmd_doctor,
    cmd_import,
    cmd_report,
    cmd_setup,
    cmd_status,
    cmd_telegram_setup,
)
from log import setup_logging
from model_prefs import FEATURES, resolve_model_route
from profiles import ProfileConfigError, cmd_profile, resolve_cli_profile


def main() -> None:
    """Entry point: parse CLI args and dispatch to the appropriate subcommand."""
    load_dotenv()
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Apple Health data pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        dest="global_db",
        metavar="PATH",
        help="Explicit SQLite database path (overrides --profile)",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_db(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--db",
            metavar="PATH",
            help="Explicit SQLite database path (overrides --profile)",
        )
        p.add_argument(
            "--profile",
            metavar="NAME",
            help="Profile name (default: operator profile)",
        )

    # import
    p_import = sub.add_parser(
        "import", help="Parse export directory and upsert into DB"
    )
    p_import.add_argument("--data-dir", metavar="PATH", help="Path to data folder")
    p_import.add_argument(
        "--source",
        choices=("http", "local", "google-drive"),
        default=None,
        help="Import source (default: selected profile configuration)",
    )
    p_import.add_argument(
        "--google-drive-service-account",
        metavar="PATH",
        default=GOOGLE_DRIVE_SERVICE_ACCOUNT,
        help="Service-account JSON path (or ZDROWSKIT_GOOGLE_DRIVE_SERVICE_ACCOUNT)",
    )
    p_import.add_argument(
        "--google-drive-metrics-folder-id",
        metavar="ID",
        default=None,
        help="Metrics folder ID (default: selected profile configuration)",
    )
    p_import.add_argument(
        "--google-drive-workouts-folder-id",
        metavar="ID",
        default=None,
        help="Workouts folder ID (default: selected profile configuration)",
    )
    p_import.add_argument(
        "--google-drive-force",
        action="store_true",
        help="Redownload Drive files even when the local manifest is current",
    )
    p_import.add_argument(
        "--google-drive-timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="Google Drive request timeout (default: 30)",
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

    # db admin
    p_db = sub.add_parser("db", help="Database admin: migrations and schema")
    _add_db(p_db)
    db_sub = p_db.add_subparsers(dest="db_cmd", required=True)
    p_db_status = db_sub.add_parser(
        "status", help="Show migration status for the database"
    )
    p_db_status.add_argument("--all", action="store_true", help="Check every profile")
    p_db_migrate = db_sub.add_parser("migrate", help="Apply pending migrations")
    p_db_migrate.add_argument(
        "--all", action="store_true", help="Migrate every profile database"
    )
    db_sub.add_parser("schema", help="Print the live SQLite schema")

    # context
    p_context = sub.add_parser(
        "context", help="Show context files used by insights and their status"
    )
    p_context.add_argument("--profile", metavar="NAME")

    # first-run setup
    p_setup = sub.add_parser("setup", help="Create first-run files and directories")
    p_setup.add_argument(
        "--skip-env",
        action="store_true",
        help="Do not create .env from .env_example",
    )
    sub.add_parser("doctor", help="Check local setup without calling external APIs")

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
        default=None,
        metavar="MODEL",
        help=f"litellm model string (default: {resolve_model_route('insights').primary})",
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
        "--reasoning-effort",
        choices=["none", "low", "medium", "high"],
        default="medium",
        help=(
            "Extended thinking budget for the model (default: medium). "
            "Use 'none' to disable, 'high' for the deepest analysis."
        ),
    )
    p_insights.add_argument(
        "--telegram",
        action="store_true",
        help=(
            "Send report via Telegram "
            "(requires TELEGRAM_BOT_TOKEN and the selected profile's telegram_id)"
        ),
    )
    _add_db(p_insights)

    # daemon
    p_daemon_install = sub.add_parser(
        "daemon-install", help="Generate and load the launchd daemon plist"
    )
    p_daemon_install.add_argument(
        "--no-start",
        action="store_true",
        help="Write the plist but do not load/start it",
    )
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
            "strategy_updated",
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
        default=None,
        metavar="MODEL",
        help=f"litellm model string (default: {resolve_model_route('nudge').primary})",
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
        default=None,
        metavar="MODEL",
        help=f"litellm model string (default: {resolve_model_route('coach').primary})",
    )
    p_coach.add_argument(
        "--reasoning-effort",
        choices=["none", "low", "medium", "high"],
        default="medium",
        help=(
            "Extended thinking budget for the model (default: medium). "
            "Use 'none' to disable, 'high' for the deepest analysis."
        ),
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
        "--trace",
        type=int,
        metavar="ID",
        help="Show all LLM calls in a trace by trace ID",
    )
    p_llm_log.add_argument(
        "--stats",
        action="store_true",
        help="Show aggregate usage summary by request type and model",
    )
    p_llm_log.add_argument(
        "--feedback",
        action="store_true",
        help="Show recent thumbs-down feedback joined to LLM calls",
    )
    p_llm_log.add_argument("--json", action="store_true", help="Output JSON")
    _add_db(p_llm_log)

    # notify
    p_notify = sub.add_parser("notify", help="Show or reset notification settings")
    p_notify.add_argument("--profile", metavar="NAME")
    notify_sub = p_notify.add_subparsers(dest="notify_cmd")
    notify_sub.add_parser("show", help="Show current notification settings")
    p_notify_reset = notify_sub.add_parser(
        "reset",
        help="Reset notification settings to built-in defaults",
    )
    p_notify_reset.add_argument(
        "target",
        nargs="?",
        default="all",
        choices=NOTIFY_RESET_TARGETS,
        help="Notification target to reset (default: all)",
    )

    # models
    # Derived, never hand-listed: a duplicate of this list is how `memory`
    # shipped as a routable feature that `models set` could not reach.
    feature_choices = list(FEATURES)
    p_models = sub.add_parser("models", help="Show or change LLM model routing")
    p_models.add_argument("--profile", metavar="NAME")
    p_models.add_argument("--json", action="store_true", help="Output JSON")
    models_sub = p_models.add_subparsers(dest="models_cmd")
    p_models_reset = models_sub.add_parser(
        "reset",
        help="Reset a feature route or all routes",
    )
    p_models_reset.add_argument(
        "feature",
        nargs="?",
        choices=feature_choices,
        help="Feature to reset (omit when using --all)",
    )
    p_models_reset.add_argument(
        "--all",
        action="store_true",
        help="Reset every feature and both profiles to built-in defaults",
    )
    p_models_profile = models_sub.add_parser("profile", help="Set a profile route")
    p_models_profile.add_argument("route_profile", choices=["pro", "flash"])
    p_models_profile.add_argument("--primary", required=True, metavar="MODEL")
    p_models_profile.add_argument("--fallback", required=True, metavar="MODEL")
    p_models_set = models_sub.add_parser("set", help="Set a feature route")
    p_models_set.add_argument("feature", choices=feature_choices)
    p_models_set.add_argument("primary", metavar="MODEL")
    p_models_set.add_argument(
        "--fallback",
        metavar="MODEL",
        help="Explicit fallback model, or 'auto' to defer to the profile fallback",
    )
    p_models_set.add_argument(
        "--reasoning",
        choices=["none", "low", "medium", "high"],
        help="Override reasoning effort for this feature",
    )
    p_models_set.add_argument(
        "--temperature",
        metavar="VALUE",
        help="Override temperature as a float, or 'omit'",
    )
    models_sub.add_parser("doctor", help="Check model routing for likely issues")

    # events
    p_events = sub.add_parser(
        "events",
        help="Show system diagnostic events (nudges, imports, coach decisions, …)",
    )
    p_events.add_argument(
        "--category",
        choices=list(EVENT_CATEGORIES),
        help="Filter to a single category",
    )
    p_events.add_argument(
        "--kind",
        metavar="KIND",
        help="Filter to a single kind (e.g. fired, llm_skip, rate_limited)",
    )
    p_events.add_argument(
        "--since",
        metavar="WHEN",
        help="Only events after this point (e.g. '3d', '24h', '2026-04-10')",
    )
    p_events.add_argument(
        "--limit",
        type=int,
        default=100,
        metavar="N",
        help="Maximum rows to show (default: 100)",
    )
    p_events.add_argument(
        "--usage",
        action="store_true",
        help="Aggregate Telegram command and inline-button usage",
    )
    p_events.add_argument("--json", action="store_true", help="Output JSON")
    _add_db(p_events)

    # profile management
    p_profile = sub.add_parser("profile", help="Create or adopt isolated profiles")
    profile_sub = p_profile.add_subparsers(dest="profile_cmd", required=True)
    p_profile_add = profile_sub.add_parser("add", help="Create a profile")
    p_profile_add.add_argument("name")
    p_profile_add.add_argument("--telegram-id", required=True, type=int)
    p_profile_add.add_argument("--operator", action="store_true")
    p_profile_add.add_argument(
        "--source",
        choices=("http", "local", "google-drive"),
        default="http",
    )
    p_profile_add.add_argument("--google-drive-metrics-folder-id", metavar="ID")
    p_profile_add.add_argument("--google-drive-workouts-folder-id", metavar="ID")
    p_profile_adopt = profile_sub.add_parser(
        "adopt", help="Adopt an existing single-profile installation"
    )
    p_profile_adopt.add_argument("name")
    p_profile_adopt.add_argument("--telegram-id", type=int)
    p_profile_adopt.add_argument("--dry-run", action="store_true")
    p_profile_source = profile_sub.add_parser(
        "source", help="Change a profile's health import source"
    )
    p_profile_source.add_argument("name")
    p_profile_source.add_argument("source", choices=("http", "local", "google-drive"))

    # authenticated Auto Export HTTP ingestion
    p_ingest = sub.add_parser("ingest", help="Configure Auto Export HTTP ingestion")
    ingest_sub = p_ingest.add_subparsers(dest="ingest_cmd", required=True)
    p_ingest_setup = ingest_sub.add_parser(
        "setup", help="Create missing tokens and show Auto Export settings"
    )
    p_ingest_setup.add_argument(
        "--funnel",
        action="store_true",
        help="Also start the persistent Tailscale Funnel",
    )
    p_ingest_token = ingest_sub.add_parser(
        "token", help="Issue or rotate one profile upload token"
    )
    p_ingest_token.add_argument("name")
    p_ingest_token.add_argument(
        "--rotate", action="store_true", help="Revoke existing profile tokens"
    )
    ingest_sub.add_parser("status", help="Show receiver and per-profile upload state")

    args = parser.parse_args()

    profile = None
    database_commands = {
        "import",
        "report",
        "status",
        "db",
        "insights",
        "nudge",
        "coach",
        "llm-log",
        "events",
    }
    profile_path_commands = database_commands | {"context", "notify", "models"}
    if args.cmd in profile_path_commands:
        try:
            explicit_db = getattr(args, "db", None) or args.global_db
            if args.cmd in database_commands:
                profile, db_path = resolve_cli_profile(
                    getattr(args, "profile", None),
                    db=explicit_db,
                    require_existing=not (
                        args.cmd == "db"
                        and (args.db_cmd == "status" or getattr(args, "all", False))
                    ),
                )
                args.db = str(db_path)
            else:
                profile, _ = resolve_cli_profile(
                    getattr(args, "profile", None),
                    db=None,
                )
        except ProfileConfigError as exc:
            parser.error(str(exc))
        if profile is not None:
            args.profile_config = profile
            args.context_dir = profile.context
            args.reports_dir = profile.reports
            args.nudges_dir = profile.nudges
            args.notification_prefs_path = profile.notification_prefs
            args.model_prefs_path = profile.model_prefs
            args.telegram_id = profile.telegram_id
            if args.cmd == "import":
                args.source = args.source or profile.import_source
                args.google_drive_metrics_folder_id = (
                    args.google_drive_metrics_folder_id
                    or profile.drive_metrics_folder_id
                )
                args.google_drive_workouts_folder_id = (
                    args.google_drive_workouts_folder_id
                    or profile.drive_workouts_folder_id
                )
                if not args.data_dir:
                    if profile.import_source == "google-drive":
                        args.data_dir = str(profile.drive_cache)
                    elif profile.import_source == "http":
                        args.data_dir = str(profile.http_cache)

    def _cli_coach(coach_args: argparse.Namespace) -> None:
        """CLI wrapper for cmd_coach.

        cmd_coach already prints the bundled review (narrative + per-edit
        diffs) to stdout. The daemon path consumes the returned proposals
        as inline Accept/Reject buttons; in CLI mode there is no such
        delivery, so we just append a footer reminding the user to run via
        Telegram for actionable buttons.
        """
        _, proposals = cmd_coach(coach_args)
        if proposals:
            print(
                "\nNote: CLI mode only previews edits — run via the daemon "
                "(/coach in Telegram) to get Approve/Reject buttons."
            )

    dispatch = {
        "import": cmd_import,
        "report": cmd_report,
        "status": cmd_status,
        "db": cmd_db,
        "context": cmd_context,
        "setup": cmd_setup,
        "doctor": cmd_doctor,
        "insights": cmd_insights,
        "nudge": cmd_nudge,
        "coach": _cli_coach,
        "llm-log": cmd_llm_log,
        "notify": cmd_notify,
        "models": cmd_models,
        "events": cmd_events,
        "daemon-install": cmd_daemon_install,
        "daemon-restart": cmd_daemon_restart,
        "daemon-stop": cmd_daemon_stop,
        "telegram-setup": cmd_telegram_setup,
        "profile": cmd_profile,
        "ingest": cmd_ingest,
    }
    try:
        dispatch[args.cmd](args)
    except ProfileConfigError as exc:
        parser.error(str(exc))
    except InsufficientWeekData as exc:
        # Not a crash: the profile is simply younger than the week it was
        # asked to report on. Say so plainly rather than showing a traceback.
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
