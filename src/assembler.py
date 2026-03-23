"""Join all parsed data sources into a list of DailySnapshot objects.

Supports two data sources via the ``source`` parameter:

- ``shortcuts``: iOS Shortcuts export (Metrics/*.json + Workouts/workouts.json
  + Sleep/sleep.json + Routes/*.xml).
- ``autoexport``: Auto Export app automation (Metrics/*.json with embedded
  sleep_analysis + Workouts/*.json with embedded routes).

Both produce the same ``list[DailySnapshot]`` output.

Public API:
    assemble(data_dir, source) -- parse all sources and return daily snapshots

Example:
    from pathlib import Path
    from assembler import assemble

    snapshots = assemble(Path("..."), source="autoexport")
    for day in snapshots:
        print(day.date, day.steps, day.resting_hr)
"""

from __future__ import annotations
from pathlib import Path

from models import DailySnapshot, WorkoutSnapshot
from parsers.metrics import parse_all_metrics
from parsers.workouts import parse_workouts, parse_workouts_dir


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


def _parse_shortcuts(
    data_dir: Path,
) -> tuple[
    dict[str, dict[str, float]],
    dict[str, dict[str, float]],
    list[WorkoutSnapshot],
]:
    """Parse data from iOS Shortcuts export format.

    Args:
        data_dir: Root of the MyHealth data directory.

    Returns:
        Tuple of (metrics_by_date, sleep_by_date, workouts).
    """
    from parsers.gpx import match_gpx_to_workout, parse_all_gpx
    from parsers.sleep import parse_sleep

    metrics_dir = data_dir / "Metrics"
    workouts_path = data_dir / "Workouts" / "workouts.json"
    routes_dir = data_dir / "Routes"
    sleep_path = data_dir / "Sleep" / "sleep.json"

    metrics_by_date = parse_all_metrics(metrics_dir) if metrics_dir.exists() else {}
    workouts = parse_workouts(workouts_path) if workouts_path.exists() else []
    gpx_index = parse_all_gpx(routes_dir) if routes_dir.exists() else {}
    sleep_by_date = parse_sleep(sleep_path) if sleep_path.exists() else {}

    # Attach GPX stats to workouts that have a matching route
    for w in workouts:
        gpx = match_gpx_to_workout(w.start_utc, gpx_index)
        if gpx is not None:
            w.gpx_distance_km = round(gpx.distance_km, 3)
            w.gpx_elevation_gain_m = gpx.elevation_gain_m
            w.gpx_avg_speed_ms = round(gpx.avg_speed_ms, 4)
            w.gpx_max_speed_p95_ms = round(gpx.max_speed_p95_ms, 4)

    return metrics_by_date, sleep_by_date, workouts


def _parse_autoexport(
    data_dir: Path,
) -> tuple[
    dict[str, dict[str, float]],
    dict[str, dict[str, float]],
    list[WorkoutSnapshot],
]:
    """Parse data from Auto Export app automation format.

    Sleep data is embedded in metrics files as ``sleep_analysis``.
    Route data is embedded in workout files.

    Args:
        data_dir: Root of the Auto Export data directory.

    Returns:
        Tuple of (metrics_by_date, sleep_by_date, workouts).
        sleep_by_date is empty — sleep fields are in metrics_by_date.
    """
    metrics_dir = data_dir / "Metrics"
    workouts_dir = data_dir / "Workouts"

    metrics_by_date = parse_all_metrics(metrics_dir) if metrics_dir.exists() else {}
    workouts = parse_workouts_dir(workouts_dir) if workouts_dir.exists() else []

    # Sleep is already in metrics_by_date (from sleep_analysis handling).
    return metrics_by_date, {}, workouts


def _build_snapshots(
    metrics_by_date: dict[str, dict[str, float]],
    sleep_by_date: dict[str, dict[str, float]],
    workouts: list[WorkoutSnapshot],
) -> list[DailySnapshot]:
    """Build DailySnapshot objects from parsed data.

    Args:
        metrics_by_date: Metrics (and possibly sleep) fields keyed by date.
        sleep_by_date: Separate sleep fields keyed by date (empty for autoexport).
        workouts: List of parsed WorkoutSnapshots.

    Returns:
        A list of DailySnapshot objects sorted chronologically.
    """
    # Group workouts by date
    workouts_by_date: dict[str, list[WorkoutSnapshot]] = {}
    for w in workouts:
        workouts_by_date.setdefault(_workout_date(w), []).append(w)

    # Collect all dates
    all_dates = sorted(
        set(metrics_by_date.keys())
        | set(workouts_by_date.keys())
        | set(sleep_by_date.keys())
    )

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

        # Sleep: prefer fields from metrics (autoexport), fall back to separate
        # sleep dict (shortcuts).
        def _sleep(field: str) -> float | None:
            return _safe_float(m.get(field) or sl.get(field))

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
            sleep_total_h=_sleep("sleep_total_h"),
            sleep_in_bed_h=_sleep("sleep_in_bed_h"),
            sleep_efficiency_pct=_sleep("sleep_efficiency_pct"),
            sleep_deep_h=_sleep("sleep_deep_h"),
            sleep_core_h=_sleep("sleep_core_h"),
            sleep_rem_h=_sleep("sleep_rem_h"),
            sleep_awake_h=_sleep("sleep_awake_h"),
            workouts=day_workouts,
            recovery_index=recovery_index,
        )
        snapshots.append(snap)

    return snapshots


def assemble(data_dir: Path, source: str = "autoexport") -> list[DailySnapshot]:
    """Parse all data sources under data_dir and return daily snapshots.

    Args:
        data_dir: Root data directory (layout depends on source).
        source: Data source format — "shortcuts" or "autoexport".

    Returns:
        A list of DailySnapshot objects, one per calendar date, sorted
        chronologically.
    """
    if source == "shortcuts":
        metrics, sleep, workouts = _parse_shortcuts(data_dir)
    else:
        metrics, sleep, workouts = _parse_autoexport(data_dir)

    return _build_snapshots(metrics, sleep, workouts)
