"""Telegram controls for weekly progress and durable check-in replies."""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from context_edit import MAX_LOG_BULLET_CHARS, ContextEdit, apply_edit
from plan_frame import load_plan_frame, progress_paused, set_progress_paused
from quiet_week import CHOICE_BY_KEY, journal_entry, record_answer
from store import open_db
from weekly_targets import ensure_weekly_targets, week_start_for
from weekly_progress import measure_week, render_progress_block

if TYPE_CHECKING:
    from daemon_telegram_chat import TelegramChatHandler

logger = logging.getLogger(__name__)


def handle_targets(
    chat: TelegramChatHandler, action: str, message_id: int | None
) -> None:
    """Inspect progress, refresh extraction, or explicitly pause/resume the strip."""
    if action not in {"", "show", "refresh", "pause", "resume"}:
        chat._poller.send_reply(
            "Use /targets, /targets refresh, /targets pause, or /targets resume."
        )
        return
    conn = open_db(chat._daemon.db)
    try:
        if action in {"pause", "resume"}:
            set_progress_paused(conn, action == "pause")
        path = chat._daemon.context_dir / "strategy.md"
        strategy = path.read_text() if path.exists() else None
        today = date.today()
        week = week_start_for(today)
        targets = ensure_weekly_targets(
            conn,
            strategy_md=strategy,
            week_start=week,
            force=action == "refresh",
            model_prefs_path=chat._daemon.model_prefs_path,
        )
        block = render_progress_block(
            measure_week(conn, targets, week_start=week, today=today), week_start=week
        )
        lines = [block or "No measurable weekly targets."]
        for target in targets:
            if target.goal_text:
                lines.append(f"{target.label}: {target.goal_text}")
        paused = progress_paused(conn)
        if paused:
            lines.append("Progress is paused until you resume it.")
        else:
            frame = load_plan_frame(conn)
            if frame and frame.mode != "full":
                lines.append(
                    f"Last automatic display decision: {frame.mode} — {frame.reason}"
                )
        lines.append(
            'To correct a goal, tell me here, for example: "Change my weekly running goal to 20 km." I’ll show the proposed strategy edit for approval.'
        )
        keyboard = [
            [
                {
                    "text": "Resume progress" if paused else "Pause progress",
                    "callback_data": "targets:resume" if paused else "targets:pause",
                },
                {"text": "Refresh targets", "callback_data": "targets:refresh"},
            ]
        ]
        chat._poller.send_reply(
            "\n\n".join(lines),
            reply_to_message_id=message_id,
            reply_markup={"inline_keyboard": keyboard},
        )
    finally:
        conn.close()


def _save_answer(
    chat: TelegramChatHandler, week: str, answer: str, note: str | None
) -> bool:
    """Acknowledge only after the journal and ledger are saved; allow safe retries."""
    choice = CHOICE_BY_KEY[answer]
    line = " ".join((note or "").split()) or choice.journal
    if not line:
        return False
    conn = open_db(chat._daemon.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT answered_at FROM checkin WHERE week_start = ?", (week,)
        ).fetchone()
        if row is None:
            return False
        if row["answered_at"]:
            return True
        # Preserve the question's week even if the person replies much later.
        entry = journal_entry(
            f"Check-in for week of {week}: {line}", today=date.today()
        )
        path = chat._daemon.context_dir / "log.md"
        if not path.exists() or f"Check-in for week of {week}:" not in path.read_text():
            chat._daemon._self_originated_writes.add(path.resolve())
            apply_edit(
                chat._daemon.context_dir,
                ContextEdit(
                    file="log",
                    action="append",
                    content=entry,
                    summary="Weekly check-in answer",
                ),
                strict=True,
            )
        record_answer(conn, week_start=week, answer=answer, note=note)
        return True
    except Exception:
        logger.exception("Could not save check-in answer")
        return False
    finally:
        conn.close()


def handle_checkin_callback(
    chat: TelegramChatHandler, cb_id: str, data: str, msg_id: int | None
) -> None:
    """Persist the note prompt identity so replies survive process restarts."""
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[2] not in CHOICE_BY_KEY:
        chat._poller.answer_callback_query(cb_id, "Unknown action.")
        return
    _, week, answer = parts
    conn = open_db(chat._daemon.db)
    try:
        row = conn.execute(
            "SELECT message_id, answered_at FROM checkin WHERE week_start = ?", (week,)
        ).fetchone()
        if row is None or row["message_id"] != msg_id:
            chat._poller.answer_callback_query(cb_id, "This check-in is unavailable.")
            return
        if row["answered_at"]:
            chat._poller.answer_callback_query(cb_id, "Already saved.")
            return
        if answer == "note":
            prompt_id = chat._poller.send_reply(
                "What happened? One line is plenty.", force_reply=True
            )
            if prompt_id is None:
                chat._poller.answer_callback_query(
                    cb_id, "Could not send the prompt. Tap again."
                )
                return
            with conn:
                conn.execute(
                    "UPDATE checkin SET note_prompt_id = ? WHERE week_start = ?",
                    (prompt_id, week),
                )
            chat._poller.answer_callback_query(cb_id, "Reply to the question.")
            return
    finally:
        conn.close()
    if not _save_answer(chat, week, answer, None):
        chat._poller.answer_callback_query(cb_id, "Could not save. Please tap again.")
        return
    chat._poller.answer_callback_query(cb_id, "Saved.")
    if msg_id:
        chat._poller.edit_message(msg_id, f"✓ Saved — {CHOICE_BY_KEY[answer].label}")


def handle_checkin_reply(chat: TelegramChatHandler, message: dict) -> bool:
    """Route a reply by its stored Telegram message ID, never by prompt wording."""
    reply_id = (message.get("reply_to_message") or {}).get("message_id")
    if reply_id is None:
        return False
    conn = open_db(chat._daemon.db)
    try:
        row = conn.execute(
            "SELECT week_start FROM checkin WHERE note_prompt_id = ?", (reply_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    note = " ".join((message.get("text") or "").split())
    prefix = journal_entry(
        f"Check-in for week of {row['week_start']}: ", today=date.today()
    )
    available = MAX_LOG_BULLET_CHARS - len(prefix)
    if not note or len(note) > available:
        chat._poller.send_reply(
            f"Please reply with a short note, up to {available} characters.",
            reply_to_message_id=message["message_id"],
        )
        return True
    saved = _save_answer(chat, row["week_start"], "note", note)
    chat._poller.send_reply(
        "Saved — I’ll keep that in mind."
        if saved
        else "Could not save your note. Please reply to the same question again.",
        reply_to_message_id=message["message_id"],
    )
    return True
