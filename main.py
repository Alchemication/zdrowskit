"""zdrowskit — Apple Health data pipeline.

Subcommands:
    import    Parse a MyHealth export directory and upsert into the database.
    report    Load stored data and print a summary report.
    status    Show the date range and row counts in the database.
    insights  Generate a personalized LLM-driven health report.

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

    uv run python main.py insights --no-history
        Generate report without appending memory to history.md.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Ensure src/ is on the path when running from project root
sys.path.insert(0, str(Path(__file__).parent / "src"))

from assembler import assemble
from aggregator import summarise
from llm import (
    ReportResult,
    append_history,
    build_messages,
    extract_memory,
    generate_report,
    load_context,
)
from log import setup_logging
from models import DailySnapshot, WeeklySummary
from store import (
    default_db_path,
    load_date_range,
    load_snapshots,
    open_db,
    store_snapshots,
)

logger = logging.getLogger(__name__)


DEFAULT_DATA_DIR = Path.home() / "Documents/zdrowskit/MyHealth"


def _resolve_data_dir(arg: str | None) -> Path:
    """Resolve the data directory from CLI arg, env var, or default path.

    Priority: CLI --data-dir > HEALTH_DATA_DIR env var > DEFAULT_DATA_DIR.

    Args:
        arg: Value of the --data-dir CLI argument, or None if not provided.

    Returns:
        An absolute Path to the resolved data directory.
    """
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("HEALTH_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_DATA_DIR


def _compute_baselines(conn: object) -> str:
    """Compute rolling baseline metrics from the database.

    Calculates 30-day and 90-day averages for key health metrics,
    plus weekly training volume aggregates.

    Args:
        conn: Open SQLite database connection.

    Returns:
        A formatted markdown string with baseline tables.
    """
    lines = ["## Baselines (auto-computed from your data)\n"]

    # Daily metrics — 30-day and 90-day averages
    daily_metrics = [
        ("Resting HR", "resting_hr", "bpm", 0),
        ("HRV (SDNN)", "hrv_ms", "ms", 1),
        ("Recovery Index", "recovery_index", "", 2),
        ("VO2max", "vo2max", "ml/kg/min", 1),
        ("Walking HR", "walking_hr_avg", "bpm", 0),
        ("Steps", "steps", "", 0),
        ("Walking Speed", "walking_speed_kmh", "km/h", 1),
    ]

    lines.append("| Metric | 30-day avg | 90-day avg | Unit |")
    lines.append("|--------|-----------|-----------|------|")

    for label, col, unit, decimals in daily_metrics:
        vals = {}
        for period, days in [("30d", 30), ("90d", 90)]:
            row = conn.execute(
                f"SELECT AVG({col}) FROM daily "  # noqa: S608
                f"WHERE {col} IS NOT NULL "
                f"AND date >= date('now', '-{days} days')",
            ).fetchone()
            vals[period] = row[0] if row and row[0] is not None else None

        fmt_30 = f"{vals['30d']:.{decimals}f}" if vals["30d"] is not None else "—"
        fmt_90 = f"{vals['90d']:.{decimals}f}" if vals["90d"] is not None else "—"
        lines.append(f"| {label} | {fmt_30} | {fmt_90} | {unit} |")

    # Weekly training volume — averages over last 4 and 12 weeks
    lines.append("")
    lines.append("| Training Volume | Last 4 weeks avg | Last 12 weeks avg |")
    lines.append("|-----------------|-------------------|-------------------|")

    volume_queries = [
        (
            "Run distance",
            "km/week",
            "SELECT SUM(gpx_distance_km) FROM workout "
            "WHERE category = 'run' AND gpx_distance_km IS NOT NULL "
            "AND date >= date('now', '-{days} days')",
        ),
        (
            "Run sessions",
            "/week",
            "SELECT COUNT(*) FROM workout "
            "WHERE category = 'run' "
            "AND date >= date('now', '-{days} days')",
        ),
        (
            "Lift sessions",
            "/week",
            "SELECT COUNT(*) FROM workout "
            "WHERE category = 'lift' "
            "AND date >= date('now', '-{days} days')",
        ),
        (
            "Lift duration",
            "min/week",
            "SELECT SUM(duration_min) FROM workout "
            "WHERE category = 'lift' AND duration_min IS NOT NULL "
            "AND date >= date('now', '-{days} days')",
        ),
    ]

    for label, unit, query_template in volume_queries:
        vals = {}
        for period, days, weeks in [("4w", 28, 4), ("12w", 84, 12)]:
            row = conn.execute(query_template.format(days=days)).fetchone()
            total = row[0] if row and row[0] is not None else 0
            vals[period] = total / weeks
        lines.append(
            f"| {label} | {vals['4w']:.1f} {unit} | {vals['12w']:.1f} {unit} |"
        )

    # Best recent pace (from runs with GPX data, last 30 days)
    pace_row = conn.execute(
        "SELECT MIN(duration_min / gpx_distance_km) FROM workout "
        "WHERE category = 'run' AND gpx_distance_km > 0 "
        "AND date >= date('now', '-30 days')"
    ).fetchone()
    if pace_row and pace_row[0] is not None:
        pace = pace_row[0]
        pace_min = int(pace)
        pace_sec = int((pace - pace_min) * 60)
        lines.append(f"\n**Best pace (30d):** {pace_min}:{pace_sec:02d} min/km")

    return "\n".join(lines)


def _to_dict(obj: object) -> object:
    """Recursively convert dataclass instances and lists to plain dicts.

    Args:
        obj: A dataclass instance, a list, or a plain scalar value.

    Returns:
        A JSON-serialisable dict, list, or scalar equivalent of obj.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, list):
        return [_to_dict(i) for i in obj]
    return obj


def _fmt(val: float | int | None, unit: str = "", decimals: int = 1) -> str:
    """Format a numeric value for display, returning '—' for None.

    Args:
        val: The value to format; None, int, or float.
        unit: Optional unit suffix to append, e.g. ' bpm'.
        decimals: Decimal places for float formatting.

    Returns:
        A formatted string, e.g. '52.0 bpm', or '—' when val is None.
    """
    if val is None:
        return "—"
    if isinstance(val, float):
        return f"{val:.{decimals}f}{unit}"
    return f"{val}{unit}"


def print_summary(snapshots: list[DailySnapshot], summary: WeeklySummary) -> None:
    """Print the weekly summary report to stdout.

    Args:
        snapshots: List of DailySnapshot objects for the week.
        summary: WeeklySummary computed from those snapshots.
    """
    print(f"\n{'=' * 60}")
    print(f"  Weekly Summary — {summary.week_label}")
    print(f"{'=' * 60}")

    print(
        f"\nWorkouts ({summary.run_count} runs / {summary.lift_count} lifts / {summary.walk_count} walks)"
    )
    print(f"  Run consistency:   {_fmt(summary.run_consistency_pct, '%', 0)}")
    print(f"  Lift consistency:  {_fmt(summary.lift_consistency_pct, '%', 0)}")
    print(f"  Total run km:      {_fmt(summary.total_run_km, ' km')}")
    print(f"  Best pace:         {_fmt(summary.best_pace_min_per_km, ' min/km')}")
    print(f"  Avg run HR:        {_fmt(summary.avg_run_hr, ' bpm')}")
    print(f"  Peak run HR:       {_fmt(summary.peak_run_hr, ' bpm', 0)}")
    print(f"  Avg elevation gain:{_fmt(summary.avg_elevation_gain_m, ' m')}")
    print(f"  Avg run power:     {_fmt(summary.avg_running_power_w, ' W')}")
    print(f"  Avg stride length: {_fmt(summary.avg_running_stride_m, ' m')}")
    print(f"  Avg run temp:      {_fmt(summary.avg_run_temp_c, '°C')}")
    print(f"  Avg run humidity:  {_fmt(summary.avg_run_humidity_pct, '%', 0)}")
    print(f"  Total lift time:   {_fmt(summary.total_lift_min, ' min')}")
    print(f"  Avg lift HR:       {_fmt(summary.avg_lift_hr, ' bpm')}")

    print("\nActivity Rings (daily averages)")
    print(f"  Steps:             {_fmt(summary.avg_steps)}")
    print(
        f"  Active energy:     {_fmt(summary.avg_active_energy_kj, ' kJ')}  "
        f"({_fmt(summary.avg_active_energy_kj / 4.184 if summary.avg_active_energy_kj else None, ' kcal')})"
    )
    print(f"  Exercise minutes:  {_fmt(summary.avg_exercise_min, ' min')}")
    print(f"  Stand hours:       {_fmt(summary.avg_stand_hours, ' hr')}")

    print("\nCardiac Health")
    print(f"  Resting HR:        {_fmt(summary.avg_resting_hr, ' bpm')}")
    print(
        f"  HRV:               {_fmt(summary.avg_hrv_ms, ' ms')}  (trend: {summary.hrv_trend or '—'})"
    )
    print(f"  Walking HR avg:    {_fmt(summary.avg_walking_hr, ' bpm')}")
    print(f"  VO2max (latest):   {_fmt(summary.latest_vo2max, ' ml/kg·min')}")
    print(f"  Recovery index:    {_fmt(summary.avg_recovery_index)}")
    print()


def _ri_label(ri: float) -> str:
    """Return a human-readable recovery label for a recovery index value.

    Args:
        ri: Recovery index (hrv_ms / resting_hr).

    Returns:
        "low", "normal", or "high".
    """
    if ri < 0.9:
        return "low"
    if ri > 1.5:
        return "high"
    return "normal"


def print_daily(snapshots: list[DailySnapshot]) -> None:
    """Print a per-day breakdown of workouts and key metrics to stdout.

    Each day is printed as two lines: the first shows workout activity,
    the second shows day-wide metrics (steps, cardiac, recovery).

    Args:
        snapshots: List of DailySnapshot objects, printed in order.
    """
    print(f"\n{'─' * 60}")
    print("  Daily Breakdown")
    print(f"{'─' * 60}")
    indent = "              "
    for s in snapshots:
        if s.workouts:
            parts = []
            for w in s.workouts:
                dist = f"  {w.gpx_distance_km:.2f} km" if w.gpx_distance_km else ""
                hr_parts = []
                if w.hr_avg is not None:
                    hr_parts.append(f"avg {w.hr_avg:.0f}")
                if w.hr_max is not None:
                    hr_parts.append(f"max {w.hr_max}")
                hr = f"  HR {' / '.join(hr_parts)}" if hr_parts else ""
                parts.append(f"{w.type}{dist}{hr}")
            print(f"  {s.date}  " + f"\n{indent}".join(parts))
        else:
            print(f"  {s.date}  rest")

        daily_parts = []
        if s.steps is not None:
            daily_parts.append(f"{s.steps:,} steps (day total)")
        if s.resting_hr is not None:
            daily_parts.append(f"RHR {s.resting_hr} bpm")
        if s.hrv_ms is not None:
            daily_parts.append(f"HRV {s.hrv_ms:.1f} ms")
        if s.recovery_index is not None:
            daily_parts.append(
                f"RI {s.recovery_index:.2f} ({_ri_label(s.recovery_index)})"
            )
        print(f"{indent}" + " · ".join(daily_parts))
    print()


def _current_week_bounds(max_date: str) -> tuple[str, str]:
    """Return the Monday–Sunday ISO date strings for the ISO week containing max_date.

    Args:
        max_date: An ISO date string (e.g. "2026-03-15").

    Returns:
        A (monday, sunday) tuple of ISO date strings.
    """
    d = date.fromisoformat(max_date)
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def _group_by_week(snapshots: list[DailySnapshot]) -> list[list[DailySnapshot]]:
    """Group snapshots into ISO-week buckets, sorted chronologically.

    Args:
        snapshots: List of DailySnapshot objects in any order.

    Returns:
        A list of per-week snapshot lists, each sorted by date ascending.
    """
    buckets: dict[str, list[DailySnapshot]] = defaultdict(list)
    for s in snapshots:
        d = date.fromisoformat(s.date)
        iso = d.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        buckets[key].append(s)
    return [sorted(v, key=lambda s: s.date) for _, v in sorted(buckets.items())]


def _build_llm_data(conn: object, months: int, week: str = "current") -> dict:
    """Build the combined current-week + history JSON structure for LLM consumption.

    Args:
        conn: Open SQLite database connection.
        months: Number of months of history to include.
        week: Which week to report on — "current" for the ISO week containing
              today, "last" for the previous ISO week.

    Returns:
        A dict with 'current_week' and 'history' keys, JSON-serialisable.
        Returns empty structure if the database has no data.
    """
    dr = load_date_range(conn)
    if dr is None:
        return {"current_week": {"summary": None, "days": []}, "history": []}

    anchor = date.fromisoformat(dr[1])
    if week == "last":
        anchor = anchor - timedelta(days=7)
    week_start, week_end = _current_week_bounds(anchor.isoformat())
    current_snaps = load_snapshots(conn, start=week_start, end=week_end)

    history_end = (date.fromisoformat(week_start) - timedelta(days=1)).isoformat()
    history_start = (
        date.fromisoformat(week_start) - timedelta(days=30 * months)
    ).isoformat()
    history_snaps = load_snapshots(conn, start=history_start, end=history_end)
    history_weeks = _group_by_week(history_snaps)

    return {
        "current_week": {
            "summary": _to_dict(summarise(current_snaps)) if current_snaps else None,
            "days": [_to_dict(s) for s in current_snaps],
        },
        "history": [{"summary": _to_dict(summarise(w))} for w in history_weeks],
    }


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_import(args: argparse.Namespace) -> None:
    """Handle the 'import' subcommand: parse export dir and upsert into DB.

    Args:
        args: Parsed CLI arguments with data_dir and db attributes.
    """
    data_dir = _resolve_data_dir(args.data_dir)
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
        output = _build_llm_data(conn, args.months)
        print(json.dumps(output, indent=2))
        return

    # --- History mode ---
    if args.history:
        snapshots = load_snapshots(conn, start=args.since, end=args.until)
        if not snapshots:
            print(f"No data in range {args.since or dr[0]} – {args.until or dr[1]}")
            sys.exit(1)
        weeks = _group_by_week(snapshots)
        if args.json:
            output = [
                {"summary": _to_dict(summarise(w)), "days": [_to_dict(s) for s in w]}
                for w in weeks
            ]
            print(json.dumps(output, indent=2))
        else:
            for week_snapshots in weeks:
                print_summary(week_snapshots, summarise(week_snapshots))
        return

    # --- Default mode: current week (or explicit range) ---
    if not args.since and not args.until:
        args.since, args.until = _current_week_bounds(dr[1])

    snapshots = load_snapshots(conn, start=args.since, end=args.until)
    if not snapshots:
        print(f"No data in range {args.since} – {args.until}")
        sys.exit(1)

    summary = summarise(snapshots)
    if args.json:
        output = {
            "summary": _to_dict(summary),
            "days": [_to_dict(s) for s in snapshots],
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


def _print_explain(
    context: dict[str, str],
    context_dir: Path,
    messages: list[dict[str, str]],
    result: ReportResult,
    memory: str | None,
    baselines: str | None = None,
) -> None:
    """Print LLM call diagnostics to stderr using rich formatting.

    Args:
        context: Dict from load_context() with file stems as keys.
        context_dir: Path to the context files directory.
        messages: The system + user messages sent to the LLM.
        result: ReportResult from generate_report().
        memory: Extracted memory string, or None.
        baselines: Auto-computed baselines markdown, or None if skipped.
    """
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel

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
                "[yellow]Skipped[/yellow] (--no-baselines)",
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


def cmd_insights(args: argparse.Namespace) -> None:
    """Handle the 'insights' subcommand: generate LLM-driven health report.

    Args:
        args: Parsed CLI arguments with db, data_dir, months, model,
              no_history, and explain attributes.
    """
    context_dir = Path.home() / "Documents" / "zdrowskit" / "ContextFiles"

    try:
        context = load_context(context_dir)
    except FileNotFoundError as e:
        logger.error("%s", e)
        sys.exit(1)

    conn = open_db(Path(args.db))
    health_data = _build_llm_data(conn, args.months, week=args.week)
    if health_data["current_week"]["summary"] is None:
        logger.error("Database is empty. Run 'import' first.")
        sys.exit(1)

    baselines = None
    if not args.no_baselines:
        baselines = _compute_baselines(conn)

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

    if args.explain:
        _print_explain(context, context_dir, messages, result, memory, baselines)

    print(visible_report)

    if memory and not args.no_history:
        append_history(context_dir, memory)
    elif not memory:
        logger.info("No <memory> block in response; history.md unchanged")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point: parse CLI args and dispatch to the appropriate subcommand."""
    load_dotenv()
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Apple Health data pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    db_default = str(default_db_path())
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
    p_import.add_argument("--data-dir", metavar="PATH", help="Path to MyHealth folder")
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
        default="anthropic/claude-haiku-4-5-20251001",
        metavar="MODEL",
        help="litellm model string (default: claude-haiku-4-5)",
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
        "--no-baselines",
        action="store_true",
        help="Skip auto-computed baselines (30/90-day rolling averages from DB)",
    )
    p_insights.add_argument(
        "--no-history",
        action="store_true",
        help="Do not append memory to history.md after generation",
    )
    p_insights.add_argument(
        "--explain",
        action="store_true",
        help="Show context, prompt, and LLM call diagnostics on stderr",
    )
    _add_db(p_insights)

    args = parser.parse_args()

    if args.cmd == "import":
        cmd_import(args)
    elif args.cmd == "report":
        cmd_report(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "insights":
        cmd_insights(args)


if __name__ == "__main__":
    main()
