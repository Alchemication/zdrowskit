"""Tests for the Sunday-evening coach schedule, data check and postponement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from daemon_coach_flow import (
    STALE_DATA_NOTE,
    CoachScheduleFlow,
    next_monday_fallback,
    synced_today,
)
from notification_prefs import DEFAULT_NOTIFICATION_PREFS
from store import open_db

TZ = timezone.utc
SUNDAY_EVENING = datetime(2026, 9, 27, 19, 5, tzinfo=TZ)


def _daemon(tmp_path: Path, *, imported_at: datetime | None) -> SimpleNamespace:
    db = tmp_path / "health.db"
    conn = open_db(db)
    if imported_at is not None:
        with conn:
            conn.execute(
                "INSERT INTO daily (date, imported_at) VALUES (?, ?)",
                (imported_at.date().isoformat(), imported_at.isoformat()),
            )
    conn.close()
    return SimpleNamespace(
        db=db,
        _state={},
        _save_state=MagicMock(),
        _run_import=MagicMock(),
        _run_coach=MagicMock(),
        _poller=MagicMock(),
    )


def _prefs(**overrides: object) -> dict:
    prefs = dict(DEFAULT_NOTIFICATION_PREFS)
    prefs.update(overrides)
    return prefs


class TestSyncedToday:
    def test_today_yesterday_and_never(self, tmp_path: Path) -> None:
        today = _daemon(tmp_path / "a", imported_at=SUNDAY_EVENING - timedelta(hours=2))
        yesterday = _daemon(
            tmp_path / "b", imported_at=SUNDAY_EVENING - timedelta(days=1)
        )
        never = _daemon(tmp_path / "c", imported_at=None)

        assert synced_today(today.db, SUNDAY_EVENING)
        assert not synced_today(yesterday.db, SUNDAY_EVENING)
        assert not synced_today(never.db, SUNDAY_EVENING)


class TestNextMondayFallback:
    def test_from_sunday_evening(self) -> None:
        assert next_monday_fallback(SUNDAY_EVENING) == datetime(
            2026, 9, 28, 8, 0, tzinfo=TZ
        )

    def test_from_a_monday_it_is_the_following_one(self) -> None:
        monday = datetime(2026, 9, 28, 9, 0, tzinfo=TZ)

        assert next_monday_fallback(monday) == datetime(2026, 10, 5, 8, 0, tzinfo=TZ)


class TestScheduledCheck:
    def test_runs_once_on_the_current_week_when_data_synced(
        self, tmp_path: Path
    ) -> None:
        daemon = _daemon(tmp_path, imported_at=SUNDAY_EVENING - timedelta(hours=1))
        flow = CoachScheduleFlow(daemon)

        flow.check(SUNDAY_EVENING, _prefs())
        flow.check(SUNDAY_EVENING + timedelta(minutes=30), _prefs())

        daemon._run_coach.assert_called_once_with(week="current", skip_import=True)

    def test_not_before_the_scheduled_time(self, tmp_path: Path) -> None:
        daemon = _daemon(tmp_path, imported_at=SUNDAY_EVENING)
        flow = CoachScheduleFlow(daemon)

        flow.check(SUNDAY_EVENING - timedelta(hours=1), _prefs())

        daemon._run_coach.assert_not_called()

    def test_no_data_today_asks_instead_of_running(self, tmp_path: Path) -> None:
        daemon = _daemon(tmp_path, imported_at=SUNDAY_EVENING - timedelta(days=1))
        flow = CoachScheduleFlow(daemon)

        flow.check(SUNDAY_EVENING, _prefs())

        daemon._run_coach.assert_not_called()
        text, rows = daemon._poller.send_message_with_keyboard.call_args.args
        assert "No health data has synced today" in text
        assert [b["callback_data"] for b in rows[0]] == [
            "coachrun:now",
            "coachrun:later",
            "coachrun:monday",
        ]

    def test_muted_review_does_nothing(self, tmp_path: Path) -> None:
        daemon = _daemon(tmp_path, imported_at=SUNDAY_EVENING)
        flow = CoachScheduleFlow(daemon)
        prefs = _prefs(overrides={"weekly_coach": {"enabled": False}})

        flow.check(SUNDAY_EVENING, prefs)

        daemon._run_coach.assert_not_called()
        daemon._poller.send_message_with_keyboard.assert_not_called()


class TestPostponement:
    def test_run_now_marks_the_data_as_missing(self, tmp_path: Path) -> None:
        daemon = _daemon(tmp_path, imported_at=None)
        flow = CoachScheduleFlow(daemon)

        flow.handle_callback("cb", "coachrun:now", 7)

        daemon._run_coach.assert_called_once_with(
            week="current", force=True, data_note=STALE_DATA_NOTE
        )

    def test_later_rechecks_after_the_delay(self, tmp_path: Path) -> None:
        daemon = _daemon(tmp_path, imported_at=None)
        flow = CoachScheduleFlow(daemon)
        flow.handle_callback("cb", "coachrun:later", 7)
        until = datetime.fromisoformat(daemon._state["coach_postponed_until"])
        # A sync lands while waiting.
        conn = open_db(daemon.db)
        with conn:
            conn.execute(
                "INSERT INTO daily (date, imported_at) VALUES (?, ?)",
                (until.date().isoformat(), until.isoformat()),
            )
        conn.close()

        flow.check(until - timedelta(minutes=1), _prefs())
        daemon._run_coach.assert_not_called()
        flow.check(until + timedelta(minutes=1), _prefs())

        daemon._run_coach.assert_called_once_with(week="current", skip_import=True)
        assert "coach_postponed_until" not in daemon._state

    def test_monday_reviews_the_complete_week(self, tmp_path: Path) -> None:
        daemon = _daemon(tmp_path, imported_at=None)
        flow = CoachScheduleFlow(daemon)
        flow.handle_callback("cb", "coachrun:monday", 7)
        until = datetime.fromisoformat(daemon._state["coach_postponed_until"])

        flow.check(until + timedelta(minutes=1), _prefs())

        daemon._run_coach.assert_called_once_with(week="last", force=True)
        assert until.weekday() == 0
