"""SQLite persistence layer for the Apple Health pipeline.

Public API:
    open_db          -- open or create the database, return a connection
    store_snapshots  -- upsert DailySnapshots (and their workouts) into the DB
    load_snapshots   -- load DailySnapshots with nested workouts from the DB
    load_date_range  -- return the (min, max) date stored, or None if empty
    log_llm_call     -- insert an LLM call record (input, output, params, metadata)

Example:
    from pathlib import Path
    from store import open_db, store_snapshots, load_snapshots

    conn = open_db(Path("~/.local/share/zdrowskit/health.db").expanduser())
    store_snapshots(conn, snapshots)
    days = load_snapshots(conn, start="2026-01-01")
"""

from __future__ import annotations

import json
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
    sleep_total_h               REAL,
    sleep_in_bed_h              REAL,
    sleep_efficiency_pct        REAL,
    sleep_deep_h                REAL,
    sleep_core_h                REAL,
    sleep_rem_h                 REAL,
    sleep_awake_h               REAL,
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

CREATE TABLE IF NOT EXISTS llm_call (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    request_type    TEXT NOT NULL,
    model           TEXT NOT NULL,
    messages_json   TEXT NOT NULL,
    response_text   TEXT NOT NULL,
    params_json     TEXT,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    total_tokens    INTEGER NOT NULL,
    latency_s       REAL NOT NULL,
    cost            REAL,
    metadata_json   TEXT
);

CREATE INDEX IF NOT EXISTS llm_call_type ON llm_call(request_type);
CREATE INDEX IF NOT EXISTS llm_call_ts   ON llm_call(timestamp);

CREATE TABLE IF NOT EXISTS llm_feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    llm_call_id   INTEGER NOT NULL REFERENCES llm_call(id),
    category      TEXT NOT NULL,
    reason        TEXT,
    created_at    TEXT NOT NULL,
    message_type  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS llm_feedback_call ON llm_feedback(llm_call_id);
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
    _migrate(conn)
    conn.commit()
    logger.debug("Opened database: %s", path)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply incremental schema migrations for columns added after initial DDL.

    Each migration is guarded by a column-existence check so it runs at most
    once and is safe to call on every startup.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_call)").fetchall()}
    if "cost" not in cols:
        conn.execute("ALTER TABLE llm_call ADD COLUMN cost REAL")
        logger.info("Migrated llm_call: added 'cost' column")

    daily_cols = {r[1] for r in conn.execute("PRAGMA table_info(daily)").fetchall()}
    if not daily_cols:
        return  # daily table doesn't exist yet (e.g. partial schema in tests)
    sleep_cols = [
        "sleep_total_h",
        "sleep_in_bed_h",
        "sleep_efficiency_pct",
        "sleep_deep_h",
        "sleep_core_h",
        "sleep_rem_h",
        "sleep_awake_h",
    ]
    for col in sleep_cols:
        if col not in daily_cols:
            conn.execute(f"ALTER TABLE daily ADD COLUMN {col} REAL")
    if sleep_cols[0] not in daily_cols:
        logger.info("Migrated daily: added sleep columns")


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
                    sleep_total_h, sleep_in_bed_h, sleep_efficiency_pct,
                    sleep_deep_h, sleep_core_h, sleep_rem_h, sleep_awake_h,
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
                    ?, ?, ?,
                    ?, ?, ?, ?,
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
                    s.sleep_total_h,
                    s.sleep_in_bed_h,
                    s.sleep_efficiency_pct,
                    s.sleep_deep_h,
                    s.sleep_core_h,
                    s.sleep_rem_h,
                    s.sleep_awake_h,
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
            sleep_total_h=row["sleep_total_h"],
            sleep_in_bed_h=row["sleep_in_bed_h"],
            sleep_efficiency_pct=row["sleep_efficiency_pct"],
            sleep_deep_h=row["sleep_deep_h"],
            sleep_core_h=row["sleep_core_h"],
            sleep_rem_h=row["sleep_rem_h"],
            sleep_awake_h=row["sleep_awake_h"],
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


def log_llm_call(
    conn: sqlite3.Connection,
    request_type: str,
    model: str,
    messages: list[dict[str, str]],
    response_text: str,
    params: dict | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    latency_s: float = 0.0,
    cost: float | None = None,
    metadata: dict | None = None,
) -> int:
    """Insert an LLM call record into the llm_call table.

    Args:
        conn: Open database connection returned by open_db().
        request_type: Product-level call type, e.g. "insights" or "nudge".
        model: The litellm model string used.
        messages: The message list sent to the LLM.
        response_text: The full LLM response text.
        params: LLM call parameters (max_tokens, temperature, etc.).
        input_tokens: Number of input tokens reported by the API.
        output_tokens: Number of output tokens reported by the API.
        total_tokens: Total tokens (input + output).
        latency_s: Wall-clock time for the LLM call in seconds.
        cost: Actual cost in USD as reported by litellm.
        metadata: Product context (e.g. week, trigger_type).

    Returns:
        The row id of the inserted record.
    """
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO llm_call (
            timestamp, request_type, model, messages_json, response_text,
            params_json, input_tokens, output_tokens, total_tokens,
            latency_s, cost, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now,
            request_type,
            model,
            json.dumps(messages),
            response_text,
            json.dumps(params) if params else None,
            input_tokens,
            output_tokens,
            total_tokens,
            latency_s,
            cost,
            json.dumps(metadata) if metadata else None,
        ),
    )
    conn.commit()
    logger.debug(
        "Logged LLM call id=%d type=%s model=%s", cursor.lastrowid, request_type, model
    )
    return cursor.lastrowid


def log_feedback(
    conn: sqlite3.Connection,
    llm_call_id: int,
    category: str,
    message_type: str,
    reason: str | None = None,
) -> int:
    """Insert an LLM feedback record.

    Args:
        conn: Open database connection.
        llm_call_id: The llm_call row this feedback refers to.
        category: Feedback category (inaccurate, not_useful, too_verbose, wrong_tone).
        message_type: The LLM output type (insights, nudge, coach, chat).
        reason: Optional free-text explanation.

    Returns:
        The row id of the inserted record.
    """
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO llm_feedback (llm_call_id, category, reason, created_at, message_type)
        VALUES (?, ?, ?, ?, ?)
        """,
        (llm_call_id, category, reason, now, message_type),
    )
    conn.commit()
    logger.debug(
        "Logged feedback id=%d llm_call_id=%d category=%s",
        cursor.lastrowid,
        llm_call_id,
        category,
    )
    return cursor.lastrowid


def update_feedback_reason(
    conn: sqlite3.Connection,
    feedback_id: int,
    reason: str,
) -> None:
    """Update the free-text reason on an existing feedback record.

    Args:
        conn: Open database connection.
        feedback_id: The llm_feedback row to update.
        reason: The user-provided explanation text.
    """
    conn.execute(
        "UPDATE llm_feedback SET reason = ? WHERE id = ?",
        (reason, feedback_id),
    )
    conn.commit()
    logger.debug("Updated feedback id=%d with reason", feedback_id)
