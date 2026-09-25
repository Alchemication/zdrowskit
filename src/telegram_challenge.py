"""Telegram controls for coach challenges: decide, inspect, drop, and announce.

Buttons carry the challenge's database id, not an in-memory token, so a proposal
left untapped survives a daemon restart until it expires. Reason replies are
routed by the stored prompt message id, the same way check-in notes are.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from typing import TYPE_CHECKING

from challenges import (
    PROPOSED,
    Challenge,
    accept,
    active_challenge,
    challenge_for_reason_prompt,
    close_finished,
    describe_active,
    describe_challenge,
    describe_history,
    drop,
    expire_stale_proposals,
    get_challenge,
    open_proposal,
    outcome_message,
    proposal_for_call,
    reject,
    set_reason,
    set_reason_prompt,
)
from store import open_db

if TYPE_CHECKING:
    from daemon_telegram_chat import TelegramChatHandler
    from telegram_bot import TelegramPoller

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "chal:"


def proposal_buttons(challenge_id: int) -> list[list[dict[str, str]]]:
    """Return the Accept / Reject row for a proposed challenge."""
    return [
        [
            {
                "text": "🎯 Accept challenge",
                "callback_data": f"{CALLBACK_PREFIX}accept:{challenge_id}",
            },
            {
                "text": "✖️ Reject challenge",
                "callback_data": f"{CALLBACK_PREFIX}reject:{challenge_id}",
            },
        ]
    ]


def _drop_buttons(challenge_id: int) -> list[list[dict[str, str]]]:
    return [
        [
            {
                "text": "Drop challenge",
                "callback_data": f"{CALLBACK_PREFIX}drop:{challenge_id}",
            }
        ]
    ]


def buttons_for_coach_call(
    db_path: object, llm_call_id: int | None
) -> list[list[dict[str, str]]]:
    """Return Accept / Reject buttons for the challenge a coach call proposed."""
    if llm_call_id is None:
        return []
    conn = open_db(db_path)
    try:
        proposal = proposal_for_call(conn, llm_call_id)
    finally:
        conn.close()
    return proposal_buttons(proposal.id) if proposal else []


def _started_text(challenge: Challenge) -> str:
    return (
        f"🎯 Challenge on: {challenge.title}\n"
        f"{describe_challenge(challenge)}, {challenge.start_week} to "
        f"{challenge.end_date}. /challenge shows progress."
    )


def handle_challenge_command(chat: TelegramChatHandler, message_id: int | None) -> None:
    """Show the active challenge, an open proposal, or the recent history."""
    today = date.today()
    conn = open_db(chat._daemon.db)
    try:
        expire_stale_proposals(conn, today=today)
        active = active_challenge(conn)
        if active is not None:
            text = describe_active(conn, today=today) or describe_challenge(active)
            chat._poller.send_message_with_keyboard(
                f"🎯 {text}", _drop_buttons(active.id)
            )
            return
        pending = open_proposal(conn)
        if pending is not None:
            chat._poller.send_message_with_keyboard(
                f"🎯 Proposed by the coach: {pending.title}\n"
                f"{describe_challenge(pending)}\nServes: {pending.goal}",
                proposal_buttons(pending.id),
            )
            return
        chat._poller.send_reply(
            "No challenge is active. The weekly coach proposes one when the "
            f"data suggests it.\n\nRecent:\n{describe_history(conn)}",
            reply_to_message_id=message_id,
        )
    finally:
        conn.close()


def _ask_reason(
    chat: TelegramChatHandler,
    conn: sqlite3.Connection,
    challenge_id: int,
    question: str,
    msg_id: int | None,
) -> None:
    prompt_id = chat._poller.send_reply(
        question, reply_to_message_id=msg_id, force_reply=True
    )
    if prompt_id is not None:
        set_reason_prompt(conn, challenge_id, prompt_id)


def handle_challenge_callback(
    chat: TelegramChatHandler, cb_id: str, data: str, msg_id: int | None
) -> None:
    """Handle Accept, Reject and Drop buttons."""
    try:
        action, raw_id = data[len(CALLBACK_PREFIX) :].split(":", 1)
        challenge_id = int(raw_id)
    except ValueError:
        chat._poller.answer_callback_query(cb_id, "Invalid challenge action.")
        return
    conn = open_db(chat._daemon.db)
    try:
        if action == "accept":
            started = accept(conn, challenge_id, today=date.today())
            if started is None:
                current = get_challenge(conn, challenge_id)
                why = (
                    "Another challenge is already active."
                    if current is not None and current.status == PROPOSED
                    else "No longer available."
                )
                chat._poller.answer_callback_query(cb_id, why)
                return
            # The buttons stay: in a coach review they share the keyboard with
            # the strategy-edit and feedback rows. A second tap is refused.
            chat._poller.answer_callback_query(cb_id, "Challenge on.")
            chat._poller.send_reply(_started_text(started))
        elif action == "reject":
            rejected = reject(conn, challenge_id)
            if rejected is None:
                chat._poller.answer_callback_query(cb_id, "No longer available.")
                return
            chat._poller.answer_callback_query(cb_id, "Rejected.")
            _ask_reason(
                chat,
                conn,
                challenge_id,
                "Optional: reply with why this challenge didn't fit.",
                msg_id,
            )
        elif action == "drop":
            dropped = drop(conn, challenge_id)
            if dropped is None:
                chat._poller.answer_callback_query(cb_id, "Not active.")
                return
            chat._poller.answer_callback_query(cb_id, "Dropped.")
            if msg_id:
                chat._poller.edit_message(msg_id, f"Dropped: {dropped.title}")
            _ask_reason(
                chat,
                conn,
                challenge_id,
                "Optional: reply with why you dropped it.",
                msg_id,
            )
        else:
            chat._poller.answer_callback_query(cb_id, "Invalid challenge action.")
    finally:
        conn.close()


def handle_challenge_reason_reply(chat: TelegramChatHandler, message: dict) -> bool:
    """Store a reason reply if it answers a challenge's reason prompt."""
    reply_id = (message.get("reply_to_message") or {}).get("message_id")
    if reply_id is None:
        return False
    conn = open_db(chat._daemon.db)
    try:
        challenge = challenge_for_reason_prompt(conn, reply_id)
        if challenge is None:
            return False
        text = " ".join((message.get("text") or "").split())
        if text:
            set_reason(conn, challenge.id, text)
    finally:
        conn.close()
    chat._poller.send_reply(
        "Noted — the coach will take that into account.",
        reply_to_message_id=message.get("message_id"),
    )
    return True


def close_and_announce(db_path: object, poller: TelegramPoller | None) -> int:
    """Close finished challenges, expire stale proposals, announce outcomes.

    Idempotent and cheap, so the scheduled loop can call it every tick.

    Returns:
        Number of challenges closed.
    """
    today = date.today()
    conn = open_db(db_path)
    try:
        expire_stale_proposals(conn, today=today)
        closed = close_finished(conn, today=today)
    finally:
        conn.close()
    for challenge in closed:
        logger.info("Challenge %s closed as %s", challenge.id, challenge.status)
        if poller is not None:
            poller.send_reply(outcome_message(challenge))
    return len(closed)
