"""Report formatting and display utilities.

Public API:
    to_dict             — recursively convert dataclass instances to plain dicts.
    fmt                 — format a numeric value for display.
    ri_label            — human-readable recovery index label.
    print_summary       — print weekly summary report to stdout.
    print_daily         — print per-day breakdown to stdout.
    current_week_bounds — Monday–Sunday ISO date strings for a given date's week.
    group_by_week       — group snapshots into ISO-week buckets.

Example:
    from report import print_summary, print_daily, current_week_bounds
    bounds = current_week_bounds("2026-03-15")
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from datetime import date, timedelta

from models import DailySnapshot, WeeklySummary


def to_dict(obj: object) -> object:
    """Recursively convert dataclass instances and lists to plain dicts.

    Args:
        obj: A dataclass instance, a list, or a plain scalar value.

    Returns:
        A JSON-serialisable dict, list, or scalar equivalent of obj.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, list):
        return [to_dict(i) for i in obj]
    return obj


def fmt(val: float | int | None, unit: str = "", decimals: int = 1) -> str:
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


def ri_label(ri: float) -> str:
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
    print(f"  Run consistency:   {fmt(summary.run_consistency_pct, '%', 0)}")
    print(f"  Lift consistency:  {fmt(summary.lift_consistency_pct, '%', 0)}")
    print(f"  Total run km:      {fmt(summary.total_run_km, ' km')}")
    print(f"  Best pace:         {fmt(summary.best_pace_min_per_km, ' min/km')}")
    print(f"  Avg run HR:        {fmt(summary.avg_run_hr, ' bpm')}")
    print(f"  Peak run HR:       {fmt(summary.peak_run_hr, ' bpm', 0)}")
    print(f"  Avg elevation gain:{fmt(summary.avg_elevation_gain_m, ' m')}")
    print(f"  Avg run power:     {fmt(summary.avg_running_power_w, ' W')}")
    print(f"  Avg stride length: {fmt(summary.avg_running_stride_m, ' m')}")
    print(f"  Avg run temp:      {fmt(summary.avg_run_temp_c, '°C')}")
    print(f"  Avg run humidity:  {fmt(summary.avg_run_humidity_pct, '%', 0)}")
    print(f"  Total lift time:   {fmt(summary.total_lift_min, ' min')}")
    print(f"  Avg lift HR:       {fmt(summary.avg_lift_hr, ' bpm')}")

    print("\nActivity Rings (daily averages)")
    print(f"  Steps:             {fmt(summary.avg_steps)}")
    print(
        f"  Active energy:     {fmt(summary.avg_active_energy_kj, ' kJ')}  "
        f"({fmt(summary.avg_active_energy_kj / 4.184 if summary.avg_active_energy_kj else None, ' kcal')})"
    )
    print(f"  Exercise minutes:  {fmt(summary.avg_exercise_min, ' min')}")
    print(f"  Stand hours:       {fmt(summary.avg_stand_hours, ' hr')}")

    print("\nCardiac Health")
    print(f"  Resting HR:        {fmt(summary.avg_resting_hr, ' bpm')}")
    print(
        f"  HRV:               {fmt(summary.avg_hrv_ms, ' ms')}  (trend: {summary.hrv_trend or '—'})"
    )
    print(f"  Walking HR avg:    {fmt(summary.avg_walking_hr, ' bpm')}")
    print(f"  VO2max (latest):   {fmt(summary.latest_vo2max, ' ml/kg·min')}")
    print(f"  Recovery index:    {fmt(summary.avg_recovery_index)}")
    print()


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
                f"RI {s.recovery_index:.2f} ({ri_label(s.recovery_index)})"
            )
        print(f"{indent}" + " · ".join(daily_parts))
    print()


def current_week_bounds(max_date: str) -> tuple[str, str]:
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


def group_by_week(snapshots: list[DailySnapshot]) -> list[list[DailySnapshot]]:
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
