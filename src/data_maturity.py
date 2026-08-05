"""How much this profile actually knows about its owner.

Every prompt reads health data that looks the same whether it covers eight
years or eight days. Without an explicit statement of coverage the model infers
maturity from the data's mere presence and infers it generously — it will
compare a beginner's first week to a baseline built from that same week, or
open a report by referring to coaching history that does not exist.

This module states the facts. What to *do* about a thin profile is a coaching
decision and lives in the prompts under ``src/prompts/``.

Public API:
    build_data_maturity — render the maturity block for prompt injection
"""

from __future__ import annotations

import sqlite3

from baselines import days_of_data, metric_sample_counts
from config import BASELINE_MIN_SAMPLES, METRIC_TRUST_WINDOW_DAYS
from llm_context import UNFILLED_CONTEXT

# Below this the profile has not lived through a full week of anything, so
# week-over-week language and weekly plans have nothing to attach to.
_FIRST_WEEK_DAYS = 7


def _describe_history(conn: sqlite3.Connection) -> list[str]:
    """Describe how much health history exists and how densely it is filled."""
    covered = days_of_data(conn)
    if covered == 0:
        return ["- History: none. No health data has been imported yet."]

    row = conn.execute(
        "SELECT MIN(date) AS first, MAX(date) AS last, COUNT(*) AS rows FROM daily"
    ).fetchone()
    lines = [
        f"- History: {covered} days ({row['first']} to {row['last']}), "
        f"{row['rows']} of them carrying data."
    ]
    if covered < _FIRST_WEEK_DAYS:
        lines.append(
            "- This profile has not yet completed a full week, so there is no "
            "previous week to compare against."
        )
    return lines


def _describe_workouts(conn: sqlite3.Connection) -> list[str]:
    """Describe recorded training, including its absence."""
    total = conn.execute("SELECT COUNT(*) AS n FROM workout").fetchone()["n"]
    if not total:
        return [
            "- Workouts recorded: none. Nothing here shows whether this person "
            "trains; absence of workouts is not evidence that they do not."
        ]
    categories = [
        f"{row['category'] or 'uncategorised'} ×{row['n']}"
        for row in conn.execute(
            "SELECT category, COUNT(*) AS n FROM workout "
            "GROUP BY category ORDER BY n DESC"
        )
    ]
    return [f"- Workouts recorded: {total} ({', '.join(categories)})."]


def _describe_metrics(conn: sqlite3.Connection) -> list[str]:
    """Split daily metrics into those with enough readings to discuss and not."""
    counts = metric_sample_counts(conn, METRIC_TRUST_WINDOW_DAYS)
    trusted = [label for label, n in counts.items() if n >= BASELINE_MIN_SAMPLES]
    thin = [(label, n) for label, n in counts.items() if 0 < n < BASELINE_MIN_SAMPLES]
    absent = [label for label, n in counts.items() if n == 0]

    lines: list[str] = []
    if trusted:
        lines.append(
            f"- Established metrics (30d): {', '.join(trusted)}. These have "
            "enough readings to describe what is normal for this person."
        )
    else:
        lines.append(
            "- Established metrics (30d): none. No metric yet has "
            f"{BASELINE_MIN_SAMPLES} readings, so nothing here defines a "
            "personal normal."
        )
    if thin:
        rendered = ", ".join(f"{label} ({n})" for label, n in thin)
        lines.append(
            f"- Too few readings to generalise from: {rendered}. Report these "
            "as individual observations, never as trends or averages."
        )
    if absent:
        lines.append(
            f"- Not tracked at all: {', '.join(absent)}. Treat as unavailable "
            "rather than as zero or as a problem."
        )
    return lines


def _describe_relationship(context: dict[str, str] | None) -> list[str]:
    """Describe what the coach has been told and what it has said before."""
    if context is None:
        return []

    lines: list[str] = []
    unfilled = [
        name
        for name in ("me", "strategy")
        if context.get(name, "(not provided)")
        in (UNFILLED_CONTEXT, "(not provided)", "")
    ]
    if unfilled:
        rendered = " and ".join(f"{name}.md" for name in unfilled)
        lines.append(
            f"- Not filled in yet: {rendered}. Do not infer its contents from "
            "the health data, and do not assume goals that were never stated."
        )
    if "strategy" in unfilled:
        lines.append(
            "- There is no weekly plan, so nothing can be adherence-checked "
            "and no session may be prescribed as though one were agreed."
        )

    prior = context.get("history", "(not provided)")
    if prior in (UNFILLED_CONTEXT, "(not provided)", "(none)", ""):
        lines.append(
            "- No coaching history: you have not spoken with this person "
            "before. Do not refer back to earlier reports, predictions, or "
            "commitments."
        )
    return lines


def build_data_maturity(
    conn: sqlite3.Connection,
    context: dict[str, str] | None = None,
) -> str:
    """Render the data-maturity block injected into coaching prompts.

    Args:
        conn: Open SQLite database connection for this profile.
        context: Loaded context files, used to report what the coach has been
            told. Omit to describe stored health data only.

    Returns:
        Markdown describing coverage, metric trust, and relationship history.
    """
    lines = ["## Data Maturity", ""]
    lines.extend(_describe_history(conn))
    lines.extend(_describe_workouts(conn))
    lines.extend(_describe_metrics(conn))
    lines.extend(_describe_relationship(context))
    return "\n".join(lines) + "\n"
