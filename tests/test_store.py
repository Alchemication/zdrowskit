"""Tests for src/store.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from models import DailySnapshot, WorkoutSnapshot
from store import (
    load_date_range,
    load_snapshots,
    log_llm_call,
    open_db,
    store_snapshots,
)


class TestOpenDb:
    def test_creates_tables(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "test.db")
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "daily" in tables
        assert "workout" in tables
        assert "llm_call" in tables
        conn.close()


class TestStoreAndLoad:
    def test_round_trip(self, in_memory_db: sqlite3.Connection) -> None:
        """Store snapshots then load them back — all fields should survive."""
        workout = WorkoutSnapshot(
            type="Outdoor Run",
            category="run",
            start_utc="2026-03-10T07:00:00Z",
            duration_min=35.0,
            hr_min=120,
            hr_avg=155.0,
            hr_max=178,
            active_energy_kj=900.0,
            temperature_c=8.0,
            humidity_pct=65,
            gpx_distance_km=5.2,
            gpx_elevation_gain_m=45.0,
            gpx_avg_speed_ms=2.5,
            gpx_max_speed_p95_ms=3.8,
        )
        original = DailySnapshot(
            date="2026-03-10",
            steps=12000,
            distance_km=9.8,
            active_energy_kj=2200.0,
            exercise_min=55,
            stand_hours=12,
            resting_hr=54,
            hrv_ms=55.0,
            vo2max=45.2,
            recovery_index=55.0 / 54,
            workouts=[workout],
        )
        store_snapshots(in_memory_db, [original])
        loaded = load_snapshots(in_memory_db)

        assert len(loaded) == 1
        day = loaded[0]
        assert day.date == "2026-03-10"
        assert day.steps == 12000
        assert day.resting_hr == 54
        assert day.hrv_ms == 55.0
        assert day.vo2max == 45.2
        assert len(day.workouts) == 1

        w = day.workouts[0]
        assert w.type == "Outdoor Run"
        assert w.category == "run"
        assert w.duration_min == 35.0
        assert w.hr_avg == 155.0
        assert w.gpx_distance_km == 5.2
        assert w.temperature_c == 8.0
        assert w.humidity_pct == 65

    def test_upsert_overwrites(self, in_memory_db: sqlite3.Connection) -> None:
        day1 = DailySnapshot(date="2026-03-10", steps=5000, resting_hr=50)
        store_snapshots(in_memory_db, [day1])

        day1_updated = DailySnapshot(date="2026-03-10", steps=12000, resting_hr=54)
        store_snapshots(in_memory_db, [day1_updated])

        loaded = load_snapshots(in_memory_db)
        assert len(loaded) == 1
        assert loaded[0].steps == 12000
        assert loaded[0].resting_hr == 54

    def test_date_filter(self, in_memory_db: sqlite3.Connection) -> None:
        days = [
            DailySnapshot(date=f"2026-03-{d:02d}", steps=1000 * d) for d in range(9, 16)
        ]
        store_snapshots(in_memory_db, days)

        loaded = load_snapshots(in_memory_db, start="2026-03-11", end="2026-03-13")
        assert len(loaded) == 3
        assert loaded[0].date == "2026-03-11"
        assert loaded[-1].date == "2026-03-13"

    def test_empty_db(self, in_memory_db: sqlite3.Connection) -> None:
        assert load_snapshots(in_memory_db) == []
        assert load_date_range(in_memory_db) is None


class TestLoadDateRange:
    def test_returns_min_max(self, in_memory_db: sqlite3.Connection) -> None:
        days = [
            DailySnapshot(date="2026-03-09"),
            DailySnapshot(date="2026-03-15"),
        ]
        store_snapshots(in_memory_db, days)
        result = load_date_range(in_memory_db)
        assert result == ("2026-03-09", "2026-03-15")


class TestLogLlmCall:
    def test_inserts_record(self, in_memory_db: sqlite3.Connection) -> None:
        row_id = log_llm_call(
            in_memory_db,
            request_type="insights",
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_text="response",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            latency_s=1.5,
            cost=0.01,
        )
        assert row_id is not None
        assert row_id > 0

        row = in_memory_db.execute(
            "SELECT * FROM llm_call WHERE id = ?", (row_id,)
        ).fetchone()
        assert row["request_type"] == "insights"
        assert row["model"] == "test-model"
        assert row["cost"] == 0.01
