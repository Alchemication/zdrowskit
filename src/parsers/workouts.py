"""Parse workout JSON files.

Supports two export formats:
  - Shortcuts: single workouts.json with nested qty/units dicts.
  - Auto Export: N files by time period (HealthAutoExport-YYYY-WW.json), same
    workout schema but with embedded route trackpoints and summary stats.

Schema: {"data": {"workouts": [...]}}

Public API:
    parse_workouts(path)          -- parse a single workouts JSON file
    parse_workouts_dir(directory) -- parse all JSON files in a directory, deduplicated

Example:
    from pathlib import Path
    from parsers.workouts import parse_workouts, parse_workouts_dir

    workouts = parse_workouts(Path("Workouts/workouts.json"))
    workouts = parse_workouts_dir(Path("Workouts/"))
"""

from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

from models import WorkoutSnapshot


# Maps workout name → category
_CATEGORY_MAP: dict[str, str] = {
    "outdoor run": "run",
    "indoor run": "run",
    "treadmill running": "run",
    "traditional strength training": "lift",
    "functional strength training": "lift",
    "outdoor walk": "walk",
    "indoor walk": "walk",
    "outdoor cycle": "cycle",
    "indoor cycle": "cycle",
}

_MIN_WORKOUT_DURATION_MIN = 1.0
_FUNCTIONAL_LIFT_MIN_DURATION = 15.0


def _category(name: str) -> str:
    """Map a workout name to its normalised category string.

    Args:
        name: Raw workout name from the JSON, e.g. "Outdoor Run".

    Returns:
        One of "run", "lift", "walk", "cycle", or "other".
    """
    return _CATEGORY_MAP.get(name.lower(), "other")


def _counts_as_lift(name: str, duration_min: float) -> bool:
    """Return whether a workout should count as a completed lift.

    Args:
        name: Raw workout name from the JSON.
        duration_min: Elapsed workout duration in minutes.

    Returns:
        True when the workout should count toward weekly lift completion.
    """
    normalized = name.lower()
    if normalized == "traditional strength training":
        return True
    if normalized == "functional strength training":
        return duration_min >= _FUNCTIONAL_LIFT_MIN_DURATION
    return False


def _qty(obj: dict | None) -> float | None:
    """Safely extract the numeric value from an Apple Health qty dict.

    Args:
        obj: A dict like ``{"qty": 81.6, "units": "count/min"}``, or None.

    Returns:
        The float value of "qty", or None if obj is None or "qty" is absent.
    """
    if obj is None:
        return None
    v = obj.get("qty")
    return float(v) if v is not None else None


def _parse_apple_dt(raw: str) -> datetime:
    """Parse an Apple Health datetime string to a UTC datetime.

    Args:
        raw: String in the form '2026-03-14 06:47:51 +0000'.

    Returns:
        A timezone-aware datetime normalised to UTC.
    """
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S %z").astimezone(timezone.utc)


def _duration_min(w: dict, start_dt: datetime) -> float:
    """Return workout duration in minutes from explicit or derived fields.

    Args:
        w: Raw workout dict from the JSON.
        start_dt: Parsed workout start datetime in UTC.

    Returns:
        Workout duration in minutes.
    """
    duration_s = w.get("duration")
    if duration_s is not None:
        return float(duration_s) / 60.0

    end_raw = w.get("end")
    if isinstance(end_raw, str):
        end_dt = _parse_apple_dt(end_raw)
        return max(0.0, (end_dt - start_dt).total_seconds() / 60.0)

    return 0.0


def _percentile(values: list[float], p: float) -> float:
    """Return the p-th percentile using linear interpolation.

    Args:
        values: Input list of floats (need not be sorted).
        p: Percentile to compute in the range 0–100.

    Returns:
        The interpolated percentile value, or 0.0 for an empty list.
    """
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = (p / 100) * (len(sorted_vals) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def _extract_route_stats(w: dict) -> dict[str, float | None]:
    """Extract route/distance stats from Auto Export workout fields.

    Uses Apple-computed summary fields (distance, speed, elevationUp) when
    available, and computes p95 max speed from embedded route trackpoints.

    Args:
        w: Raw workout dict from the JSON.

    Returns:
        Dict with gpx_distance_km, gpx_elevation_gain_m, gpx_avg_speed_ms,
        gpx_max_speed_p95_ms — any may be None if data is absent.
    """
    distance_km = _qty(w.get("distance"))
    elevation_m = _qty(w.get("elevationUp"))

    # Convert speed from km/h to m/s
    speed_kmh = _qty(w.get("speed"))
    avg_speed_ms = round(speed_kmh / 3.6, 4) if speed_kmh is not None else None

    # Compute p95 max speed from route trackpoints
    max_speed_p95_ms: float | None = None
    route = w.get("route", [])
    if route:
        speeds = [pt["speed"] for pt in route if pt.get("speed", 0) > 0]
        if speeds:
            max_speed_p95_ms = round(_percentile(speeds, 95), 4)

    return {
        "gpx_distance_km": round(distance_km, 3) if distance_km is not None else None,
        "gpx_elevation_gain_m": elevation_m,
        "gpx_avg_speed_ms": avg_speed_ms,
        "gpx_max_speed_p95_ms": max_speed_p95_ms,
    }


def parse_workouts(path: Path) -> list[WorkoutSnapshot]:
    """Parse a workouts JSON file into a list of WorkoutSnapshots.

    Handles both Shortcuts format (single workouts.json) and Auto Export format
    (with embedded route data and summary stats).

    Args:
        path: Path to a workouts JSON file.

    Returns:
        A list of WorkoutSnapshot objects ordered chronologically by start_utc.
    """
    with path.open() as f:
        data = json.load(f)

    snapshots: list[WorkoutSnapshot] = []

    for w in data["data"]["workouts"]:
        name = w.get("name", "Unknown")
        start_dt = _parse_apple_dt(w["start"])
        duration_min = _duration_min(w, start_dt)

        if duration_min < _MIN_WORKOUT_DURATION_MIN:
            continue

        # Heart rate — nested {"avg": {"qty": x}, "min": {...}, "max": {...}}
        hr_block = w.get("heartRate", {})
        hr_avg = _qty(hr_block.get("avg"))
        hr_min_val = _qty(hr_block.get("min"))
        hr_max_val = _qty(hr_block.get("max"))

        # Fallback: avgHeartRate / maxHeartRate top-level fields
        if hr_avg is None:
            hr_avg = _qty(w.get("avgHeartRate"))
        if hr_max_val is None:
            hr_max_val = _qty(w.get("maxHeartRate"))

        active_energy = _qty(w.get("activeEnergyBurned")) or 0.0
        intensity = _qty(w.get("intensity"))
        temperature = _qty(w.get("temperature"))
        humidity = (
            w.get("humidity", {}).get("qty")
            if isinstance(w.get("humidity"), dict)
            else None
        )

        # Extract embedded route/summary stats (Auto Export format)
        route_stats = _extract_route_stats(w)

        snapshots.append(
            WorkoutSnapshot(
                type=name,
                category=_category(name),
                counts_as_lift=_counts_as_lift(name, duration_min),
                start_utc=start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                duration_min=duration_min,
                hr_min=int(hr_min_val) if hr_min_val is not None else None,
                hr_avg=hr_avg,
                hr_max=int(hr_max_val) if hr_max_val is not None else None,
                active_energy_kj=active_energy,
                intensity_kcal_per_hr_kg=intensity,
                temperature_c=temperature,
                humidity_pct=int(humidity) if humidity is not None else None,
                gpx_distance_km=route_stats["gpx_distance_km"],
                gpx_elevation_gain_m=route_stats["gpx_elevation_gain_m"],
                gpx_avg_speed_ms=route_stats["gpx_avg_speed_ms"],
                gpx_max_speed_p95_ms=route_stats["gpx_max_speed_p95_ms"],
            )
        )

    snapshots.sort(key=lambda s: s.start_utc)
    return snapshots


def parse_workouts_dir(workouts_dir: Path) -> list[WorkoutSnapshot]:
    """Parse all JSON files in a workouts directory and deduplicate.

    Used for Auto Export format where workouts are split across multiple
    time-period files.

    Args:
        workouts_dir: Path to a directory containing workout JSON files.

    Returns:
        A deduplicated list of WorkoutSnapshot objects sorted by start_utc.
    """
    seen: set[str] = set()
    snapshots: list[WorkoutSnapshot] = []

    for json_file in sorted(workouts_dir.glob("*.json")):
        for w in parse_workouts(json_file):
            if w.start_utc not in seen:
                seen.add(w.start_utc)
                snapshots.append(w)

    snapshots.sort(key=lambda s: s.start_utc)
    return snapshots
