"""Parse MyHealth/Workouts/workouts.json.

Schema: {"data": {"workouts": [...]}}

Each workout entry has nested qty/units dicts, e.g.::

    "activeEnergyBurned": {"qty": 612.8, "units": "kJ"}
    "heartRate": {"avg": {"qty": 81.6, "units": "count/min"}, "min": {...}, "max": {...}}
    "temperature": {"qty": 2.4, "units": "degC"}

start/end are strings like "2026-03-14 06:47:51 +0000".

Public API:
    parse_workouts(path) -- parse workouts.json → list of WorkoutSnapshot

Example:
    from pathlib import Path
    from parsers.workouts import parse_workouts

    workouts = parse_workouts(Path("MyHealth/Workouts/workouts.json"))
    runs = [w for w in workouts if w.category == "run"]
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


def _category(name: str) -> str:
    """Map a workout name to its normalised category string.

    Args:
        name: Raw workout name from the JSON, e.g. "Outdoor Run".

    Returns:
        One of "run", "lift", "walk", "cycle", or "other".
    """
    return _CATEGORY_MAP.get(name.lower(), "other")


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


def parse_workouts(path: Path) -> list[WorkoutSnapshot]:
    """Parse workouts.json into a list of WorkoutSnapshots sorted by start time.

    Args:
        path: Path to the workouts.json file.

    Returns:
        A list of WorkoutSnapshot objects ordered chronologically by start_utc.
    """
    with path.open() as f:
        data = json.load(f)

    snapshots: list[WorkoutSnapshot] = []

    for w in data["data"]["workouts"]:
        name = w.get("name", "Unknown")
        start_dt = _parse_apple_dt(w["start"])
        duration_s = w.get("duration", 0.0)

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

        snapshots.append(
            WorkoutSnapshot(
                type=name,
                category=_category(name),
                start_utc=start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                duration_min=duration_s / 60.0,
                hr_min=int(hr_min_val) if hr_min_val is not None else None,
                hr_avg=hr_avg,
                hr_max=int(hr_max_val) if hr_max_val is not None else None,
                active_energy_kj=active_energy,
                intensity_kcal_per_hr_kg=intensity,
                temperature_c=temperature,
                humidity_pct=int(humidity) if humidity is not None else None,
            )
        )

    snapshots.sort(key=lambda s: s.start_utc)
    return snapshots
