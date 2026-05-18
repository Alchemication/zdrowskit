"""Add locality-level workout locations."""

from __future__ import annotations

import sqlite3

NAME = "workout locations"


def upgrade(conn: sqlite3.Connection) -> None:
    """Add canonical locations, coordinate cache, and workout links."""
    conn.executescript(
        """
        CREATE TABLE location (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            label               TEXT NOT NULL,
            locality            TEXT NOT NULL,
            region              TEXT NOT NULL DEFAULT '',
            country             TEXT NOT NULL DEFAULT '',
            country_code        TEXT NOT NULL DEFAULT '',
            source              TEXT NOT NULL DEFAULT 'geocoder',
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            UNIQUE(locality, region, country_code)
        );

        CREATE TABLE location_point_cache (
            lat_round           REAL NOT NULL,
            lon_round           REAL NOT NULL,
            location_id         INTEGER NOT NULL REFERENCES location(id),
            raw_json            TEXT,
            source              TEXT NOT NULL DEFAULT 'nominatim',
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            PRIMARY KEY (lat_round, lon_round)
        );

        ALTER TABLE workout ADD COLUMN location_id INTEGER REFERENCES location(id);
        ALTER TABLE manual_workout ADD COLUMN location_id INTEGER REFERENCES location(id);

        DROP VIEW workout_all;

        CREATE VIEW workout_all AS
            SELECT w.start_utc, w.date, w.type, w.category, w.counts_as_lift,
                   w.duration_min, w.hr_min, w.hr_avg, w.hr_max,
                   w.active_energy_kj, w.intensity_kcal_per_hr_kg,
                   w.temperature_c, w.humidity_pct,
                   w.gpx_distance_km, w.gpx_elevation_gain_m,
                   w.gpx_avg_speed_ms, w.gpx_max_speed_p95_ms,
                   w.location_id,
                   l.label AS location_label,
                   l.locality AS location_locality,
                   l.region AS location_region,
                   l.country AS location_country,
                   l.country_code AS location_country_code,
                   'import' AS source
            FROM workout w
            LEFT JOIN location l ON l.id = w.location_id
            UNION ALL
            SELECT mw.start_utc, mw.date, mw.type, mw.category, mw.counts_as_lift,
                   mw.duration_min, mw.hr_min, mw.hr_avg, mw.hr_max,
                   mw.active_energy_kj, mw.intensity_kcal_per_hr_kg,
                   mw.temperature_c, mw.humidity_pct,
                   mw.gpx_distance_km, mw.gpx_elevation_gain_m,
                   mw.gpx_avg_speed_ms, mw.gpx_max_speed_p95_ms,
                   mw.location_id,
                   l.label AS location_label,
                   l.locality AS location_locality,
                   l.region AS location_region,
                   l.country AS location_country,
                   l.country_code AS location_country_code,
                   'manual' AS source
            FROM manual_workout mw
            LEFT JOIN location l ON l.id = mw.location_id;
        """
    )
