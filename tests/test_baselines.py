"""Tests for src/baselines.py."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from models import DailySnapshot, WorkoutSnapshot
from baselines import compute_baselines
from store import store_snapshots


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


class TestComputeBaselines:
    def test_empty_db(self, in_memory_db: sqlite3.Connection) -> None:
        result = compute_baselines(in_memory_db)
        assert "Baselines" in result
        # All daily metrics should show "—" for no data
        assert "—" in result

    def test_daily_metric_averages(self, in_memory_db: sqlite3.Connection) -> None:
        snapshots = [
            DailySnapshot(
                date=_days_ago(i), resting_hr=50 + i, hrv_ms=60.0 - i, steps=10000
            )
            for i in range(10)
        ]
        store_snapshots(in_memory_db, snapshots)
        result = compute_baselines(in_memory_db)
        assert "Resting HR" in result
        assert "HRV (SDNN)" in result
        assert "Steps" in result
        # Should have actual numbers, not all dashes
        assert "50" in result or "54" in result

    def test_training_volume(self, in_memory_db: sqlite3.Connection) -> None:
        snapshots = []
        for i in range(8):
            d = _days_ago(i)
            snapshots.append(DailySnapshot(date=d))
            if i % 2 == 0:
                snapshots[-1].workouts = [
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc=f"{d}T07:00:00Z",
                        duration_min=30.0,
                        gpx_distance_km=5.0,
                    )
                ]
            else:
                snapshots[-1].workouts = [
                    WorkoutSnapshot(
                        type="Traditional Strength Training",
                        category="lift",
                        start_utc=f"{d}T17:00:00Z",
                        duration_min=45.0,
                    )
                ]
        store_snapshots(in_memory_db, snapshots)
        result = compute_baselines(in_memory_db)
        assert "Run distance" in result
        assert "Run sessions" in result
        assert "Lift sessions" in result
        assert "Lift duration" in result

    def test_best_pace(self, in_memory_db: sqlite3.Connection) -> None:
        d = _days_ago(5)
        snap = DailySnapshot(
            date=d,
            workouts=[
                WorkoutSnapshot(
                    type="Outdoor Run",
                    category="run",
                    start_utc=f"{d}T07:00:00Z",
                    duration_min=30.0,
                    gpx_distance_km=5.0,
                ),
            ],
        )
        store_snapshots(in_memory_db, [snap])
        result = compute_baselines(in_memory_db)
        # 30min / 5km = 6:00 min/km
        assert "6:00" in result
        assert "Best pace" in result

    def test_no_runs_no_pace(self, in_memory_db: sqlite3.Connection) -> None:
        d = _days_ago(5)
        snap = DailySnapshot(
            date=d,
            workouts=[
                WorkoutSnapshot(
                    type="Traditional Strength Training",
                    category="lift",
                    start_utc=f"{d}T17:00:00Z",
                    duration_min=45.0,
                ),
            ],
        )
        store_snapshots(in_memory_db, [snap])
        result = compute_baselines(in_memory_db)
        assert "Best pace" not in result
