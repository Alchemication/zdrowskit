"""Tests for the data-maturity block injected into coaching prompts."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from data_maturity import build_data_maturity
from llm_context import UNFILLED_CONTEXT
from store import DailySnapshot, WorkoutSnapshot, store_snapshots


def _days_ago(n: int) -> str:
    """Return an ISO date n days before today."""
    return (date.today() - timedelta(days=n)).isoformat()


def _seed(conn: sqlite3.Connection, days: int, *, with_workouts: bool = False) -> None:
    """Store a trailing run of complete days."""
    snapshots = []
    for i in range(days):
        d = _days_ago(i)
        workouts = []
        if with_workouts:
            workouts.append(
                WorkoutSnapshot(
                    type="Outdoor Run",
                    category="run",
                    start_utc=f"{d}T07:00:00Z",
                    duration_min=30.0,
                    gpx_distance_km=5.0,
                )
            )
        snapshots.append(
            DailySnapshot(
                date=d,
                resting_hr=52,
                hrv_ms=56.0,
                steps=8000,
                sleep_total_h=7.2,
                workouts=workouts,
            )
        )
    store_snapshots(conn, snapshots)


class TestBuildDataMaturity:
    def test_empty_profile(self, in_memory_db: sqlite3.Connection) -> None:
        result = build_data_maturity(in_memory_db)

        assert "History: none" in result
        assert "Workouts recorded: none" in result
        assert "Established metrics (30d): none" in result

    def test_thin_profile_lists_untrustworthy_metrics(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Three readings are observations, and must be labelled as such."""
        _seed(in_memory_db, 3)

        result = build_data_maturity(in_memory_db)

        assert "History: 3 days" in result
        assert "not yet completed a full week" in result
        assert "Established metrics (30d): none" in result
        assert "Too few readings to generalise from" in result
        assert "Resting HR (3)" in result

    def test_mature_profile_reports_established_metrics(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, 60, with_workouts=True)

        result = build_data_maturity(in_memory_db)

        assert "History: 60 days" in result
        assert "Established metrics (30d): Resting HR" in result
        assert "Too few readings" not in result
        assert "not yet completed a full week" not in result
        assert "run ×60" in result

    def test_absent_metrics_are_not_a_problem(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A metric the phone never records is unavailable, not a zero."""
        _seed(in_memory_db, 40)

        result = build_data_maturity(in_memory_db)

        assert "Not tracked at all" in result
        assert "VO2max" in result
        assert "rather than as zero" in result

    def test_no_workouts_is_not_evidence_of_inactivity(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, 40)

        result = build_data_maturity(in_memory_db)

        assert "Workouts recorded: none" in result
        assert "not evidence that they do not" in result

    def test_unfilled_context_is_reported(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, 40)
        context = {
            "me": UNFILLED_CONTEXT,
            "strategy": UNFILLED_CONTEXT,
            "history": UNFILLED_CONTEXT,
        }

        result = build_data_maturity(in_memory_db, context)

        assert "Not filled in yet: me.md and strategy.md" in result
        assert "no weekly plan" in result
        assert "No coaching history" in result

    def test_filled_context_adds_no_warnings(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A mature profile must not pay tokens for cold-start caveats."""
        _seed(in_memory_db, 60, with_workouts=True)
        context = {
            "me": "38, runs and lifts.",
            "strategy": "## Weekly Plan\n- 3 runs",
            "history": "## 2026-W30\n- Tempo held.",
        }

        result = build_data_maturity(in_memory_db, context)

        assert "Not filled in yet" not in result
        assert "no weekly plan" not in result
        assert "No coaching history" not in result

    def test_missing_files_read_as_not_told(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """An absent context file means the coach was never told, same as blank."""
        _seed(in_memory_db, 40)
        context = {"me": "(not provided)", "strategy": "(not provided)"}

        result = build_data_maturity(in_memory_db, context)

        assert "Not filled in yet: me.md and strategy.md" in result
