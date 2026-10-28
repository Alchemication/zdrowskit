"""Parse health metrics JSON files.

Supports two export formats:
  - Shortcuts: 3 files by category (activity.json, heart.json, mobility.json)
  - Auto Export: N files by time period (HealthAutoExport-YYYY-WW.json), all metrics
    combined, with sleep_analysis embedded.

Both share the schema:
  {"data": {"metrics": [{"name": str, "units": str, "data": [{"date": str, "qty"?: float, ...}]}]}}

heart_rate entries use Min/Avg/Max instead of qty.
sleep_analysis entries use pre-aggregated nightly totals (Auto Export format).

Auto Export labels each metric with the unit the phone exports in (``mi`` on an
imperial phone). Values are converted to the stored unit named in ``METRIC_MAP``
by ``parsers.normalise``; a metric whose unit is unrecognised or missing is
logged and dropped rather than stored in the wrong unit, except for single-unit
metrics (counts, heart rate, HRV, ...), which keep their value.

Public API:
    parse_metrics_file(path)      -- parse a single metrics JSON file
    parse_all_metrics(metrics_dir) -- parse and merge all files in Metrics/
    METRIC_MAP                    -- Apple Health metric name → MetricSpec(field, stored unit)

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
from typing import NamedTuple

from parsers.normalise import UnitNormaliser


class MetricSpec(NamedTuple):
    """Where a metric is stored and the unit its stored value is in."""

    field: str
    unit: str


# Stored unit of the heart_rate and sleep_analysis special cases, which sit
# outside METRIC_MAP.
_HEART_RATE_UNIT = "count/min"
_SLEEP_UNIT = "hr"

# Maps Apple Health metric names to our internal field names and stored units
METRIC_MAP: dict[str, MetricSpec] = {
    # activity.json
    "apple_exercise_time": MetricSpec("exercise_min", "min"),
    "apple_stand_time": MetricSpec(
        "stand_time_min", "min"
    ),  # not used in DailySnapshot, kept for completeness
    "active_energy": MetricSpec("active_energy_kj", "kJ"),
    "flights_climbed": MetricSpec("flights_climbed", "count"),
    "apple_stand_hour": MetricSpec("stand_hours", "count"),
    "walking_running_distance": MetricSpec("distance_km", "km"),
    "step_count": MetricSpec("steps", "count"),
    # heart.json
    "vo2_max": MetricSpec("vo2max", "mL/min·kg"),
    "walking_heart_rate_average": MetricSpec("walking_hr_avg", "count/min"),
    "resting_heart_rate": MetricSpec("resting_hr", "count/min"),
    "heart_rate_variability": MetricSpec("hrv_ms", "ms"),
    # heart_rate handled separately (Min/Avg/Max)
    # mobility.json
    "stair_speed_down": MetricSpec("stair_speed_down_ms", "m/s"),
    "stair_speed_up": MetricSpec("stair_speed_up_ms", "m/s"),
    "walking_asymmetry_percentage": MetricSpec("walking_asymmetry_pct", "%"),
    "running_stride_length": MetricSpec("running_stride_length_m", "m"),
    "running_power": MetricSpec("running_power_w", "W"),
    "running_speed": MetricSpec("running_speed_kmh", "km/h"),
    "walking_double_support_percentage": MetricSpec("walking_double_support_pct", "%"),
    "walking_speed": MetricSpec("walking_speed_kmh", "km/h"),
    "walking_step_length": MetricSpec("walking_step_length_cm", "cm"),
    # overnight recovery metrics — see _NIGHT_ATTRIBUTED_METRICS
    "respiratory_rate": MetricSpec("respiratory_rate", "count/min"),
    "apple_sleeping_wrist_temperature": MetricSpec("sleeping_wrist_temp_c", "degC"),
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
    norm = UnitNormaliser()

    for metric in data["data"]["metrics"]:
        name = metric["name"]

        if name == "heart_rate":
            if not norm.accepts(metric.get("units"), _HEART_RATE_UNIT, field=name):
                continue
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
            sleep_unit = metric.get("units")
            if not norm.accepts(sleep_unit, _SLEEP_UNIT, field=name):
                continue

            def hours(value: float | None) -> float | None:
                # Nulls pass through so the guards below behave as they always
                # did. The unit was accepted above, so convert cannot return None
                # for a real number.
                if value is None:
                    return None
                return norm.convert(value, sleep_unit, _SLEEP_UNIT, field=name)

            # Auto Export format: pre-aggregated nightly totals.
            # Fields: totalSleep, deep, core, rem, awake (hours),
            #         sleepStart/sleepEnd, date.
            for entry in metric.get("data", []):
                total = hours(entry.get("totalSleep", 0.0))
                # Auto Export emits zero-hour entries for nights the watch was
                # not worn. Storing them makes an untracked night look like a
                # night without sleep to every average and every SQL query.
                if not total or total <= 0:
                    continue
                deep = hours(entry.get("deep", 0.0))
                core = hours(entry.get("core", 0.0))
                rem = hours(entry.get("rem", 0.0))
                awake = hours(entry.get("awake", 0.0))
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

        spec = METRIC_MAP.get(name)
        if spec is None:
            continue  # unknown metric — ignore, before its unit is looked at
        field = spec.field

        # One label covers the whole metric. Check it once up front so an
        # unusable unit drops the metric with a single warning.
        unit = metric.get("units")
        if not norm.accepts(unit, spec.unit, field=name):
            continue

        if name in _NIGHT_ATTRIBUTED_METRICS:
            for entry in metric.get("data", []):
                if "qty" not in entry:
                    continue
                night_date = _night_start_date(entry["date"])
                if night_date is None:
                    continue
                # Convert each reading before averaging: degF -> degC is affine.
                night_readings.setdefault((night_date, field), []).append(
                    norm.convert(float(entry["qty"]), unit, spec.unit, field=name)
                )
            continue

        for entry in metric.get("data", []):
            if "qty" not in entry:
                continue
            date = _parse_date(entry["date"])
            day_readings.setdefault((date, name, field), []).append(
                norm.convert(float(entry["qty"]), unit, spec.unit, field=name)
            )

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
