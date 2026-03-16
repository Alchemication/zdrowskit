"""SQLite persistence layer for the Apple Health pipeline.

Public API:
    open_db          -- open or create the database, return a connection
    store_snapshots  -- upsert DailySnapshots (and their workouts) into the DB
    load_snapshots   -- load DailySnapshots with nested workouts from the DB
    load_date_range  -- return the (min, max) date stored, or None if empty

Example:
    from pathlib import Path
    from store import open_db, store_snapshots, load_snapshots

    conn = open_db(Path("~/.local/share/zdrowskit/health.db").expanduser())
    store_snapshots(conn, snapshots)
    days = load_snapshots(conn, start="2026-01-01")
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from models import DailySnapshot, WorkoutSnapshot

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path.home() / "Documents" / "zdrowskit" / "health.db"

_DDL = """
CREATE TABLE IF NOT EXISTS daily (
    date                        TEXT PRIMARY KEY,
    steps                       INTEGER,
    distance_km                 REAL,
    active_energy_kj            REAL,
    exercise_min                INTEGER,
    stand_hours                 INTEGER,
    flights_climbed             REAL,
    resting_hr                  INTEGER,
    hrv_ms                      REAL,
    walking_hr_avg              REAL,
    hr_day_min                  INTEGER,
    hr_day_max                  INTEGER,
    vo2max                      REAL,
    walking_speed_kmh           REAL,
    walking_step_length_cm      REAL,
    walking_asymmetry_pct       REAL,
    walking_double_support_pct  REAL,
    stair_speed_up_ms           REAL,
    stair_speed_down_ms         REAL,
    running_stride_length_m     REAL,
    running_power_w             REAL,
    running_speed_kmh           REAL,
    recovery_index              REAL,
    imported_at                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workout (
    start_utc                TEXT PRIMARY KEY,
    date                     TEXT NOT NULL REFERENCES daily(date),
    type                     TEXT NOT NULL,
    category                 TEXT NOT NULL,
    duration_min             REAL NOT NULL,
    hr_min                   INTEGER,
    hr_avg                   REAL,
    hr_max                   INTEGER,
    active_energy_kj         REAL,
    intensity_kcal_per_hr_kg REAL,
    temperature_c            REAL,
    humidity_pct             INTEGER,
    gpx_distance_km          REAL,
    gpx_elevation_gain_m     REAL,
    gpx_avg_speed_ms         REAL,
    gpx_max_speed_p95_ms     REAL,
    imported_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS workout_date     ON workout(date);
CREATE INDEX IF NOT EXISTS workout_category ON workout(category);
"""


def default_db_path() -> Path:
    """Return the default database path.

    Returns:
        Path to ~/.local/share/zdrowskit/health.db, or the value of the
        zdrowskit_DB environment variable if set.
    """
    import os

    env = os.environ.get("zdrowskit_DB")
    if env:
        return Path(env).expanduser().resolve()
    return _DEFAULT_DB


def open_db(path: Path) -> sqlite3.Connection:
    """Open or create the SQLite database at *path*, run DDL, return connection.

    The parent directory is created if it does not exist. DDL uses
    CREATE TABLE IF NOT EXISTS so this is safe to call on every startup.

    Args:
        path: Filesystem path for the SQLite database file.

    Returns:
        An open sqlite3.Connection with foreign keys enabled and WAL mode set.
    """
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_DDL)
    conn.commit()
    logger.debug("Opened database: %s", path)
    return conn


def store_snapshots(conn: sqlite3.Connection, snapshots: list[DailySnapshot]) -> int:
    """Upsert DailySnapshots and their workouts into the database.

    Each day is replaced atomically: the daily row is upserted, existing
    workout rows for that date are deleted, then the current workouts are
    inserted. The entire batch runs in a single transaction.

    Args:
        conn: Open database connection returned by open_db().
        snapshots: List of DailySnapshot objects to persist.

    Returns:
        Number of days written.
    """
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        for s in snapshots:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily (
                    date, steps, distance_km, active_energy_kj,
                    exercise_min, stand_hours, flights_climbed,
                    resting_hr, hrv_ms, walking_hr_avg,
                    hr_day_min, hr_day_max, vo2max,
                    walking_speed_kmh, walking_step_length_cm,
                    walking_asymmetry_pct, walking_double_support_pct,
                    stair_speed_up_ms, stair_speed_down_ms,
                    running_stride_length_m, running_power_w, running_speed_kmh,
                    recovery_index, imported_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?, ?,
                    ?, ?
                )
                """,
                (
                    s.date,
                    s.steps,
                    s.distance_km,
                    s.active_energy_kj,
                    s.exercise_min,
                    s.stand_hours,
                    s.flights_climbed,
                    s.resting_hr,
                    s.hrv_ms,
                    s.walking_hr_avg,
                    s.hr_day_min,
                    s.hr_day_max,
                    s.vo2max,
                    s.walking_speed_kmh,
                    s.walking_step_length_cm,
                    s.walking_asymmetry_pct,
                    s.walking_double_support_pct,
                    s.stair_speed_up_ms,
                    s.stair_speed_down_ms,
                    s.running_stride_length_m,
                    s.running_power_w,
                    s.running_speed_kmh,
                    s.recovery_index,
                    now,
                ),
            )
            # Clear stale workout rows before re-inserting the current set.
            conn.execute("DELETE FROM workout WHERE date = ?", (s.date,))
            for w in s.workouts:
                conn.execute(
                    """
                    INSERT INTO workout (
                        start_utc, date, type, category, duration_min,
                        hr_min, hr_avg, hr_max,
                        active_energy_kj, intensity_kcal_per_hr_kg,
                        temperature_c, humidity_pct,
                        gpx_distance_km, gpx_elevation_gain_m,
                        gpx_avg_speed_ms, gpx_max_speed_p95_ms,
                        imported_at
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?,
                        ?, ?,
                        ?, ?,
                        ?, ?,
                        ?
                    )
                    """,
                    (
                        w.start_utc,
                        s.date,
                        w.type,
                        w.category,
                        w.duration_min,
                        w.hr_min,
                        w.hr_avg,
                        w.hr_max,
                        w.active_energy_kj,
                        w.intensity_kcal_per_hr_kg,
                        w.temperature_c,
                        w.humidity_pct,
                        w.gpx_distance_km,
                        w.gpx_elevation_gain_m,
                        w.gpx_avg_speed_ms,
                        w.gpx_max_speed_p95_ms,
                        now,
                    ),
                )
    logger.info("Stored %d day(s) to database", len(snapshots))
    return len(snapshots)


def load_snapshots(
    conn: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
) -> list[DailySnapshot]:
    """Load DailySnapshots with nested WorkoutSnapshots from the database.

    Args:
        conn: Open database connection returned by open_db().
        start: Inclusive ISO date lower bound, e.g. "2026-01-01". None = unbounded.
        end: Inclusive ISO date upper bound, e.g. "2026-03-15". None = unbounded.

    Returns:
        List of DailySnapshot objects sorted by date ascending, each with its
        workouts list populated.
    """
    conditions: list[str] = []
    params: list[str] = []
    if start:
        conditions.append("d.date >= ?")
        params.append(start)
    if end:
        conditions.append("d.date <= ?")
        params.append(end)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    daily_rows = conn.execute(
        f"""
        SELECT * FROM daily d
        {where}
        ORDER BY d.date ASC
        """,
        params,
    ).fetchall()

    if not daily_rows:
        return []

    # Load all workouts for the matched date range in one query.
    dates = [r["date"] for r in daily_rows]
    placeholders = ",".join("?" * len(dates))
    workout_rows = conn.execute(
        f"""
        SELECT * FROM workout
        WHERE date IN ({placeholders})
        ORDER BY date ASC, start_utc ASC
        """,
        dates,
    ).fetchall()

    # Group workouts by date for O(n) assembly.
    workouts_by_date: dict[str, list[WorkoutSnapshot]] = {d: [] for d in dates}
    for row in workout_rows:
        workouts_by_date[row["date"]].append(
            WorkoutSnapshot(
                type=row["type"],
                category=row["category"],
                start_utc=row["start_utc"],
                duration_min=row["duration_min"],
                hr_min=row["hr_min"],
                hr_avg=row["hr_avg"],
                hr_max=row["hr_max"],
                active_energy_kj=row["active_energy_kj"] or 0.0,
                intensity_kcal_per_hr_kg=row["intensity_kcal_per_hr_kg"],
                temperature_c=row["temperature_c"],
                humidity_pct=row["humidity_pct"],
                gpx_distance_km=row["gpx_distance_km"],
                gpx_elevation_gain_m=row["gpx_elevation_gain_m"],
                gpx_avg_speed_ms=row["gpx_avg_speed_ms"],
                gpx_max_speed_p95_ms=row["gpx_max_speed_p95_ms"],
            )
        )

    return [
        DailySnapshot(
            date=row["date"],
            steps=row["steps"],
            distance_km=row["distance_km"],
            active_energy_kj=row["active_energy_kj"],
            exercise_min=row["exercise_min"],
            stand_hours=row["stand_hours"],
            flights_climbed=row["flights_climbed"],
            resting_hr=row["resting_hr"],
            hrv_ms=row["hrv_ms"],
            walking_hr_avg=row["walking_hr_avg"],
            hr_day_min=row["hr_day_min"],
            hr_day_max=row["hr_day_max"],
            vo2max=row["vo2max"],
            walking_speed_kmh=row["walking_speed_kmh"],
            walking_step_length_cm=row["walking_step_length_cm"],
            walking_asymmetry_pct=row["walking_asymmetry_pct"],
            walking_double_support_pct=row["walking_double_support_pct"],
            stair_speed_up_ms=row["stair_speed_up_ms"],
            stair_speed_down_ms=row["stair_speed_down_ms"],
            running_stride_length_m=row["running_stride_length_m"],
            running_power_w=row["running_power_w"],
            running_speed_kmh=row["running_speed_kmh"],
            recovery_index=row["recovery_index"],
            workouts=workouts_by_date[row["date"]],
        )
        for row in daily_rows
    ]


def load_date_range(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """Return the (min_date, max_date) of all stored daily rows.

    Args:
        conn: Open database connection returned by open_db().

    Returns:
        A (min_date, max_date) tuple of ISO date strings, or None if the
        daily table is empty.
    """
    row = conn.execute("SELECT MIN(date), MAX(date) FROM daily").fetchone()
    if row[0] is None:
        return None
    return row[0], row[1]
