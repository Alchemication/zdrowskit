"""Aggregate a list of DailySnapshots into a WeeklySummary.

Public API:
    summarise(snapshots) -- compute a WeeklySummary from a list of DailySnapshots

Example:
    from aggregator import summarise

    summary = summarise(snapshots)
    print(summary.week_label, summary.avg_hrv_ms, summary.hrv_trend)
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta

from models import DailySnapshot, WeeklySummary, WorkoutSnapshot, WorkoutSplit

ADJACENT_RUN_SESSION_GAP_MIN = 5.0
"""Maximum gap between run records that still counts as one training session."""


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


def _workout_start(workout: WorkoutSnapshot) -> datetime | None:
    """Parse a workout start timestamp.

    Args:
        workout: Workout snapshot with an ISO UTC start timestamp.

    Returns:
        Parsed datetime, or None if the timestamp is unavailable/malformed.
    """
    if not workout.start_utc:
        return None
    try:
        return datetime.fromisoformat(workout.start_utc.replace("Z", "+00:00"))
    except ValueError:
        return None


def _workout_end(workout: WorkoutSnapshot) -> datetime | None:
    """Return a workout end timestamp when start and duration are known."""
    start = _workout_start(workout)
    if start is None:
        return None
    return start + timedelta(minutes=workout.duration_min)


def _weighted_mean(
    workouts: list[WorkoutSnapshot],
    attr: str,
    *,
    weights: list[float] | None = None,
) -> float | None:
    """Return a duration-weighted mean for a numeric workout attribute."""
    numerator = 0.0
    denominator = 0.0
    for index, workout in enumerate(workouts):
        value = getattr(workout, attr)
        if value is None:
            continue
        weight = weights[index] if weights is not None else workout.duration_min
        if weight <= 0:
            continue
        numerator += float(value) * weight
        denominator += weight
    if denominator == 0:
        return None
    return numerator / denominator


def _sum_optional(workouts: list[WorkoutSnapshot], attr: str) -> float | None:
    """Sum a numeric workout attribute, preserving None when all values are missing."""
    values = [getattr(workout, attr) for workout in workouts]
    nonnull = _nonnull(values)
    if not nonnull:
        return None
    return sum(nonnull)


def _merge_run_session(workouts: list[WorkoutSnapshot]) -> WorkoutSnapshot:
    """Merge adjacent run records into one training-session snapshot."""
    if len(workouts) == 1:
        return workouts[0]

    first = workouts[0]
    total_duration = sum(workout.duration_min for workout in workouts)
    distance_km = _sum_optional(workouts, "gpx_distance_km")
    elevation_m = _sum_optional(workouts, "gpx_elevation_gain_m")
    active_energy_kj = sum(workout.active_energy_kj for workout in workouts)
    distance_weights = [
        workout.gpx_distance_km or workout.duration_min for workout in workouts
    ]
    hr_mins = _nonnull([workout.hr_min for workout in workouts])
    hr_maxes = _nonnull([workout.hr_max for workout in workouts])
    max_speed_p95s = _nonnull([workout.gpx_max_speed_p95_ms for workout in workouts])
    humidity = _weighted_mean(workouts, "humidity_pct", weights=distance_weights)
    speed_ms = None
    if distance_km is not None and total_duration > 0:
        speed_ms = round((distance_km * 1000.0) / (total_duration * 60.0), 4)

    return WorkoutSnapshot(
        type=first.type,
        category=first.category,
        counts_as_lift=first.counts_as_lift,
        start_utc=first.start_utc,
        duration_min=total_duration,
        hr_min=min(hr_mins) if hr_mins else None,
        hr_avg=_weighted_mean(workouts, "hr_avg"),
        hr_max=max(hr_maxes) if hr_maxes else None,
        active_energy_kj=active_energy_kj,
        intensity_kcal_per_hr_kg=_weighted_mean(workouts, "intensity_kcal_per_hr_kg"),
        temperature_c=_weighted_mean(
            workouts, "temperature_c", weights=distance_weights
        ),
        humidity_pct=round(humidity) if humidity is not None else None,
        gpx_distance_km=distance_km,
        gpx_elevation_gain_m=elevation_m,
        gpx_avg_speed_ms=speed_ms,
        gpx_max_speed_p95_ms=max(max_speed_p95s) if max_speed_p95s else None,
        location_lat=first.location_lat,
        location_lon=first.location_lon,
        location_id=first.location_id,
        location_label=first.location_label,
        location_locality=first.location_locality,
        location_country=first.location_country,
        location_country_code=first.location_country_code,
        splits=[
            WorkoutSplit(
                km_index=index,
                pace_min_km=split.pace_min_km,
                avg_speed_ms=split.avg_speed_ms,
                elevation_gain_m=split.elevation_gain_m,
                elevation_loss_m=split.elevation_loss_m,
            )
            for index, split in enumerate(
                [split for workout in workouts for split in workout.splits],
                start=1,
            )
        ],
    )


def collapse_adjacent_run_sessions(
    workouts: list[WorkoutSnapshot],
    *,
    gap_min: float = ADJACENT_RUN_SESSION_GAP_MIN,
) -> list[WorkoutSnapshot]:
    """Collapse back-to-back run records into training sessions.

    Apple Watch can split one continuous run into multiple workouts. For weekly
    target counts, adjacent run records with only a tiny pause between them are
    one training run, while separated runs still count independently.

    Args:
        workouts: Workout snapshots for any date range.
        gap_min: Maximum gap in minutes between run records to merge.

    Returns:
        Workout snapshots with adjacent runs merged and other workouts untouched.
    """
    if not workouts:
        return []

    sorted_workouts = sorted(workouts, key=lambda workout: workout.start_utc or "")
    collapsed: list[WorkoutSnapshot] = []
    pending_runs: list[WorkoutSnapshot] = []

    def flush_runs() -> None:
        nonlocal pending_runs
        if pending_runs:
            collapsed.append(_merge_run_session(pending_runs))
            pending_runs = []

    for workout in sorted_workouts:
        if workout.category != "run":
            flush_runs()
            collapsed.append(workout)
            continue

        if not pending_runs:
            pending_runs.append(workout)
            continue

        previous = pending_runs[-1]
        previous_end = _workout_end(previous)
        current_start = _workout_start(workout)
        if previous_end is None or current_start is None:
            flush_runs()
            pending_runs.append(workout)
            continue

        gap = current_start - previous_end
        if timedelta(0) <= gap <= timedelta(minutes=gap_min):
            pending_runs.append(workout)
        else:
            flush_runs()
            pending_runs.append(workout)

    flush_runs()
    collapsed.sort(key=lambda workout: workout.start_utc or "")
    return collapsed


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
    all_workouts = [
        w for s in snapshots for w in collapse_adjacent_run_sessions(s.workouts)
    ]
    runs = [w for w in all_workouts if w.category == "run"]
    lifts = [w for w in all_workouts if w.counts_as_lift]
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
    )
