"""Manual workout and sleep tables with unified views."""

from __future__ import annotations

import sqlite3

NAME = "manual activity tables and views"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create manual_workout, manual_sleep tables and workout_all, sleep_all views."""
    conn.executescript(
        """
        CREATE TABLE manual_workout (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            start_utc                TEXT NOT NULL,
            date                     TEXT NOT NULL,
            type                     TEXT NOT NULL,
            category                 TEXT NOT NULL,
            counts_as_lift           INTEGER NOT NULL DEFAULT 0,
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
            source_note              TEXT,
            created_at               TEXT NOT NULL
        );

        CREATE INDEX manual_workout_date ON manual_workout(date);

        CREATE TABLE manual_sleep (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL UNIQUE,
            sleep_total_h   REAL NOT NULL,
            sleep_in_bed_h  REAL,
            created_at      TEXT NOT NULL
        );

        CREATE INDEX manual_sleep_date ON manual_sleep(date);

        CREATE VIEW workout_all AS
            SELECT start_utc, date, type, category, counts_as_lift, duration_min,
                   hr_min, hr_avg, hr_max, active_energy_kj,
                   intensity_kcal_per_hr_kg, temperature_c, humidity_pct,
                   gpx_distance_km, gpx_elevation_gain_m,
                   gpx_avg_speed_ms, gpx_max_speed_p95_ms,
                   'import' AS source
            FROM workout
            UNION ALL
            SELECT start_utc, date, type, category, counts_as_lift, duration_min,
                   hr_min, hr_avg, hr_max, active_energy_kj,
                   intensity_kcal_per_hr_kg, temperature_c, humidity_pct,
                   gpx_distance_km, gpx_elevation_gain_m,
                   gpx_avg_speed_ms, gpx_max_speed_p95_ms,
                   'manual' AS source
            FROM manual_workout;

        CREATE VIEW sleep_all AS
            SELECT date, sleep_total_h, sleep_in_bed_h,
                   sleep_efficiency_pct, sleep_deep_h, sleep_core_h,
                   sleep_rem_h, sleep_awake_h,
                   'import' AS source
            FROM daily
            WHERE sleep_total_h IS NOT NULL
            UNION ALL
            SELECT date, sleep_total_h, sleep_in_bed_h,
                   NULL, NULL, NULL, NULL, NULL,
                   'manual' AS source
            FROM manual_sleep;
        """
    )
