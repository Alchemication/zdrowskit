"""/notify Telegram command flow.

Handles the conversational notification-preferences command: parsing the
user request via the LLM, prompting for clarification if needed, presenting
proposed changes with accept/reject buttons, and persisting the result.

Extracted from ``daemon.py`` to keep that module focused on file watching
and scheduling. The handler owns its own pending-state maps and lock; it
borrows the daemon's ``db``, ``_poller``, and notification-prefs helpers.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from config import MAX_NUDGES_PER_DAY

if TYPE_CHECKING:
    from daemon import ZdrowskitDaemon

logger = logging.getLogger(__name__)


@dataclass
class PendingNotifyProposal:
    """A pending notification preference proposal awaiting user confirmation."""

    request_text: str
    preview: str
    summary: str
    changes: list[dict]


@dataclass
class PendingNotifyClarification:
    """A pending clarification prompt for a /notify request."""

    request_text: str


def _notify_keyboard(proposal_id: str) -> list[list[dict[str, str]]]:
    """Inline keyboard for a pending /notify proposal."""
    return [
        [
            {
                "text": "\u2705 Accept",
                "callback_data": f"notify_accept:{proposal_id}",
            },
            {
                "text": "\u274c Reject",
                "callback_data": f"notify_reject:{proposal_id}",
            },
        ]
    ]


class NotifyFlowHandler:
    """State machine for the /notify Telegram command.

    Owns its own pending-state maps and lock. Borrows the daemon's
    ``_poller``, ``db``, and notification-prefs helpers for I/O.
    """

    def __init__(self, daemon: "ZdrowskitDaemon") -> None:
        self._daemon = daemon
        self._lock = threading.Lock()
        self._pending_proposals: dict[str, PendingNotifyProposal] = {}
        self._pending_clarifications: dict[int, PendingNotifyClarification] = {}

    @property
    def _poller(self):  # type: ignore[no-untyped-def]
        return self._daemon._poller

    def handle_command(self, request_text: str, message_id: int) -> None:
        """Handle the Telegram /notify command."""
        from cmd_notify_interpreter import interpret_notify_request
        from notification_prefs import (
            format_notification_summary,
            format_proposed_changes,
        )

        now = datetime.now().astimezone()
        prefs = self._daemon._load_notification_prefs(now=now)

        if not request_text:
            self._poller.send_reply(
                format_notification_summary(
                    prefs,
                    now=now,
                    include_examples=True,
                    max_nudges_per_day=MAX_NUDGES_PER_DAY,
                ),
                reply_to_message_id=message_id,
            )
            return

        try:
            payload = interpret_notify_request(
                request_text,
                db=self._daemon.db,
                prefs=prefs,
                now=now,
            )
        except Exception:
            logger.error("Notify interpretation failed", exc_info=True)
            self._poller.send_reply(
                "I couldn't interpret that notification request. Try /notify to see supported examples.",
                reply_to_message_id=message_id,
            )
            return

        status = payload["status"]
        if status == "unsupported":
            reason = (
                payload.get("summary")
                or payload.get("reason")
                or "That request is not supported yet."
            )
            summary_text = format_notification_summary(
                prefs,
                now=now,
                include_examples=True,
                max_nudges_per_day=MAX_NUDGES_PER_DAY,
            )
            text = f"{reason}\n\n{summary_text}"
            self._poller.send_reply(text, reply_to_message_id=message_id)
            return

        if status == "needs_clarification":
            prompt_id = self._poller.send_reply(
                payload["clarification_question"],
                reply_to_message_id=message_id,
                force_reply=True,
            )
            if prompt_id is not None:
                with self._lock:
                    self._pending_clarifications[prompt_id] = (
                        PendingNotifyClarification(request_text=request_text)
                    )
            return

        if payload["intent"] == "show" and not payload["changes"]:
            self._poller.send_reply(
                format_notification_summary(
                    prefs,
                    now=now,
                    include_examples=True,
                    max_nudges_per_day=MAX_NUDGES_PER_DAY,
                ),
                reply_to_message_id=message_id,
            )
            return

        proposal_id = f"np_{time.time_ns()}"
        preview = format_proposed_changes(prefs, payload["changes"], now=now)
        summary = (
            payload.get("summary") or "Review the proposed notification changes below."
        )
        with self._lock:
            self._pending_proposals[proposal_id] = PendingNotifyProposal(
                request_text=request_text,
                preview=preview,
                summary=summary,
                changes=payload["changes"],
            )
        self._poller.send_message_with_keyboard(
            f"{summary}\n\n{preview}",
            _notify_keyboard(proposal_id),
            reply_to_message_id=message_id,
        )

    def consume_clarification(
        self,
        reply_to: dict,
        text: str,
        message: dict,
    ) -> bool:
        """Handle a free-text clarification reply for /notify."""
        from cmd_notify_interpreter import interpret_notify_request
        from notification_prefs import (
            format_notification_summary,
            format_proposed_changes,
        )

        prompt_id = reply_to.get("message_id")
        if prompt_id is None:
            return False

        with self._lock:
            pending = self._pending_clarifications.pop(prompt_id, None)
        if pending is None:
            return False

        now = datetime.now().astimezone()
        prefs = self._daemon._load_notification_prefs(now=now)
        try:
            payload = interpret_notify_request(
                pending.request_text,
                db=self._daemon.db,
                prefs=prefs,
                now=now,
                clarification_answer=text,
            )
        except Exception:
            logger.error("Notify clarification failed", exc_info=True)
            self._poller.send_reply(
                "I still couldn't parse that. Try /notify to see supported examples.",
                reply_to_message_id=message["message_id"],
            )
            return True

        if payload["status"] == "needs_clarification":
            next_prompt_id = self._poller.send_reply(
                payload["clarification_question"],
                reply_to_message_id=message["message_id"],
                force_reply=True,
            )
            if next_prompt_id is not None:
                with self._lock:
                    self._pending_clarifications[next_prompt_id] = pending
            return True

        if payload["status"] == "unsupported":
            self._poller.send_reply(
                payload.get("summary")
                or "That notification request is not supported yet.",
                reply_to_message_id=message["message_id"],
            )
            return True

        if payload["intent"] == "show" and not payload["changes"]:
            self._poller.send_reply(
                format_notification_summary(
                    prefs,
                    now=now,
                    include_examples=True,
                    max_nudges_per_day=MAX_NUDGES_PER_DAY,
                ),
                reply_to_message_id=message["message_id"],
            )
            return True

        proposal_id = f"np_{time.time_ns()}"
        preview = format_proposed_changes(prefs, payload["changes"], now=now)
        summary = (
            payload.get("summary") or "Review the proposed notification changes below."
        )
        with self._lock:
            self._pending_proposals[proposal_id] = PendingNotifyProposal(
                request_text=pending.request_text,
                preview=preview,
                summary=summary,
                changes=payload["changes"],
            )
        self._poller.send_message_with_keyboard(
            f"{summary}\n\n{preview}",
            _notify_keyboard(proposal_id),
            reply_to_message_id=message["message_id"],
        )
        return True

    def handle_callback(self, cb_id: str, data: str, msg_id: int | None) -> None:
        """Dispatch ``notify_accept:`` / ``notify_reject:`` inline-keyboard callbacks."""
        if data.startswith("notify_accept:"):
            self._handle_accept(cb_id, data.split(":", 1)[1], msg_id)
        elif data.startswith("notify_reject:"):
            self._handle_reject(cb_id, data.split(":", 1)[1], msg_id)
        else:
            self._poller.answer_callback_query(cb_id, "Unknown action.")

    def _handle_accept(self, cb_id: str, proposal_id: str, msg_id: int | None) -> None:
        with self._lock:
            pending = self._pending_proposals.pop(proposal_id, None)
        if not pending:
            self._poller.answer_callback_query(cb_id, "This proposal expired.")
            if msg_id:
                self._poller.edit_message(
                    msg_id, "This notification proposal has expired."
                )
            return

        from notification_prefs import apply_notification_changes

        now = datetime.now().astimezone()
        prefs = self._daemon._load_notification_prefs(now=now)
        updated = apply_notification_changes(prefs, pending.changes)
        self._daemon._save_notification_prefs(updated)
        self._poller.answer_callback_query(cb_id, "Applied!")
        if msg_id:
            self._poller.edit_message(
                msg_id,
                f"\u2705 Applied notification changes.\n\n{pending.preview}",
            )

    def _handle_reject(self, cb_id: str, proposal_id: str, msg_id: int | None) -> None:
        with self._lock:
            pending = self._pending_proposals.pop(proposal_id, None)
        self._poller.answer_callback_query(cb_id, "Discarded.")
        if msg_id:
            if pending is None:
                self._poller.edit_message(
                    msg_id, "This notification proposal has expired."
                )
            else:
                self._poller.edit_message(
                    msg_id,
                    f"\u274c Discarded notification changes.\n\n{pending.preview}",
                )
