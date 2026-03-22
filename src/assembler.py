"""Join all parsed data sources into a list of DailySnapshot objects.

Steps:
1. Collect all dates present across metrics, workouts, and GPX.
2. For each date, populate a DailySnapshot from the metrics dict.
3. Attach WorkoutSnapshots (with GPX stats merged in) to the correct date.
4. Compute derived field: recovery_index.

Public API:
    assemble(data_dir) -- parse all sources under data_dir and return daily snapshots

Example:
    from pathlib import Path
    from assembler import assemble

    snapshots = assemble(Path("MyHealth/"))
    for day in snapshots:
        print(day.date, day.steps, day.resting_hr)
"""

from __future__ import annotations
from pathlib import Path

from models import DailySnapshot, WorkoutSnapshot
from parsers.metrics import parse_all_metrics
from parsers.sleep import parse_sleep
from parsers.workouts import parse_workouts
from parsers.gpx import parse_all_gpx, match_gpx_to_workout


def _workout_date(w: WorkoutSnapshot) -> str:
    """Extract the YYYY-MM-DD date portion from a workout's start_utc string.

    Args:
        w: A WorkoutSnapshot with a populated start_utc field.

    Returns:
        ISO date string, e.g. '2026-03-13'.
    """
    return w.start_utc[:10]


def _safe_float(val: float | None) -> float | None:
    """Cast val to float if not None, otherwise return None.

    Args:
        val: A numeric value or None.

    Returns:
        float(val) or None.
    """
    return float(val) if val is not None else None


def assemble(data_dir: Path) -> list[DailySnapshot]:
    """Parse all data sources under data_dir and return daily snapshots.

    Reads Metrics/*.json, Workouts/workouts.json, and Routes/*.xml, merges
    them by date, attaches GPX stats to matching workouts, and computes the
    recovery_index derived field.

    Args:
        data_dir: Root of the MyHealth data directory (must exist).

    Returns:
        A list of DailySnapshot objects, one per calendar date, sorted
        chronologically.
    """
    metrics_dir = data_dir / "Metrics"
    workouts_path = data_dir / "Workouts" / "workouts.json"
    routes_dir = data_dir / "Routes"
    sleep_path = data_dir / "Sleep" / "sleep.json"

    # --- Parse all sources ---
    metrics_by_date = parse_all_metrics(metrics_dir) if metrics_dir.exists() else {}
    workouts = parse_workouts(workouts_path) if workouts_path.exists() else []
    gpx_index = parse_all_gpx(routes_dir) if routes_dir.exists() else {}
    sleep_by_date = parse_sleep(sleep_path) if sleep_path.exists() else {}

    # --- Attach GPX stats to workouts that have a matching route ---
    for w in workouts:
        gpx = match_gpx_to_workout(w.start_utc, gpx_index)
        if gpx is not None:
            w.gpx_distance_km = round(gpx.distance_km, 3)
            w.gpx_elevation_gain_m = gpx.elevation_gain_m
            w.gpx_avg_speed_ms = round(gpx.avg_speed_ms, 4)
            w.gpx_max_speed_p95_ms = round(gpx.max_speed_p95_ms, 4)

    # --- Group workouts by date ---
    workouts_by_date: dict[str, list[WorkoutSnapshot]] = {}
    for w in workouts:
        workouts_by_date.setdefault(_workout_date(w), []).append(w)

    # --- Collect all dates ---
    all_dates = sorted(
        set(metrics_by_date.keys())
        | set(workouts_by_date.keys())
        | set(sleep_by_date.keys())
    )

    # --- Build DailySnapshots ---
    snapshots: list[DailySnapshot] = []
    for date in all_dates:
        m = metrics_by_date.get(date, {})
        sl = sleep_by_date.get(date, {})
        day_workouts = workouts_by_date.get(date, [])

        # Cast numeric fields to appropriate types
        steps_raw = m.get("steps")
        stand_hours_raw = m.get("stand_hours")
        resting_hr_raw = m.get("resting_hr")
        hr_min_raw = m.get("hr_day_min")
        hr_max_raw = m.get("hr_day_max")

        resting_hr = int(round(resting_hr_raw)) if resting_hr_raw is not None else None
        hrv_ms = m.get("hrv_ms")

        # Derived: recovery index
        recovery_index = None
        if hrv_ms is not None and resting_hr is not None and resting_hr > 0:
            recovery_index = round(hrv_ms / resting_hr, 4)

        snap = DailySnapshot(
            date=date,
            steps=int(round(steps_raw)) if steps_raw is not None else None,
            distance_km=_safe_float(m.get("distance_km")),
            active_energy_kj=_safe_float(m.get("active_energy_kj")),
            exercise_min=int(round(m["exercise_min"])) if "exercise_min" in m else None,
            stand_hours=int(round(stand_hours_raw))
            if stand_hours_raw is not None
            else None,
            flights_climbed=_safe_float(m.get("flights_climbed")),
            resting_hr=resting_hr,
            hrv_ms=hrv_ms,
            walking_hr_avg=_safe_float(m.get("walking_hr_avg")),
            hr_day_min=int(round(hr_min_raw)) if hr_min_raw is not None else None,
            hr_day_max=int(round(hr_max_raw)) if hr_max_raw is not None else None,
            vo2max=_safe_float(m.get("vo2max")),
            walking_speed_kmh=_safe_float(m.get("walking_speed_kmh")),
            walking_step_length_cm=_safe_float(m.get("walking_step_length_cm")),
            walking_asymmetry_pct=_safe_float(m.get("walking_asymmetry_pct")),
            walking_double_support_pct=_safe_float(m.get("walking_double_support_pct")),
            stair_speed_up_ms=_safe_float(m.get("stair_speed_up_ms")),
            stair_speed_down_ms=_safe_float(m.get("stair_speed_down_ms")),
            running_stride_length_m=_safe_float(m.get("running_stride_length_m")),
            running_power_w=_safe_float(m.get("running_power_w")),
            running_speed_kmh=_safe_float(m.get("running_speed_kmh")),
            sleep_total_h=_safe_float(sl.get("sleep_total_h")),
            sleep_in_bed_h=_safe_float(sl.get("sleep_in_bed_h")),
            sleep_efficiency_pct=_safe_float(sl.get("sleep_efficiency_pct")),
            sleep_deep_h=_safe_float(sl.get("sleep_deep_h")),
            sleep_core_h=_safe_float(sl.get("sleep_core_h")),
            sleep_rem_h=_safe_float(sl.get("sleep_rem_h")),
            sleep_awake_h=_safe_float(sl.get("sleep_awake_h")),
            workouts=day_workouts,
            recovery_index=recovery_index,
        )
        snapshots.append(snap)

    return snapshots
