"""Aggregate a list of DailySnapshots into a WeeklySummary.

Targets (used for consistency % calculation):
  WEEKLY_RUN_TARGET  = 2 runs/week
  WEEKLY_LIFT_TARGET = 2 lifts/week

Public API:
    summarise(snapshots) -- compute a WeeklySummary from a list of DailySnapshots

Example:
    from aggregator import summarise

    summary = summarise(snapshots)
    print(summary.week_label, summary.avg_hrv_ms, summary.hrv_trend)
"""

from __future__ import annotations
import statistics
from datetime import date

from models import DailySnapshot, WeeklySummary, WorkoutSnapshot

WEEKLY_RUN_TARGET = 2
WEEKLY_LIFT_TARGET = 2


def _nonnull(values: list) -> list:
    """Filter None values from a list.

    Args:
        values: A list that may contain None entries.

    Returns:
        A new list with all None elements removed.
    """
    return [v for v in values if v is not None]


def _safe_mean(values: list[float | None]) -> float | None:
    """Compute the mean of a list, ignoring None entries.

    Args:
        values: A list of floats and/or None values.

    Returns:
        The arithmetic mean of non-None values, or None if all are None.
    """
    vals = _nonnull(values)
    return statistics.mean(vals) if vals else None


def _hrv_trend(snapshots: list[DailySnapshot]) -> str | None:
    """Determine the HRV trend direction via a simple linear regression.

    Requires at least 3 days with hrv_ms data. The slope threshold is
    ±0.5 ms/day — anything within that band is labelled "stable".

    Args:
        snapshots: Ordered list of DailySnapshot objects for the week.

    Returns:
        "improving", "declining", "stable", or None if fewer than 3
        data points are available.
    """
    pairs = [(i, s.hrv_ms) for i, s in enumerate(snapshots) if s.hrv_ms is not None]
    if len(pairs) < 3:
        return None

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    n = len(xs)
    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(ys)

    num = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
    den = sum((xs[i] - x_mean) ** 2 for i in range(n))
    if den == 0:
        return "stable"

    slope = num / den  # ms per day

    if slope > 0.5:
        return "improving"
    if slope < -0.5:
        return "declining"
    return "stable"


def _week_label(snapshots: list[DailySnapshot]) -> str:
    """Build a human-readable week label from the snapshot date range.

    Args:
        snapshots: List of DailySnapshot objects; order does not matter.

    Returns:
        A string like "2026-W11 (2026-03-09 – 2026-03-15)", or "unknown"
        if snapshots is empty.
    """
    if not snapshots:
        return "unknown"
    dates = sorted(s.date for s in snapshots)
    start = dates[0]
    end = dates[-1]
    # ISO week from first date
    d = date.fromisoformat(start)
    iso_week = f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"
    return f"{iso_week} ({start} – {end})"


def _best_run_pace(runs: list[WorkoutSnapshot]) -> float | None:
    """Find the best (lowest) pace across all runs with GPX distance data.

    Pace is computed as duration_min / gpx_distance_km. Runs without GPX
    data are excluded; there is no fallback to mobility speed.

    Args:
        runs: List of WorkoutSnapshot objects with category == "run".

    Returns:
        The minimum pace in min/km, or None if no runs have GPX distance.
    """
    paces = []
    for r in runs:
        if r.gpx_distance_km and r.gpx_distance_km > 0:
            pace = r.duration_min / r.gpx_distance_km
            paces.append(pace)
    return min(paces) if paces else None


def summarise(snapshots: list[DailySnapshot]) -> WeeklySummary:
    """Compute a WeeklySummary from a list of DailySnapshots.

    Aggregates workout counts, run/lift metrics, activity ring averages,
    cardiac averages, and derived fields (recovery index, HRV trend,
    consistency scores).

    Args:
        snapshots: List of DailySnapshot objects, typically covering 7 days.
            None-valued fields are excluded from averages automatically.

    Returns:
        A fully populated WeeklySummary dataclass.
    """
    all_workouts = [w for s in snapshots for w in s.workouts]
    runs = [w for w in all_workouts if w.category == "run"]
    lifts = [w for w in all_workouts if w.category == "lift"]
    walks = [w for w in all_workouts if w.category == "walk"]

    # --- Run aggregates ---
    run_distances = _nonnull([r.gpx_distance_km for r in runs])
    total_run_km = sum(run_distances)
    avg_run_km = statistics.mean(run_distances) if run_distances else 0.0

    run_hrs = _nonnull([r.hr_avg for r in runs])
    avg_run_hr = statistics.mean(run_hrs) if run_hrs else None

    run_hr_maxes = _nonnull([r.hr_max for r in runs])
    peak_run_hr = max(run_hr_maxes) if run_hr_maxes else None

    elev_gains = _nonnull([r.gpx_elevation_gain_m for r in runs])
    avg_elevation_gain = statistics.mean(elev_gains) if elev_gains else None

    run_powers_week = _nonnull([s.running_power_w for s in snapshots])
    avg_running_power = statistics.mean(run_powers_week) if run_powers_week else None

    run_strides_week = _nonnull([s.running_stride_length_m for s in snapshots])
    avg_running_stride = statistics.mean(run_strides_week) if run_strides_week else None

    run_temps = _nonnull([r.temperature_c for r in runs])
    avg_run_temp = statistics.mean(run_temps) if run_temps else None

    run_humidities = _nonnull([r.humidity_pct for r in runs])
    avg_run_humidity = statistics.mean(run_humidities) if run_humidities else None

    # --- Lift aggregates ---
    total_lift_min = sum(w.duration_min for w in lifts)
    lift_hrs = _nonnull([w.hr_avg for w in lifts])
    avg_lift_hr = statistics.mean(lift_hrs) if lift_hrs else None

    # --- Activity rings ---
    steps_vals = _nonnull([s.steps for s in snapshots])
    avg_steps = int(round(statistics.mean(steps_vals))) if steps_vals else 0

    energy_vals = _nonnull([s.active_energy_kj for s in snapshots])
    avg_energy = statistics.mean(energy_vals) if energy_vals else 0.0

    ex_min_vals = _nonnull([s.exercise_min for s in snapshots])
    avg_exercise_min = statistics.mean(ex_min_vals) if ex_min_vals else 0.0

    stand_vals = _nonnull([s.stand_hours for s in snapshots])
    avg_stand_hours = statistics.mean(stand_vals) if stand_vals else 0.0

    # --- Cardiac ---
    avg_resting_hr = _safe_mean([s.resting_hr for s in snapshots])
    avg_hrv = _safe_mean([s.hrv_ms for s in snapshots])
    avg_walking_hr = _safe_mean([s.walking_hr_avg for s in snapshots])

    vo2max_vals = _nonnull([s.vo2max for s in snapshots])
    latest_vo2max = vo2max_vals[-1] if vo2max_vals else None

    # --- Derived ---
    avg_recovery = _safe_mean([s.recovery_index for s in snapshots])
    hrv_trend = _hrv_trend(snapshots)

    # --- Sleep ---
    avg_sleep_total = _safe_mean([s.sleep_total_h for s in snapshots])
    avg_sleep_efficiency = _safe_mean([s.sleep_efficiency_pct for s in snapshots])
    avg_sleep_deep = _safe_mean([s.sleep_deep_h for s in snapshots])
    avg_sleep_core = _safe_mean([s.sleep_core_h for s in snapshots])
    avg_sleep_rem = _safe_mean([s.sleep_rem_h for s in snapshots])
    avg_sleep_awake = _safe_mean([s.sleep_awake_h for s in snapshots])

    run_consistency = min(100.0, (len(runs) / WEEKLY_RUN_TARGET) * 100)
    lift_consistency = min(100.0, (len(lifts) / WEEKLY_LIFT_TARGET) * 100)

    return WeeklySummary(
        week_label=_week_label(snapshots),
        run_count=len(runs),
        lift_count=len(lifts),
        walk_count=len(walks),
        total_run_km=round(total_run_km, 2),
        avg_run_km=round(avg_run_km, 2),
        best_pace_min_per_km=round(_best_run_pace(runs), 2)
        if _best_run_pace(runs)
        else None,
        avg_run_hr=round(avg_run_hr, 1) if avg_run_hr is not None else None,
        peak_run_hr=int(peak_run_hr) if peak_run_hr is not None else None,
        avg_elevation_gain_m=round(avg_elevation_gain, 1)
        if avg_elevation_gain is not None
        else None,
        avg_running_power_w=round(avg_running_power, 1)
        if avg_running_power is not None
        else None,
        avg_running_stride_m=round(avg_running_stride, 3)
        if avg_running_stride is not None
        else None,
        avg_run_temp_c=round(avg_run_temp, 1) if avg_run_temp is not None else None,
        avg_run_humidity_pct=round(avg_run_humidity)
        if avg_run_humidity is not None
        else None,
        total_lift_min=round(total_lift_min, 1),
        avg_lift_hr=round(avg_lift_hr, 1) if avg_lift_hr is not None else None,
        avg_steps=avg_steps,
        avg_active_energy_kj=round(avg_energy, 1),
        avg_exercise_min=round(avg_exercise_min, 1),
        avg_stand_hours=round(avg_stand_hours, 1),
        avg_resting_hr=round(avg_resting_hr, 1) if avg_resting_hr is not None else None,
        avg_hrv_ms=round(avg_hrv, 1) if avg_hrv is not None else None,
        avg_walking_hr=round(avg_walking_hr, 1) if avg_walking_hr is not None else None,
        latest_vo2max=round(latest_vo2max, 2) if latest_vo2max is not None else None,
        avg_recovery_index=round(avg_recovery, 3) if avg_recovery is not None else None,
        hrv_trend=hrv_trend,
        avg_sleep_total_h=round(avg_sleep_total, 2)
        if avg_sleep_total is not None
        else None,
        avg_sleep_efficiency_pct=round(avg_sleep_efficiency, 1)
        if avg_sleep_efficiency is not None
        else None,
        avg_sleep_deep_h=round(avg_sleep_deep, 2)
        if avg_sleep_deep is not None
        else None,
        avg_sleep_core_h=round(avg_sleep_core, 2)
        if avg_sleep_core is not None
        else None,
        avg_sleep_rem_h=round(avg_sleep_rem, 2) if avg_sleep_rem is not None else None,
        avg_sleep_awake_h=round(avg_sleep_awake, 2)
        if avg_sleep_awake is not None
        else None,
        run_consistency_pct=round(run_consistency, 1),
        lift_consistency_pct=round(lift_consistency, 1),
    )
