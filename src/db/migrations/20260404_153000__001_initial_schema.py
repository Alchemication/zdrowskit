"""Initial SQLite schema for zdrowskit."""

from __future__ import annotations

import sqlite3

NAME = "initial schema"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the original application schema."""
    conn.executescript(
        """
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

        CREATE INDEX IF NOT EXISTS workout_date ON workout(date);
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
            metadata_json   TEXT
        );

        CREATE INDEX IF NOT EXISTS llm_call_type ON llm_call(request_type);
        CREATE INDEX IF NOT EXISTS llm_call_ts ON llm_call(timestamp);

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
    )
