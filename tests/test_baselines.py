"""Tests for src/baselines.py."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from models import DailySnapshot, WorkoutSnapshot, WorkoutSplit
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

    def test_yoy_window_with_single_sample_is_suppressed(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A single sparse reading a year ago must not poison the YoY column."""
        snapshots = [
            DailySnapshot(date=_days_ago(i), sleep_total_h=7.0) for i in range(10)
        ]
        # One solitary very-short sleep sample a year ago; real history has >0.
        snapshots.append(DailySnapshot(date=_days_ago(365), sleep_total_h=0.26))
        store_snapshots(in_memory_db, snapshots)

        result = compute_baselines(in_memory_db)

        # The suppressed YoY value must not render as a numeric baseline.
        assert "0.26" not in result

    def test_yoy_window_ignores_zero_padded_sleep_rows(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Apple writes sleep_total_h=0 for untracked nights — ignore those."""
        snapshots = [
            DailySnapshot(date=_days_ago(i), sleep_total_h=7.0) for i in range(10)
        ]
        # A year ago: 20 zero-tracked nights plus one real 6.5 hr night. If the
        # guard only checked IS NOT NULL, AVG would collapse toward zero.
        for i in range(20):
            snapshots.append(DailySnapshot(date=_days_ago(360 + i), sleep_total_h=0.0))
        snapshots.append(DailySnapshot(date=_days_ago(365), sleep_total_h=6.5))
        store_snapshots(in_memory_db, snapshots)

        result = compute_baselines(in_memory_db)

        # Neither a drag-to-zero value nor a single-sample 6.5 should render.
        assert "0.31" not in result
        assert "6.50" not in result

    def test_year_over_year_and_seasonal_sections(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        snapshots = []
        # Seed multiple days per year-offset anchor so the YoY ±15-day window
        # contains enough samples to clear the minimum-sample guard.
        seed_rows = [
            (7, 50, 60.0, 10000, 5.8),
            (365 + 7, 53, 56.0, 9500, 5.5),
            (365 * 2 + 7, 55, 54.0, 9000, 5.2),
            (365 * 3 + 7, 57, 52.0, 8500, 4.9),
        ]
        for anchor_days_ago, resting_hr, hrv_ms, steps, split_pace in seed_rows:
            for offset in range(10):
                days_ago = anchor_days_ago + offset
                d = _days_ago(days_ago)
                workouts = []
                if offset == 0:
                    workouts.append(
                        WorkoutSnapshot(
                            type="Outdoor Run",
                            category="run",
                            start_utc=f"{d}T07:00:00Z",
                            duration_min=split_pace * 5,
                            gpx_distance_km=5.0,
                            splits=[
                                WorkoutSplit(
                                    km_index=index + 1,
                                    pace_min_km=split_pace,
                                    avg_speed_ms=3.0,
                                )
                                for index in range(5)
                            ],
                        )
                    )
                snapshots.append(
                    DailySnapshot(
                        date=d,
                        resting_hr=resting_hr,
                        hrv_ms=hrv_ms,
                        steps=steps,
                        workouts=workouts,
                    )
                )
        store_snapshots(in_memory_db, snapshots)

        result = compute_baselines(in_memory_db)

        assert "Same-season comparison" in result
        assert "Same month last year" in result
        assert "Seasonal run volume" in result
        assert "Same 4w 3y ago" in result
        assert "Annual best 5 km pace" in result
        assert "2026" in result
