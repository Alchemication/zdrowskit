"""Parse health metrics JSON files.

Supports two export formats:
  - Shortcuts: 3 files by category (activity.json, heart.json, mobility.json)
  - Auto Export: N files by time period (HealthAutoExport-YYYY-WW.json), all metrics
    combined, with sleep_analysis embedded.

Both share the schema:
  {"data": {"metrics": [{"name": str, "units": str, "data": [{"date": str, "qty"?: float, ...}]}]}}

heart_rate entries use Min/Avg/Max instead of qty.
sleep_analysis entries use pre-aggregated nightly totals (Auto Export format).

Public API:
    parse_metrics_file(path)      -- parse a single metrics JSON file
    parse_all_metrics(metrics_dir) -- parse and merge all files in Metrics/
    METRIC_MAP                    -- Apple Health metric name → internal field name

Example:
    from pathlib import Path
    from parsers.metrics import parse_all_metrics

    metrics = parse_all_metrics(Path("Metrics/"))
    # {"2026-03-13": {"steps": 9500.0, "resting_hr": 52.0, ...}, ...}
"""

from __future__ import annotations
import json
from datetime import datetime, timedelta
from pathlib import Path


# Maps Apple Health metric names to our internal field names
METRIC_MAP: dict[str, str] = {
    # activity.json
    "apple_exercise_time": "exercise_min",
    "apple_stand_time": "stand_time_min",  # not used in DailySnapshot, kept for completeness
    "active_energy": "active_energy_kj",
    "flights_climbed": "flights_climbed",
    "apple_stand_hour": "stand_hours",
    "walking_running_distance": "distance_km",
    "step_count": "steps",
    # heart.json
    "vo2_max": "vo2max",
    "walking_heart_rate_average": "walking_hr_avg",
    "resting_heart_rate": "resting_hr",
    "heart_rate_variability": "hrv_ms",
    # heart_rate handled separately (Min/Avg/Max)
    # mobility.json
    "stair_speed_down": "stair_speed_down_ms",
    "stair_speed_up": "stair_speed_up_ms",
    "walking_asymmetry_percentage": "walking_asymmetry_pct",
    "running_stride_length": "running_stride_length_m",
    "running_power": "running_power_w",
    "running_speed": "running_speed_kmh",
    "walking_double_support_percentage": "walking_double_support_pct",
    "walking_speed": "walking_speed_kmh",
    "walking_step_length": "walking_step_length_cm",
    # overnight recovery metrics — see _NIGHT_ATTRIBUTED_METRICS
    "respiratory_rate": "respiratory_rate",
    "apple_sleeping_wrist_temperature": "sleeping_wrist_temp_c",
}

# Metrics sampled while asleep, which Apple stamps in local clock time. A single
# night straddles midnight — observed respiratory-rate samples run 22:00 to
# 07:00 — so filing them by calendar date splits one night across two rows and
# lands the evening samples and the morning samples on different days. These are
# attributed to the night-start date instead, the same rule sleep_analysis uses,
# so a night's sleep, respiratory rate, and wrist temperature share one row and
# can be read together.
#
# They are also averaged across the night rather than taking the last reading of
# the date, which is what the generic qty path does. A night's mean is the
# meaningful figure for both, and with samples on both sides of midnight
# last-wins would otherwise pick a different point in the night for each metric.
_NIGHT_ATTRIBUTED_METRICS: frozenset[str] = frozenset(
    {"respiratory_rate", "apple_sleeping_wrist_temperature"}
)

# Metrics whose daily figure is the sum of its entries, not one of them.
#
# Auto Export's "Time Grouping" decides how many entries a day arrives as: at
# Daily there is one, at Hourly up to twenty-four. Everything here is a running
# total over the grouping window, so a day's value is the sum across whatever
# bins the export happened to use. Summing is correct at every grouping — a
# single daily entry sums to itself — which is what keeps the parser independent
# of a setting on someone's phone.
#
# Every other qty metric is a rate or a level (resting heart rate, VO2 max,
# walking speed) and is averaged instead. Averaging is likewise identity for a
# lone daily entry. The unweighted mean is a deliberate simplification: the
# payload carries no sample count per bin, and an hourly mean is far closer to
# the truth than the arbitrary reading last-wins used to pick.
_SUMMED_METRICS: frozenset[str] = frozenset(
    {
        "step_count",
        "walking_running_distance",
        "active_energy",
        "basal_energy_burned",
        "apple_exercise_time",
        "apple_stand_time",
        "apple_stand_hour",
        "flights_climbed",
    }
)


def _parse_date(raw: str) -> str:
    """Extract ISO date (YYYY-MM-DD) from an Apple Health date string.

    Args:
        raw: Date string in the form '2026-03-13 00:00:00 +0000'.

    Returns:
        The YYYY-MM-DD portion, e.g. '2026-03-13'.
    """
    return raw.split(" ")[0]


def _night_start_date(raw: str) -> str | None:
    """Return the date of the night a timestamp belongs to.

    Shifts back twelve hours before taking the date, so an evening reading and
    the following pre-dawn readings land on the same night — the same rule
    ``sleep_analysis`` uses for its nightly totals.

    Args:
        raw: Date string in the form '2026-03-13 23:00:00 +0000'.

    Returns:
        The night-start date as YYYY-MM-DD, or None if the string is unparseable.
    """
    try:
        parsed = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    return (parsed - timedelta(hours=12)).date().isoformat()


def parse_metrics_file(path: Path) -> dict[str, dict[str, float]]:
    """Parse a single Apple Health metrics JSON file.

    Args:
        path: Path to a metrics JSON file (activity, heart, or mobility).

    Returns:
        A dict mapping ISO date strings to a flat dict of field name → value, e.g.::

            {
              "2026-03-09": {"steps": 5399.0, "active_energy_kj": 1917.3, ...},
              "2026-03-10": {...},
            }
    """
    with path.open() as f:
        return parse_metrics_payload(json.load(f))


def parse_metrics_payload(data: dict) -> dict[str, dict[str, float]]:
    """Parse an already-decoded Apple Health metrics payload.

    Args:
        data: Decoded Auto Export metrics JSON.

    Returns:
        A dict mapping ISO date strings to a flat dict of field name → value.
    """
    result: dict[str, dict[str, float]] = {}
    # (night_date, field) -> readings, averaged once the file is fully read.
    night_readings: dict[tuple[str, str], list[float]] = {}
    # (date, apple name, field) -> readings, reduced once the file is fully
    # read. Held per Apple name so the reduction can consult _SUMMED_METRICS.
    day_readings: dict[tuple[str, str, str], list[float]] = {}

    for metric in data["data"]["metrics"]:
        name = metric["name"]

        if name == "heart_rate":
            # Special case: entries have Min, Avg, Max instead of qty. The
            # day's extremes are the extremes across its entries — taking the
            # last entry's would report the final hour's range as the day's.
            for entry in metric.get("data", []):
                date = _parse_date(entry["date"])
                day = result.setdefault(date, {})
                if "Min" in entry:
                    low = float(entry["Min"])
                    prev_low = day.get("hr_day_min")
                    day["hr_day_min"] = low if prev_low is None else min(prev_low, low)
                if "Max" in entry:
                    high = float(entry["Max"])
                    prev_high = day.get("hr_day_max")
                    day["hr_day_max"] = (
                        high if prev_high is None else max(prev_high, high)
                    )
            continue

        if name == "sleep_analysis":
            # Auto Export format: pre-aggregated nightly totals.
            # Fields: totalSleep, deep, core, rem, awake (hours),
            #         sleepStart/sleepEnd, date.
            for entry in metric.get("data", []):
                total = entry.get("totalSleep", 0.0)
                deep = entry.get("deep", 0.0)
                core = entry.get("core", 0.0)
                rem = entry.get("rem", 0.0)
                awake = entry.get("awake", 0.0)
                in_bed = total + awake
                efficiency = (total / in_bed * 100) if in_bed > 0 else 0.0

                # Assign to the night's date: use sleepStart, and if the
                # start time is before noon assign to the previous day.
                sleep_start = entry.get("sleepStart", entry.get("date", ""))
                dt = datetime.strptime(sleep_start[:19], "%Y-%m-%d %H:%M:%S")
                night_date = (dt - timedelta(hours=12)).date().isoformat()

                day = result.setdefault(night_date, {})
                day["sleep_total_h"] = round(total, 2)
                day["sleep_in_bed_h"] = round(in_bed, 2)
                day["sleep_efficiency_pct"] = round(efficiency, 1)
                day["sleep_deep_h"] = round(deep, 2)
                day["sleep_core_h"] = round(core, 2)
                day["sleep_rem_h"] = round(rem, 2)
                day["sleep_awake_h"] = round(awake, 2)
            continue

        field = METRIC_MAP.get(name)
        if field is None:
            continue  # unknown metric — ignore

        if name in _NIGHT_ATTRIBUTED_METRICS:
            for entry in metric.get("data", []):
                if "qty" not in entry:
                    continue
                night_date = _night_start_date(entry["date"])
                if night_date is None:
                    continue
                night_readings.setdefault((night_date, field), []).append(
                    float(entry["qty"])
                )
            continue

        for entry in metric.get("data", []):
            if "qty" not in entry:
                continue
            date = _parse_date(entry["date"])
            day_readings.setdefault((date, name, field), []).append(float(entry["qty"]))

    for (date, name, field), readings in day_readings.items():
        if name in _SUMMED_METRICS:
            value = sum(readings)
        else:
            value = sum(readings) / len(readings)
        result.setdefault(date, {})[field] = value

    for (night_date, field), readings in night_readings.items():
        result.setdefault(night_date, {})[field] = round(
            sum(readings) / len(readings), 2
        )

    return result


def parse_all_metrics(metrics_dir: Path) -> dict[str, dict[str, float]]:
    """Parse all JSON files in the Metrics/ directory and merge by date.

    Later files overwrite earlier ones for the same field. In practice the three
    files (activity, heart, mobility) have disjoint metric names, so conflicts
    won't occur.

    Args:
        metrics_dir: Path to the Metrics/ directory containing the JSON files.

    Returns:
        A merged dict mapping ISO date strings to a flat dict of all available
        fields across all three source files.
    """
    combined: dict[str, dict[str, float]] = {}

    for json_file in sorted(metrics_dir.glob("*.json")):
        file_data = parse_metrics_file(json_file)
        for date, fields in file_data.items():
            combined.setdefault(date, {}).update(fields)

    return combined
