"""DB-backed health data assembly for LLM prompts.

Builds the compact summary + history dict that ``llm_health`` renders into
prompt text. Split from ``llm_health`` to keep each module a manageable size.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta

from aggregator import summarise
from config import SLEEP_SYNC_CUTOFF_HOUR
from report import group_by_week, to_dict
from store import load_date_range, load_snapshots

logger = logging.getLogger(__name__)

_SLEEP_KEYS = frozenset(
    {
        "sleep_total_h",
        "sleep_in_bed_h",
        "sleep_efficiency_pct",
        "sleep_deep_h",
        "sleep_core_h",
        "sleep_rem_h",
        "sleep_awake_h",
    }
)


def build_llm_data(
    conn: sqlite3.Connection, months: int, week: str = "current"
) -> dict:
    """Build a compact summary + history JSON for LLM consumption.

    Args:
        conn: Open SQLite database connection.
        months: Number of months of history to include.
        week: Which week to report on — "current" or "last".

    Returns:
        A dict with 'current_week', 'history', 'since_days', 'week_complete',
        'week_label', 'week_start' and 'week_end' keys, JSON-serialisable. The
        early return for an empty database carries only 'current_week',
        'history' and 'week_complete'.
    """
    dr = load_date_range(conn)
    if dr is None:
        return {
            "current_week": {"summary": None},
            "history": [],
            "week_complete": False,
        }

    today = date.today()
    if week == "last":
        last_sunday = today - timedelta(days=today.weekday() + 1)
        week_start = (last_sunday - timedelta(days=6)).isoformat()
        week_end = last_sunday.isoformat()
    else:
        monday = today - timedelta(days=today.weekday())
        week_start = monday.isoformat()
        week_end = (monday + timedelta(days=6)).isoformat()

    logger.info(
        "Report dates: mode=%s, week=%s..%s, today=%s, db_range=%s..%s",
        week,
        week_start,
        week_end,
        today,
        dr[0],
        dr[1],
    )

    # Fetch one extra day before the week so we can shift sleep forward.
    sleep_start = (date.fromisoformat(week_start) - timedelta(days=1)).isoformat()
    current_snaps = load_snapshots(conn, start=sleep_start, end=week_end)

    history_end = (date.fromisoformat(week_start) - timedelta(days=1)).isoformat()
    # Round forward to a Monday: a window cut mid-week renders its first week
    # as two days reading "0 run, 1 lift", indistinguishable from a real week.
    history_start_day = date.fromisoformat(week_start) - timedelta(days=30 * months)
    history_start = (
        history_start_day + timedelta(days=-history_start_day.weekday() % 7)
    ).isoformat()
    history_snaps = load_snapshots(conn, start=history_start, end=history_end)
    history_weeks = group_by_week(history_snaps)

    ws = date.fromisoformat(week_start)
    iso = ws.isocalendar()
    week_label = f"{iso.year}-W{iso.week:02d}"

    days = _shift_sleep_forward([to_dict(s) for s in current_snaps], week_start)

    # --- Classify sleep status per day and compute compliance ---
    today_iso = today.isoformat()
    sleep_tracked, sleep_total_eligible, not_tracked_dates = _classify_sleep_status(
        days, today
    )

    # --- Build the today snapshot ---
    today_snapshot = _build_today_snapshot(days, today_iso)

    # --- Assemble the summary ---
    week_snaps = [s for s in current_snaps if s.date >= week_start]
    summary = to_dict(summarise(week_snaps)) if week_snaps else None

    if summary:
        summary["sleep_nights_tracked"] = sleep_tracked
        summary["sleep_nights_total"] = sleep_total_eligible
        summary["sleep_not_tracked_dates"] = not_tracked_dates
        if today_snapshot:
            summary["today"] = today_snapshot

    return {
        "current_week": {
            "summary": summary,
            "days": days,
        },
        "history": [{"summary": to_dict(summarise(w))} for w in history_weeks],
        "since_days": _build_since_days(conn, week_end, today),
        "week_complete": today > date.fromisoformat(week_end),
        "week_label": week_label,
        "week_start": week_start,
        "week_end": week_end,
    }


def _shift_sleep_forward(days: list[dict], boundary: str) -> list[dict]:
    """Move each night's sleep onto the morning it belongs to.

    Apple records a night's sleep against the day it started, but the user
    experiences it as the morning they woke up. Callers fetch one extra day
    before the window so the first in-window morning has a night to inherit;
    that extra day is dropped afterwards.

    Args:
        days: Day dicts in ascending date order, oldest first.
        boundary: First date that belongs to the window, ISO format. Days
            before it are dropped once their sleep has been shifted forward.

    Returns:
        The in-window days, sleep shifted.
    """
    for i in range(len(days) - 1, 0, -1):
        prev_day, cur_day = days[i - 1], days[i]
        for k in _SLEEP_KEYS:
            cur_day[k] = prev_day.get(k)
            prev_day.pop(k, None)
    if days and days[0].get("date", "") < boundary:
        days = days[1:]
    return days


def _classify_sleep_status(days: list[dict], today: date) -> tuple[int, int, list[str]]:
    """Tag each day with a sleep status and count tracking compliance.

    A night can be missing because the user did not wear the watch or because
    the export has not caught up yet. Only the first is a compliance miss, so
    today — and yesterday before the morning sync window closes — is "pending"
    rather than "not tracked" and does not count against the total.

    Args:
        days: Day dicts, mutated in place with a ``sleep_status`` key.
        today: The current date.

    Returns:
        Tuple of (nights tracked, nights eligible, dates not tracked).
    """
    today_iso = today.isoformat()
    yesterday_iso = (today - timedelta(days=1)).isoformat()
    before_sync_cutoff = datetime.now().hour < SLEEP_SYNC_CUTOFF_HOUR

    tracked = 0
    eligible = 0
    not_tracked_dates: list[str] = []

    for day in days:
        if not isinstance(day, dict):
            continue
        day_date = day.get("date")
        has_sleep = any(day.get(k) is not None for k in _SLEEP_KEYS)

        if has_sleep:
            day["sleep_status"] = "tracked"
            tracked += 1
            eligible += 1
        elif day_date == today_iso or (
            day_date == yesterday_iso and before_sync_cutoff
        ):
            day["sleep_status"] = "pending"
        else:
            day["sleep_status"] = "not_tracked"
            eligible += 1
            not_tracked_dates.append(day_date or "")

    return tracked, eligible, not_tracked_dates


def _build_since_days(
    conn: sqlite3.Connection, week_end: str, today: date
) -> list[dict]:
    """Build day cards for everything after the reported week, up to today.

    The weekly report runs on Monday morning but can be triggered by hand at
    any point in the following week. Without this the model is blind to the
    days between: it would talk about the finished week as if no time had
    passed, and recommend a session the user already did.

    Args:
        conn: Open SQLite database connection.
        week_end: Last date of the reported week, ISO format.
        today: The current date.

    Returns:
        Day dicts from the day after ``week_end`` through today, sleep
        shifted. Empty when the reported week is the current one.
    """
    first = date.fromisoformat(week_end) + timedelta(days=1)
    if first > today:
        return []
    # Fetch week_end too, so the first morning has a night to inherit.
    snaps = load_snapshots(conn, start=week_end, end=today.isoformat())
    days = _shift_sleep_forward([to_dict(s) for s in snaps], first.isoformat())
    _classify_sleep_status(days, today)
    return days


def _build_today_snapshot(days: list[dict], today_iso: str) -> dict | None:
    """Extract a compact snapshot for today from the shifted day list."""
    today_day = None
    for d in days:
        if isinstance(d, dict) and d.get("date") == today_iso:
            today_day = d
            break
    if today_day is None:
        return None

    workouts = []
    for w in today_day.get("workouts", []):
        distance_km = w.get("gpx_distance_km")
        duration_min = w.get("duration_min")
        pace_min_per_km: float | None = None
        if distance_km and duration_min and distance_km > 0:
            pace_min_per_km = round(duration_min / distance_km, 2)
        workouts.append(
            {
                "type": w.get("type"),
                "category": w.get("category"),
                "counts_as_lift": w.get("counts_as_lift"),
                "duration_min": duration_min,
                "distance_km": distance_km,
                "pace_min_per_km": pace_min_per_km,
                "avg_hr": w.get("hr_avg"),
                "elevation_gain_m": w.get("gpx_elevation_gain_m"),
                "location_label": w.get("location_label"),
                "location_locality": w.get("location_locality"),
                "location_country": w.get("location_country"),
                "location_country_code": w.get("location_country_code"),
            }
        )

    snapshot: dict = {
        "date": today_iso,
        "hrv_ms": today_day.get("hrv_ms"),
        "resting_hr": today_day.get("resting_hr"),
        "recovery_index": today_day.get("recovery_index"),
        "steps": today_day.get("steps"),
        "exercise_min": today_day.get("exercise_min"),
        "sleep_status": today_day.get("sleep_status", "pending"),
    }
    if snapshot["sleep_status"] == "tracked":
        snapshot["sleep_total_h"] = today_day.get("sleep_total_h")
        snapshot["sleep_efficiency_pct"] = today_day.get("sleep_efficiency_pct")

    if workouts:
        snapshot["workouts"] = workouts

    return snapshot
