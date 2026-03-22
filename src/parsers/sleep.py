"""Parse MyHealth/Sleep/sleep.json into per-night sleep summaries.

The export contains a single metric (sleep_analysis) with per-segment entries,
each having a start/end timestamp and a stage value (Core, Deep, REM, Awake).

Segments are grouped into nights: any segment starting before noon is assigned
to the previous calendar day's night.  Each night produces total sleep hours,
time in bed, sleep efficiency, and per-stage breakdowns.

Public API:
    parse_sleep(path) -- parse sleep.json and return per-date sleep dicts

Example:
    from pathlib import Path
    from parsers.sleep import parse_sleep

    sleep = parse_sleep(Path("MyHealth/Sleep/sleep.json"))
    # {"2026-03-16": {"sleep_total_h": 7.6, "sleep_deep_h": 0.73, ...}, ...}
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


def parse_sleep(path: Path) -> dict[str, dict[str, float]]:
    """Parse a sleep.json file into per-night sleep summaries.

    Segments are grouped by night: a segment starting before noon belongs to
    the previous calendar day's sleep session. ``Awake`` segments count toward
    time-in-bed but not toward total sleep or any stage bucket.

    Args:
        path: Path to the sleep.json file.

    Returns:
        A dict mapping ISO date strings to sleep metrics::

            {
              "2026-03-16": {
                "sleep_total_h": 7.4,
                "sleep_in_bed_h": 7.6,
                "sleep_efficiency_pct": 97.4,
                "sleep_deep_h": 0.73,
                "sleep_core_h": 4.69,
                "sleep_rem_h": 2.01,
                "sleep_awake_h": 0.17,
              },
              ...
            }
    """
    with path.open() as f:
        data = json.load(f)

    entries = data["data"]["metrics"][0]["data"]

    # Group segments by night.
    nights: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        dt = datetime.strptime(entry["start"][:19], "%Y-%m-%d %H:%M:%S")
        # If segment starts before noon, it belongs to the previous day's night.
        night_date = (dt - timedelta(hours=12)).date().isoformat()
        nights[night_date].append(entry)

    result: dict[str, dict[str, float]] = {}
    for night_date, segments in nights.items():
        stage_hours: dict[str, float] = defaultdict(float)
        for seg in segments:
            stage_hours[seg["value"]] += float(seg["qty"])

        awake_h = stage_hours.get("Awake", 0.0)
        deep_h = stage_hours.get("Deep", 0.0)
        core_h = stage_hours.get("Core", 0.0)
        rem_h = stage_hours.get("REM", 0.0)

        total_sleep_h = deep_h + core_h + rem_h  # excludes Awake
        in_bed_h = total_sleep_h + awake_h
        efficiency = (total_sleep_h / in_bed_h * 100) if in_bed_h > 0 else 0.0

        result[night_date] = {
            "sleep_total_h": round(total_sleep_h, 2),
            "sleep_in_bed_h": round(in_bed_h, 2),
            "sleep_efficiency_pct": round(efficiency, 1),
            "sleep_deep_h": round(deep_h, 2),
            "sleep_core_h": round(core_h, 2),
            "sleep_rem_h": round(rem_h, 2),
            "sleep_awake_h": round(awake_h, 2),
        }

    return result
