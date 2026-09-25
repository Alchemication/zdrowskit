"""Telegram controls for challenges: buttons, reasons, restarts, announcements."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from challenges import (
    ACTIVE,
    DROPPED,
    REJECTED,
    accept,
    coerce_proposal,
    get_challenge,
    propose,
)
from store import open_db
from telegram_challenge import (
    buttons_for_coach_call,
    close_and_announce,
    handle_challenge_callback,
    handle_challenge_command,
    handle_challenge_reason_reply,
)

MONDAY = date(2026, 9, 28)


def _chat(tmp_path: Path) -> SimpleNamespace:
    daemon = SimpleNamespace(db=tmp_path / "health.db")
    poller = MagicMock()
    poller.send_reply.return_value = 501
    return SimpleNamespace(_daemon=daemon, _poller=poller)


def _proposal(db: Path, llm_call_id: int = 42) -> int:
    conn = open_db(db)
    try:
        raw = {
            "title": "Four-run weeks",
            "goal": "Consistency comes first",
            "metric": "sessions_week",
            "category": "run",
            "target": 4,
            "weeks": 2,
        }
        return propose(conn, coerce_proposal(raw, frozenset()), llm_call_id=llm_call_id)
    finally:
        conn.close()


def _status(db: Path, challenge_id: int) -> str:
    conn = open_db(db)
    try:
        return get_challenge(conn, challenge_id).status
    finally:
        conn.close()


class TestTelegramChallenge:
    def test_coach_call_gets_buttons_for_its_own_proposal(self, tmp_path: Path) -> None:
        chat = _chat(tmp_path)
        challenge_id = _proposal(chat._daemon.db, llm_call_id=42)

        rows = buttons_for_coach_call(chat._daemon.db, 42)

        assert rows[0][0]["callback_data"] == f"chal:accept:{challenge_id}"
        assert buttons_for_coach_call(chat._daemon.db, 43) == []

    def test_accept_survives_a_restart(self, tmp_path: Path) -> None:
        """The button carries a database id, not an in-memory token."""
        challenge_id = _proposal(_chat(tmp_path)._daemon.db)
        restarted = _chat(tmp_path)

        with patch("telegram_challenge.date") as fake_date:
            fake_date.today.return_value = MONDAY
            handle_challenge_callback(
                restarted, "cb", f"chal:accept:{challenge_id}", 10
            )

        assert _status(restarted._daemon.db, challenge_id) == ACTIVE
        restarted._poller.answer_callback_query.assert_called_with(
            "cb", "Challenge on."
        )
        started = restarted._poller.send_reply.call_args.args[0]
        assert "2026-09-28 to 2026-10-11" in started

    def test_second_tap_is_refused(self, tmp_path: Path) -> None:
        chat = _chat(tmp_path)
        challenge_id = _proposal(chat._daemon.db)
        handle_challenge_callback(chat, "cb", f"chal:accept:{challenge_id}", 10)

        handle_challenge_callback(chat, "cb2", f"chal:reject:{challenge_id}", 10)

        chat._poller.answer_callback_query.assert_called_with(
            "cb2", "No longer available."
        )
        assert _status(chat._daemon.db, challenge_id) == ACTIVE

    def test_reject_then_reason_reply_is_stored(self, tmp_path: Path) -> None:
        chat = _chat(tmp_path)
        challenge_id = _proposal(chat._daemon.db)

        handle_challenge_callback(chat, "cb", f"chal:reject:{challenge_id}", 10)
        handled = handle_challenge_reason_reply(
            _chat(tmp_path),
            {
                "message_id": 502,
                "text": "  four runs is too many with a toddler ",
                "reply_to_message": {"message_id": 501},
            },
        )

        assert handled
        conn = open_db(chat._daemon.db)
        stored = get_challenge(conn, challenge_id)
        conn.close()
        assert stored.status == REJECTED
        assert stored.reason == "four runs is too many with a toddler"

    def test_unrelated_reply_is_not_consumed(self, tmp_path: Path) -> None:
        chat = _chat(tmp_path)

        assert not handle_challenge_reason_reply(
            chat, {"message_id": 5, "text": "hi", "reply_to_message": {"message_id": 4}}
        )

    def test_command_offers_drop_and_drop_asks_why(self, tmp_path: Path) -> None:
        chat = _chat(tmp_path)
        challenge_id = _proposal(chat._daemon.db)
        conn = open_db(chat._daemon.db)
        accept(conn, challenge_id, today=MONDAY)
        conn.close()

        handle_challenge_command(chat, 1)
        rows = chat._poller.send_message_with_keyboard.call_args.args[1]
        handle_challenge_callback(chat, "cb", rows[0][0]["callback_data"], 11)

        assert _status(chat._daemon.db, challenge_id) == DROPPED
        assert chat._poller.send_reply.call_args.kwargs["force_reply"] is True

    def test_command_with_nothing_active(self, tmp_path: Path) -> None:
        chat = _chat(tmp_path)

        handle_challenge_command(chat, 1)

        assert "No challenge is active" in chat._poller.send_reply.call_args.args[0]

    def test_finished_challenge_is_announced_once(self, tmp_path: Path) -> None:
        chat = _chat(tmp_path)
        challenge_id = _proposal(chat._daemon.db)
        conn = open_db(chat._daemon.db)
        accept(conn, challenge_id, today=MONDAY)
        conn.close()

        with patch("telegram_challenge.date") as fake_date:
            fake_date.today.return_value = date(2026, 10, 12)
            first = close_and_announce(chat._daemon.db, chat._poller)
            second = close_and_announce(chat._daemon.db, chat._poller)

        assert (first, second) == (1, 0)
        announcement = chat._poller.send_reply.call_args.args[0]
        assert announcement.startswith("Challenge finished: Four-run weeks")
        assert "0 of 2 weeks met" in announcement
