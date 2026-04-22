"""Tests for src/milestones.py."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from milestones import compute_milestones
from models import DailySnapshot, WorkoutSnapshot, WorkoutSplit
from store import store_snapshots


def _date_days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


class TestComputeMilestones:
    def test_picks_fastest_5km_segment_and_reports_pr_age(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        slower_date = _date_days_ago(60)
        faster_date = _date_days_ago(10)
        snapshots = [
            DailySnapshot(
                date=slower_date,
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc=f"{slower_date}T07:00:00Z",
                        duration_min=27.5,
                        gpx_distance_km=5.0,
                        splits=[
                            WorkoutSplit(km_index=index + 1, pace_min_km=5.5)
                            for index in range(5)
                        ],
                    )
                ],
            ),
            DailySnapshot(
                date=faster_date,
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc=f"{faster_date}T07:00:00Z",
                        duration_min=22.5,
                        gpx_distance_km=5.0,
                        splits=[
                            WorkoutSplit(km_index=index + 1, pace_min_km=4.5)
                            for index in range(5)
                        ],
                    )
                ],
            ),
        ]
        store_snapshots(in_memory_db, snapshots)

        result = compute_milestones(in_memory_db)

        assert "Run PRs" in result
        assert "5 km PR: **4:30/km**" in result
        assert faster_date in result
        assert "10 days ago" in result
