"""Sunday-evening coach review: schedule, data check, and postponement.

The coach's proposals are for the coming week, so it runs on Sunday evening,
before the week starts; the Monday report then reviews the finished week. On
Sunday evening the week is not quite over, and if nothing has synced today the
review would plan the week without its last day in it. Rather than guess, the
flow asks: run anyway, check again in a few minutes, or wait for Monday morning
when the whole week has synced.

State kept in the daemon's state file, so a postponement survives a restart:
``coach_scheduled_week`` is the Monday of the week already handled,
``coach_postponed_until`` and ``coach_postponed_mode`` a pending retry.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from config import COACH_MONDAY_FALLBACK_HHMM, COACH_POSTPONE_MINUTES
from store import latest_metric_date, open_db
from weekly_targets import week_start_for

if TYPE_CHECKING:
    from daemon import ProfileRuntime

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "coachrun:"
STALE_DATA_NOTE = (
    "Today's health data had not arrived when this review ran, so today's "
    "activity is missing from the week under review."
)
_RECHECK = "recheck"
_MONDAY = "monday"


def todays_data_present(db_path: object, now: datetime) -> bool:
    """Return whether today's metrics have arrived, local date.

    An import landing today is not enough: on 2026-09-20 the 23:55 export held
    days only up to Saturday, and the Monday report reviewed a week missing its
    Sunday. What counts is a metric-bearing row dated today.
    """
    today = now.date().isoformat()
    conn = open_db(db_path)
    try:
        return latest_metric_date(conn, through=today) == today
    except sqlite3.Error as exc:
        logger.warning("Could not read the latest metric date: %s", exc)
        return False
    finally:
        conn.close()


def next_monday_fallback(now: datetime) -> datetime:
    """Return the next Monday at ``COACH_MONDAY_FALLBACK_HHMM``, local time."""
    hour, minute = (int(part) for part in COACH_MONDAY_FALLBACK_HHMM.split(":"))
    days = (7 - now.weekday()) % 7 or 7
    target = (now + timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return target


class CoachScheduleFlow:
    """Runs the scheduled coach review and handles its postponement buttons."""

    def __init__(self, daemon: ProfileRuntime) -> None:
        self._daemon = daemon

    @property
    def _state(self) -> dict[str, Any]:
        return self._daemon._state

    def check(self, now: datetime, prefs: dict[str, Any]) -> None:
        """Run from the scheduled loop: fire a due review or a due retry."""
        from notification_prefs import evaluate_report_delivery, scheduled_report_due

        postponed = self._state.get("coach_postponed_until")
        if postponed:
            try:
                due = now >= datetime.fromisoformat(postponed)
            except ValueError:
                due = True
            if due:
                mode = self._state.get("coach_postponed_mode", _RECHECK)
                self._clear_postponement()
                if mode == _MONDAY:
                    # The whole week has synced overnight: review it complete.
                    self._daemon._run_coach(week="last", force=True)
                else:
                    self.attempt(now)
            return

        if not scheduled_report_due(prefs, "weekly_coach", now=now):
            return
        if evaluate_report_delivery(prefs, "weekly_coach", now=now)["status"] != (
            "allowed"
        ):
            return
        week_key = week_start_for(now.date())
        if self._state.get("coach_scheduled_week") == week_key:
            return
        self._state["coach_scheduled_week"] = week_key
        self._daemon._save_state()
        self.attempt(now)

    def attempt(self, now: datetime) -> None:
        """Run the review if today's data has synced, otherwise ask what to do."""
        self._daemon._run_import()
        if todays_data_present(self._daemon.db, now):
            self._daemon._run_coach(week="current", skip_import=True)
            return
        poller = self._daemon._poller
        if poller is None:
            return
        logger.info("Sunday coach review: today's data missing; asking the user")
        poller.send_message_with_keyboard(
            "Today's health data hasn't arrived yet, so the weekly coach review "
            "would plan next week without today in it. Open the export app to "
            "sync, then choose:",
            [
                [
                    {"text": "Run now", "callback_data": f"{CALLBACK_PREFIX}now"},
                    {
                        "text": f"In {COACH_POSTPONE_MINUTES} min",
                        "callback_data": f"{CALLBACK_PREFIX}later",
                    },
                    {
                        "text": "Monday morning",
                        "callback_data": f"{CALLBACK_PREFIX}monday",
                    },
                ]
            ],
        )

    def handle_callback(self, cb_id: str, data: str, msg_id: int | None) -> None:
        """Handle Run now, In 15 min and Monday morning."""
        poller = self._daemon._poller
        action = data[len(CALLBACK_PREFIX) :]
        now = datetime.now().astimezone()
        if action == "now":
            poller.answer_callback_query(cb_id, "Running the review.")
            if msg_id:
                poller.edit_message(
                    msg_id, "Running the coach review with today missing."
                )
            self._daemon._run_coach(
                week="current", force=True, data_note=STALE_DATA_NOTE
            )
        elif action == "later":
            until = now + timedelta(minutes=COACH_POSTPONE_MINUTES)
            self._postpone(until, _RECHECK)
            poller.answer_callback_query(cb_id, "Checking again soon.")
            if msg_id:
                poller.edit_message(
                    msg_id, f"I'll check for today's data again at {until:%H:%M}."
                )
        elif action == "monday":
            until = next_monday_fallback(now)
            self._postpone(until, _MONDAY)
            poller.answer_callback_query(cb_id, "Monday it is.")
            if msg_id:
                poller.edit_message(
                    msg_id,
                    f"The coach review will run Monday at {until:%H:%M}, on the "
                    "complete week.",
                )
        else:
            poller.answer_callback_query(cb_id, "Unknown action.")

    def _postpone(self, until: datetime, mode: str) -> None:
        self._state["coach_postponed_until"] = until.isoformat()
        self._state["coach_postponed_mode"] = mode
        self._daemon._save_state()

    def _clear_postponement(self) -> None:
        self._state.pop("coach_postponed_until", None)
        self._state.pop("coach_postponed_mode", None)
        self._daemon._save_state()
