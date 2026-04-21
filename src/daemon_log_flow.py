"""/log Telegram command flow.

Handles the fast tap-through daily log check-in. An LLM designs a 1–3 step
interview (see :func:`cmd_llm.build_log_flow`), the user taps through
inline keyboards, and a deterministic writer appends one bullet to log.md.

The handler owns its own pending-state map, lock, and ``+ note`` free-text
intercept. Borrows the daemon's ``db``, ``context_dir``, and ``_poller``
through a back-reference.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

from context_edit import MAX_LOG_BULLET_CHARS

if TYPE_CHECKING:
    from cmd_llm import LogFlow
    from daemon import ZdrowskitDaemon

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 10 * 60

# End-date picker shown after a ``ask_end_date_if_selected`` option is chosen.
# Keys are stable callback tokens; labels are shown on the button.
_END_DATE_CHOICES: list[tuple[str, str]] = [
    ("today", "today"),
    ("tomorrow", "tomorrow"),
    ("sunday", "Sunday"),
    ("nextweek", "+1 week"),
    ("skip", "skip"),
]


@dataclass
class PendingLogEntry:
    """In-memory state for an active /log tap session.

    Attributes:
        token: Random token embedded in callback_data (survives double-taps).
        flow: The LLM-designed interview.
        session_date: The calendar date this /log session is for.
        step_index: Index into ``flow.steps``; equals ``len(flow.steps)``
            when the interview itself is done and we're waiting on the
            end-date picker or the bullet has been committed.
        answers: Selected options keyed by step id.
        chat_message_id: Telegram message id of the keyboard message.
        original_message_id: Telegram message id of the user's /log command.
        created_at: Monotonic creation time for TTL sweeping.
        awaiting_end_date: True when we're showing the date picker.
        end_date: Picked end-date ISO string, or None.
        awaiting_note: True when the next free-text message is the bullet's tail.
        note: Final free-text note, or None.
    """

    token: str
    flow: "LogFlow"
    session_date: date
    chat_message_id: int | None
    original_message_id: int
    step_index: int = 0
    answers: dict[str, list[str]] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    awaiting_end_date: bool = False
    end_date: str | None = None
    awaiting_note: bool = False
    note: str | None = None
    followup_consulted: bool = False


def _resolve_end_date(choice: str, today: date) -> str | None:
    """Map an end-date button key to an ISO date string, or None for skip."""
    if choice == "skip":
        return None
    if choice == "today":
        return today.isoformat()
    if choice == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if choice == "sunday":
        days_ahead = (6 - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return (today + timedelta(days=days_ahead)).isoformat()
    if choice == "nextweek":
        return (today + timedelta(days=7)).isoformat()
    return None


def compose_bullet(
    date_iso: str,
    answers: dict[str, list[str]],
    end_date: str | None,
    note: str | None,
) -> str:
    """Render the final log.md bullet line from interview answers.

    The output is guaranteed to be a single line no longer than
    ``MAX_LOG_BULLET_CHARS`` so it satisfies the same validation the LLM
    ``update_context`` path uses. A long note is collapsed to one line and
    truncated with an ellipsis; tokens and the ``until`` tail are kept
    because they carry structured meaning.
    """
    tokens = [f"[{opt}]" for step_answers in answers.values() for opt in step_answers]
    line = f"- {date_iso}"
    if tokens:
        line += " " + " ".join(tokens)
    if end_date:
        line += f" until {end_date}"
    if note:
        clean = " ".join(note.split())
        if clean:
            tail = f" — {clean}"
            overflow = len(line) + len(tail) - MAX_LOG_BULLET_CHARS
            if overflow > 0:
                clean = clean[: len(clean) - overflow - 1].rstrip() + "\u2026"
                tail = f" — {clean}"
            line += tail
    return line


def _step_keyboard(
    pending: PendingLogEntry,
) -> list[list[dict[str, str]]]:
    """Build the inline keyboard for the current step."""
    step = pending.flow.steps[pending.step_index]
    selected = set(pending.answers.get(step.id, []))
    rows: list[list[dict[str, str]]] = []
    # Two option buttons per row keeps labels readable on mobile.
    current_row: list[dict[str, str]] = []
    for option_idx, option in enumerate(step.options):
        mark = "\u2713 " if option in selected else ""
        current_row.append(
            {
                "text": f"{mark}{option}",
                "callback_data": (
                    f"log_toggle:{pending.token}:{pending.step_index}:{option_idx}"
                ),
            }
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    controls: list[dict[str, str]] = [
        {
            "text": "\U0001f4dd + note",
            "callback_data": f"log_note:{pending.token}",
        },
        {
            "text": "\u274c cancel",
            "callback_data": f"log_cancel:{pending.token}",
        },
    ]
    rows.append(controls)

    controls = []
    is_last_step = pending.step_index == len(pending.flow.steps) - 1
    if is_last_step:
        controls.append(
            {
                "text": "\u2705 done",
                "callback_data": f"log_done:{pending.token}",
            }
        )
    else:
        controls.append(
            {
                "text": "next \u27a1\ufe0f",
                "callback_data": f"log_next:{pending.token}",
            }
        )
    if controls:
        rows.append(controls)
    return rows


def _end_date_keyboard(token: str) -> list[list[dict[str, str]]]:
    """Build the end-date picker keyboard shown after a multi-day option."""
    rows: list[list[dict[str, str]]] = []
    current_row: list[dict[str, str]] = []
    for key, label in _END_DATE_CHOICES:
        current_row.append(
            {"text": label, "callback_data": f"log_enddate:{token}:{key}"}
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    return rows


def _step_prompt_text(pending: PendingLogEntry) -> str:
    step = pending.flow.steps[pending.step_index]
    header = f"\U0001f4cb Log — step {pending.step_index + 1}/{len(pending.flow.steps)}"
    hint = ""
    if step.multi_select:
        hint += "\n_Multi-select — tap options, then_ next"
    if step.optional:
        hint += "\n_Optional — you can just tap_ next"
    return f"{header}\n\n**{step.question}**{hint}"


def _end_date_prompt_text(options: list[str]) -> str:
    joined = ", ".join(options)
    return (
        f"\U0001f4c5 Until when?\n\nYou picked: _{joined}_. Choose an end date or skip."
    )


class LogFlowHandler:
    """State machine for the /log Telegram command."""

    def __init__(self, daemon: "ZdrowskitDaemon") -> None:
        self._daemon = daemon
        self._lock = threading.Lock()
        self._pending: dict[str, PendingLogEntry] = {}
        # Token of the session currently waiting on a free-text `+ note`.
        # Single-chat daemons only ever have one active at a time.
        self._awaiting_note_token: str | None = None

    @property
    def _poller(self):  # type: ignore[no-untyped-def]
        return self._daemon._poller

    # ------------------------------------------------------------------
    # Command entry
    # ------------------------------------------------------------------

    def handle_command(self, message_id: int) -> None:
        """Handle the Telegram /log command: build an LLM flow and render step 1."""
        from cmd_llm import build_log_flow

        placeholder_id = self._poller.send_reply(
            "Building today's check-in\u2026",
            reply_to_message_id=message_id,
        )

        try:
            flow = build_log_flow(db=self._daemon.db)
        except Exception:
            logger.error("build_log_flow failed", exc_info=True)
            if placeholder_id is not None:
                self._poller.edit_message(
                    placeholder_id,
                    "Couldn't build the log flow right now. Try /log again in a minute.",
                )
            else:
                self._poller.send_reply(
                    "Couldn't build the log flow right now. Try /log again in a minute.",
                    reply_to_message_id=message_id,
                )
            return

        token = f"lf_{secrets.token_hex(4)}"
        pending = PendingLogEntry(
            token=token,
            flow=flow,
            session_date=date.today(),
            chat_message_id=placeholder_id,
            original_message_id=message_id,
        )
        with self._lock:
            self._sweep_expired_locked()
            self._pending[token] = pending

        text = _step_prompt_text(pending)
        keyboard = _step_keyboard(pending)
        if placeholder_id is not None:
            self._poller.edit_message_with_keyboard(placeholder_id, text, keyboard)
        else:
            new_id = self._poller.send_message_with_keyboard(
                text, keyboard, reply_to_message_id=message_id
            )
            with self._lock:
                if token in self._pending:
                    self._pending[token].chat_message_id = new_id

    # ------------------------------------------------------------------
    # Callback dispatch
    # ------------------------------------------------------------------

    def handle_callback(self, cb_id: str, data: str, msg_id: int | None) -> None:
        """Dispatch ``log_*`` inline-keyboard callbacks."""
        with self._lock:
            self._sweep_expired_locked()

        parts = data.split(":")
        action = parts[0]
        if action == "log_toggle" and len(parts) == 4:
            self._handle_toggle(cb_id, parts[1], int(parts[2]), int(parts[3]), msg_id)
        elif action == "log_note" and len(parts) == 2:
            self._handle_note_request(cb_id, parts[1], msg_id)
        elif action == "log_next" and len(parts) == 2:
            self._handle_next(cb_id, parts[1], msg_id)
        elif action == "log_done" and len(parts) == 2:
            self._handle_done(cb_id, parts[1], msg_id)
        elif action == "log_cancel" and len(parts) == 2:
            self._handle_cancel(cb_id, parts[1], msg_id)
        elif action == "log_enddate" and len(parts) == 3:
            self._handle_enddate(cb_id, parts[1], parts[2], msg_id)
        else:
            self._poller.answer_callback_query(cb_id, "Unknown log action.")

    # ------------------------------------------------------------------
    # Free-text note intercept
    # ------------------------------------------------------------------

    def maybe_consume_note(self, text: str, message_id: int) -> bool:
        """Consume a free-text message as the active session's `+ note` tail.

        Must be called BEFORE the chat LLM handler in daemon_telegram_chat
        so the text is not forwarded as a chat turn.

        Returns True when the message was consumed.
        """
        if text.startswith("/"):
            return False

        with self._lock:
            token = self._awaiting_note_token
            if token is None:
                return False
            pending = self._pending.get(token)
            if pending is None or not pending.awaiting_note:
                self._awaiting_note_token = None
                return False
            pending.note = text.strip() or None
            pending.awaiting_note = False
            self._awaiting_note_token = None

        self._poller.send_reply(
            "\u2713 Note captured. Tap \u2705 done when ready.",
            reply_to_message_id=message_id,
        )
        return True

    # ------------------------------------------------------------------
    # Private handlers
    # ------------------------------------------------------------------

    def _sweep_expired_locked(self) -> None:
        """Drop sessions older than SESSION_TTL_SECONDS. Caller holds the lock."""
        now = time.time()
        expired = [
            token
            for token, pending in self._pending.items()
            if now - pending.created_at > SESSION_TTL_SECONDS
        ]
        for token in expired:
            logger.info("Expiring /log session %s", token)
            self._pending.pop(token, None)
            if self._awaiting_note_token == token:
                self._awaiting_note_token = None

    def _render_current(self, pending: PendingLogEntry) -> None:
        """Re-render the current step's message (text + keyboard)."""
        if pending.chat_message_id is None:
            return
        if pending.awaiting_end_date:
            # Show the end-date picker using the multi-day options the user
            # selected on the step that triggered it.
            step = pending.flow.steps[pending.step_index]
            triggers = step.ask_end_date_if_selected or []
            selected = pending.answers.get(step.id, [])
            matched = [o for o in selected if o in triggers]
            self._safe_edit(
                pending.chat_message_id,
                _end_date_prompt_text(matched),
                _end_date_keyboard(pending.token),
            )
            return
        self._safe_edit(
            pending.chat_message_id,
            _step_prompt_text(pending),
            _step_keyboard(pending),
        )

    def _safe_edit(
        self,
        message_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]],
    ) -> None:
        """Edit a message, swallowing 'message is not modified' errors."""
        try:
            self._poller.edit_message_with_keyboard(message_id, text, keyboard)
        except Exception:
            # The poller already logs at warning level; nothing more to do.
            logger.debug("Redundant edit on message %d suppressed", message_id)

    def _expired_message(self, cb_id: str, msg_id: int | None) -> None:
        self._poller.answer_callback_query(cb_id, "This log session expired.")
        if msg_id:
            self._poller.edit_message(
                msg_id, "This log session expired \u2014 run /log again."
            )

    def _handle_toggle(
        self,
        cb_id: str,
        token: str,
        step_idx: int,
        option_idx: int,
        msg_id: int | None,
    ) -> None:
        with self._lock:
            pending = self._pending.get(token)
            if pending is None:
                self._expired_message(cb_id, msg_id)
                return
            if pending.awaiting_end_date or pending.step_index != step_idx:
                # Stale button from a previous step / date picker.
                self._poller.answer_callback_query(cb_id, "Stale button.")
                return
            step = pending.flow.steps[step_idx]
            if option_idx < 0 or option_idx >= len(step.options):
                self._poller.answer_callback_query(cb_id, "Unknown option.")
                return
            option = step.options[option_idx]
            current = pending.answers.setdefault(step.id, [])
            if option in current:
                current.remove(option)
            else:
                if step.multi_select:
                    current.append(option)
                else:
                    current.clear()
                    current.append(option)

        self._poller.answer_callback_query(cb_id)
        self._render_current(pending)

    def _handle_note_request(self, cb_id: str, token: str, msg_id: int | None) -> None:
        with self._lock:
            pending = self._pending.get(token)
            if pending is None:
                self._expired_message(cb_id, msg_id)
                return
            pending.awaiting_note = True
            self._awaiting_note_token = token

        self._poller.answer_callback_query(cb_id, "Send your note as the next message.")
        self._poller.send_reply(
            "Reply with the note text \u2014 it'll be appended to the bullet.",
            reply_to_message_id=pending.original_message_id,
        )

    def _handle_next(self, cb_id: str, token: str, msg_id: int | None) -> None:
        with self._lock:
            pending = self._pending.get(token)
            if pending is None:
                self._expired_message(cb_id, msg_id)
                return
            step = pending.flow.steps[pending.step_index]
            selected = pending.answers.get(step.id, [])
            if not selected and not step.optional:
                self._poller.answer_callback_query(cb_id, "Pick at least one option.")
                return
            # Does this step need the end-date picker before advancing?
            triggers = step.ask_end_date_if_selected or []
            needs_end_date = pending.end_date is None and any(
                opt in triggers for opt in selected
            )
            if needs_end_date:
                pending.awaiting_end_date = True
            else:
                pending.step_index += 1

        self._poller.answer_callback_query(cb_id)
        self._render_current(pending)

    def _handle_enddate(
        self, cb_id: str, token: str, choice: str, msg_id: int | None
    ) -> None:
        with self._lock:
            pending = self._pending.get(token)
            if pending is None or not pending.awaiting_end_date:
                self._expired_message(cb_id, msg_id)
                return
            pending.end_date = _resolve_end_date(choice, pending.session_date)
            pending.awaiting_end_date = False
            pending.step_index += 1

        self._poller.answer_callback_query(cb_id, "Got it.")
        # If we just finished the last step, try the reactive follow-up
        # (or commit). Otherwise render the next pre-designed step.
        if pending.step_index >= len(pending.flow.steps):
            self._followup_or_commit(pending)
        else:
            self._render_current(pending)

    def _handle_done(self, cb_id: str, token: str, msg_id: int | None) -> None:
        with self._lock:
            pending = self._pending.get(token)
            if pending is None:
                self._expired_message(cb_id, msg_id)
                return
            # User might tap "done" on the last step without going through
            # "next" first — enforce the same end-date requirement here.
            step = pending.flow.steps[pending.step_index]
            selected = pending.answers.get(step.id, [])
            if not selected and not step.optional:
                self._poller.answer_callback_query(cb_id, "Pick at least one option.")
                return
            # If every step was optional and skipped, a bare `- YYYY-MM-DD`
            # bullet carries no signal — require at least one tag or a note.
            has_any = any(pending.answers.values()) or pending.note
            if not has_any:
                self._poller.answer_callback_query(
                    cb_id, "Pick at least one option or add a note."
                )
                return
            triggers = step.ask_end_date_if_selected or []
            needs_end_date = pending.end_date is None and any(
                opt in triggers for opt in selected
            )
            if needs_end_date:
                pending.awaiting_end_date = True
                self._poller.answer_callback_query(cb_id)
                self._render_current(pending)
                return

        self._poller.answer_callback_query(cb_id, "Saving\u2026")
        self._followup_or_commit(pending)

    def _handle_cancel(self, cb_id: str, token: str, msg_id: int | None) -> None:
        """Cancel the /log flow and discard any in-progress state."""
        with self._lock:
            self._pending.pop(token, None)
            if self._awaiting_note_token == token:
                self._awaiting_note_token = None

        self._poller.answer_callback_query(cb_id, "Cancelled.")
        if msg_id:
            self._poller.edit_message(msg_id, "Cancelled.")
            self._poller.edit_message_reply_markup(msg_id, None)

    # ------------------------------------------------------------------
    # Reactive follow-up + commit
    # ------------------------------------------------------------------

    def _followup_or_commit(self, pending: PendingLogEntry) -> None:
        """Consult the reactive follow-up step (once) or commit.

        Fired once per session, after the user finishes the initial state
        step. If the follow-up LLM returns a tailored step, it is
        appended to the flow and rendered. If it returns ``None`` (or
        the flow was already multi-step to begin with), the bullet is
        committed immediately.
        """
        if pending.followup_consulted or len(pending.flow.steps) > 1:
            self._commit(pending)
            return

        pending.followup_consulted = True
        prior_step = pending.flow.steps[0]
        prior_answer = list(pending.answers.get(prior_step.id, []))

        if pending.chat_message_id is not None:
            try:
                self._poller.edit_message(
                    pending.chat_message_id, "\U0001f914 Tailoring step 2…"
                )
            except Exception:
                logger.debug("Tailoring-step edit failed; continuing", exc_info=True)

        try:
            from cmd_llm import build_log_step_followup

            next_step = build_log_step_followup(
                prior_step=prior_step,
                prior_answer=prior_answer,
                db=self._daemon.db,
            )
        except Exception:
            logger.error("build_log_step_followup failed", exc_info=True)
            next_step = None

        if next_step is None:
            self._commit(pending)
            return

        pending.flow.steps.append(next_step)
        pending.step_index = len(pending.flow.steps) - 1
        self._render_current(pending)

    def _commit(self, pending: PendingLogEntry) -> None:
        """Append the composed bullet to log.md and finalise the message."""
        date_iso = pending.session_date.isoformat()
        bullet = compose_bullet(
            date_iso=date_iso,
            answers=pending.answers,
            end_date=pending.end_date,
            note=pending.note,
        )

        log_path = self._daemon.context_dir / "log.md"
        self._daemon._self_originated_writes.add(log_path.resolve())
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                # Ensure we start on a new line, in case the file lacks a
                # trailing newline.
                f.write("\n" + bullet + "\n")
        except OSError:
            logger.error("Failed to append to %s", log_path, exc_info=True)
            if pending.chat_message_id is not None:
                self._poller.edit_message(
                    pending.chat_message_id,
                    "Failed to write to log.md. Nothing saved.",
                )
            with self._lock:
                self._pending.pop(pending.token, None)
                if self._awaiting_note_token == pending.token:
                    self._awaiting_note_token = None
            return

        with self._lock:
            self._pending.pop(pending.token, None)
            if self._awaiting_note_token == pending.token:
                self._awaiting_note_token = None

        if pending.chat_message_id is not None:
            self._poller.edit_message(
                pending.chat_message_id,
                f"\u2705 Logged:\n\n`{bullet}`",
            )
