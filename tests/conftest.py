"""Shared fixtures for zdrowskit tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from models import DailySnapshot, WorkoutSnapshot

import store as store_mod

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to the tests/fixtures/ directory."""
    return FIXTURES_DIR


@pytest.fixture
def sample_workout_run() -> WorkoutSnapshot:
    """A typical outdoor run with GPX data."""
    return WorkoutSnapshot(
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


@pytest.fixture
def sample_workout_lift() -> WorkoutSnapshot:
    """A typical strength training session."""
    return WorkoutSnapshot(
        type="Traditional Strength Training",
        category="lift",
        start_utc="2026-03-09T17:00:00Z",
        duration_min=60.0,
        hr_min=80,
        hr_avg=110.0,
        hr_max=145,
        active_energy_kj=600.0,
    )


@pytest.fixture
def sample_snapshots(
    sample_workout_run: WorkoutSnapshot,
    sample_workout_lift: WorkoutSnapshot,
) -> list[DailySnapshot]:
    """A full week (Mon 2026-03-09 to Sun 2026-03-15) with 2 workouts."""
    return [
        DailySnapshot(
            date="2026-03-09",
            steps=9500,
            distance_km=6.2,
            active_energy_kj=1800.0,
            exercise_min=30,
            stand_hours=10,
            resting_hr=52,
            hrv_ms=58.0,
            recovery_index=58.0 / 52,
            workouts=[sample_workout_lift],
        ),
        DailySnapshot(
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
            workouts=[sample_workout_run],
        ),
        DailySnapshot(
            date="2026-03-11",
            steps=8000,
            distance_km=5.5,
            active_energy_kj=1500.0,
            exercise_min=20,
            stand_hours=9,
            resting_hr=50,
            hrv_ms=62.0,
            recovery_index=62.0 / 50,
        ),
        DailySnapshot(
            date="2026-03-12",
            steps=10000,
            distance_km=7.0,
            active_energy_kj=1700.0,
            exercise_min=35,
            stand_hours=11,
            resting_hr=51,
            hrv_ms=60.0,
            recovery_index=60.0 / 51,
        ),
        DailySnapshot(
            date="2026-03-13",
            steps=7500,
            distance_km=5.0,
            active_energy_kj=1400.0,
            exercise_min=15,
            stand_hours=8,
            resting_hr=53,
            hrv_ms=57.0,
            recovery_index=57.0 / 53,
        ),
        DailySnapshot(
            date="2026-03-14",
            steps=11000,
            distance_km=8.0,
            active_energy_kj=2000.0,
            exercise_min=45,
            stand_hours=12,
            resting_hr=49,
            hrv_ms=65.0,
            recovery_index=65.0 / 49,
        ),
        DailySnapshot(
            date="2026-03-15",
            steps=6000,
            distance_km=4.0,
            active_energy_kj=1200.0,
            exercise_min=10,
            stand_hours=7,
            resting_hr=50,
            hrv_ms=63.0,
            recovery_index=63.0 / 50,
        ),
    ]


@pytest.fixture
def in_memory_db() -> sqlite3.Connection:
    """An in-memory SQLite database with schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(store_mod._DDL)
    return conn
