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

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from config import MAX_TOKENS_CHAT

if TYPE_CHECKING:
    from daemon import ProfileRuntime
    from telegram_bot import TelegramSender

logger = logging.getLogger(__name__)

AGENT_MODE_TIMEOUT_MIN = 30
"""Minutes after which plain-message agent mode (codex/claude) turns off."""


def _agent_session_key(kind: str) -> str:
    """State key holding the saved session id for an agent kind."""
    return f"{kind}_session_id"


_AGENT_LABELS = {"codex": "Codex", "claude": "Claude"}

_CHAT_TOOL_MARKUP_RETRY = (
    "The previous output was internal tool-call markup. Answer the user in "
    "plain text using the tool results already provided. Do not call tools. "
    "Do not emit tool markup, XML, DSML, JSON, or function-call syntax."
)
_CHAT_TOOL_MARKUP_FALLBACK = (
    "I couldn't turn the tool results into a clean Telegram reply. Try again "
    "with a narrower question."
)
_CHAT_FINAL_SYNTHESIS = (
    "Tool use is finished. Answer the user's latest message now in plain "
    "Telegram text using the tool results below as facts. Do not call tools. "
    "Do not emit SQL, tool-call markup, DSML, JSON, or function-call syntax. "
    "If the result is a trend over time or the user asked for a chart/plot, "
    "include one <chart> block using the rows variable."
)
_CHAT_TOOL_RESULTS_MAX_CHARS = 12000
_CHART_INTENT_MARKERS = (
    "chart",
    "plot",
    "graph",
    "trend",
    "over time",
    "by week",
    "by month",
    "by day",
    "by date",
    "weekly",
    "monthly",
    "daily",
)


def _looks_like_internal_tool_markup(text: str) -> bool:
    """Return True when a final chat response leaks tool-call syntax."""
    stripped = text.strip()
    if not stripped:
        return False

    markers = (
        "<｜｜DSML｜｜",
        "<||DSML||",
        "｜｜tool_calls",
        "tool_calls>",
        "invoke name=",
        "<tool_call",
        "</tool_call",
    )
    if any(marker in stripped for marker in markers):
        return True

    if stripped.startswith(("{", "[")) and "tool_calls" in stripped:
        return True

    return False


def _tool_call_signature(tool_call: object) -> tuple[str, str]:
    """Return a stable signature for detecting repeated tool calls."""
    function = getattr(tool_call, "function", None)
    name = str(getattr(function, "name", ""))
    raw_args = getattr(function, "arguments", "")
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
        except (ValueError, json.JSONDecodeError):
            return name, raw_args.strip()
    else:
        args = raw_args
    try:
        normalized = json.dumps(args, sort_keys=True, separators=(",", ":"))
    except TypeError:
        normalized = str(args)
    return name, normalized


def _tool_result_signature(name: str, content: str) -> tuple[str, str]:
    """Return a stable signature for detecting repeated tool results."""
    try:
        parsed = json.loads(content)
        normalized = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, json.JSONDecodeError):
        normalized = content.strip()
    return name, normalized


def _format_tool_result_for_synthesis(
    name: str, args: dict[str, object], content: str
) -> str:
    """Render a tool result as plain prompt text for final synthesis."""
    query = args.get("query")
    if name == "run_sql" and isinstance(query, str):
        heading = f"{name} query:\n{query.strip()}"
    else:
        heading = f"{name} result"
    return f"{heading}\n\nResult:\n{content}"


def _plain_synthesis_messages(
    messages: list[dict],
    tool_results: list[str],
    instruction: str = _CHAT_FINAL_SYNTHESIS,
) -> list[dict[str, str]]:
    """Build a clean no-tools transcript for final user-facing synthesis."""
    clean: list[dict[str, str]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            continue
        if role == "assistant" and message.get("tool_calls"):
            continue
        if role not in {"system", "user", "assistant"}:
            continue
        content = message.get("content") or ""
        clean.append({"role": str(role), "content": str(content)})

    results_text = "\n\n---\n\n".join(tool_results) or "(no tool results)"
    if len(results_text) > _CHAT_TOOL_RESULTS_MAX_CHARS:
        results_text = results_text[-_CHAT_TOOL_RESULTS_MAX_CHARS:]
        results_text = "[truncated to latest tool output]\n" + results_text
    clean.append(
        {
            "role": "user",
            "content": f"{instruction}\n\nTool results:\n{results_text}",
        }
    )
    return clean


def _message_wants_chart(text: str) -> bool:
    """Return True when the user asked for a chartable trend/comparison."""
    lowered = text.lower()
    return any(marker in lowered for marker in _CHART_INTENT_MARKERS)


def _format_telegram_command(command: dict[str, str]) -> str:
    """Render a command entry with usage hints for commands with arguments."""
    name = command["command"]
    description = command["description"]
    if name == "review":
        return f"/review — {description} (always last week)"
    if name == "coach":
        return f"/coach [current|last] — {description} (default: last)"
    if name == "context":
        return f"/context [name] — {description}"
    if name == "add":
        return f"/add — {description} (workouts, sleep)"
    if name == "events":
        return (
            f"/events [N|usage N] [category] — {description} "
            "(events: 3 days, usage: 30 days)"
        )
    if name == "llm_log":
        return f"/llm_log [N|id ID|trace ID] — {description}"
    return f"/{name} — {description}"


def _safe_json_dict(raw: object) -> dict:
    """Parse a JSON object string, returning empty dict on malformed input."""
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clip_text(text: object, limit: int) -> str:
    """Trim text for compact Telegram diagnostic views."""
    clipped = " ".join(str(text or "").split())
    if len(clipped) <= limit:
        return clipped
    return clipped[: limit - 3].rstrip() + "..."


class TelegramChatHandler:
    """Message router, command dispatcher, and LLM chat loop for Telegram.

    Owns:
        ``_poller``          — profile-bound :class:`TelegramSender`.
        ``_conversation``    — :class:`ConversationBuffer` for multi-turn chat.
        ``_pending_edits``   — :class:`PendingEdits` for context-edit proposals.

    Borrows from the daemon (via ``self._daemon``):
        ``db``, ``model``, ``context_dir``, ``_state``, ``_lock``,
        ``_stop_event``, ``_pending_rejection_reasons``,
        ``_pending_feedback_reasons``, ``_self_originated_writes``,
        ``_add_flow``, ``_notify_flow``, and various helper methods.
    """

    def __init__(self, daemon: "ProfileRuntime") -> None:
        self._daemon = daemon
        # These are initialized when the process-wide daemon attaches a sender.
        self._poller = None  # type: ignore[assignment]
        self._conversation = None  # type: ignore[assignment]
        self._pending_edits = None  # type: ignore[assignment]
        self._status_lock = threading.Lock()
        self._handler_status: dict[str, object] = {
            "active_handlers": 0,
            "last_handler_start_at": None,
            "last_handler_kind": None,
            "last_handler_id": None,
            "last_handler_done_at": None,
            "last_handler_error_at": None,
            "last_handler_error": None,
        }

    def start(self, sender: "TelegramSender") -> None:
        """Attach this profile's sender and initialize chat-owned state."""
        from telegram_bot import ConversationBuffer

        self._poller = sender
        self._conversation = ConversationBuffer()

        from context_edit import PendingEdits

        self._pending_edits = PendingEdits()

        logger.info("Telegram sender attached for profile %s", self._daemon.name)

    def telegram_status(self) -> dict[str, object]:
        """Return a compact snapshot of Telegram poller and handler health."""
        process_poller = self._daemon.telegram_poller
        poller_status = (
            process_poller.status()
            if process_poller is not None and hasattr(process_poller, "status")
            else (self._poller.status() if self._poller is not None else {})
        )
        if not isinstance(poller_status, dict):
            poller_status = {}
        with self._status_lock:
            handler_status = dict(self._handler_status)
        return {
            "configured": self._poller is not None,
            "poller": poller_status,
            "handler": handler_status,
        }

    def _record_handler_start(self, kind: str, item_id: object) -> None:
        """Record one Telegram handler starting."""
        with self._status_lock:
            self._handler_status["active_handlers"] = (
                int(self._handler_status.get("active_handlers") or 0) + 1
            )
            self._handler_status["last_handler_start_at"] = datetime.now(
                tz=timezone.utc
            ).isoformat()
            self._handler_status["last_handler_kind"] = kind
            self._handler_status["last_handler_id"] = str(item_id)

    def _record_handler_done(self) -> None:
        """Record one Telegram handler finishing."""
        with self._status_lock:
            active = int(self._handler_status.get("active_handlers") or 0)
            self._handler_status["active_handlers"] = max(active - 1, 0)
            self._handler_status["last_handler_done_at"] = datetime.now(
                tz=timezone.utc
            ).isoformat()

    def _record_handler_error(self, exc: Exception) -> None:
        """Record one Telegram handler failure."""
        with self._status_lock:
            active = int(self._handler_status.get("active_handlers") or 0)
            self._handler_status["active_handlers"] = max(active - 1, 0)
            self._handler_status["last_handler_error_at"] = datetime.now(
                tz=timezone.utc
            ).isoformat()
            self._handler_status["last_handler_error"] = (
                f"{type(exc).__name__}: {_clip_text(exc, 120)}"
            )

    def _record_usage(self, kind: str, action: str | None) -> None:
        """Record a privacy-safe Telegram interaction metric."""
        if not action:
            return
        label = f"/{action}" if kind == "command" else action
        self._daemon._record_event(
            "telegram",
            kind,
            f"Telegram {kind}: {label}",
            {"action": action},
        )

    def _handle_telegram_message_monitored(self, message: dict) -> None:
        """Run message handling while recording lifecycle status."""
        text = (message.get("text") or "").strip()
        if not text:
            return
        self._record_handler_start("message", message.get("message_id"))
        try:
            self._handle_telegram_message(message)
        except Exception as exc:
            self._record_handler_error(exc)
            raise
        self._record_handler_done()

    def _handle_telegram_callback_monitored(self, callback_query: dict) -> None:
        """Run callback handling while recording lifecycle status."""
        self._record_handler_start("callback", callback_query.get("id"))
        try:
            self._handle_telegram_callback(callback_query)
        except Exception as exc:
            self._record_handler_error(exc)
            raise
        self._record_handler_done()

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
        from telegram_progress import handle_checkin_reply

        if reply_to and handle_checkin_reply(self, message):
            return

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

        # Keep this placeholder static. An animation thread editing the same
        # message can finish after the final-response edit and overwrite it.
        placeholder_id = self._poller.send_reply(
            "Working\u2026", reply_to_message_id=message_id
        )

        try:
            from store import open_db

            conn = open_db(self._daemon.db)
            try:
                result, deferred_edits, query_rows = self._chat_reply(conn)
            finally:
                conn.close()
        except Exception:
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

        reply = result.text

        # Extract and render any <chart> blocks from the response.
        from charts import (
            chart_figure_caption,
            extract_charts,
            render_chart,
            render_rows_chart,
            strip_charts,
        )

        chart_blocks = extract_charts(reply)
        if chart_blocks:
            extra_ns = {"rows": query_rows} if query_rows else None
            for index, block in enumerate(chart_blocks, start=1):
                try:
                    img = render_chart(block.code, {}, extra_namespace=extra_ns)
                    if img is None and query_rows:
                        logger.warning(
                            "Chart render failed for '%s'; using rows fallback",
                            block.title,
                        )
                        img = render_rows_chart(query_rows, title=block.title)
                    if img is None:
                        logger.warning(
                            "Chart render produced no image: %s", block.title
                        )
                        continue
                    self._poller.send_photo(
                        img, caption=chart_figure_caption(index, block.title)
                    )
                except Exception:
                    logger.warning(
                        "Chart render failed: %s", block.title, exc_info=True
                    )
            reply = strip_charts(reply)
        elif query_rows and _message_wants_chart(text):
            try:
                img = render_rows_chart(query_rows)
                if img is not None:
                    self._poller.send_photo(
                        img, caption=chart_figure_caption(1, "Trend")
                    )
                else:
                    logger.warning("Rows auto-chart produced no image")
            except Exception:
                logger.warning("Rows auto-chart failed", exc_info=True)

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
        from events import normalize_telegram_command

        command = normalize_telegram_command(text)
        self._record_usage("command", command)
        cmd = f"/{command}" if command else ""

        if cmd == "/clear":
            self._conversation.clear()
            self._poller.send_reply(
                "Conversation cleared.", reply_to_message_id=message_id
            )
        elif cmd == "/review":
            # The week argument is gone, but somebody who used to type
            # "/review current" should be told the mid-week report no longer
            # exists rather than silently handed last week's.
            if len(text.split()) > 1:
                self._poller.send_reply(
                    "/review takes no arguments — it always covers last week. "
                    "The mid-week report was removed; ask me in chat for "
                    "this week so far.",
                    reply_to_message_id=message_id,
                )
                return
            status_prefix = "Running review for last week "
            status_id = self._poller.send_reply(
                f"{status_prefix}.", reply_to_message_id=message_id
            )
            stop_anim, anim_thread = self._start_placeholder_animation(
                status_id, prefix=status_prefix
            )
            try:
                self._daemon._run_review(skip_import=False)
            finally:
                self._stop_placeholder_animation(stop_anim, anim_thread)
                if status_id is not None:
                    self._poller.edit_message(
                        status_id, "\u2713 Review for last week done."
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
        elif cmd == "/targets":
            from telegram_progress import handle_targets

            parts = text.split(maxsplit=1)
            handle_targets(self, parts[1].strip() if len(parts) > 1 else "", message_id)
        elif cmd == "/models":
            self._daemon._model_flow.handle_command(message_id)
        elif cmd == "/status":
            self._poller.send_reply(
                "\n".join(self._daemon._build_status_lines()),
                reply_to_message_id=message_id,
            )
        elif cmd == "/events":
            self._handle_events_command(text, message_id)
        elif cmd in {"/llm_log", "/llm-log"}:
            self._handle_llm_log_command(text, message_id)
        elif cmd == "/codex":
            if self._daemon.operator:
                self._handle_agent_command(text, message_id, kind="codex")
            else:
                self._poller.send_reply(
                    "Operator access required.", reply_to_message_id=message_id
                )
        elif cmd == "/claude":
            if self._daemon.operator:
                self._handle_agent_command(text, message_id, kind="claude")
            else:
                self._poller.send_reply(
                    "Operator access required.", reply_to_message_id=message_id
                )
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
            from config import PROMPTS_DIR

            ctx_names = sorted(
                f.stem
                for d in (self._daemon.context_dir, PROMPTS_DIR)
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
        """Handle ``/events [N | category | usage]`` diagnostics.

        With no argument, shows events from the last 3 days. A numeric
        argument overrides the day window; a category token (nudge,
        import, coach, …) filters to that category over the default 3-day
        window. ``/events usage [N]`` shows Telegram command and button
        counts, defaulting to 30 days.

        Args:
            text: The full ``/events …`` message text.
            message_id: Telegram message ID for reply threading.
        """
        from datetime import datetime, timedelta, timezone

        from cmd_events import format_events_for_telegram, format_usage_for_telegram
        from events import CATEGORIES, query_events, query_telegram_usage
        from store import open_db

        parts = text.split()[1:]
        usage = any(part.lower() == "usage" for part in parts)
        days = 30 if usage else 3
        category: str | None = None
        for part in parts:
            if part.isdigit():
                days = max(1, int(part))
            elif part.lower() == "usage":
                continue
            elif part.lower() in CATEGORIES:
                category = part.lower()
            else:
                self._poller.send_reply(
                    "Usage: /events [N] [category] or /events usage [N]. "
                    f"Categories: {', '.join(CATEGORIES)}.",
                    reply_to_message_id=message_id,
                )
                return

        if usage and category:
            self._poller.send_reply(
                "Usage: /events usage [N]. Category filters apply only to the event log.",
                reply_to_message_id=message_id,
            )
            return

        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        conn = open_db(self._daemon.db)
        try:
            if usage:
                rows = query_telegram_usage(conn, since=since)
            else:
                rows = query_events(conn, category=category, since=since, limit=200)
        finally:
            conn.close()

        if not rows:
            scope = f"last {days}d"
            if usage:
                scope += " · Telegram usage"
            elif category:
                scope += f" · {category}"
            self._poller.send_reply(
                f"No system events ({scope}).", reply_to_message_id=message_id
            )
            return

        header_scope = f"last {days}d"
        if usage:
            body = format_usage_for_telegram(rows)
        else:
            body = format_events_for_telegram(rows)
        if category:
            header_scope += f" · {category}"
        self._poller.send_reply(
            f"_{header_scope}_\n{body}", reply_to_message_id=message_id
        )

    def _handle_llm_log_command(self, text: str, message_id: int) -> None:
        """Handle ``/llm_log [N|id ID|trace ID]`` with compact trace output.

        Args:
            text: The full command text.
            message_id: Telegram message ID for reply threading.
        """
        parts = text.split()[1:]
        mode = "recent"
        value = 5
        if parts:
            first = parts[0].lower()
            if first.isdigit():
                value = min(20, max(1, int(first)))
            elif first in {"id", "trace"} and len(parts) == 2 and parts[1].isdigit():
                mode = "call" if first == "id" else "trace"
                value = int(parts[1])
            else:
                self._poller.send_reply(
                    "Usage: /llm_log [N|id ID|trace ID].",
                    reply_to_message_id=message_id,
                )
                return

        view = self._build_llm_log_view(mode, value)
        if view is None:
            self._poller.send_reply(
                f"No LLM calls found for /llm_log {mode} {value}.",
                reply_to_message_id=message_id,
            )
            return
        self._poller.send_message_with_keyboard(
            view[0],
            view[1],
            reply_to_message_id=message_id,
        )

    def _handle_llm_log_callback(
        self,
        cb_id: str,
        data: str,
        msg_id: int | None,
    ) -> None:
        """Handle llm-log inline navigation callbacks."""
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in {"recent", "trace", "call"}:
            self._poller.answer_callback_query(cb_id, "Invalid LLM log action.")
            return
        try:
            value = int(parts[2])
        except ValueError:
            self._poller.answer_callback_query(cb_id, "Invalid LLM log id.")
            return

        view = self._build_llm_log_view(parts[1], value)
        if view is None:
            self._poller.answer_callback_query(cb_id, "LLM log row not found.")
            return

        self._poller.answer_callback_query(cb_id)
        if msg_id is not None:
            self._poller.edit_message_with_keyboard(msg_id, view[0], view[1])

    def _build_llm_log_view(
        self,
        mode: str,
        value: int,
    ) -> tuple[str, list[list[dict[str, str]]]] | None:
        """Build an interactive Telegram view for LLM logs."""
        from store import open_db

        conn = open_db(self._daemon.db)
        try:
            if mode == "call":
                row = self._query_llm_call_row(conn, value)
                if row is None:
                    return None
                return self._format_llm_call_view(row)
            if mode == "trace":
                rows = self._query_llm_trace_rows(conn, value)
                if not rows:
                    return None
                return self._format_llm_trace_view(value, rows)
            rows = self._query_recent_llm_rows(conn, value)
            if not rows:
                return None
            return self._format_llm_recent_view(value, rows)
        finally:
            conn.close()

    @staticmethod
    def _query_recent_llm_rows(conn: sqlite3.Connection, limit: int) -> list:
        """Return recent LLM call rows."""
        return conn.execute(
            """
            SELECT id, timestamp, request_type, model, trace_id,
                   latency_s, cost, metadata_json
            FROM llm_call
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    @staticmethod
    def _query_llm_trace_rows(conn: sqlite3.Connection, trace_id: int) -> list:
        """Return compact rows for one LLM trace."""
        return conn.execute(
            """
            SELECT id, timestamp, request_type, model, trace_id,
                   latency_s, cost, metadata_json
            FROM llm_call
            WHERE trace_id = ?
            ORDER BY timestamp ASC, id ASC
            """,
            (trace_id,),
        ).fetchall()

    @staticmethod
    def _query_llm_call_row(conn: sqlite3.Connection, call_id: int):
        """Return one LLM call row for Telegram display."""
        return conn.execute(
            """
            SELECT id, timestamp, request_type, model, trace_id, latency_s, cost,
                   metadata_json, response_text
            FROM llm_call
            WHERE id = ?
            """,
            (call_id,),
        ).fetchone()

    def _format_llm_recent_view(
        self,
        limit: int,
        rows: list,
    ) -> tuple[str, list[list[dict[str, str]]]]:
        """Format recent LLM calls with trace buttons."""
        text = self._format_llm_log_rows(f"Recent LLM calls ({limit})", rows)
        buttons: list[list[dict[str, str]]] = []
        for row in rows[:8]:
            trace_id = row["trace_id"]
            if trace_id is not None:
                buttons.append(
                    [
                        {
                            "text": f"Trace {trace_id}",
                            "callback_data": f"llmlog:trace:{trace_id}",
                        },
                        {
                            "text": f"Call {row['id']}",
                            "callback_data": f"llmlog:call:{row['id']}",
                        },
                    ]
                )
            else:
                buttons.append(
                    [
                        {
                            "text": f"Call {row['id']}",
                            "callback_data": f"llmlog:call:{row['id']}",
                        }
                    ]
                )
        buttons.append([{"text": "Refresh", "callback_data": f"llmlog:recent:{limit}"}])
        return text, buttons

    def _format_llm_trace_view(
        self,
        trace_id: int,
        rows: list,
    ) -> tuple[str, list[list[dict[str, str]]]]:
        """Format one trace with call drill-down buttons."""
        text = self._format_llm_log_rows(f"LLM trace {trace_id}", rows)
        buttons: list[list[dict[str, str]]] = []
        row_buttons: list[dict[str, str]] = []
        for row in rows[:12]:
            row_buttons.append(
                {
                    "text": f"Call {row['id']}",
                    "callback_data": f"llmlog:call:{row['id']}",
                }
            )
            if len(row_buttons) == 2:
                buttons.append(row_buttons)
                row_buttons = []
        if row_buttons:
            buttons.append(row_buttons)
        buttons.append(
            [
                {"text": "Recent", "callback_data": "llmlog:recent:5"},
                {"text": "Refresh", "callback_data": f"llmlog:trace:{trace_id}"},
            ]
        )
        return text, buttons

    def _format_llm_call_view(
        self,
        row,
    ) -> tuple[str, list[list[dict[str, str]]]]:
        """Format one call with response preview and navigation buttons."""
        metadata = _safe_json_dict(row["metadata_json"])
        lines = [f"*LLM call {row['id']}*"]
        lines.append(self._format_llm_row_summary(row))
        if metadata:
            visible = {
                key: metadata[key]
                for key in ("iteration", "stage", "verdict", "issue_count")
                if key in metadata
            }
            if visible:
                lines.append(f"`metadata`: `{json.dumps(visible, sort_keys=True)}`")
        response = str(row["response_text"] or "").strip()
        if response:
            preview = response[:900]
            if len(response) > len(preview):
                preview += "\n\n[truncated]"
            lines.append(f"\n*Response preview*\n{preview}")
        buttons = []
        if row["trace_id"] is not None:
            buttons.append(
                [
                    {
                        "text": f"Trace {row['trace_id']}",
                        "callback_data": f"llmlog:trace:{row['trace_id']}",
                    }
                ]
            )
        buttons.append([{"text": "Recent", "callback_data": "llmlog:recent:5"}])
        lines.append("\nFull prompt: `uv run python main.py llm-log --id N`")
        return "\n".join(lines), buttons

    def _handle_failure_detail_callback(
        self,
        cb_id: str,
        data: str,
        msg_id: int | None,
    ) -> None:
        """Handle verifier failure detail buttons."""
        parts = data.split(":")
        if len(parts) != 2:
            self._poller.answer_callback_query(cb_id, "Invalid failure action.")
            return
        try:
            call_id = int(parts[1])
        except ValueError:
            self._poller.answer_callback_query(cb_id, "Invalid failure id.")
            return

        view = self._build_failure_detail_view(call_id)
        if view is None:
            self._poller.answer_callback_query(cb_id, "Failure details not found.")
            return

        self._poller.answer_callback_query(cb_id)
        if msg_id is not None:
            self._poller.edit_message_with_keyboard(msg_id, view[0], view[1])

    def _build_failure_detail_view(
        self,
        call_id: int,
    ) -> tuple[str, list[list[dict[str, str]]]] | None:
        """Build a compact verifier failure detail view."""
        row = None
        from store import open_db

        conn = open_db(self._daemon.db)
        try:
            row = self._query_llm_call_row(conn, call_id)
        finally:
            conn.close()
        if row is None:
            return None

        metadata = _safe_json_dict(row["metadata_json"])
        verification = next(
            (
                value
                for key, value in metadata.items()
                if key.endswith("_verification") and isinstance(value, dict)
            ),
            {},
        )
        if not verification:
            return None

        issues = verification.get("issues")
        first_issue = issues[0] if isinstance(issues, list) and issues else {}
        verifier_call_id = verification.get("verifier_call_id")
        lines = [f"*Verifier blocked call {call_id}*"]
        lines.append(
            "Verdict: "
            f"`{verification.get('verdict', 'unknown')}`"
            f" / confidence `{verification.get('confidence', 'unknown')}`"
        )
        if verifier_call_id:
            lines.append(f"Verifier call: `{verifier_call_id}`")
        if isinstance(first_issue, dict) and first_issue:
            severity = first_issue.get("severity", "issue")
            lines.append(f"\n*{str(severity).title()} issue*")
            lines.append(_clip_text(first_issue.get("problem"), 900))
            correction = first_issue.get("correction")
            if correction:
                lines.append(f"\n*Fix*\n{_clip_text(correction, 500)}")
            evidence = first_issue.get("evidence")
            if evidence:
                lines.append(f"\n*Evidence*\n{_clip_text(evidence, 500)}")

        buttons: list[list[dict[str, str]]] = []
        row_buttons = [
            {"text": f"Source {call_id}", "callback_data": f"llmlog:call:{call_id}"}
        ]
        if verifier_call_id:
            row_buttons.append(
                {
                    "text": f"Verifier {verifier_call_id}",
                    "callback_data": f"llmlog:call:{verifier_call_id}",
                }
            )
        buttons.append(row_buttons)
        if row["trace_id"] is not None:
            buttons.append(
                [
                    {
                        "text": f"Trace {row['trace_id']}",
                        "callback_data": f"llmlog:trace:{row['trace_id']}",
                    },
                    {"text": "Recent", "callback_data": "llmlog:recent:5"},
                ]
            )
        else:
            buttons.append([{"text": "Recent", "callback_data": "llmlog:recent:5"}])
        return "\n".join(lines), buttons

    def _format_llm_log_rows(self, title: str, rows: list) -> str:
        """Format LLM log rows for Telegram."""
        lines = [f"*{title}*"]
        for row in rows:
            lines.append(self._format_llm_row_summary(row))
        lines.append(
            "\nTap a button to drill in. Full prompt: "
            "`uv run python main.py llm-log --id N`."
        )
        return "\n".join(lines)

    @staticmethod
    def _format_llm_row_summary(row) -> str:
        """Format one compact LLM row."""
        metadata = _safe_json_dict(row["metadata_json"])
        stage = metadata.get("stage")
        iteration = metadata.get("iteration")
        stage_text = ""
        if stage is not None:
            stage_text = f" · {stage}"
        elif iteration is not None:
            stage_text = f" · iter {iteration}"
        trace_text = f" · trace {row['trace_id']}" if row["trace_id"] else ""
        model = str(row["model"]).split("/")[-1]
        cost = row["cost"]
        cost_text = f" · ${float(cost):.4f}" if cost is not None else ""
        return (
            f"`#{row['id']}`{trace_text} · {row['request_type']}{stage_text}\n"
            f"{str(row['timestamp'])[:16]} · `{model}` · "
            f"{float(row['latency_s']):.1f}s{cost_text}"
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
        from config import PROMPTS_DIR

        if file_arg:
            # Show full content of a specific file.
            stem = file_arg.removesuffix(".md")
            path = self._daemon.context_dir / f"{stem}.md"
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
        ctx_files = sorted(self._daemon.context_dir.glob("*.md"))
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

        from events import normalize_telegram_callback

        self._record_usage("callback", normalize_telegram_callback(data))

        if data.startswith("targets:"):
            from telegram_progress import handle_targets

            self._poller.answer_callback_query(cb_id, "Updating progress.")
            handle_targets(self, data.split(":", 1)[1], msg_id)
            return

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

        elif data.startswith("checkin:"):
            from telegram_progress import handle_checkin_callback

            handle_checkin_callback(self, cb_id, data, msg_id)

        elif data.startswith("notify_"):
            self._daemon._notify_flow.handle_callback(cb_id, data, msg_id)

        elif data.startswith("model_"):
            self._daemon._model_flow.handle_callback(cb_id, data, msg_id)

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

        elif data.startswith("llmlog:"):
            self._handle_llm_log_callback(cb_id, data, msg_id)

        elif data.startswith("faildetail:"):
            self._handle_failure_detail_callback(cb_id, data, msg_id)

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
        from baselines import compute_baselines, unestablished_metrics
        from config import MAX_TOOL_ITERATIONS, METRIC_TRUST_WINDOW_DAYS
        from data_maturity import build_data_maturity
        from llm import call_llm
        from llm_context import build_messages, load_context
        from llm_health import (
            build_llm_data,
            format_recent_nudges,
            render_health_data,
        )
        from model_prefs import resolve_model_route
        from store import create_llm_trace
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

        messages = build_messages(
            ctx,
            health_data_text=render_health_data(
                health_data,
                prompt_kind="chat",
                unestablished=unestablished_metrics(conn, METRIC_TRUST_WINDOW_DAYS),
            ),
            baselines=baselines,
            data_maturity=build_data_maturity(conn, ctx),
        )

        # Inject conversation history before the last user message.
        # build_messages returns [system, user-prompt]. We insert the
        # conversation buffer between them so the LLM sees:
        #   system → context prompt → ...conversation turns...
        conv_msgs = self._conversation.to_messages()
        if conv_msgs:
            messages = messages[:2] + conv_msgs

        trace_id = create_llm_trace(conn, "chat")

        tools = all_chat_tools()
        query_rows: list[dict] = []
        tool_results_for_synthesis: list[str] = []
        seen_tool_calls: set[tuple[str, str]] = set()
        seen_tool_results: set[tuple[str, str]] = set()
        stopped_for_repeated_tool = False
        deferred_edits: list = []
        route = (
            {"model": self._daemon.model}
            if self._daemon.model
            else resolve_model_route(
                "chat", path=self._daemon.model_prefs_path
            ).call_kwargs()
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
                trace_id=trace_id,
                max_tokens=MAX_TOKENS_CHAT,
                metadata={"iteration": _iteration},
            )

            if not result.tool_calls:
                if _looks_like_internal_tool_markup(result.text):
                    logger.warning(
                        "Chat returned internal tool markup without tool calls; "
                        "forcing clean synthesis"
                    )
                    break
                return result, deferred_edits, query_rows

            repeated = [
                signature
                for signature in (_tool_call_signature(tc) for tc in result.tool_calls)
                if signature in seen_tool_calls
            ]
            if repeated:
                logger.warning(
                    "Chat repeated tool call(s); forcing final synthesis: %s",
                    repeated,
                )
                stopped_for_repeated_tool = True
                break
            for tc in result.tool_calls:
                seen_tool_calls.add(_tool_call_signature(tc))

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
                        json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    )
                except (ValueError, json.JSONDecodeError):
                    args = {}
                if not isinstance(args, dict):
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
                        tool_result = (
                            "Not proposed: invalid context update. If this was a "
                            "log append, use exactly one '- YYYY-MM-DD ...' line "
                            "under 160 chars."
                        )
                else:
                    tool_result = execute_tool(fn_name, args, self._daemon.db)
                    # Keep latest query rows for chart rendering.
                    if fn_name == "run_sql":
                        try:
                            parsed = json.loads(tool_result)
                            if isinstance(parsed, list):
                                query_rows.clear()
                                query_rows.extend(parsed)
                                logger.info("run_sql returned %d rows", len(parsed))
                            elif isinstance(parsed, dict) and "error" in parsed:
                                logger.warning("run_sql error: %s", parsed["error"])
                        except (ValueError, json.JSONDecodeError):
                            pass
                    tool_results_for_synthesis.append(
                        _format_tool_result_for_synthesis(fn_name, args, tool_result)
                    )
                    if fn_name == "run_sql":
                        signature = _tool_result_signature(fn_name, tool_result)
                        if signature in seen_tool_results:
                            logger.warning(
                                "Chat repeated run_sql result; forcing final synthesis"
                            )
                            stopped_for_repeated_tool = True
                        seen_tool_results.add(signature)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result,
                    }
                )
            if stopped_for_repeated_tool:
                break

        # If we exhausted iterations with an empty text (model still wanted
        # to call tools), force one final synthesis pass without tools so
        # the user never sees a blank reply.
        assert result is not None
        if (
            stopped_for_repeated_tool
            or result.tool_calls
            or not result.text.strip()
            or _looks_like_internal_tool_markup(result.text)
        ):
            logger.warning(
                "Chat loop forcing final synthesis (tool_calls=%s repeated=%s)",
                bool(result.tool_calls),
                stopped_for_repeated_tool,
            )
            budget_prompt = load_prompt_text("tool_budget_chat").strip()
            result = call_llm(
                _plain_synthesis_messages(
                    messages,
                    tool_results_for_synthesis,
                    f"{budget_prompt}\n\n{_CHAT_FINAL_SYNTHESIS}",
                ),
                **route,
                tools=None,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                conn=conn,
                request_type="chat",
                trace_id=trace_id,
                max_tokens=MAX_TOKENS_CHAT,
                metadata={"iteration": "final_synthesis"},
            )

        if _looks_like_internal_tool_markup(result.text):
            logger.warning(
                "Chat final synthesis returned internal tool markup; retrying once"
            )
            result = call_llm(
                _plain_synthesis_messages(
                    messages,
                    tool_results_for_synthesis,
                    _CHAT_TOOL_MARKUP_RETRY,
                ),
                **route,
                tools=None,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                conn=conn,
                request_type="chat",
                trace_id=trace_id,
                max_tokens=MAX_TOKENS_CHAT,
                metadata={"iteration": "final_synthesis_tool_markup_retry"},
            )
            if _looks_like_internal_tool_markup(result.text):
                logger.error(
                    "Chat final synthesis retry still returned internal tool markup"
                )
                raw_response_text = result.text
                result.text = _CHAT_TOOL_MARKUP_FALLBACK
                if result.llm_call_id is not None:
                    from store import update_llm_call_response

                    update_llm_call_response(
                        conn,
                        result.llm_call_id,
                        result.text,
                        metadata_patch={
                            "postprocessed_response_text": True,
                            "postprocess_reason": "internal_tool_markup_fallback",
                            "raw_response_text": raw_response_text,
                        },
                    )

        return result, deferred_edits, query_rows
