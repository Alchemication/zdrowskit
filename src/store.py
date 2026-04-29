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

from config import APP_HOME
from db.migrations import apply_migrations
from models import DailySnapshot, WorkoutSnapshot, WorkoutSplit

logger = logging.getLogger(__name__)

_DEFAULT_DB = APP_HOME / "health.db"


def default_db_path() -> Path:
    """Return the default database path.

    Returns:
        Path to the default app database, or the value of the ZDROWSKIT_DB
        environment variable if set.
    """
    import os

    env = os.environ.get("ZDROWSKIT_DB") or os.environ.get("zdrowskit_DB")
    if env:
        return Path(env).expanduser().resolve()
    return _DEFAULT_DB


def connect_db(path: Path, *, migrate: bool = True) -> sqlite3.Connection:
    """Open or create the SQLite database at *path* and optionally migrate.

    The parent directory is created if it does not exist.

    Args:
        path: Filesystem path for the SQLite database file.
        migrate: Whether to auto-apply pending migrations.

    Returns:
        An open sqlite3.Connection with foreign keys enabled and WAL mode set.
    """
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    if migrate:
        apply_migrations(conn)
    logger.debug("Opened database: %s", path)
    return conn


def open_db(path: Path) -> sqlite3.Connection:
    """Open or create the SQLite database at *path* and apply migrations."""
    return connect_db(path, migrate=True)


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
                        start_utc, date, type, category, counts_as_lift, duration_min,
                        hr_min, hr_avg, hr_max,
                        active_energy_kj, intensity_kcal_per_hr_kg,
                        temperature_c, humidity_pct,
                        gpx_distance_km, gpx_elevation_gain_m,
                        gpx_avg_speed_ms, gpx_max_speed_p95_ms,
                        imported_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?,
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
                        int(w.counts_as_lift),
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
                if w.splits:
                    conn.executemany(
                        """
                        INSERT INTO workout_split (
                            start_utc, km_index, pace_min_km, avg_speed_ms,
                            elevation_gain_m, elevation_loss_m
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                w.start_utc,
                                split.km_index,
                                split.pace_min_km,
                                split.avg_speed_ms,
                                split.elevation_gain_m,
                                split.elevation_loss_m,
                            )
                            for split in w.splits
                        ],
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
    workout_starts = [row["start_utc"] for row in workout_rows]
    splits_by_start: dict[str, list[WorkoutSplit]] = {}
    if workout_starts:
        split_placeholders = ",".join("?" * len(workout_starts))
        split_rows = conn.execute(
            f"""
            SELECT * FROM workout_split
            WHERE start_utc IN ({split_placeholders})
            ORDER BY start_utc ASC, km_index ASC
            """,
            workout_starts,
        ).fetchall()
        for row in split_rows:
            splits_by_start.setdefault(row["start_utc"], []).append(
                WorkoutSplit(
                    km_index=row["km_index"],
                    pace_min_km=row["pace_min_km"],
                    avg_speed_ms=row["avg_speed_ms"],
                    elevation_gain_m=row["elevation_gain_m"],
                    elevation_loss_m=row["elevation_loss_m"],
                )
            )

    for row in workout_rows:
        workouts_by_date[row["date"]].append(
            WorkoutSnapshot(
                type=row["type"],
                category=row["category"],
                counts_as_lift=bool(row["counts_as_lift"]),
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
                splits=splits_by_start.get(row["start_utc"], []),
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
        category: Feedback category (inaccurate, not_useful, too_verbose, wrong_tone, other).
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


def delete_feedback(conn: sqlite3.Connection, feedback_id: int) -> bool:
    """Delete a feedback record by id.

    Args:
        conn: Open database connection.
        feedback_id: The llm_feedback row to delete.

    Returns:
        True if a row was deleted, False otherwise.
    """
    cursor = conn.execute("DELETE FROM llm_feedback WHERE id = ?", (feedback_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    logger.debug("Deleted feedback id=%d deleted=%s", feedback_id, deleted)
    return deleted


def load_feedback_for_call(
    conn: sqlite3.Connection,
    llm_call_id: int,
) -> list[sqlite3.Row]:
    """Return feedback rows associated with a specific LLM call.

    Args:
        conn: Open database connection.
        llm_call_id: The llm_call row id.

    Returns:
        Feedback rows ordered newest-first.
    """
    return conn.execute(
        """
        SELECT id, llm_call_id, category, reason, created_at, message_type
        FROM llm_feedback
        WHERE llm_call_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (llm_call_id,),
    ).fetchall()


def load_feedback_entries(
    conn: sqlite3.Connection,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Return recent feedback entries joined to their originating LLM call.

    Args:
        conn: Open database connection.
        limit: Maximum number of entries to return.

    Returns:
        Joined feedback + llm_call rows ordered newest-first.
    """
    return conn.execute(
        """
        SELECT
            f.id AS feedback_id,
            f.llm_call_id,
            f.category,
            f.reason,
            f.created_at,
            f.message_type,
            c.timestamp,
            c.request_type,
            c.model,
            c.metadata_json
        FROM llm_feedback AS f
        JOIN llm_call AS c
          ON c.id = f.llm_call_id
        ORDER BY f.created_at DESC, f.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Manual activity helpers
# ---------------------------------------------------------------------------

# Column names shared between workout and manual_workout (excluding PKs and
# metadata columns like imported_at / created_at).
_WORKOUT_CLONE_COLUMNS = (
    "type",
    "category",
    "counts_as_lift",
    "duration_min",
    "hr_min",
    "hr_avg",
    "hr_max",
    "active_energy_kj",
    "intensity_kcal_per_hr_kg",
    "temperature_c",
    "humidity_pct",
    "gpx_distance_km",
    "gpx_elevation_gain_m",
    "gpx_avg_speed_ms",
    "gpx_max_speed_p95_ms",
)


def insert_manual_workout(
    conn: sqlite3.Connection,
    clone_row: dict,
    date: str,
    source_note: str | None = None,
    feel: str | None = None,
    feel_adjusted: bool = False,
) -> int:
    """Insert a manually-logged workout cloned from a historical entry.

    Args:
        conn: Open database connection.
        clone_row: Dict with workout column values (keys matching
            ``_WORKOUT_CLONE_COLUMNS``).  Missing keys default to ``None``.
        date: ISO date for the new entry (e.g. "2026-04-04").
        source_note: Free-text provenance note (e.g. "cloned from Apr 1 run").
        feel: Subjective feel tag (``easy``/``solid``/``hard``/``wrecked``).
        feel_adjusted: True when deterministic feel multipliers were applied
            on top of the clone.

    Returns:
        The ``id`` of the inserted ``manual_workout`` row.
    """
    now = datetime.now(timezone.utc).isoformat()
    # Generate a synthetic start_utc — noon UTC on the target date, with a
    # fractional-second suffix derived from current time to avoid collisions.
    micro = datetime.now(timezone.utc).strftime("%f")
    start_utc = f"{date}T12:00:00.{micro}Z"

    values = {col: clone_row.get(col) for col in _WORKOUT_CLONE_COLUMNS}
    # Ensure NOT NULL columns have safe defaults.
    if values.get("counts_as_lift") is None:
        values["counts_as_lift"] = 0
    if values.get("duration_min") is None:
        values["duration_min"] = 0
    values["start_utc"] = start_utc
    values["date"] = date
    values["source_note"] = source_note
    values["feel"] = feel
    values["feel_adjusted"] = 1 if feel_adjusted else 0
    values["created_at"] = now

    cols = ", ".join(values.keys())
    placeholders = ", ".join("?" * len(values))
    cursor = conn.execute(
        f"INSERT INTO manual_workout ({cols}) VALUES ({placeholders})",
        tuple(values.values()),
    )
    conn.commit()
    logger.info(
        "Inserted manual workout id=%d type=%s date=%s",
        cursor.lastrowid,
        clone_row.get("type"),
        date,
    )
    return cursor.lastrowid


def insert_manual_sleep(
    conn: sqlite3.Connection,
    date: str,
    sleep_total_h: float,
    sleep_in_bed_h: float | None = None,
    feel: str | None = None,
    feel_adjusted: bool = False,
) -> int:
    """Insert or replace a manually-logged sleep entry.

    Args:
        conn: Open database connection.
        date: ISO date (the morning date, i.e. when the user woke up).
        sleep_total_h: Total sleep hours (excluding awake time).
        sleep_in_bed_h: Total time in bed (including awake). Estimated from
            sleep_total_h if not provided.
        feel: Subjective feel tag (``solid``/``ok``/``restless``/``wrecked``).
        feel_adjusted: True when a non-default in-bed multiplier was applied.

    Returns:
        The ``id`` of the inserted ``manual_sleep`` row.
    """
    now = datetime.now(timezone.utc).isoformat()
    if sleep_in_bed_h is None:
        sleep_in_bed_h = round(sleep_total_h * 1.08, 2)
    cursor = conn.execute(
        """
        INSERT OR REPLACE INTO manual_sleep
            (date, sleep_total_h, sleep_in_bed_h, feel, feel_adjusted, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            date,
            sleep_total_h,
            sleep_in_bed_h,
            feel,
            1 if feel_adjusted else 0,
            now,
        ),
    )
    conn.commit()
    logger.info("Inserted manual sleep date=%s total_h=%.1f", date, sleep_total_h)
    return cursor.lastrowid


def delete_manual_workout(conn: sqlite3.Connection, workout_id: int) -> bool:
    """Delete a manual workout by id.

    Args:
        conn: Open database connection.
        workout_id: The ``manual_workout.id`` to delete.

    Returns:
        True if a row was deleted, False otherwise.
    """
    cursor = conn.execute("DELETE FROM manual_workout WHERE id = ?", (workout_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    logger.debug("Deleted manual workout id=%d deleted=%s", workout_id, deleted)
    return deleted


def delete_manual_sleep(conn: sqlite3.Connection, date: str) -> bool:
    """Delete a manual sleep entry by date.

    Args:
        conn: Open database connection.
        date: ISO date of the sleep entry to delete.

    Returns:
        True if a row was deleted, False otherwise.
    """
    cursor = conn.execute("DELETE FROM manual_sleep WHERE date = ?", (date,))
    conn.commit()
    deleted = cursor.rowcount > 0
    logger.debug("Deleted manual sleep date=%s deleted=%s", date, deleted)
    return deleted


def get_frequent_workout_types(
    conn: sqlite3.Connection,
    limit: int = 4,
) -> list[dict]:
    """Return the most frequent workout types across imported and manual data.

    Args:
        conn: Open database connection.
        limit: Maximum number of types to return.

    Returns:
        List of dicts with ``type``, ``category``, and ``count`` keys,
        ordered by frequency descending.
    """
    rows = conn.execute(
        """
        SELECT type, category, COUNT(*) AS count
        FROM workout_all
        GROUP BY type, category
        ORDER BY count DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {"type": r["type"], "category": r["category"], "count": r["count"]}
        for r in rows
    ]
