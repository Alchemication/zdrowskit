"""Import snapshots and human-readable health-data deltas for the daemon."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from config import STANDOUT_RECENCY_DAYS
from store import open_db

logger = logging.getLogger(__name__)


def data_snapshot(db: Path, *, today: date | None = None) -> dict:
    """Snapshot database markers and recent imported workout values.

    Args:
        db: Known profile database path.
        today: Date anchoring the standout recency window.

    Returns:
        Row counts and max-date markers plus stable fingerprints for imported
        workouts inside the standout recency window. Empty dict on failure.
    """
    try:
        conn = open_db(db)
        cur = conn.cursor()
        snapshot: dict = {}
        for table, date_col in (
            ("daily", "date"),
            ("workout_all", "start_utc"),
            ("sleep_all", "date"),
        ):
            try:
                row = cur.execute(
                    f"SELECT COUNT(*), MAX({date_col}) FROM {table}"
                ).fetchone()
            except sqlite3.Error:
                continue
            snapshot[f"{table}_count"] = row[0] if row else 0
            snapshot[f"{table}_max"] = row[1] if row else None

        anchor = today or date.today()
        cutoff = (anchor - timedelta(days=STANDOUT_RECENCY_DAYS)).isoformat()
        workout_rows = cur.execute(
            "SELECT start_utc, date, category, duration_min, gpx_distance_km "
            "FROM workout WHERE date >= ? ORDER BY start_utc",
            (cutoff,),
        ).fetchall()
        split_rows = cur.execute(
            "SELECT ws.start_utc, ws.km_index, ws.pace_min_km "
            "FROM workout_split AS ws "
            "JOIN workout AS w ON w.start_utc = ws.start_utc "
            "WHERE w.date >= ? ORDER BY ws.start_utc, ws.km_index",
            (cutoff,),
        ).fetchall()
        splits: dict[str, list[list[int | float | None]]] = {}
        for start_utc, km_index, pace in split_rows:
            splits.setdefault(start_utc, []).append([km_index, pace])
        snapshot["recent_workouts"] = {
            start_utc: [
                workout_date,
                category,
                duration_min,
                distance_km,
                splits.get(start_utc, []),
            ]
            for start_utc, workout_date, category, duration_min, distance_km in workout_rows
        }
        conn.close()
        return snapshot
    except sqlite3.Error as exc:
        logger.warning("Data snapshot failed: %s", exc)
        return {}


def changed_workout_ids(before: dict, after: dict) -> set[str]:
    """Return recent imported workouts inserted or changed by one import."""
    before_rows = before.get("recent_workouts")
    after_rows = after.get("recent_workouts")
    if not isinstance(before_rows, dict) or not isinstance(after_rows, dict):
        return set()
    return {
        str(workout_id)
        for workout_id, fingerprint in after_rows.items()
        if before_rows.get(workout_id) != fingerprint
    }


def format_data_delta(db: Path, before: dict, after: dict) -> str:
    """Describe records that arrived between two data snapshots.

    Args:
        db: Known profile database path.
        before: Snapshot taken before the import ran.
        after: Snapshot taken after the import ran.

    Returns:
        Human-readable text the LLM can use to know what is actually new.
    """
    if not after:
        return "New health data synced (delta unavailable)."

    lines: list[str] = []
    try:
        conn = open_db(db)

        prev_workout_max = before.get("workout_all_max")
        if prev_workout_max:
            rows = conn.execute(
                "SELECT start_utc, date, type, category, duration_min, "
                "gpx_distance_km FROM workout_all "
                "WHERE start_utc > ? ORDER BY start_utc",
                (prev_workout_max,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT start_utc, date, type, category, duration_min, "
                "gpx_distance_km FROM workout_all "
                "ORDER BY start_utc DESC LIMIT 3"
            ).fetchall()
        for row in rows:
            duration = row["duration_min"]
            duration_text = f"{duration:.0f} min" if duration is not None else "?"
            distance = row["gpx_distance_km"]
            distance_text = f", {distance:.2f} km" if distance is not None else ""
            lines.append(
                f"- New workout: {row['type']} ({row['category']}), "
                f"{duration_text}{distance_text} on {row['date']}"
            )

        prev_sleep_max = before.get("sleep_all_max")
        if prev_sleep_max:
            rows = conn.execute(
                "SELECT date, sleep_total_h, sleep_efficiency_pct "
                "FROM sleep_all WHERE date > ? ORDER BY date",
                (prev_sleep_max,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT date, sleep_total_h, sleep_efficiency_pct "
                "FROM sleep_all ORDER BY date DESC LIMIT 2"
            ).fetchall()
        for row in rows:
            hours = row["sleep_total_h"]
            efficiency = row["sleep_efficiency_pct"]
            hours_text = f"{hours:.1f}h" if hours is not None else "?h"
            efficiency_text = (
                f", {efficiency:.0f}% efficiency" if efficiency is not None else ""
            )
            lines.append(
                f"- New sleep night: {row['date']} — {hours_text}{efficiency_text}"
            )

        prev_daily_max = before.get("daily_max")
        if prev_daily_max:
            rows = conn.execute(
                "SELECT date, steps, hrv_ms, resting_hr FROM daily "
                "WHERE date > ? ORDER BY date",
                (prev_daily_max,),
            ).fetchall()
            for row in rows:
                parts = []
                if row["steps"] is not None:
                    parts.append(f"steps {row['steps']}")
                if row["hrv_ms"] is not None:
                    parts.append(f"HRV {row['hrv_ms']:.0f} ms")
                if row["resting_hr"] is not None:
                    parts.append(f"RHR {row['resting_hr']:.0f} bpm")
                detail = ", ".join(parts) if parts else "(no metrics yet)"
                lines.append(f"- New daily row: {row['date']} — {detail}")
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Delta query failed: %s", exc)
        return "New health data synced (delta query failed)."

    if not lines:
        return (
            "Health data refreshed but no new completed activities or sleep "
            "nights since the previous sync. Today's metrics may have updated "
            "in place."
        )
    return "Records added in this import:\n" + "\n".join(lines)
