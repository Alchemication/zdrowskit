"""Tests for computed goal adherence and the coach's review triggers."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from goal_check import build_goal_check, record_triggers
from models import DailySnapshot, WorkoutSnapshot
from store import store_snapshots
from weekly_targets import SPEC_BY_KEY, StoredTarget, save_targets

# Four completed Monday-start weeks, then the week the coach runs in.
WEEKS = ["2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21"]
TODAY = date(2026, 9, 28)  # Monday after the fourth week


def _target(key: str, value: float, category: str) -> StoredTarget:
    return StoredTarget(
        spec=SPEC_BY_KEY[key],
        category=category,
        target=value,
        threshold=None,
        goal_text=None,
        strategy_hash="hash",
        llm_call_id=None,
    )


def _seed(
    conn: sqlite3.Connection,
    runs_per_week: list[int],
    *,
    weeks: list[str] = WEEKS,
    lifts_per_week: int = 2,
) -> None:
    """Store 3-run/2-lift targets and the given sessions for each week."""
    snapshots: list[DailySnapshot] = []
    for week_start, runs in zip(weeks, runs_per_week):
        save_targets(
            conn,
            week_start,
            [
                _target("sessions_week", 3, "run"),
                _target("sessions_week", 2, "lift"),
            ],
        )
        monday = date.fromisoformat(week_start)
        for i in range(runs):
            day = (monday + timedelta(days=i)).isoformat()
            snapshots.append(
                DailySnapshot(
                    date=day,
                    workouts=[
                        WorkoutSnapshot(
                            type="Outdoor Run",
                            category="run",
                            start_utc=f"{day}T07:00:00Z",
                            duration_min=30.0,
                            gpx_distance_km=5.0,
                        )
                    ],
                )
            )
        for i in range(lifts_per_week):
            day = (monday + timedelta(days=4 + i)).isoformat()
            snapshots.append(
                DailySnapshot(
                    date=day,
                    workouts=[
                        WorkoutSnapshot(
                            type="Traditional Strength Training",
                            category="lift",
                            start_utc=f"{day}T17:00:00Z",
                            duration_min=45.0,
                        )
                    ],
                )
            )
    store_snapshots(conn, snapshots)


class TestBuildGoalCheck:
    def test_no_target_history(self, in_memory_db: sqlite3.Connection) -> None:
        check = build_goal_check(in_memory_db, None, today=TODAY)

        assert "No completed weeks with weekly targets yet." in check.text
        assert check.triggers == []

    def test_target_missed_most_weeks_forces_a_review(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, [3, 2, 2, 1])

        check = build_goal_check(in_memory_db, None, today=TODAY)

        assert [t.key for t in check.triggers] == ["miss:sessions_week/run"]
        assert "missed in 3 of the last 4 weeks" in check.triggers[0].sentence
        assert "W36 3/3, W37 2/3, W38 2/3, W39 1/3" in check.text
        assert "SKIP is not an option" in check.text

    def test_target_met_every_week_is_reported_not_forced(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Under a consistency goal, hitting the target is the plan working."""
        _seed(in_memory_db, [3, 3, 3, 3])

        check = build_goal_check(in_memory_db, None, today=TODAY)

        assert "met 4 of 4" in check.text
        assert check.triggers == []
        assert "Review required this week: no." in check.text

    def test_missed_half_the_time_is_a_stretch_not_a_trigger(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, [3, 2, 3, 2])

        check = build_goal_check(in_memory_db, None, today=TODAY)

        assert check.triggers == []

    def test_too_few_weeks_cannot_trigger(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, [1, 1, 1], weeks=WEEKS[1:])

        check = build_goal_check(in_memory_db, None, today=TODAY)

        assert check.triggers == []
        assert "too few to call any target persistently missed" in check.text

    def test_the_current_week_is_not_judged(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A week still in progress is not a miss."""
        _seed(in_memory_db, [3, 3, 3, 3])
        _seed(in_memory_db, [0], weeks=["2026-09-28"])

        check = build_goal_check(in_memory_db, None, today=TODAY)

        assert "W40" not in check.text
        assert check.triggers == []

    def test_dropped_target_does_not_trigger(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A target no longer set is not worth a review."""
        _seed(in_memory_db, [1, 1, 1, 1])
        save_targets(in_memory_db, WEEKS[-1], [_target("sessions_week", 2, "lift")])

        check = build_goal_check(in_memory_db, None, today=TODAY)

        assert check.triggers == []

    def test_trigger_cools_down_after_it_is_raised(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, [1, 1, 1, 1])
        first = build_goal_check(in_memory_db, None, today=TODAY)
        record_triggers(in_memory_db, first.triggers, today=TODAY, llm_call_id=7)

        next_week = build_goal_check(
            in_memory_db, None, today=TODAY + timedelta(days=7)
        )
        month_later = build_goal_check(
            in_memory_db, None, today=TODAY + timedelta(days=28)
        )

        assert next_week.triggers == []
        assert [t.key for t in month_later.triggers] == ["miss:sessions_week/run"]

    def test_unrecorded_trigger_fires_again(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A coach that skipped never raised it, so it must come back."""
        _seed(in_memory_db, [1, 1, 1, 1])
        build_goal_check(in_memory_db, None, today=TODAY)

        again = build_goal_check(in_memory_db, None, today=TODAY)

        assert [t.key for t in again.triggers] == ["miss:sessions_week/run"]


class TestReviewDate:
    STRATEGY = "## Goals — Priority (set 2026-08-01, review by {due})\n- 3 runs\n"

    def test_passed_review_date_forces_a_review(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        check = build_goal_check(
            in_memory_db, self.STRATEGY.format(due="2026-09-20"), today=TODAY
        )

        assert [t.key for t in check.triggers] == ["review:2026-09-20"]
        assert "passed 8 days ago" in check.text

    def test_future_review_date_is_reported(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        check = build_goal_check(
            in_memory_db, self.STRATEGY.format(due="2026-11-19"), today=TODAY
        )

        assert check.triggers == []
        assert "2026-11-19, in 52 days" in check.text

    def test_no_review_date(self, in_memory_db: sqlite3.Connection) -> None:
        check = build_goal_check(in_memory_db, "## Goals\n- 3 runs\n", today=TODAY)

        assert "The goals carry no review date." in check.text
        assert check.triggers == []

    def test_earliest_review_date_is_the_one_that_counts(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        strategy = "## Goals — A (review by 2026-12-01)\n\n## Goals — B (review by 2026-09-01)\n"

        check = build_goal_check(in_memory_db, strategy, today=TODAY)

        assert [t.key for t in check.triggers] == ["review:2026-09-01"]
