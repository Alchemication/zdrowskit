"""Tests for workout locality resolution."""

from __future__ import annotations

import sqlite3

from db.migrations import apply_migrations
from locations import resolve_workout_location


class TestResolveWorkoutLocation:
    def test_reverse_geocodes_and_caches_coordinate(self, monkeypatch) -> None:
        """Nearby starts should reuse the rounded coordinate cache."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_migrations(conn)
        calls = []

        def fake_reverse_geocode(lat: float, lon: float):
            calls.append((lat, lon))
            return (
                {
                    "display_name": "Crosshaven, County Cork, Ireland",
                    "address": {
                        "village": "Crosshaven",
                        "county": "County Cork",
                        "country": "Ireland",
                        "country_code": "ie",
                    },
                },
                "test",
            )

        monkeypatch.setattr("locations._reverse_geocode", fake_reverse_geocode)

        first_id = resolve_workout_location(conn, 51.80989, -8.35793)
        second_id = resolve_workout_location(conn, 51.80981, -8.35799)

        assert first_id == second_id
        assert calls == [(51.81, -8.36)]
        location = conn.execute("SELECT * FROM location").fetchone()
        assert location["label"] == "Crosshaven"
        assert location["country_code"] == "ie"
        workout_count = conn.execute(
            "SELECT COUNT(*) AS n FROM location_point_cache WHERE location_id = ?",
            (first_id,),
        ).fetchone()
        assert workout_count["n"] == 1
