"""Tests for quiet-week detection and the check-in it triggers."""

from __future__ import annotations

import sqlite3
import types
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

import pytest

from models import DailySnapshot, WorkoutSnapshot
from quiet_week import (
    CHECKIN_CHOICES,
    CHOICE_BY_KEY,
    WeekActivity,
    already_asked,
    build_checkin_messages,
    checkin_keyboard,
    compose_checkin,
    consecutive_silences,
    journal_entry,
    measure_week_activity,
    record_answer,
    record_asked,
    should_ask_checkin,
)
from store import store_snapshots

FRIDAY = date(2026, 9, 4)
MONDAY = FRIDAY - timedelta(days=FRIDAY.weekday())
NOW = datetime(2026, 9, 4, 17, 0, tzinfo=timezone.utc)


def _seed(
    conn: sqlite3.Connection,
    *,
    this_week: int,
    weeks: int = 12,
    per_week: int = 4,
) -> None:
    """Seed a steady habit plus however much of this week has happened."""
    snapshots: list[DailySnapshot] = []
    for back in range(weeks, 0, -1):
        week = MONDAY - timedelta(weeks=back)
        for offset in range(per_week):
            day = (week + timedelta(days=offset)).isoformat()
            snapshots.append(
                DailySnapshot(
                    date=day,
                    workouts=[
                        WorkoutSnapshot(
                            type="Outdoor Run",
                            category="run",
                            start_utc=f"{day}T07:00:00Z",
                            duration_min=40.0,
                        )
                    ],
                )
            )
    for offset in range(this_week):
        day = (MONDAY + timedelta(days=offset)).isoformat()
        snapshots.append(
            DailySnapshot(
                date=day,
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc=f"{day}T07:00:00Z",
                        duration_min=40.0,
                    )
                ],
            )
        )
    store_snapshots(conn, snapshots)


def _activity(sessions: int, baseline: float = 4.0, day: int = 5) -> WeekActivity:
    return WeekActivity(
        sessions=sessions,
        baseline_per_week=baseline,
        weeks_of_history=12,
        days_elapsed=day,
    )


class TestWeekActivity:
    def test_expected_scales_with_the_week_so_far(self) -> None:
        assert _activity(0, baseline=7.0, day=3).expected_by_now == pytest.approx(3.0)

    def test_a_normal_week_is_not_quiet(self) -> None:
        assert _activity(3).is_quiet is False

    def test_a_near_empty_week_is_quiet(self) -> None:
        assert _activity(1).is_quiet is True
        assert _activity(0).is_quiet is True

    def test_no_baseline_is_never_quiet(self) -> None:
        """Nothing to be quiet against is not the same as being quiet."""
        assert _activity(0, baseline=0.0).is_quiet is False


class TestMeasureWeekActivity:
    def test_counts_this_week_against_the_personal_median(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, this_week=1)
        activity = measure_week_activity(in_memory_db, today=FRIDAY)
        assert activity is not None
        assert activity.sessions == 1
        assert activity.baseline_per_week == 4.0
        assert activity.days_elapsed == 5

    def test_every_sport_counts(self, in_memory_db: sqlite3.Connection) -> None:
        """A week spent swimming instead of running is not a quiet week."""
        _seed(in_memory_db, this_week=0)
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(
                    date=MONDAY.isoformat(),
                    workouts=[
                        WorkoutSnapshot(
                            type="Pool Swim",
                            category="other",
                            start_utc=f"{MONDAY.isoformat()}T07:00:00Z",
                            duration_min=45.0,
                        )
                    ],
                )
            ],
        )
        activity = measure_week_activity(in_memory_db, today=FRIDAY)
        assert activity is not None and activity.sessions == 1

    def test_one_odd_week_does_not_move_the_median(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A holiday fortnight at zero must not redefine what normal is."""
        _seed(in_memory_db, this_week=0)
        activity = measure_week_activity(in_memory_db, today=FRIDAY)
        assert activity is not None and activity.baseline_per_week == 4.0

    def test_an_empty_database_has_no_baseline(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        activity = measure_week_activity(in_memory_db, today=FRIDAY)
        assert activity is not None
        assert activity.baseline_per_week == 0.0
        assert activity.weeks_of_history == 0


class TestShouldAskCheckin:
    def _ask(
        self,
        conn: sqlite3.Connection,
        *,
        today: date = FRIDAY,
        knows: bool = False,
    ) -> tuple[bool, str]:
        ask, reason, _ = should_ask_checkin(
            conn, data_current=True, today=today, plan_frame_knows=lambda: knows
        )
        return ask, reason

    def test_the_costly_gate_is_not_consulted_when_a_cheap_one_rejects(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Resolving the plan frame can cost a model call; six days in seven it
        must not be reached at all."""
        _seed(in_memory_db, this_week=3)
        consulted = False

        def _knows() -> bool:
            nonlocal consulted
            consulted = True
            return False

        should_ask_checkin(
            in_memory_db, data_current=True, today=FRIDAY, plan_frame_knows=_knows
        )
        assert consulted is False

    def test_asks_when_a_regular_week_goes_quiet(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, this_week=0)
        assert self._ask(in_memory_db)[0] is True

    def test_asks_when_one_session_stands_in_for_four(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, this_week=1)
        assert self._ask(in_memory_db)[0] is True

    def test_stays_quiet_on_a_normal_week(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, this_week=3)
        ask, reason = self._ask(in_memory_db)
        assert ask is False
        assert reason == "week is running normally"

    def test_only_fires_on_the_checkin_weekday(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, this_week=0)
        assert self._ask(in_memory_db, today=FRIDAY - timedelta(days=2))[0] is False

    def test_stays_quiet_when_the_journal_already_explains_it(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Asking then would prove the system had not been listening."""
        _seed(in_memory_db, this_week=0)
        ask, reason = self._ask(in_memory_db, knows=True)
        assert ask is False
        assert reason == "context already explains this week"

    def test_a_beginner_is_left_alone(self, in_memory_db: sqlite3.Connection) -> None:
        """Three weeks of history has no normal for a quiet week to break."""
        _seed(in_memory_db, this_week=0, weeks=3)
        ask, reason = self._ask(in_memory_db)
        assert ask is False
        assert "not enough history" in reason

    def test_someone_without_a_rhythm_is_left_alone(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, this_week=0, per_week=1)
        ask, reason = self._ask(in_memory_db)
        assert ask is False
        assert "no established weekly rhythm" in reason

    def test_never_asks_twice_in_one_week(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, this_week=0)
        activity = measure_week_activity(in_memory_db, today=FRIDAY)
        assert activity is not None
        record_asked(
            in_memory_db,
            week_start=MONDAY.isoformat(),
            activity=activity,
            message_id=1,
            llm_call_id=None,
            now=NOW,
        )
        ask, reason = self._ask(in_memory_db)
        assert ask is False
        assert reason == "already asked this week"

    def test_backs_off_after_two_silences(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Two silences is an answer; continuing would be nagging."""
        _seed(in_memory_db, this_week=0)
        for back in (4, 3):
            week = (MONDAY - timedelta(weeks=back)).isoformat()
            in_memory_db.execute(
                "INSERT INTO checkin (week_start, asked_at) VALUES (?, 'x')", (week,)
            )
        in_memory_db.commit()
        ask, reason = self._ask(in_memory_db)
        assert ask is False
        assert "backing off" in reason

    def test_one_answer_resets_the_back_off(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed(in_memory_db, this_week=0)
        for back in (4, 3):
            week = (MONDAY - timedelta(weeks=back)).isoformat()
            in_memory_db.execute(
                "INSERT INTO checkin (week_start, asked_at) VALUES (?, 'x')", (week,)
            )
        in_memory_db.execute(
            "UPDATE checkin SET answered_at = 'y', answer = 'rest' WHERE week_start = ?",
            ((MONDAY - timedelta(weeks=3)).isoformat(),),
        )
        in_memory_db.commit()
        assert self._ask(in_memory_db)[0] is True


class TestLedger:
    def test_asking_is_recorded(self, in_memory_db: sqlite3.Connection) -> None:
        record_asked(
            in_memory_db,
            week_start=MONDAY.isoformat(),
            activity=_activity(0),
            message_id=7,
            llm_call_id=11,
            now=NOW,
        )
        assert already_asked(in_memory_db, MONDAY.isoformat()) is True
        assert consecutive_silences(in_memory_db) == 1

    def test_answering_clears_the_silence(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        record_asked(
            in_memory_db,
            week_start=MONDAY.isoformat(),
            activity=_activity(0),
            message_id=7,
            llm_call_id=None,
            now=NOW,
        )
        record_answer(
            in_memory_db, week_start=MONDAY.isoformat(), answer="rest", now=NOW
        )
        assert consecutive_silences(in_memory_db) == 0

    def test_each_button_writes_its_own_line(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        for choice in CHECKIN_CHOICES:
            if choice.key == "note":
                continue
            record_asked(
                in_memory_db,
                week_start=MONDAY.isoformat(),
                activity=_activity(0),
                message_id=1,
                llm_call_id=None,
                now=NOW,
            )
            line = record_answer(
                in_memory_db, week_start=MONDAY.isoformat(), answer=choice.key, now=NOW
            )
            assert line == choice.journal

    def test_a_typed_note_wins_over_the_canned_line(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        line = record_answer(
            in_memory_db,
            week_start=MONDAY.isoformat(),
            answer="note",
            note="Dad in hospital since Tuesday.",
            now=NOW,
        )
        assert line == "Dad in hospital since Tuesday."

    def test_a_bare_note_tap_writes_nothing(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A placeholder in log.md would say less than nothing."""
        assert (
            record_answer(
                in_memory_db, week_start=MONDAY.isoformat(), answer="note", now=NOW
            )
            is None
        )

    def test_an_unknown_answer_is_refused(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        assert (
            record_answer(
                in_memory_db, week_start=MONDAY.isoformat(), answer="maybe", now=NOW
            )
            is None
        )

    def test_journal_entries_match_the_log_format(self) -> None:
        assert journal_entry("Took it easy.", today=FRIDAY) == (
            "- 2026-09-04 — Took it easy."
        )


class TestKeyboardAndMessage:
    def test_every_choice_gets_a_button(self) -> None:
        rows = checkin_keyboard(MONDAY.isoformat())
        assert len(rows) == len(CHECKIN_CHOICES)

    def test_callback_payloads_round_trip(self) -> None:
        for row in checkin_keyboard(MONDAY.isoformat()):
            prefix, week, key = row[0]["callback_data"].split(":", 2)
            assert prefix == "checkin"
            assert week == MONDAY.isoformat()
            assert key in CHOICE_BY_KEY

    def test_the_prompt_states_the_shortfall(self) -> None:
        content = build_checkin_messages(
            me="Adam, 38.",
            log="(none)",
            history=None,
            activity=_activity(1),
            today=FRIDAY,
        )[0]["content"]
        assert "Friday" in content
        assert "Adam, 38." in content

    def test_a_failed_call_still_asks(
        self, in_memory_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The decision was already made; the buttons carry the value."""
        import llm

        monkeypatch.setattr(
            llm,
            "call_llm",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider down")),
        )
        text, keyboard, call_id = compose_checkin(
            in_memory_db, activity=_activity(0), today=FRIDAY
        )
        assert text
        assert len(keyboard) == len(CHECKIN_CHOICES)
        assert call_id is None

    def test_an_empty_answer_falls_back(
        self, in_memory_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import llm

        monkeypatch.setattr(
            llm,
            "call_llm",
            lambda *a, **k: types.SimpleNamespace(
                text="   ", llm_call_id=5, model="stub"
            ),
        )
        text, _, call_id = compose_checkin(
            in_memory_db, activity=_activity(0), today=FRIDAY
        )
        assert text.strip()
        assert call_id is None


class TestCheckinFreshness:
    def test_unknown_sync_never_asks(self, in_memory_db: sqlite3.Connection) -> None:
        _seed(in_memory_db, this_week=0)
        ask, reason, _ = should_ask_checkin(in_memory_db, today=FRIDAY)
        assert not ask
        assert "sync" in reason

    def test_recent_metrics_cannot_hide_stale_workout_export(
        self, in_memory_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        import os
        from quiet_week import checkin_data_current

        store_snapshots(
            in_memory_db, [DailySnapshot(date=FRIDAY.isoformat(), steps=8000)]
        )
        workouts = tmp_path / "Workouts"
        workouts.mkdir()
        payload = workouts / "latest.json"
        payload.write_text("{}")
        stale = (NOW - timedelta(days=7)).timestamp()
        os.utime(payload, (stale, stale))
        assert not checkin_data_current(
            in_memory_db, health_dir=tmp_path, source="local", now=NOW
        )
        os.utime(payload, (NOW.timestamp(), NOW.timestamp()))
        assert checkin_data_current(
            in_memory_db, health_dir=tmp_path, source="local", now=NOW
        )

    def test_http_upload_must_be_imported(
        self, in_memory_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        import json
        from quiet_week import checkin_data_current

        store_snapshots(
            in_memory_db, [DailySnapshot(date=FRIDAY.isoformat(), steps=8000)]
        )
        state = {
            "uploads": {
                "workouts": {
                    "received_at": NOW.isoformat(),
                    "sha256": "new",
                    "session_id": "s",
                }
            },
            "last_import_uploads": {"workouts": "old:s"},
        }
        path = tmp_path / ".ingest_state.json"
        path.write_text(json.dumps(state))
        assert not checkin_data_current(
            in_memory_db, health_dir=tmp_path, source="http", now=NOW
        )
        state["last_import_uploads"]["workouts"] = "new:s"
        path.write_text(json.dumps(state))
        assert checkin_data_current(
            in_memory_db, health_dir=tmp_path, source="http", now=NOW
        )
        state["last_error"] = "parse failed"
        path.write_text(json.dumps(state))
        assert not checkin_data_current(
            in_memory_db, health_dir=tmp_path, source="http", now=NOW
        )
