"""Compare a newly synced run with the person's own similar runs.

The comparison a nudge can offer that the phone cannot is how today's run sits
against runs like it. The model used to be asked to write that query itself,
which it did on half of all nudges; most queries re-fetched data already in the
prompt, and one joined sleep to the wrong night over zero-padded history. This
computes the one comparison worth having, gated on sample count, so the model
reads a checked figure instead of deriving one.

Public API:
    describe_run_comparisons — prompt text for the runs a sync brought in
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta

from config import (
    BASELINE_MIN_SAMPLES,
    RUN_COMPARISON_DISTANCE_RATIO,
    RUN_COMPARISON_MIN_KM,
    RUN_COMPARISON_PACE_BAND_MIN,
    RUN_COMPARISON_WINDOW_DAYS,
)
from units import (
    Quantity,
    UnitSystem,
    format_pace,
    format_quantity,
    to_display,
    unit_label,
)

NO_RUN_COMPARISON = "(no run comparison for this sync)"


def _describe_one(conn: sqlite3.Connection, run: sqlite3.Row, units: UnitSystem) -> str:
    """Return one comparison line for a single run."""
    distance = float(run["gpx_distance_km"])
    pace = float(run["duration_min"]) / distance
    hr = float(run["hr_avg"])
    low_km = distance * (1 - RUN_COMPARISON_DISTANCE_RATIO)
    high_km = distance * (1 + RUN_COMPARISON_DISTANCE_RATIO)
    fast = pace - RUN_COMPARISON_PACE_BAND_MIN
    slow = pace + RUN_COMPARISON_PACE_BAND_MIN
    since = (
        date.fromisoformat(run["date"]) - timedelta(days=RUN_COMPARISON_WINDOW_DAYS)
    ).isoformat()

    rows = conn.execute(
        """
        SELECT hr_avg
        FROM workout_all
        WHERE category = 'run'
          AND start_utc < ?
          AND date >= ?
          AND hr_avg IS NOT NULL
          AND gpx_distance_km BETWEEN ? AND ?
          AND duration_min / gpx_distance_km BETWEEN ? AND ?
        """,
        (run["start_utc"], since, low_km, high_km, fast, slow),
    ).fetchall()
    peer_hrs = [float(row["hr_avg"]) for row in rows]

    head = (
        f"- {run['type']} on {run['date']}, {format_quantity(distance, Quantity.DISTANCE, units)} at "
        f"{format_pace(pace, units)}, avg HR {hr:.0f} bpm"
    )
    noun = "similar run" if len(peer_hrs) == 1 else "similar runs"
    band = f"{format_pace(fast, units, suffix=False)}–{format_pace(slow, units)}"
    low_shown = to_display(low_km, Quantity.DISTANCE, units)
    high_shown = to_display(high_km, Quantity.DISTANCE, units)
    distance_unit = unit_label(Quantity.DISTANCE, units)
    scope = (
        f"{noun} in the previous {RUN_COMPARISON_WINDOW_DAYS} days "
        f"({low_shown:.1f}–{high_shown:.1f} {distance_unit}, {band})"
    )
    if len(peer_hrs) < BASELINE_MIN_SAMPLES:
        return (
            f"{head}: only {len(peer_hrs)} {scope} — too few to compare. "
            "Do not describe this run's heart rate as high or low for them."
        )
    median_hr = statistics.median(peer_hrs)
    above = sum(1 for peer in peer_hrs if hr > peer)
    below = sum(1 for peer in peer_hrs if hr < peer)
    return (
        f"{head}: {len(peer_hrs)} {scope} had a median HR of {median_hr:.0f} bpm. "
        f"This run's HR was higher than {above} of them and lower than {below}."
    )


def describe_run_comparisons(
    conn: sqlite3.Connection,
    workout_ids: set[str] | list[str],
    *,
    units: UnitSystem = UnitSystem.METRIC,
) -> str:
    """Describe how each run a sync brought in compares with similar runs.

    Similar means within ``RUN_COMPARISON_DISTANCE_RATIO`` of the distance and
    ``RUN_COMPARISON_PACE_BAND_MIN`` of the pace, over the previous
    ``RUN_COMPARISON_WINDOW_DAYS``. With pace held in a band, heart rate is the
    figure that moves, so that is what is compared. Fewer than
    ``BASELINE_MIN_SAMPLES`` matches is reported as too few rather than
    compared.

    Args:
        conn: Open database connection.
        workout_ids: ``start_utc`` of workouts inserted or changed by the sync.
        units: Display unit system for distances and paces.

    Returns:
        One line per qualifying run, or ``NO_RUN_COMPARISON``.
    """
    ids = sorted(str(value) for value in workout_ids)
    if not ids:
        return NO_RUN_COMPARISON
    placeholders = ", ".join("?" for _ in ids)
    runs = conn.execute(
        f"""
        SELECT start_utc, date, type, duration_min, gpx_distance_km, hr_avg
        FROM workout_all
        WHERE start_utc IN ({placeholders})
          AND category = 'run'
          AND hr_avg IS NOT NULL
          AND gpx_distance_km >= ?
          AND duration_min > 0
        ORDER BY start_utc
        """,  # noqa: S608
        (*ids, RUN_COMPARISON_MIN_KM),
    ).fetchall()
    if not runs:
        return NO_RUN_COMPARISON
    return "\n".join(_describe_one(conn, run, units) for run in runs)
