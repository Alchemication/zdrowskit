"""Inspect and refresh the weekly targets a progress strip is drawn against."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from config import CONTEXT_DIR
from llm_context import load_context
from plan_frame import load_plan_frame
from store import open_db
from weekly_progress import measure_week, render_progress_block, week_label_for
from weekly_targets import (
    StoredTarget,
    clear_targets,
    ensure_weekly_targets,
    extract_goal_text,
    week_start_for,
)

logger = logging.getLogger(__name__)

_NO_GOALS_HELP = """No measurable weekly goals found in strategy.md, so notifications
show no progress strip.

Add a `## Goals` section naming a number a week — "run 30 km", "two strength
sessions", "sleep 7+ hours on 5 of 7 nights" — or a `## Weekly Plan` that
schedules sessions, then run:

    uv run python main.py targets refresh"""


def _provenance_line(item: StoredTarget) -> str:
    """Render one target's number and the sentence it came from."""
    threshold = ""
    if item.spec.needs_threshold and item.threshold is not None:
        threshold = f" (per-day bar {item.threshold:g} {item.spec.threshold_unit})"
    goal = f'  ← "{item.goal_text}"' if item.goal_text else ""
    return f"  {item.slot_label}: {item.target:g} {item.spec.unit}{threshold}{goal}"


def cmd_targets(args: argparse.Namespace) -> None:
    """Handle the 'targets' subcommand: show, refresh, or clear weekly targets.

    Args:
        args: Parsed CLI arguments carrying db, context_dir, model_prefs_path,
            and an optional ``targets_cmd`` of ``refresh`` or ``clear``.
    """
    conn = open_db(Path(args.db))
    today = date.today()
    week_start = week_start_for(today)
    subcommand = getattr(args, "targets_cmd", None)

    if subcommand == "clear":
        removed = clear_targets(conn, week_start)
        print(
            f"Cleared {removed} target(s) for week {week_label_for(week_start)}. "
            "The next notification will derive them again."
        )
        return

    context_dir = Path(getattr(args, "context_dir", CONTEXT_DIR))
    try:
        context = load_context(
            context_dir, prompt_file="insights_prompt", max_history=0, max_log=0
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    strategy_md = context.get("strategy")
    if not extract_goal_text(strategy_md):
        print(_NO_GOALS_HELP)
        return

    targets = ensure_weekly_targets(
        conn,
        strategy_md=strategy_md,
        week_start=week_start,
        model_prefs_path=getattr(args, "model_prefs_path", None),
        force=subcommand == "refresh",
    )
    if not targets:
        print(_NO_GOALS_HELP)
        return

    # The strip's own reduction is reported rather than applied: this command
    # exists to show what the system holds, and a suppressed strip is invisible
    # everywhere else by construction.
    frame = load_plan_frame(conn)

    rings = measure_week(conn, targets, week_start=week_start, today=today)
    block = render_progress_block(rings, week_start=week_start)
    if block:
        # Printed without the markdown fence: the fence exists so Telegram
        # renders a <pre>, and a terminal is already monospace.
        print("\n".join(block.splitlines()[1:-1]))

    print("\nDerived from strategy.md:")
    for item in targets:
        print(_provenance_line(item))

    if frame is not None and frame.mode != "full":
        print(
            f"\nNotifications are currently showing this strip as "
            f"'{frame.mode}': {frame.reason or '(no reason recorded)'}"
        )
        if frame.llm_call_id:
            print(
                f"  Decided by: uv run python main.py llm-log --id {frame.llm_call_id}"
            )

    call_ids = sorted({item.llm_call_id for item in targets if item.llm_call_id})
    if call_ids:
        ids = ", ".join(str(call_id) for call_id in call_ids)
        print(f"\nDerivation call: uv run python main.py llm-log --id {ids}")
