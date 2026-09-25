"""Tests for coach challenges: validation, lifecycle, scoring and text."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from challenges import (
    NO_CHALLENGE_NEWS,
    ACHIEVED,
    ACTIVE,
    DROPPED,
    EXPIRED,
    MISSED,
    PARTIAL,
    REJECTED,
    ChallengeError,
    accept,
    active_challenge,
    challenge_status_text,
    close_finished,
    coerce_proposal,
    describe_active,
    describe_challenge,
    describe_history,
    drop,
    expire_stale_proposals,
    get_challenge,
    nudge_challenge_text,
    open_proposal,
    outcome_message,
    propose,
    reject,
    set_reason,
)
from models import DailySnapshot, WorkoutSnapshot
from store import store_snapshots

MONDAY = date(2026, 9, 28)
KNOWN_TYPES: frozenset[str] = frozenset()


def _raw(**overrides: object) -> dict:
    raw = {
        "title": "Four-run weeks",
        "goal": "Consistency comes first: 3 runs (15 km) + 2 lifts",
        "rationale": "Runs held at 3 for four weeks.",
        "metric": "sessions_week",
        "category": "run",
        "target": 4,
        "weeks": 2,
    }
    raw.update(overrides)
    return raw


def _proposed(conn: sqlite3.Connection, **overrides: object) -> int:
    return propose(conn, coerce_proposal(_raw(**overrides), KNOWN_TYPES), llm_call_id=9)


def _runs(conn: sqlite3.Connection, week_start: date, count: int) -> None:
    snapshots = []
    for i in range(count):
        day = (week_start + timedelta(days=i)).isoformat()
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
    store_snapshots(conn, snapshots)


class TestCoerceProposal:
    def test_valid_proposal(self) -> None:
        proposal = coerce_proposal(_raw(), KNOWN_TYPES)

        assert proposal.weeks == 2
        assert proposal.target.spec.key == "sessions_week"
        assert proposal.target.category == "run"
        assert describe_challenge(proposal) == "Runs: 4 sessions a week for 2 weeks"

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"title": ""}, "title is required"),
            ({"title": "x" * 81}, "at most 80"),
            ({"goal": " "}, "goal is required"),
            ({"weeks": 0}, "between 1 and 4"),
            ({"weeks": 5}, "between 1 and 4"),
            ({"weeks": "two"}, "whole number"),
            ({"metric": "vo2max_week"}, "invalid for the vocabulary"),
            ({"category": "swim"}, "invalid for the vocabulary"),
            ({"metric": "sleep_nights_week", "category": None, "target": 5}, "invalid"),
        ],
    )
    def test_invalid_proposals_say_why(self, overrides: dict, message: str) -> None:
        with pytest.raises(ChallengeError, match=message):
            coerce_proposal(_raw(**overrides), KNOWN_TYPES)

    def test_threshold_metric(self) -> None:
        proposal = coerce_proposal(
            _raw(metric="sleep_nights_week", category=None, target=5, threshold=7),
            KNOWN_TYPES,
        )

        assert describe_challenge(proposal) == "Sleep ≥7h: 5 nights a week for 2 weeks"


class TestLifecycle:
    def test_propose_expires_the_previous_open_proposal(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        first = _proposed(in_memory_db)
        second = _proposed(in_memory_db, title="Second")

        assert get_challenge(in_memory_db, first).status == EXPIRED
        assert open_proposal(in_memory_db).id == second

    def test_cannot_propose_while_one_is_active(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        accept(in_memory_db, _proposed(in_memory_db), today=MONDAY)

        with pytest.raises(ChallengeError, match="already active"):
            _proposed(in_memory_db)

    @pytest.mark.parametrize(
        ("accepted_on", "start"),
        [
            (MONDAY, "2026-09-28"),
            (MONDAY + timedelta(days=1), "2026-09-28"),
            (MONDAY + timedelta(days=2), "2026-10-05"),
            (MONDAY + timedelta(days=6), "2026-10-05"),
        ],
    )
    def test_accept_starts_in_whole_weeks(
        self, in_memory_db: sqlite3.Connection, accepted_on: date, start: str
    ) -> None:
        challenge = accept(in_memory_db, _proposed(in_memory_db), today=accepted_on)

        assert challenge.status == ACTIVE
        assert challenge.start_week == start
        end = date.fromisoformat(start) + timedelta(days=13)
        assert challenge.end_date == end.isoformat()

    def test_accept_twice_or_after_reject_does_nothing(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        challenge_id = _proposed(in_memory_db)
        assert reject(in_memory_db, challenge_id).status == REJECTED

        assert accept(in_memory_db, challenge_id, today=MONDAY) is None
        assert reject(in_memory_db, challenge_id) is None

    def test_drop_ends_an_active_challenge(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        challenge_id = _proposed(in_memory_db)
        accept(in_memory_db, challenge_id, today=MONDAY)

        dropped = drop(in_memory_db, challenge_id)
        set_reason(in_memory_db, challenge_id, "  sick all week  ")

        assert dropped.status == DROPPED
        assert active_challenge(in_memory_db) is None
        assert get_challenge(in_memory_db, challenge_id).reason == "sick all week"

    def test_stale_proposals_expire(self, in_memory_db: sqlite3.Connection) -> None:
        challenge_id = _proposed(in_memory_db)
        proposed_on = date.fromisoformat(
            get_challenge(in_memory_db, challenge_id).proposed_at[:10]
        )

        assert expire_stale_proposals(in_memory_db, today=proposed_on) == 0
        assert (
            expire_stale_proposals(in_memory_db, today=proposed_on + timedelta(days=8))
            == 1
        )
        assert get_challenge(in_memory_db, challenge_id).status == EXPIRED


class TestScoring:
    def _active(self, conn: sqlite3.Connection) -> int:
        challenge_id = _proposed(conn)
        accept(conn, challenge_id, today=MONDAY)
        return challenge_id

    @pytest.mark.parametrize(
        ("runs", "status", "met"),
        [([4, 5], ACHIEVED, 2), ([4, 2], PARTIAL, 1), ([1, 2], MISSED, 0)],
    )
    def test_closes_with_the_measured_outcome(
        self,
        in_memory_db: sqlite3.Connection,
        runs: list[int],
        status: str,
        met: int,
    ) -> None:
        challenge_id = self._active(in_memory_db)
        _runs(in_memory_db, MONDAY, runs[0])
        _runs(in_memory_db, MONDAY + timedelta(days=7), runs[1])

        closed = close_finished(in_memory_db, today=date(2026, 10, 12))

        assert [c.id for c in closed] == [challenge_id]
        assert closed[0].status == status
        assert closed[0].outcome["weeks_met"] == met
        assert closed[0].outcome["per_week"][0]["actual"] == runs[0]

    def test_not_closed_before_the_last_week_ends(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        self._active(in_memory_db)

        assert close_finished(in_memory_db, today=date(2026, 10, 11)) == []
        assert active_challenge(in_memory_db) is not None

    def test_closing_is_idempotent(self, in_memory_db: sqlite3.Connection) -> None:
        self._active(in_memory_db)
        close_finished(in_memory_db, today=date(2026, 10, 12))

        assert close_finished(in_memory_db, today=date(2026, 10, 20)) == []


class TestText:
    def test_active_progress_is_measured(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        challenge_id = _proposed(in_memory_db)
        accept(in_memory_db, challenge_id, today=MONDAY)
        _runs(in_memory_db, MONDAY, 4)
        _runs(in_memory_db, MONDAY + timedelta(days=7), 1)

        text = describe_active(in_memory_db, today=date(2026, 10, 8))

        assert "Runs: 4 sessions a week for 2 weeks, 2026-09-28 to 2026-10-11" in text
        assert "Proposed by the coach and accepted by the user" in text
        assert "- Week 1: 4/4 sessions (met)" in text
        assert "- Week 2: 1/4 sessions (so far)" in text
        assert "3 days left." in text

    def test_no_active_challenge(self, in_memory_db: sqlite3.Connection) -> None:
        assert describe_active(in_memory_db, today=MONDAY) is None

    def test_history_shows_outcomes_and_reasons(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        first = _proposed(in_memory_db)
        reject(in_memory_db, first)
        set_reason(in_memory_db, first, "too many runs")
        accept(in_memory_db, _proposed(in_memory_db, weeks=1), today=MONDAY)
        _runs(in_memory_db, MONDAY, 4)
        close_finished(in_memory_db, today=date(2026, 10, 5))

        history = describe_history(in_memory_db)

        assert "achieved, 1 of 1 weeks met" in history
        assert 'rejected · reason: "too many runs"' in history

    def test_outcome_message(self, in_memory_db: sqlite3.Connection) -> None:
        accept(in_memory_db, _proposed(in_memory_db), today=MONDAY)
        _runs(in_memory_db, MONDAY, 4)
        _runs(in_memory_db, MONDAY + timedelta(days=7), 3)

        closed = close_finished(in_memory_db, today=date(2026, 10, 12))[0]

        assert outcome_message(closed) == (
            "➖ Challenge partly done: Four-run weeks\n"
            "1 of 2 weeks met (week 1 4/4, week 2 3/4 sessions)."
        )


class TestNudgeGating:
    def _active_runs(self, conn: sqlite3.Connection) -> None:
        accept(conn, _proposed(conn), today=MONDAY)

    def test_run_from_this_sync_is_news(self, in_memory_db: sqlite3.Connection) -> None:
        self._active_runs(in_memory_db)
        _runs(in_memory_db, MONDAY, 1)

        text = nudge_challenge_text(
            in_memory_db, workout_ids={"2026-09-28T07:00:00Z"}, today=MONDAY
        )

        assert "- Week 1: 1/4 sessions (so far)" in text

    def test_sync_without_a_counting_workout_is_not_news(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        self._active_runs(in_memory_db)
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(
                    date="2026-09-28",
                    workouts=[
                        WorkoutSnapshot(
                            type="Traditional Strength Training",
                            category="lift",
                            start_utc="2026-09-28T17:00:00Z",
                            duration_min=45.0,
                        )
                    ],
                )
            ],
        )

        text = nudge_challenge_text(
            in_memory_db, workout_ids={"2026-09-28T17:00:00Z"}, today=MONDAY
        )

        assert text == NO_CHALLENGE_NEWS

    def test_sleep_challenge_is_news_only_as_the_week_closes(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        accept(
            in_memory_db,
            _proposed(
                in_memory_db,
                metric="sleep_nights_week",
                category=None,
                target=5,
                threshold=7,
            ),
            today=MONDAY,
        )

        wednesday = nudge_challenge_text(
            in_memory_db, workout_ids=set(), today=MONDAY + timedelta(days=2)
        )
        saturday = nudge_challenge_text(
            in_memory_db, workout_ids=set(), today=MONDAY + timedelta(days=5)
        )

        assert wednesday == NO_CHALLENGE_NEWS
        assert "Sleep ≥7h: 5 nights a week" in saturday

    def test_no_active_challenge(self, in_memory_db: sqlite3.Connection) -> None:
        assert (
            nudge_challenge_text(in_memory_db, workout_ids={"x"}, today=MONDAY)
            == NO_CHALLENGE_NEWS
        )


class TestChallengeStatusText:
    def test_states(self, in_memory_db: sqlite3.Connection) -> None:
        assert challenge_status_text(in_memory_db, today=MONDAY) == (
            "No challenge is active."
        )
        challenge_id = _proposed(in_memory_db)
        assert "still awaiting the user's decision" in challenge_status_text(
            in_memory_db, today=MONDAY
        )
        accept(in_memory_db, challenge_id, today=MONDAY)
        assert challenge_status_text(in_memory_db, today=MONDAY).startswith(
            "Active challenge:\nFour-run weeks"
        )
