"""Tests for comparing a synced run with the person's similar runs."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from models import DailySnapshot, WorkoutSnapshot
from run_comparison import NO_RUN_COMPARISON, describe_run_comparisons
from store import store_snapshots

_TODAY = date(2026, 9, 23)


def _run(
    day: date,
    *,
    km: float = 5.0,
    minutes: float = 29.0,
    hr: float | None = 155.0,
    category: str = "run",
    hour: int = 7,
) -> DailySnapshot:
    """Build one day holding a single workout."""
    iso = day.isoformat()
    return DailySnapshot(
        date=iso,
        workouts=[
            WorkoutSnapshot(
                type="Outdoor Run" if category == "run" else "Outdoor Walk",
                category=category,
                start_utc=f"{iso}T{hour:02d}:00:00Z",
                duration_min=minutes,
                gpx_distance_km=km,
                hr_avg=hr,
            )
        ],
    )


def _peers(conn: sqlite3.Connection, hrs: list[float], **kwargs: float) -> None:
    """Store one similar run per heart rate on consecutive earlier days."""
    store_snapshots(
        conn,
        [
            _run(_TODAY - timedelta(days=2 + i), hr=hr, **kwargs)
            for i, hr in enumerate(hrs)
        ],
    )


def _today_id() -> str:
    return f"{_TODAY.isoformat()}T07:00:00Z"


class TestDescribeRunComparisons:
    def test_no_workouts_in_sync(self, in_memory_db: sqlite3.Connection) -> None:
        assert describe_run_comparisons(in_memory_db, set()) == NO_RUN_COMPARISON

    def test_compares_heart_rate_with_similar_runs(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _peers(in_memory_db, [150, 152, 154, 156, 158])
        store_snapshots(in_memory_db, [_run(_TODAY, hr=160)])

        result = describe_run_comparisons(in_memory_db, {_today_id()})

        assert "5 similar runs" in result
        assert "median HR of 154 bpm" in result
        assert "higher than 5 of them and lower than 0" in result
        assert "5.0 km at 5:48/km, avg HR 160 bpm" in result

    def test_too_few_similar_runs_is_not_compared(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Four matches is not a norm, so no high/low verdict may be offered."""
        _peers(in_memory_db, [150, 152, 154, 156])
        store_snapshots(in_memory_db, [_run(_TODAY, hr=160)])

        result = describe_run_comparisons(in_memory_db, {_today_id()})

        assert "only 4 similar runs" in result
        assert "too few to compare" in result
        assert "median" not in result

    def test_singular_noun_for_one_match(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _peers(in_memory_db, [150])
        store_snapshots(in_memory_db, [_run(_TODAY)])

        result = describe_run_comparisons(in_memory_db, {_today_id()})

        assert "only 1 similar run in" in result

    def test_runs_outside_the_pace_band_do_not_count(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        # Today is 5:48/km; these five are 5:00/km, far outside the band.
        _peers(in_memory_db, [150, 152, 154, 156, 158], minutes=25.0)
        store_snapshots(in_memory_db, [_run(_TODAY)])

        result = describe_run_comparisons(in_memory_db, {_today_id()})

        assert "only 0 similar runs" in result

    def test_runs_outside_the_distance_band_do_not_count(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        # Same pace, double the distance: heart rate drifts over a longer run.
        _peers(in_memory_db, [150, 152, 154, 156, 158], km=10.0, minutes=58.0)
        store_snapshots(in_memory_db, [_run(_TODAY)])

        result = describe_run_comparisons(in_memory_db, {_today_id()})

        assert "only 0 similar runs" in result

    def test_runs_before_the_window_do_not_count(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        store_snapshots(
            in_memory_db,
            [_run(_TODAY - timedelta(days=400 + i)) for i in range(5)] + [_run(_TODAY)],
        )

        result = describe_run_comparisons(in_memory_db, {_today_id()})

        assert "only 0 similar runs" in result

    def test_only_earlier_runs_are_peers(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A later run must not be counted when an older run is re-synced."""
        store_snapshots(
            in_memory_db,
            [_run(_TODAY + timedelta(days=1 + i)) for i in range(5)] + [_run(_TODAY)],
        )

        result = describe_run_comparisons(in_memory_db, {_today_id()})

        assert "only 0 similar runs" in result

    def test_peers_without_heart_rate_are_ignored(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _peers(in_memory_db, [150, 152, 154, 156])
        store_snapshots(
            in_memory_db, [_run(_TODAY - timedelta(days=30), hr=None), _run(_TODAY)]
        )

        result = describe_run_comparisons(in_memory_db, {_today_id()})

        assert "only 4 similar runs" in result

    def test_short_runs_and_non_runs_get_no_comparison(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A 1.5 km jog at walking pace says nothing about fitness."""
        store_snapshots(
            in_memory_db,
            [
                _run(_TODAY, km=1.5, minutes=15.0),
                _run(_TODAY, category="walk", hour=18),
            ],
        )

        result = describe_run_comparisons(
            in_memory_db,
            {_today_id(), f"{_TODAY.isoformat()}T18:00:00Z"},
        )

        assert result == NO_RUN_COMPARISON
