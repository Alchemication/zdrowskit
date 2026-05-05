"""Telegram chat handler for the zdrowskit daemon.

Owns the Telegram polling thread, message routing, command dispatch,
inline-keyboard callback handling, tutorial wizard, and the LLM chat
reply loop. Extracted from ``daemon.py`` to keep that module focused on
file watching, scheduling, and runner logic.

The handler holds its own state (poller, conversation buffer, pending
edits) and borrows the daemon's shared resources (db, model, state,
lock, etc.) through a back-reference.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from config import MAX_TOKENS_CHAT

if TYPE_CHECKING:
    from daemon import ZdrowskitDaemon

logger = logging.getLogger(__name__)

AGENT_MODE_TIMEOUT_MIN = 30
"""Minutes after which plain-message agent mode (codex/claude) turns off."""


def _agent_session_key(kind: str) -> str:
    """State key holding the saved session id for an agent kind."""
    return f"{kind}_session_id"


_AGENT_LABELS = {"codex": "Codex", "claude": "Claude"}


def _format_telegram_command(command: dict[str, str]) -> str:
    """Render a command entry with usage hints for commands with arguments."""
    name = command["command"]
    description = command["description"]
    if name == "review":
        return f"/review [current|last] — {description} (default: last)"
    if name == "coach":
        return f"/coach [current|last] — {description} (default: last)"
    if name == "context":
        return f"/context [name] — {description}"
    if name == "add":
        return f"/add — {description} (workouts, sleep)"
    if name == "events":
        return f"/events [N] [category] — {description} (default: last 3 days)"
    return f"/{name} — {description}"


class TelegramChatHandler:
    """Message router, command dispatcher, and LLM chat loop for Telegram.

    Owns:
        ``_poller``          — :class:`TelegramPoller` instance (created lazily).
        ``_conversation``    — :class:`ConversationBuffer` for multi-turn chat.
        ``_pending_edits``   — :class:`PendingEdits` for context-edit proposals.

    Borrows from the daemon (via ``self._daemon``):
        ``db``, ``model``, ``context_dir``, ``_state``, ``_lock``,
        ``_stop_event``, ``_pending_rejection_reasons``,
        ``_pending_feedback_reasons``, ``_self_originated_writes``,
        ``_add_flow``, ``_notify_flow``, ``_log_flow``, and various helper methods.
    """

    def __init__(self, daemon: "ZdrowskitDaemon") -> None:
        self._daemon = daemon
        # These are initialised lazily in ``start()`` because they depend
        # on environment variables that may not be set.
        self._poller = None  # type: ignore[assignment]
        self._conversation = None  # type: ignore[assignment]
        self._pending_edits = None  # type: ignore[assignment]

    def start(self) -> None:
        """Start Telegram long-polling in a daemon thread.

        Does nothing if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are not set.
        """
        import os

        from telegram_bot import ConversationBuffer, TelegramPoller

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id:
            logger.warning(
                "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — "
                "Telegram chat listener disabled"
            )
            return

        self._poller = TelegramPoller(bot_token, chat_id)
        self._conversation = ConversationBuffer()

        from context_edit import PendingEdits

        self._pending_edits = PendingEdits()

        thread = threading.Thread(
            target=self._poller.poll_loop,
            args=(self._handle_telegram_message, self._daemon._stop_event),
            kwargs={"on_callback": self._handle_telegram_callback},
            daemon=True,
            name="telegram-poller",
        )
        thread.start()
        logger.info("Telegram chat listener started")

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    def _handle_telegram_message(self, message: dict) -> None:
        """Process an incoming Telegram message and reply via LLM.

        If the message is a reply to an earlier bot message (e.g. a nudge or
        report), the original text is injected into the conversation so the
        LLM knows what the user is responding to.

        Args:
            message: Telegram message dict from the Bot API.
        """
        text = (message.get("text") or "").strip()
        if not text:
            return

        reply_to = message.get("reply_to_message")
        if reply_to and self._daemon._consume_rejection_reason(reply_to, text):
            self._poller.send_reply(
                "Saved the rejection reason.",
                reply_to_message_id=message["message_id"],
            )
            return

        # Capture optional free-text feedback reason.
        if reply_to and self._daemon._consume_feedback_reason(reply_to, text):
            self._poller.send_reply(
                "\u2713 Feedback saved, thanks!",
                reply_to_message_id=message["message_id"],
            )
            return

        if reply_to and self._daemon._notify_flow.consume_clarification(
            reply_to, text, message
        ):
            return

        message_id = message["message_id"]

        # `+ note` free-text intercept for /log — must land BEFORE the chat
        # LLM path so the note text is not consumed as a chat turn.
        if self._daemon._log_flow.maybe_consume_note(text, message_id):
            return

        # Handle bot commands before the LLM.
        if text.startswith("/"):
            self._handle_command(text, message_id)
            return

        reply_kind = self._is_agent_reply(reply_to)
        if reply_kind:
            self._handle_agent_turn(
                text, message_id, kind=reply_kind, new_session=False
            )
            return

        active_kind = self._agent_mode_active()
        if active_kind:
            self._handle_agent_turn(
                text, message_id, kind=active_kind, new_session=False
            )
            return

        # If the user replied to a specific bot message, inject its text
        # so the LLM has the context of what they're responding to.
        if reply_to and reply_to.get("text"):
            quoted = reply_to["text"]
            # Truncate very long originals (e.g. full weekly reports)
            if len(quoted) > 800:
                quoted = quoted[:800] + "\n[...truncated]"
            self._conversation.clear()
            self._conversation.add(
                "assistant", f"[Previous message you sent]\n{quoted}"
            )

        self._conversation.add("user", text)

        # Send a placeholder so the user sees immediate feedback, and
        # animate the trailing dots so they know the bot is alive while
        # the LLM call is in flight.
        typing_prefix = "Typing "
        placeholder_id = self._poller.send_reply(
            f"{typing_prefix}.", reply_to_message_id=message_id
        )
        stop_anim, anim_thread = self._start_placeholder_animation(
            placeholder_id, prefix=typing_prefix
        )

        try:
            from store import open_db

            conn = open_db(self._daemon.db)
            try:
                result, deferred_edits, query_rows = self._chat_reply(conn)
            finally:
                conn.close()
        except Exception:
            self._stop_placeholder_animation(stop_anim, anim_thread)
            logger.error("Chat LLM call failed", exc_info=True)
            if placeholder_id:
                self._poller.edit_message(
                    placeholder_id, "Something went wrong — try again in a minute."
                )
            else:
                self._poller.send_reply(
                    "Something went wrong — try again in a minute.",
                    reply_to_message_id=message_id,
                )
            return

        self._stop_placeholder_animation(stop_anim, anim_thread)

        reply = result.text

        # Extract and render any <chart> blocks from the response.
        from charts import (
            chart_figure_caption,
            extract_charts,
            render_chart,
            strip_charts,
        )

        chart_blocks = extract_charts(reply)
        if chart_blocks:
            extra_ns = {"rows": query_rows} if query_rows else None
            for index, block in enumerate(chart_blocks, start=1):
                try:
                    img = render_chart(block.code, {}, extra_namespace=extra_ns)
                    if img:
                        self._poller.send_photo(
                            img, caption=chart_figure_caption(index, block.title)
                        )
                except Exception:
                    logger.warning(
                        "Chart render failed: %s", block.title, exc_info=True
                    )
            reply = strip_charts(reply)

        self._conversation.add("assistant", reply)

        # Send/edit the reply, attaching a feedback 👎 button if possible.
        from telegram_bot import feedback_keyboard

        if result.llm_call_id is not None:
            kb = feedback_keyboard(result.llm_call_id, "chat")
            if placeholder_id:
                self._poller.edit_message_with_keyboard(placeholder_id, reply, kb)
            else:
                self._poller.send_message_with_keyboard(
                    reply, kb, reply_to_message_id=message_id
                )
        elif placeholder_id:
            self._poller.edit_message(placeholder_id, reply)
        else:
            self._poller.send_reply(reply, reply_to_message_id=message_id)

        # Propose any deferred context edits from the tool-calling loop.
        for edit in deferred_edits:
            self._daemon._propose_context_edit(edit, source="chat")

    # ------------------------------------------------------------------
    # Placeholder animation
    # ------------------------------------------------------------------

    def _start_placeholder_animation(
        self,
        message_id: int | None,
        *,
        prefix: str = "",
        frames: tuple[str, ...] = (".", "..", "..."),
    ) -> tuple[threading.Event | None, threading.Thread | None]:
        """Start a daemon thread that animates a placeholder message.

        The thread cycles a small set of frames so the user knows a
        long-running task is still alive. Safe to call with a None
        message_id (no-op).

        Args:
            message_id: ID of the placeholder, or None to skip animation.
            prefix: Optional text shown before the animated frame.
            frames: Sequence of strings to cycle through. Defaults to
                ``.``, ``..``, ``...``.

        Returns:
            ``(stop_event, thread)`` — pass both to
            :meth:`_stop_placeholder_animation` once the task is done.
        """
        if message_id is None:
            return None, None
        stop = threading.Event()
        thread = threading.Thread(
            target=self._poller.animate_message,
            args=(message_id, stop),
            kwargs={"prefix": prefix, "frames": frames},
            daemon=True,
        )
        thread.start()
        return stop, thread

    @staticmethod
    def _stop_placeholder_animation(
        stop: threading.Event | None, thread: threading.Thread | None
    ) -> None:
        """Signal the animation thread to stop and wait briefly for it."""
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join(timeout=1.5)

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def _handle_command(self, text: str, message_id: int) -> None:
        """Handle a Telegram bot /command.

        Args:
            text: The full message text starting with ``/``.
            message_id: Telegram message ID for replies.
        """
        cmd = text.split()[0].lower().split("@")[0]  # strip @botname suffix

        if cmd == "/clear":
            self._conversation.clear()
            self._poller.send_reply(
                "Conversation cleared.", reply_to_message_id=message_id
            )
        elif cmd == "/review":
            parts = text.split()
            week = "last"
            if len(parts) > 1:
                raw_week = parts[1].lower()
                if raw_week not in {"current", "last"}:
                    self._poller.send_reply(
                        "Use /review or /review current or /review last.",
                        reply_to_message_id=message_id,
                    )
                    return
                week = raw_week
            label = "this week so far" if week == "current" else "last week"
            status_prefix = f"Running review for {label} "
            status_id = self._poller.send_reply(
                f"{status_prefix}.", reply_to_message_id=message_id
            )
            stop_anim, anim_thread = self._start_placeholder_animation(
                status_id, prefix=status_prefix
            )
            try:
                self._daemon._run_review(week=week, skip_import=False)
            finally:
                self._stop_placeholder_animation(stop_anim, anim_thread)
                if status_id is not None:
                    self._poller.edit_message(
                        status_id, f"\u2713 Review for {label} done."
                    )
        elif cmd == "/coach":
            parts = text.split()
            week = "last"
            if len(parts) > 1:
                raw_week = parts[1].lower()
                if raw_week not in {"current", "last"}:
                    self._poller.send_reply(
                        "Use /coach or /coach current or /coach last.",
                        reply_to_message_id=message_id,
                    )
                    return
                week = raw_week
            label = "this week so far" if week == "current" else "last week"
            status_prefix = f"Running coaching review for {label} "
            status_id = self._poller.send_reply(
                f"{status_prefix}.", reply_to_message_id=message_id
            )
            stop_anim, anim_thread = self._start_placeholder_animation(
                status_id, prefix=status_prefix
            )
            try:
                # force=True so the user can retrigger on demand (e.g. if the
                # Monday scheduled run was missed or silent-SKIPped and they
                # want to try again).
                self._daemon._run_coach(week=week, skip_import=False, force=True)
            finally:
                self._stop_placeholder_animation(stop_anim, anim_thread)
                if status_id is not None:
                    self._poller.edit_message(
                        status_id, f"\u2713 Coaching review for {label} done."
                    )
        elif cmd == "/notify":
            args = text.split(maxsplit=1)
            request_text = args[1].strip() if len(args) > 1 else ""
            self._daemon._notify_flow.handle_command(request_text, message_id)
        elif cmd == "/models":
            self._daemon._model_flow.handle_command(message_id)
        elif cmd == "/log":
            self._daemon._log_flow.handle_command(message_id)
        elif cmd == "/status":
            self._poller.send_reply(
                "\n".join(self._daemon._build_status_lines()),
                reply_to_message_id=message_id,
            )
        elif cmd == "/events":
            self._handle_events_command(text, message_id)
        elif cmd == "/codex":
            self._handle_agent_command(text, message_id, kind="codex")
        elif cmd == "/claude":
            self._handle_agent_command(text, message_id, kind="claude")
        elif cmd == "/context":
            parts = text.split()
            file_arg = parts[1] if len(parts) > 1 else None
            self._send_context_overview(message_id, file_arg)
        elif cmd == "/add":
            self._daemon._add_flow.handle_command(message_id)
        elif cmd == "/tutorial":
            self._handle_tutorial_start(message_id)
        elif cmd == "/advanced":
            from commands import ADVANCED_TELEGRAM_BOT_COMMANDS, TELEGRAM_BOT_COMMANDS
            from config import CONTEXT_DIR, PROMPTS_DIR

            ctx_names = sorted(
                f.stem
                for d in (CONTEXT_DIR, PROMPTS_DIR)
                for f in d.glob("*.md")
                if f.stat().st_size > 0
            )
            ctx_opts = ", ".join(ctx_names) if ctx_names else "none found"
            lines = ["Menu commands:"]
            lines.extend(
                _format_telegram_command(command) for command in TELEGRAM_BOT_COMMANDS
            )
            lines.append("\nAdvanced commands:")
            lines.extend(
                _format_telegram_command(command)
                for command in ADVANCED_TELEGRAM_BOT_COMMANDS
            )
            lines.append("\nAgents:")
            lines.append("/codex — Open Codex panel")
            lines.append("/claude — Open Claude panel")
            lines.append(f"\nAvailable context files: {ctx_opts}")
            self._poller.send_reply("\n".join(lines), reply_to_message_id=message_id)
        else:
            self._poller.send_reply(
                "Unknown command. Try /advanced",
                reply_to_message_id=message_id,
            )

    # ------------------------------------------------------------------
    # Agent bridge (Codex + Claude)
    # ------------------------------------------------------------------

    def _handle_agent_command(self, text: str, message_id: int, *, kind: str) -> None:
        """Handle ``/codex`` or ``/claude`` workspace-write repo questions."""
        label = _AGENT_LABELS[kind]

        parts = text.split(maxsplit=1)
        request_text = parts[1].strip() if len(parts) > 1 else ""
        if not request_text:
            self._send_agent_panel(kind, reply_to_message_id=message_id)
            return

        first_word, _, rest = request_text.partition(" ")
        action = first_word.lower()
        if action == "on":
            self._enable_agent_mode(kind)
            prompt = rest.strip()
            if prompt:
                self._handle_agent_turn(
                    prompt, message_id, kind=kind, new_session=False
                )
            else:
                self._poller.send_reply(
                    f"{label} mode on for {AGENT_MODE_TIMEOUT_MIN} min. "
                    f"Plain messages now go to {label}. Use /{kind} off to exit.",
                    reply_to_message_id=message_id,
                )
            return

        if action == "off" and not rest:
            if self._agent_mode_active() == kind:
                self._disable_agent_mode()
                self._poller.send_reply(
                    f"{label} mode off. Plain messages go back to health chat.",
                    reply_to_message_id=message_id,
                )
            else:
                active = self._agent_mode_active()
                if active:
                    self._poller.send_reply(
                        f"{label} mode wasn't on ({_AGENT_LABELS[active]} mode is). "
                        f"Use /{active} off.",
                        reply_to_message_id=message_id,
                    )
                else:
                    self._poller.send_reply(
                        f"{label} mode wasn't on.",
                        reply_to_message_id=message_id,
                    )
            return

        if action == "reset":
            prompt = rest.strip()
            self._clear_agent_session(kind)
            if self._agent_mode_active() == kind:
                self._refresh_agent_mode()
            self._daemon._save_state()
            if prompt:
                self._handle_agent_turn(prompt, message_id, kind=kind, new_session=True)
            else:
                self._poller.send_reply(
                    f"{label} context cleared.", reply_to_message_id=message_id
                )
            return

        if action == "stop" and not rest:
            self._clear_agent_session(kind)
            if self._agent_mode_active() == kind:
                self._disable_agent_mode(save=False)
            self._daemon._save_state()
            self._poller.send_reply(
                f"{label} session cleared and mode off.",
                reply_to_message_id=message_id,
            )
            return

        if action == "new":
            prompt = rest.strip()
            if not prompt:
                self._poller.send_reply(
                    self._agent_usage(kind), reply_to_message_id=message_id
                )
                return
            self._handle_agent_turn(prompt, message_id, kind=kind, new_session=True)
            return

        self._handle_agent_turn(request_text, message_id, kind=kind, new_session=False)

    @staticmethod
    def _agent_usage(kind: str) -> str:
        """Return the help text for an agent kind."""
        if kind == "codex":
            from daemon_agent_flow import codex_usage

            return codex_usage()
        from daemon_claude_flow import claude_usage

        return claude_usage()

    def _send_agent_panel(
        self, kind: str, *, reply_to_message_id: int | None = None
    ) -> int | None:
        """Send the compact inline-button panel for one agent."""
        text, buttons = self._agent_panel(kind)
        return self._poller.send_message_with_keyboard(
            text, buttons, reply_to_message_id=reply_to_message_id
        )

    def _edit_agent_panel(self, message_id: int, kind: str) -> None:
        """Refresh an existing agent panel message."""
        text, buttons = self._agent_panel(kind)
        self._poller.edit_message_with_keyboard(message_id, text, buttons)

    def _agent_panel(self, kind: str) -> tuple[str, list[list[dict[str, str]]]]:
        """Return panel text and buttons for one agent."""
        label = _AGENT_LABELS[kind]
        active = self._agent_mode_active()
        if active == kind:
            minutes = self._agent_mode_minutes_left()
            suffix = f" · {minutes} min left" if minutes is not None else ""
            text = f"{label}: on{suffix}"
            primary = {"text": "Turn off", "callback_data": f"agent:off:{kind}"}
        else:
            other = f" · {_AGENT_LABELS[active]} active" if active else ""
            text = f"{label}: off{other}"
            button_text = f"Switch to {label}" if active else "Turn on"
            primary = {"text": button_text, "callback_data": f"agent:on:{kind}"}

        return (
            text,
            [
                [
                    primary,
                    {"text": "New session", "callback_data": f"agent:new:{kind}"},
                ]
            ],
        )

    @staticmethod
    def _agent_exit_keyboard(kind: str) -> list[list[dict[str, str]]]:
        """Return the inline keyboard for leaving active agent mode."""
        return [[{"text": "Back to chat", "callback_data": f"agent:exit:{kind}"}]]

    def _enable_agent_mode(self, kind: str) -> None:
        """Route plain non-command messages to ``kind`` until timeout."""
        self._daemon._state["agent_mode"] = kind
        self._refresh_agent_mode(save=False)
        self._daemon._save_state()

    def _disable_agent_mode(self, *, save: bool = True) -> None:
        """Stop routing plain messages to any agent."""
        self._daemon._state.pop("agent_mode", None)
        self._daemon._state.pop("agent_mode_expires_at", None)
        if save:
            self._daemon._save_state()

    def _refresh_agent_mode(self, *, save: bool = True) -> None:
        """Extend agent mode after user activity."""
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=AGENT_MODE_TIMEOUT_MIN
        )
        self._daemon._state["agent_mode_expires_at"] = expires_at.isoformat()
        if save:
            self._daemon._save_state()

    def _agent_mode_active(self) -> str | None:
        """Return the active agent kind (``codex`` / ``claude``) or ``None``."""
        kind = self._daemon._state.get("agent_mode")
        if kind not in _AGENT_LABELS:
            return None
        expires_raw = self._daemon._state.get("agent_mode_expires_at")
        if isinstance(expires_raw, str):
            try:
                expires_at = datetime.fromisoformat(expires_raw)
            except ValueError:
                expires_at = None
            if expires_at is not None:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                now = datetime.now(expires_at.tzinfo)
                if expires_at <= now:
                    self._disable_agent_mode()
                    return None
        return kind

    def _agent_mode_minutes_left(self) -> int | None:
        """Return whole minutes left for the active agent mode."""
        expires_raw = self._daemon._state.get("agent_mode_expires_at")
        if not isinstance(expires_raw, str):
            return None
        try:
            expires_at = datetime.fromisoformat(expires_raw)
        except ValueError:
            return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        now = datetime.now(expires_at.tzinfo)
        seconds_left = max(0, (expires_at - now).total_seconds())
        return max(1, int((seconds_left + 59) // 60))

    def _clear_agent_session(self, kind: str) -> None:
        """Forget saved session pointers for one agent kind.

        Also clears the shared "last agent message id" iff it was last set
        by this kind, so replies don't accidentally continue a stale session.
        """
        self._daemon._state.pop(_agent_session_key(kind), None)
        if self._daemon._state.get("agent_last_message_kind") == kind:
            self._daemon._state.pop("agent_last_message_id", None)
            self._daemon._state.pop("agent_last_message_kind", None)

    def _handle_agent_turn(
        self,
        prompt: str,
        message_id: int,
        *,
        kind: str,
        new_session: bool,
    ) -> None:
        """Run one agent turn (codex/claude) and send the result to Telegram."""
        if kind == "codex":
            from daemon_agent_flow import CodexRunError as _RunError
            from daemon_agent_flow import run_codex_workspace as _run

            run = _run
            run_error: type[Exception] = _RunError
        else:
            from daemon_claude_flow import ClaudeRunError as _RunError
            from daemon_claude_flow import run_claude_workspace as _run

            run = _run
            run_error = _RunError
        label = _AGENT_LABELS[kind]
        session_key = _agent_session_key(kind)

        status_prefix = f"{label} reading "
        status_id = self._poller.send_reply(
            f"{status_prefix}.", reply_to_message_id=message_id
        )
        stream_agent = status_id is not None and kind in _AGENT_LABELS
        progress_callback: object | None = None
        if stream_agent and status_id is not None:
            stop_anim, anim_thread, progress_callback = self._start_agent_stream_status(
                status_id, label
            )
        else:
            stop_anim, anim_thread = self._start_placeholder_animation(
                status_id, prefix=status_prefix
            )
        session_id = None if new_session else self._daemon._state.get(session_key)
        started_at = time.monotonic()

        try:
            run_kwargs: dict[str, object] = {
                "cwd": Path(__file__).resolve().parent.parent,
                "session_id": session_id if isinstance(session_id, str) else None,
            }
            if progress_callback is not None:
                run_kwargs["progress_callback"] = progress_callback
            result = run(
                prompt,
                **run_kwargs,
            )
        except ValueError:
            self._stop_placeholder_animation(stop_anim, anim_thread)
            if status_id:
                self._poller.edit_message(status_id, f"Use /{kind} <prompt>.")
            else:
                self._poller.send_reply(
                    f"Use /{kind} <prompt>.", reply_to_message_id=message_id
                )
            return
        except run_error as exc:
            self._stop_placeholder_animation(stop_anim, anim_thread)
            logger.warning("%s Telegram command failed: %s", label, exc)
            text = str(exc)
            if status_id:
                self._poller.edit_message(status_id, text)
            else:
                self._poller.send_reply(text, reply_to_message_id=message_id)
            return

        self._stop_placeholder_animation(stop_anim, anim_thread)
        elapsed_s = int(time.monotonic() - started_at)
        result_text = (
            self._append_agent_elapsed(result.text, label, elapsed_s)
            if stream_agent
            else result.text
        )

        if result.session_id:
            self._daemon._state[session_key] = result.session_id
        agent_mode_active = self._agent_mode_active() == kind
        if agent_mode_active:
            self._refresh_agent_mode(save=False)
        if agent_mode_active:
            sent_id = self._send_agent_result_with_exit(
                result_text,
                kind=kind,
                status_id=status_id,
                reply_to_message_id=message_id,
            )
            if sent_id:
                self._daemon._state["agent_last_message_id"] = sent_id
                self._daemon._state["agent_last_message_kind"] = kind
        else:
            if status_id:
                self._poller.edit_message(status_id, result_text)
                self._daemon._state["agent_last_message_id"] = status_id
                self._daemon._state["agent_last_message_kind"] = kind
            else:
                sent_id = self._poller.send_reply(
                    result_text, reply_to_message_id=message_id
                )
                if sent_id:
                    self._daemon._state["agent_last_message_id"] = sent_id
                    self._daemon._state["agent_last_message_kind"] = kind
        self._daemon._save_state()

    def _start_agent_stream_status(
        self, status_id: int, label: str
    ) -> tuple[threading.Event, threading.Thread, object]:
        """Animate a friendly streaming status message for an agent."""
        stop = threading.Event()
        state = {"stage": "Starting up"}

        def progress_callback(progress: str) -> None:
            state["stage"] = self._friendly_agent_stage(progress)

        def animate() -> None:
            started = time.monotonic()
            frames = (".", "..", "...")
            frame_index = 0
            while not stop.is_set():
                elapsed = int(time.monotonic() - started)
                text = (
                    f"**{label} is working{frames[frame_index % len(frames)]}**\n\n"
                    f"**Status**  {state['stage']}\n"
                    f"**Elapsed**  {self._format_elapsed(elapsed)}\n\n"
                    "_Final answer will replace this message._"
                )
                self._poller.edit_message(status_id, text)
                frame_index += 1
                stop.wait(1.5)

        thread = threading.Thread(target=animate, daemon=True)
        thread.start()
        return stop, thread, progress_callback

    @staticmethod
    def _friendly_agent_stage(progress: str) -> str:
        """Convert noisy agent stream progress into a stable user-facing stage."""
        normalized = progress.lower()
        if "session" in normalized:
            return "Session ready"
        if any(token in normalized for token in ("patch", "edit", "file", "write")):
            return "Inspecting or editing files"
        if any(
            token in normalized for token in ("command", "cmd", "exec", "bash", "tool")
        ):
            return "Running a repo command"
        if any(
            token in normalized for token in ("message", "final", "answer", "assistant")
        ):
            return "Drafting the reply"
        return "Working through the request"

    @staticmethod
    def _append_agent_elapsed(text: str, label: str, elapsed_s: int) -> str:
        """Append a small elapsed-time footer to a completed agent response."""
        return f"{text.rstrip()}\n\n_{label} finished in {TelegramChatHandler._format_elapsed(elapsed_s)}._"

    @staticmethod
    def _format_elapsed(elapsed_s: int) -> str:
        """Format elapsed seconds compactly for Telegram."""
        elapsed_s = max(0, elapsed_s)
        minutes, seconds = divmod(elapsed_s, 60)
        if minutes:
            return f"{minutes}m {seconds:02d}s"
        return f"{seconds}s"

    def _send_agent_result_with_exit(
        self,
        text: str,
        *,
        kind: str,
        status_id: int | None,
        reply_to_message_id: int,
    ) -> int | None:
        """Send an active-mode agent result with a Back to chat button."""
        buttons = self._agent_exit_keyboard(kind)
        if status_id is None:
            return self._poller.send_message_with_keyboard(
                text, buttons, reply_to_message_id=reply_to_message_id
            )

        from notify import chunk_text

        chunks = chunk_text(text)
        if len(chunks) == 1:
            self._poller.edit_message_with_keyboard(status_id, text, buttons)
            return status_id

        self._poller.edit_message(status_id, chunks[0])
        sent_id = self._poller.send_message_with_keyboard(
            "\n\n".join(chunks[1:]), buttons
        )
        return sent_id or status_id

    def _is_agent_reply(self, reply_to: dict | None) -> str | None:
        """Return the agent kind whose last message ``reply_to`` matches, or ``None``."""
        if not reply_to:
            return None
        last_id = self._daemon._state.get("agent_last_message_id")
        last_kind = self._daemon._state.get("agent_last_message_kind")
        if last_id is None or last_kind not in _AGENT_LABELS:
            return None
        try:
            if int(reply_to.get("message_id")) == int(last_id):
                return last_kind
        except (TypeError, ValueError):
            return None
        return None

    # ------------------------------------------------------------------
    # Context overview
    # ------------------------------------------------------------------

    def _handle_events_command(self, text: str, message_id: int) -> None:
        """Handle ``/events [N | category]`` — show recent system events.

        With no argument, shows events from the last 3 days. A numeric
        argument overrides the day window; a category token (nudge,
        import, coach, …) filters to that category over the default 3-day
        window. Combinations like ``/events nudge 7`` are supported.

        Args:
            text: The full ``/events …`` message text.
            message_id: Telegram message ID for reply threading.
        """
        from datetime import datetime, timedelta, timezone

        from cmd_events import format_events_for_telegram
        from events import CATEGORIES, query_events
        from store import open_db

        parts = text.split()[1:]
        days = 3
        category: str | None = None
        for part in parts:
            if part.isdigit():
                days = max(1, int(part))
            elif part.lower() in CATEGORIES:
                category = part.lower()
            else:
                self._poller.send_reply(
                    "Usage: /events [N] [category]. "
                    f"Categories: {', '.join(CATEGORIES)}.",
                    reply_to_message_id=message_id,
                )
                return

        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        conn = open_db(self._daemon.db)
        try:
            rows = query_events(conn, category=category, since=since, limit=200)
        finally:
            conn.close()

        if not rows:
            scope = f"last {days}d"
            if category:
                scope += f" · {category}"
            self._poller.send_reply(
                f"No system events ({scope}).", reply_to_message_id=message_id
            )
            return

        header_scope = f"last {days}d"
        if category:
            header_scope += f" · {category}"
        body = format_events_for_telegram(rows)
        self._poller.send_reply(
            f"_{header_scope}_\n{body}", reply_to_message_id=message_id
        )

    def _send_context_overview(
        self, message_id: int, file_arg: str | None = None
    ) -> None:
        """Send context file info to Telegram.

        With no argument, sends a compact index of all files.
        With a file name (e.g. ``me``), sends the full content, split across
        multiple messages if it exceeds Telegram's 4096-char limit.

        Args:
            message_id: Telegram message ID for reply threading.
            file_arg: Optional file stem to show full content for.
        """
        from config import CONTEXT_DIR, PROMPTS_DIR

        if file_arg:
            # Show full content of a specific file.
            stem = file_arg.removesuffix(".md")
            path = CONTEXT_DIR / f"{stem}.md"
            if not path.exists():
                path = PROMPTS_DIR / f"{stem}.md"
            if not path.exists():
                self._poller.send_reply(
                    f"File not found: {stem}.md", reply_to_message_id=message_id
                )
                return
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                self._poller.send_reply(
                    f"{path.name} is empty.", reply_to_message_id=message_id
                )
                return
            # Split into chunks respecting Telegram's 4096 limit.
            header = f"\U0001f4c4 {path.name}"
            self._send_long_message(header, content, message_id)
            return

        # No argument — send compact index.
        lines: list[str] = []
        ctx_files = sorted(CONTEXT_DIR.glob("*.md"))
        for f in ctx_files:
            try:
                content = f.read_text(encoding="utf-8")
                line_count = content.count("\n")
                size = f.stat().st_size
                lines.append(f"\U0001f4c4 {f.stem} — {line_count} lines ({size} B)")
            except OSError:
                lines.append(f"\U0001f4c4 {f.stem} — (unreadable)")

        if not lines:
            self._poller.send_reply(
                "No context files found.", reply_to_message_id=message_id
            )
            return

        lines.append("\nUse /context <name> to view a file.")
        self._poller.send_reply("\n".join(lines), reply_to_message_id=message_id)

    def _send_long_message(self, header: str, content: str, message_id: int) -> None:
        """Send content that may exceed Telegram's message limit.

        Splits into multiple messages at line boundaries.

        Args:
            header: Header shown in the first message.
            content: Full text content to send.
            message_id: Telegram message ID for reply threading.
        """
        max_len = 4096
        first_max = max_len - len(header) - 4  # room for header + newlines

        if len(content) <= first_max:
            self._poller.send_reply(
                f"{header}\n\n{content}", reply_to_message_id=message_id
            )
            return

        # Split at line boundaries.
        chunks: list[str] = []
        current_max = first_max
        remaining = content
        while remaining:
            if len(remaining) <= current_max:
                chunks.append(remaining)
                break
            # Find last newline within limit.
            cut = remaining.rfind("\n", 0, current_max)
            if cut <= 0:
                cut = current_max
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")
            current_max = max_len - 20  # subsequent chunks get full space

        for i, chunk in enumerate(chunks):
            if i == 0:
                text = f"{header}\n\n{chunk}"
            else:
                text = chunk
            self._poller.send_reply(text, reply_to_message_id=message_id)

    # ------------------------------------------------------------------
    # Callback dispatch
    # ------------------------------------------------------------------

    def _handle_telegram_callback(self, callback_query: dict) -> None:
        """Handle an inline keyboard button press.

        Args:
            callback_query: Telegram callback_query dict from the Bot API.
        """
        from context_edit import apply_edit

        cb_id = callback_query["id"]
        data = callback_query.get("data", "")
        msg = callback_query.get("message", {})
        msg_id = msg.get("message_id")

        if data.startswith("ctx_accept:"):
            edit_id = data.split(":", 1)[1]
            pending = self._pending_edits.pop(edit_id)
            if pending:
                try:
                    self._daemon._self_originated_writes.add(
                        (self._daemon.context_dir / f"{pending.edit.file}.md").resolve()
                    )
                    apply_edit(self._daemon.context_dir, pending.edit, strict=True)
                    self._daemon._record_context_feedback(pending, "accepted")
                    self._poller.answer_callback_query(cb_id, "Applied!")
                    if msg_id:
                        self._poller.edit_message(
                            msg_id,
                            f"\u2705 Applied: {pending.edit.summary}",
                        )
                except Exception:
                    logger.error("Failed to apply context edit", exc_info=True)
                    self._poller.answer_callback_query(cb_id, "Error applying edit.")
            else:
                self._poller.answer_callback_query(cb_id, "Expired or already handled.")
                if msg_id:
                    self._poller.edit_message(msg_id, "This edit has expired.")

        elif data.startswith("notify_"):
            self._daemon._notify_flow.handle_callback(cb_id, data, msg_id)

        elif data.startswith("model_"):
            self._daemon._model_flow.handle_callback(cb_id, data, msg_id)

        elif data.startswith("log_"):
            self._daemon._log_flow.handle_callback(cb_id, data, msg_id)

        elif data.startswith("agent:"):
            self._handle_agent_callback(cb_id, data, msg_id)

        elif data.startswith("ctx_diff:"):
            edit_id = data.split(":", 1)[1]
            pending = self._pending_edits.peek(edit_id)
            if pending:
                self._poller.answer_callback_query(cb_id)
                text = (
                    f"\U0001f4cb Suggestion — {pending.edit.file}.md\n"
                    f"{pending.edit.summary}\n\n"
                    f"```diff\n{pending.preview}\n```"
                )
                buttons = [
                    [
                        {
                            "text": "\u2705 Accept",
                            "callback_data": f"ctx_accept:{edit_id}",
                        },
                        {
                            "text": "\u274c Reject",
                            "callback_data": f"ctx_reject:{edit_id}",
                        },
                    ]
                ]
                if msg_id:
                    self._poller.edit_message_with_keyboard(msg_id, text, buttons)
            else:
                self._poller.answer_callback_query(cb_id, "Expired or already handled.")
                if msg_id:
                    self._poller.edit_message(msg_id, "This edit has expired.")

        elif data.startswith("ctx_reject:"):
            edit_id = data.split(":", 1)[1]
            pending = self._pending_edits.pop(edit_id)
            self._poller.answer_callback_query(cb_id, "Discarded.")
            if msg_id:
                summary = pending.edit.summary if pending else "unknown"
                self._poller.edit_message(msg_id, f"\u274c Discarded: {summary}")
            if pending:
                feedback_id = self._daemon._record_context_feedback(pending, "rejected")
                prompt_id = self._poller.send_reply(
                    "Optional: reply with why you rejected this suggestion.",
                    reply_to_message_id=msg_id,
                    force_reply=True,
                )
                if prompt_id is not None:
                    with self._daemon._lock:
                        self._daemon._pending_rejection_reasons[prompt_id] = feedback_id
                    self._daemon._save_pending_reason_state()

        elif data.startswith("fb_neg:"):
            # User tapped 👎 — swap to category picker (text untouched).
            parts = data.split(":")
            if len(parts) < 2:
                self._poller.answer_callback_query(cb_id, "Invalid feedback action.")
                return
            llm_call_id_str = parts[1]
            message_type = parts[2] if len(parts) >= 3 else "unknown"
            self._poller.answer_callback_query(cb_id)
            if msg_id:
                from telegram_bot import feedback_category_keyboard

                cats = feedback_category_keyboard(int(llm_call_id_str), message_type)
                self._poller.edit_message_reply_markup(msg_id, cats)

        elif data.startswith("fb_cat:"):
            # User picked a feedback category.
            parts = data.split(":")
            if len(parts) < 3:
                self._poller.answer_callback_query(cb_id, "Invalid feedback category.")
                return
            llm_call_id = int(parts[1])
            if len(parts) >= 4:
                message_type = parts[2]
                category = parts[3]
            else:
                message_type = "unknown"
                category = parts[2]
            self._poller.answer_callback_query(cb_id)

            from store import log_feedback, open_db
            from telegram_bot import FEEDBACK_CATEGORIES, feedback_undo_keyboard

            conn = open_db(self._daemon.db)
            fb_id = log_feedback(conn, llm_call_id, category, message_type)

            label = FEEDBACK_CATEGORIES.get(category, category)
            if msg_id:
                chunk_text = msg.get("text", "")
                buttons = feedback_undo_keyboard(
                    fb_id,
                    llm_call_id,
                    message_type,
                    category,
                )
                self._poller.edit_message_with_keyboard(
                    msg_id,
                    f"{chunk_text}\n\n\U0001f44e {label}",
                    buttons,
                )

            # Send optional reason prompt.
            prompt_id = self._poller.send_reply(
                "Reply to explain more (optional).",
                reply_to_message_id=msg_id,
                force_reply=True,
            )
            if prompt_id is not None:
                with self._daemon._lock:
                    self._daemon._pending_feedback_reasons[prompt_id] = fb_id
                self._daemon._save_pending_reason_state()

        elif data.startswith("fb_undo:"):
            parts = data.split(":")
            if len(parts) < 5:
                self._poller.answer_callback_query(cb_id, "Invalid undo action.")
                return
            feedback_id = int(parts[1])
            llm_call_id = int(parts[2])
            message_type = parts[3]
            category = parts[4]

            from store import delete_feedback, open_db
            from telegram_bot import FEEDBACK_CATEGORIES, feedback_keyboard

            conn = open_db(self._daemon.db)
            deleted = delete_feedback(conn, feedback_id)
            self._daemon._drop_feedback_reason_prompts(feedback_id)
            if not deleted:
                self._poller.answer_callback_query(cb_id, "Feedback already removed.")
                return

            self._poller.answer_callback_query(cb_id, "Feedback removed.")
            if msg_id:
                label = FEEDBACK_CATEGORIES.get(category, category)
                restored = self._daemon._strip_feedback_label(
                    msg.get("text", ""), label
                )
                buttons = feedback_keyboard(llm_call_id, message_type)
                self._poller.edit_message_with_keyboard(msg_id, restored, buttons)

        elif data.startswith("tut:"):
            self._handle_tutorial_callback(cb_id, data, msg_id)

        elif data.startswith("add_"):
            self._daemon._add_flow.handle_callback(cb_id, data, msg_id)

    def _handle_agent_callback(self, cb_id: str, data: str, msg_id: int | None) -> None:
        """Handle Codex/Claude inline panel and exit buttons."""
        parts = data.split(":")
        if len(parts) != 3:
            self._poller.answer_callback_query(cb_id, "Invalid agent action.")
            return

        _, action, kind = parts
        if kind not in _AGENT_LABELS:
            self._poller.answer_callback_query(cb_id, "Unknown agent.")
            return

        label = _AGENT_LABELS[kind]
        if action == "on":
            self._enable_agent_mode(kind)
            self._poller.answer_callback_query(cb_id, f"{label} mode on.")
            if msg_id:
                self._edit_agent_panel(msg_id, kind)
            return

        if action == "off":
            if self._agent_mode_active() == kind:
                self._disable_agent_mode()
                self._poller.answer_callback_query(cb_id, f"{label} mode off.")
            else:
                self._poller.answer_callback_query(cb_id, f"{label} mode was not on.")
            if msg_id:
                self._edit_agent_panel(msg_id, kind)
            return

        if action == "new":
            self._clear_agent_session(kind)
            self._enable_agent_mode(kind)
            self._poller.answer_callback_query(cb_id, f"New {label} session.")
            if msg_id:
                self._edit_agent_panel(msg_id, kind)
            return

        if action == "exit":
            if self._agent_mode_active() == kind:
                self._disable_agent_mode()
                self._poller.answer_callback_query(cb_id, "Back to chat.")
            else:
                self._poller.answer_callback_query(cb_id, "Already back in chat.")
            if msg_id:
                self._poller.edit_message_reply_markup(msg_id, None)
            return

        self._poller.answer_callback_query(cb_id, "Unknown agent action.")

    # ------------------------------------------------------------------
    # Tutorial wizard
    # ------------------------------------------------------------------

    def _handle_tutorial_start(self, message_id: int | None) -> None:
        """Send the first step of the tutorial wizard.

        Args:
            message_id: ID of the user's ``/tutorial`` message to reply to.
        """
        from tutorial import render_step

        text, buttons = render_step(0)
        self._poller.send_message_with_keyboard(
            text, buttons, reply_to_message_id=message_id
        )

    def _handle_tutorial_callback(
        self, cb_id: str, data: str, msg_id: int | None
    ) -> None:
        """Handle a Next/Back/Exit/Done button press from the tutorial.

        The destination step lives entirely in ``data`` (``tut:<idx>``,
        ``tut:exit``, or ``tut:done``), so no per-user state is needed.

        Args:
            cb_id: Telegram callback_query id (for the loading spinner).
            data: Raw ``callback_data`` string starting with ``tut:``.
            msg_id: ID of the message holding the wizard (to edit in place).
        """
        from tutorial import render_step

        target = data.split(":", 1)[1] if ":" in data else ""

        if target == "exit":
            self._poller.answer_callback_query(cb_id, "Tutorial closed.")
            if msg_id:
                self._poller.edit_message(
                    msg_id,
                    "Tutorial closed. Type /tutorial to reopen, /advanced for commands.",
                )
            return

        if target == "done":
            self._poller.answer_callback_query(cb_id, "All set!")
            if msg_id:
                self._poller.edit_message(
                    msg_id,
                    "\u2705 Tutorial complete. Now ask the bot something — "
                    "or type /advanced to see less-used commands.",
                )
            return

        try:
            idx = int(target)
            text, buttons = render_step(idx)
        except (ValueError, IndexError):
            logger.warning("Invalid tutorial callback data: %r", data)
            self._poller.answer_callback_query(cb_id, "Tutorial unavailable.")
            return

        self._poller.answer_callback_query(cb_id)
        if msg_id:
            self._poller.edit_message_with_keyboard(msg_id, text, buttons)

    # ------------------------------------------------------------------
    # LLM chat reply loop
    # ------------------------------------------------------------------

    def _chat_reply(self, conn: sqlite3.Connection) -> tuple:
        """Build context, call the LLM with a tool-calling loop, and return.

        The LLM may call ``run_sql`` to query the database.  Each tool call
        is executed and the result fed back until the LLM produces a final
        text response or the iteration cap is reached.

        Args:
            conn: Open SQLite database connection.

        Returns:
            A tuple of (final LLMResult, deferred context edits, accumulated
            query rows for chart rendering).
        """
        from baselines import compute_baselines
        from config import MAX_TOOL_ITERATIONS
        from llm import call_llm
        from llm_context import build_messages, load_context
        from llm_health import (
            build_llm_data,
            format_recent_nudges,
            render_health_data,
        )
        from model_prefs import resolve_model_route
        from tools import all_chat_tools, execute_tool
        from llm_context import load_prompt_text

        ctx = load_context(self._daemon.context_dir, prompt_file="chat_prompt")

        # Inject recent nudge history so the LLM knows what it recently sent.
        recent = self._daemon._state.get("recent_nudges", [])
        ctx["recent_nudges"] = format_recent_nudges(recent, empty_text="(none yet)")

        # Inject last coach review for cross-message awareness.
        coach_summary = self._daemon._state.get("last_coach_summary", "")
        coach_date = self._daemon._state.get("last_coach_summary_date", "")
        if coach_summary:
            ctx["last_coach_summary"] = f"[{coach_date}] {coach_summary}"
        else:
            ctx["last_coach_summary"] = "(no recent coach review)"

        health_data = build_llm_data(conn, months=3)

        try:
            baselines = compute_baselines(conn)
        except Exception:
            logger.warning("Baselines computation failed", exc_info=True)
            baselines = None

        import json as _json

        messages = build_messages(
            ctx,
            health_data_text=render_health_data(health_data, prompt_kind="chat"),
            baselines=baselines,
        )

        # Inject conversation history before the last user message.
        # build_messages returns [system, user-prompt]. We insert the
        # conversation buffer between them so the LLM sees:
        #   system → context prompt → ...conversation turns...
        conv_msgs = self._conversation.to_messages()
        if conv_msgs:
            messages = messages[:2] + conv_msgs

        tools = all_chat_tools()
        query_rows: list[dict] = []
        deferred_edits: list = []
        route = (
            {"model": self._daemon.model}
            if self._daemon.model
            else resolve_model_route("chat").call_kwargs()
        )
        temperature = route.pop("temperature", 0.7)
        reasoning_effort = route.pop("reasoning_effort", None)

        result = None
        for _iteration in range(MAX_TOOL_ITERATIONS):
            result = call_llm(
                messages,
                **route,
                tools=tools,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                conn=conn,
                request_type="chat",
                max_tokens=MAX_TOKENS_CHAT,
                metadata={"iteration": _iteration},
            )

            if not result.tool_calls:
                return result, deferred_edits, query_rows

            # Append the assistant message with tool calls so the LLM sees
            # its own calls in the next iteration.
            messages.append(result.raw_message)

            logger.info(
                "Tool loop iteration %d: %d tool call(s)",
                _iteration,
                len(result.tool_calls),
            )

            for tc in result.tool_calls:
                fn_name = tc.function.name
                raw_args = tc.function.arguments
                try:
                    args = (
                        _json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    )
                except (ValueError, _json.JSONDecodeError):
                    args = {}

                if fn_name == "run_sql":
                    logger.info("Tool call: run_sql → %s", args.get("query", "")[:200])
                else:
                    logger.info("Tool call: %s", fn_name)

                if fn_name == "update_context":
                    from context_edit import context_edit_from_tool_call

                    edit = context_edit_from_tool_call(tc)
                    if edit:
                        deferred_edits.append(edit)
                    tool_result = "Proposed. User will be asked to confirm."
                else:
                    tool_result = execute_tool(fn_name, args, self._daemon.db)
                    # Keep latest query rows for chart rendering.
                    if fn_name == "run_sql":
                        try:
                            parsed = _json.loads(tool_result)
                            if isinstance(parsed, list):
                                query_rows.clear()
                                query_rows.extend(parsed)
                                logger.info("run_sql returned %d rows", len(parsed))
                            elif isinstance(parsed, dict) and "error" in parsed:
                                logger.warning("run_sql error: %s", parsed["error"])
                        except (ValueError, _json.JSONDecodeError):
                            pass

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result,
                    }
                )

        # If we exhausted iterations with an empty text (model still wanted
        # to call tools), force one final synthesis pass without tools so
        # the user never sees a blank reply.
        assert result is not None
        if not result.text.strip():
            logger.warning(
                "Chat loop exited with empty text (tool_calls=%s); forcing final synthesis",
                bool(result.tool_calls),
            )
            if result.tool_calls:
                messages.append(result.raw_message)
                for tc in result.tool_calls:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": _json.dumps(
                                {"error": load_prompt_text("tool_budget_chat")}
                            ),
                        }
                    )
            result = call_llm(
                messages,
                **route,
                tools=None,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                conn=conn,
                request_type="chat",
                max_tokens=MAX_TOKENS_CHAT,
                metadata={"iteration": "final_synthesis"},
            )

        return result, deferred_edits, query_rows
