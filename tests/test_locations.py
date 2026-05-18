"""Tests for workout locality resolution."""

from __future__ import annotations

import sqlite3

import pytest

from db.migrations import apply_migrations
from locations import prefetch_locations, resolve_workout_location


@pytest.fixture
def conn() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    apply_migrations(db)
    return db


class TestResolveWorkoutLocation:
    def test_reverse_geocodes_and_caches_coordinate(self, conn, monkeypatch) -> None:
        """Nearby starts should reuse the rounded coordinate cache."""
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
        cache_count = conn.execute(
            "SELECT COUNT(*) AS n FROM location_point_cache WHERE location_id = ?",
            (first_id,),
        ).fetchone()
        assert cache_count["n"] == 1

    def test_returns_none_for_missing_coordinates(self, conn) -> None:
        """Non-route workouts (no lat/lon) should resolve to None without I/O."""
        assert resolve_workout_location(conn, None, None) is None
        assert resolve_workout_location(conn, 51.81, None) is None

    def test_persists_failure_and_skips_retry(self, conn, monkeypatch) -> None:
        """A failed lookup is recorded in location_point_failed and not retried."""
        calls = []

        def fake_reverse_geocode(lat: float, lon: float):
            calls.append((lat, lon))
            return None

        monkeypatch.setattr("locations._reverse_geocode", fake_reverse_geocode)

        assert resolve_workout_location(conn, 51.81, -8.36) is None
        assert resolve_workout_location(conn, 51.81, -8.36) is None

        # Second call must hit the persisted failure cache, not the geocoder.
        assert calls == [(51.81, -8.36)]
        row = conn.execute(
            """
            SELECT attempt_count FROM location_point_failed
            WHERE lat_round = ? AND lon_round = ?
            """,
            (51.81, -8.36),
        ).fetchone()
        assert row is not None
        assert row["attempt_count"] == 1


class TestPrefetchLocations:
    def test_resolves_unique_coords_once_and_skips_cached(
        self, conn, monkeypatch
    ) -> None:
        """Prefetch coalesces duplicates and skips coords already cached."""
        calls = []

        def fake_reverse_geocode(lat: float, lon: float):
            calls.append((lat, lon))
            return (
                {
                    "display_name": "Crosshaven, Cork, Ireland",
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

        prefetch_locations(
            conn,
            [
                (51.80989, -8.35793),
                (51.80981, -8.35799),  # rounds to same cell
                (None, None),
                (51.90, -8.48),
            ],
            seen_at="2026-05-18T00:00:00+00:00",
        )

        # Two unique cells should each be looked up exactly once.
        assert sorted(calls) == [(51.81, -8.36), (51.90, -8.48)]

        calls.clear()
        # Second prefetch should be a no-op: cache covers both cells.
        prefetch_locations(
            conn,
            [(51.80989, -8.35793), (51.90, -8.48)],
            seen_at="2026-05-18T00:00:00+00:00",
        )
        assert calls == []
