"""Filesystem watcher daemon for zdrowskit.

Monitors iCloud health data files and context .md files, triggering
LLM-powered notifications when meaningful changes are detected.
Also runs a Telegram long-polling listener for interactive chat.

Public API:
    main  — parse args and run the daemon loop

Example:
    uv run python src/daemon.py
    uv run python src/daemon.py --foreground
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import logging.handlers
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from config import (
    CONTEXT_DEBOUNCE_S,
    EVENING_HOUR_END,
    EVENING_HOUR_START,
    HEALTH_DEBOUNCE_S,
    LOCK_FILE,
    LOG_FILE,
    SCHEDULED_CHECK_INTERVAL_S,
    STATE_FILE,
    TRAINING_DAYS,
)
from config import AUTOEXPORT_DATA_DIR as ICLOUD_HEALTH_DIR

# Re-exported so tests and external callers can keep importing these names
# from ``daemon`` after the /notify flow moved into ``daemon_notify_flow``.
from daemon_notify_flow import (  # noqa: F401
    PendingNotifyClarification,
    PendingNotifyProposal,
)

if TYPE_CHECKING:
    from cmd_llm import CommandResult
    from context_edit import ContextEdit
    from context_edit import PendingContextEdit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    """Load rate-limit state from the JSON state file.

    Returns:
        A dict with rate-limit keys, or an empty dict on first run / parse error.
    """
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read state file %s: %s", STATE_FILE, exc)
    return {}


def _save_state(state: dict) -> None:
    """Persist rate-limit state to the JSON state file.

    Args:
        state: The state dict to serialise.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def _make_health_handler(on_json_modified, on_xml_created):  # type: ignore[no-untyped-def]
    """Build a watchdog FileSystemEventHandler for the iCloud health data dir.

    Ignores .icloud placeholder files created during iCloud sync.
    Routes .json modified events and .xml created events to separate callbacks.

    Args:
        on_json_modified: Callable triggered when a .json file is modified.
        on_xml_created: Callable triggered when a new .xml file is created.

    Returns:
        A watchdog FileSystemEventHandler instance.
    """
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event) -> None:  # type: ignore[override]
            if event.is_directory:
                return
            path = Path(event.src_path)
            if path.suffix == ".icloud":
                return
            if path.suffix == ".json":
                on_json_modified()

        def on_created(self, event) -> None:  # type: ignore[override]
            if event.is_directory:
                return
            path = Path(event.src_path)
            if path.suffix == ".icloud":
                return
            if path.suffix == ".xml":
                on_xml_created()

    return _Handler()


def _make_context_handler(on_file_changed, self_originated):  # type: ignore[no-untyped-def]
    """Build a watchdog FileSystemEventHandler for the context .md files dir.

    Triggers on modifications to user-editable context files: me.md,
    log.md, and strategy.md. Ignores auto-managed files
    (baselines.md, history.md) and prompt templates.

    Args:
        on_file_changed: Callable(stem: str) called with the file stem
            (e.g. "log", "strategy", "me").
        self_originated: Mutable set of resolved paths the daemon has just
            written itself. When an event matches a path in this set, the
            entry is removed and the event is swallowed — no `*_updated`
            nudge fires for the daemon's own writes (accepted coach edits,
            auto-applied chat edits). Genuine user edits never appear in
            this set and still trigger nudges normally.

    Returns:
        A watchdog FileSystemEventHandler instance.
    """
    from watchdog.events import FileSystemEventHandler

    WATCHED_STEMS = {"me", "log", "strategy"}

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event) -> None:  # type: ignore[override]
            if event.is_directory:
                return
            path = Path(event.src_path)
            if path.suffix != ".md":
                return
            if path.stem not in WATCHED_STEMS:
                return
            # Swallow events that originated from the daemon's own
            # apply_edit calls. macOS FSEvents can fire multiple events per
            # save, so we discard once and rely on the existing
            # CONTEXT_DEBOUNCE_S window in _fire_context to absorb any
            # duplicate that arrives just after.
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in self_originated:
                self_originated.discard(resolved)
                return
            on_file_changed(path.stem)

    return _Handler()


# ---------------------------------------------------------------------------
# Failure capture
# ---------------------------------------------------------------------------


class _LastErrorCapture(logging.Handler):
    """Logging handler that remembers the most recent ERROR-level message.

    Used by the daemon to forward command-side error messages to Telegram.
    Subcommands like ``cmd_insights`` log the offending exception with
    ``logger.error(...)`` and then call ``sys.exit(1)``; by the time the
    daemon's ``except SystemExit`` runs, the exception object is gone but
    the log message is still useful for telling the user what broke.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.last_message: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.last_message = record.getMessage()
        except Exception:
            # Never let logging-side failures break command execution.
            pass


@contextmanager
def _capture_last_error() -> Iterator[_LastErrorCapture]:
    """Capture the last ERROR-level log message during the wrapped block.

    The handler is attached to the root logger so it sees errors emitted
    by any module the wrapped command touches (commands, llm, store, ...).
    It is removed unconditionally on exit, even if the block raises.
    """
    capture = _LastErrorCapture()
    root = logging.getLogger()
    root.addHandler(capture)
    try:
        yield capture
    finally:
        root.removeHandler(capture)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class ZdrowskitDaemon:
    """Watches health data and context files, fires LLM notifications.

    Attributes:
        model: litellm model string for LLM calls.
        db: Path to the SQLite database.
        context_dir: Path to the context .md files directory.
    """

    def __init__(self, model: str, db: Path, context_dir: Path) -> None:
        """Initialise the daemon.

        Args:
            model: litellm model string.
            db: Path to the SQLite database.
            context_dir: Path to the ContextFiles directory.
        """
        self.model = model
        self.db = db
        self.context_dir = context_dir

        self._state = _load_state()
        from config import NOTIFICATION_PREFS_PATH

        self._notification_prefs_path = NOTIFICATION_PREFS_PATH
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._health_timer: threading.Timer | None = None
        self._context_timers: dict[str, threading.Timer] = {}
        self._context_fire_times: dict[str, float] = {}
        self._pending_rejection_reasons = self._restore_pending_reason_map(
            self._state.get("pending_rejection_reasons"),
            value_type="str",
        )
        self._pending_feedback_reasons = self._restore_pending_reason_map(
            self._state.get("pending_feedback_reasons"),
            value_type="int",
        )
        from daemon_add_flow import AddFlowHandler
        from daemon_notify_flow import NotifyFlowHandler
        from daemon_runners import DaemonRunnerHandler
        from daemon_telegram_chat import TelegramChatHandler

        self._add_flow = AddFlowHandler(self)
        self._notify_flow = NotifyFlowHandler(self)
        self._chat = TelegramChatHandler(self)
        self._runners = DaemonRunnerHandler(self)
        # Paths the daemon is about to write itself (e.g. accepted coach
        # edits). The watchdog handler consults this set to suppress the
        # follow-up `*_updated` nudge that would otherwise fire from the
        # daemon's own apply_edit call. Genuine user edits to the same file
        # in a separate editor are not in the set and still trigger nudges.
        self._self_originated_writes: set[Path] = set()

    @property
    def _poller(self):  # type: ignore[no-untyped-def]
        """Telegram poller, owned by the chat handler."""
        return self._chat._poller

    @property
    def _conversation(self):  # type: ignore[no-untyped-def]
        """Conversation buffer, owned by the chat handler."""
        return self._chat._conversation

    @property
    def _pending_edits(self):  # type: ignore[no-untyped-def]
        """Pending context-edit store, owned by the chat handler."""
        return self._chat._pending_edits

    def _format_status_timestamp(self, value: str | None) -> str:
        """Return a compact local timestamp label for daemon status output."""
        if not value:
            return "never"
        try:
            ts = datetime.fromisoformat(value)
        except ValueError:
            return value
        return ts.astimezone().strftime("%Y-%m-%d %H:%M")

    def _build_status_lines(self) -> list[str]:
        """Build a Telegram-friendly external status summary."""
        from notification_prefs import (
            active_temporary_mutes,
            effective_notification_prefs,
        )
        from store import load_date_range, open_db

        now = datetime.now().astimezone()
        prefs = self._load_notification_prefs(now=now)
        effective = effective_notification_prefs(prefs)
        active_mutes = active_temporary_mutes(prefs, now=now)

        conversation = self._chat._conversation
        buffer_len = len(conversation) if conversation is not None else 0
        nudge_count = self._state.get("nudge_count_today", 0)
        quiet_queue = self._state.get("quiet_queue", [])
        queue_len = len(quiet_queue) if isinstance(quiet_queue, list) else 0

        lines = [
            "System status:",
            f"- Chat memory: {buffer_len} messages",
            f"- Nudges today: {nudge_count}/{effective['nudges']['max_per_day']}",
            f"- Last nudge: {self._format_status_timestamp(self._state.get('last_nudge_ts'))}",
            f"- Last report: {self._format_status_timestamp(self._state.get('last_report_ts'))}",
            f"- Last coach run: {self._format_status_timestamp(self._state.get('last_coach_date'))}",
            (
                "- Nudges: "
                f"{'on' if effective['nudges']['enabled'] else 'off'} "
                f"(not before {effective['nudges']['earliest_time']})"
            ),
            (
                "- Weekly report: "
                f"{'on' if effective['weekly_insights']['enabled'] else 'off'} "
                f"({effective['weekly_insights']['weekday'].title()} "
                f"{effective['weekly_insights']['time']})"
            ),
            (
                "- Midweek report: "
                f"{'on' if effective['midweek_report']['enabled'] else 'off'} "
                f"({effective['midweek_report']['weekday'].title()} "
                f"{effective['midweek_report']['time']})"
            ),
        ]

        if queue_len:
            lines.append(f"- Queued nudges: {queue_len}")

        if active_mutes:
            mute_summary = "; ".join(
                f"{entry['target']} until {self._format_status_timestamp(entry['expires_at'])}"
                for entry in active_mutes
            )
            lines.append(f"- Active mutes: {mute_summary}")
        else:
            lines.append("- Active mutes: none")

        try:
            conn = open_db(self.db)
            dr = load_date_range(conn)
            if dr is None:
                lines.append("- Data: database is empty")
            else:
                day_count = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
                workout_count = conn.execute("SELECT COUNT(*) FROM workout").fetchone()[
                    0
                ]
                lines.append(
                    f"- Data: {day_count} days, {workout_count} workouts ({dr[0]} to {dr[1]})"
                )
        except sqlite3.DatabaseError:
            logger.warning(
                "Failed to load DB status for Telegram /status",
                exc_info=True,
            )
            lines.append("- Data: unavailable")

        return lines

    @staticmethod
    def _restore_pending_reason_map(
        raw: object,
        *,
        value_type: str,
    ) -> dict[int, int] | dict[int, str]:
        """Restore a prompt-id mapping from JSON state."""
        if not isinstance(raw, dict):
            return {}

        restored: dict[int, int] | dict[int, str] = {}
        for key, value in raw.items():
            try:
                prompt_id = int(key)
            except (TypeError, ValueError):
                continue
            if value_type == "int":
                try:
                    restored[prompt_id] = int(value)
                except (TypeError, ValueError):
                    continue
            else:
                if isinstance(value, str):
                    restored[prompt_id] = value
        return restored

    def _save_pending_reason_state(self) -> None:
        """Persist pending reason prompts to the daemon state file."""
        self._state["pending_feedback_reasons"] = {
            str(prompt_id): feedback_id
            for prompt_id, feedback_id in self._pending_feedback_reasons.items()
        }
        self._state["pending_rejection_reasons"] = {
            str(prompt_id): feedback_id
            for prompt_id, feedback_id in self._pending_rejection_reasons.items()
        }
        _save_state(self._state)

    def _drop_feedback_reason_prompts(self, feedback_id: int) -> None:
        """Remove any pending reason prompts tied to a deleted feedback row."""
        stale = [
            prompt_id
            for prompt_id, pending_id in self._pending_feedback_reasons.items()
            if pending_id == feedback_id
        ]
        if not stale:
            return
        for prompt_id in stale:
            del self._pending_feedback_reasons[prompt_id]
        self._save_pending_reason_state()

    @staticmethod
    def _strip_feedback_label(text: str, label: str) -> str:
        """Remove a trailing thumbs-down label from message text."""
        suffix = f"\n\n👎 {label}"
        if text.endswith(suffix):
            return text[: -len(suffix)]
        return text

    def _load_notification_prefs(self, *, now: datetime | None = None) -> dict:
        """Load notification preferences from disk."""
        from notification_prefs import load_notification_prefs

        return load_notification_prefs(self._notification_prefs_path, now=now)

    def _save_notification_prefs(self, prefs: dict) -> None:
        """Persist notification preferences to disk."""
        from notification_prefs import save_notification_prefs

        save_notification_prefs(prefs, path=self._notification_prefs_path)

    def _queue_nudge_trigger(
        self, trigger: str, *, now: datetime | None = None
    ) -> None:
        """Append a nudge trigger to the deferred queue."""
        now = now or datetime.now().astimezone()
        queue: list[dict] = self._state.get("quiet_queue", [])
        queue.append({"trigger": trigger, "ts": now.isoformat()})
        self._state["quiet_queue"] = queue[-10:]
        _save_state(self._state)

    def _drop_queued_nudges(self) -> None:
        """Drop any queued nudges without sending them."""
        if self._state.get("quiet_queue"):
            self._state["quiet_queue"] = []
            _save_state(self._state)

    # ------------------------------------------------------------------
    # Scheduling / debounce
    # ------------------------------------------------------------------

    def _schedule_health(self) -> None:
        """Schedule a health trigger, debouncing rapid file events."""
        with self._lock:
            if self._health_timer:
                self._health_timer.cancel()
            self._health_timer = threading.Timer(HEALTH_DEBOUNCE_S, self._fire_health)
            self._health_timer.daemon = True
            self._health_timer.start()
        logger.debug("Health trigger scheduled in %ds", HEALTH_DEBOUNCE_S)

    def _schedule_context(self, stem: str) -> None:
        """Schedule a context file trigger with per-stem debounce.

        Args:
            stem: File stem that changed (e.g. "log", "strategy").
        """
        with self._lock:
            if stem in self._context_timers:
                self._context_timers[stem].cancel()
            timer = threading.Timer(
                CONTEXT_DEBOUNCE_S, self._fire_context, args=(stem,)
            )
            timer.daemon = True
            timer.start()
            self._context_timers[stem] = timer
        logger.debug(
            "Context trigger for %s.md scheduled in %ds", stem, CONTEXT_DEBOUNCE_S
        )

    # ------------------------------------------------------------------
    # Trigger actions
    # ------------------------------------------------------------------

    def _fire_health(self) -> None:
        """Handle a health data trigger: import data, then nudge."""
        logger.info("Health trigger fired")
        before = self._runners._data_snapshot()
        self._runners._run_import()
        after = self._runners._data_snapshot()
        trigger_context = self._runners._format_data_delta(before, after)
        self._state["last_data_snapshot"] = after
        _save_state(self._state)
        self._runners._run_nudge("new_data", trigger_context=trigger_context)

    def _fire_context(self, stem: str) -> None:
        """Handle a context file change trigger.

        Guards against duplicate FSEvents that can fire for a single file save
        on macOS (content write + metadata/xattr update).

        Args:
            stem: File stem that changed.
        """
        now = time.monotonic()
        with self._lock:
            last = self._context_fire_times.get(stem, 0.0)
            if now - last < CONTEXT_DEBOUNCE_S:
                logger.debug(
                    "Context trigger for %s.md suppressed (%.0fs since last fire)",
                    stem,
                    now - last,
                )
                return
            self._context_fire_times[stem] = now

        trigger_map = {
            "me": "profile_updated",
            "log": "log_update",
            "strategy": "strategy_updated",
        }
        trigger = trigger_map.get(stem, "log_update")
        logger.info("Context trigger fired: %s.md → %s", stem, trigger)
        self._record_event(
            "context",
            "edited",
            f"Context file edited: {stem}.md → {trigger}",
            {"stem": stem, "trigger": trigger},
        )
        trigger_context = self._runners._format_context_trigger(stem, trigger)
        self._runners._run_nudge(trigger, trigger_context=trigger_context)

    # ------------------------------------------------------------------
    # Runner delegation — thin wrappers for test patching and callers
    # ------------------------------------------------------------------

    def _run_review(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        """Delegate to the runner handler."""
        self._runners._run_review(**kwargs)

    def _run_coach(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        """Delegate to the runner handler."""
        self._runners._run_coach(**kwargs)

    def _run_import(self) -> None:
        """Delegate to the runner handler."""
        self._runners._run_import()

    def _run_weekly_report(self) -> None:
        """Delegate to the runner handler."""
        self._runners._run_weekly_report()

    def _run_nudge(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        """Delegate to the runner handler."""
        self._runners._run_nudge(*args, **kwargs)

    def _can_send_nudge(self) -> bool:
        """Delegate to the runner handler."""
        return self._runners._can_send_nudge()

    def _check_nudge_rate_limit(self) -> tuple[bool, str | None, dict | None]:
        """Delegate: returns (allowed, reason, details)."""
        return self._runners._check_nudge_rate_limit()

    def _record_event(
        self,
        category: str,
        kind: str,
        summary: str,
        details: dict | None = None,
        llm_call_id: int | None = None,
    ) -> None:
        """Record a diagnostic event into the events table.

        Opens a short-lived DB connection so event writes don't contend with
        long-running LLM or import work.
        """
        from events import record_event
        from store import open_db

        try:
            conn = open_db(self.db)
            try:
                record_event(conn, category, kind, summary, details, llm_call_id)
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            logger.warning("Event write failed (%s.%s)", category, kind, exc_info=True)

    def _record_report(self, report_type: str) -> None:
        """Delegate to the runner handler."""
        self._runners._record_report(report_type)

    def _notify_user_failure(self, operation: str, error_text: str | None) -> None:
        """Send a brief failure notice to Telegram so the user knows.

        Background runs (scheduled reports, manual /review, nudges, coach)
        used to fail silently — only the daemon log recorded the error. This
        forwards the most recent ERROR-level log message to Telegram so the
        user sees what broke without having to read daemon logs.

        Args:
            operation: Short human-readable label, e.g. "Weekly review".
            error_text: The captured error message, or None if nothing was
                captured (rare — falls back to a generic notice).
        """
        poller = self._chat._poller
        if poller is None:
            # Telegram not configured — nothing to notify.
            return

        if error_text:
            truncated = (
                error_text if len(error_text) <= 600 else error_text[:597] + "..."
            )
            text = f"**{operation} failed**\n\n{truncated}"
        else:
            text = f"**{operation} failed** — check daemon logs"

        try:
            poller.send_message_with_keyboard(text, [])
        except Exception:
            logger.warning("Failed to send failure notice to Telegram", exc_info=True)

    def _attach_feedback_button(
        self,
        result: "CommandResult",
        message_type: str,
    ) -> None:
        """Edit a sent Telegram message to append a feedback keyboard.

        Args:
            result: The CommandResult from a cmd_* function.
            message_type: The LLM output type (insights, nudge, coach, chat).
        """
        from telegram_bot import feedback_keyboard

        msg_id = result.telegram_message_id
        call_id = result.llm_call_id
        if msg_id is None or call_id is None:
            return

        kb = feedback_keyboard(call_id, message_type)
        self._poller.edit_message_reply_markup(msg_id, kb)

    def _handle_telegram_message(self, message: dict) -> None:
        """Delegate to the chat handler."""
        self._chat._handle_telegram_message(message)

    def _handle_telegram_callback(self, callback_query: dict) -> None:
        """Delegate to the chat handler."""
        self._chat._handle_telegram_callback(callback_query)

    def _handle_command(self, text: str, message_id: int) -> None:
        """Delegate to the chat handler."""
        self._chat._handle_command(text, message_id)

    def _record_context_feedback(
        self,
        pending: "PendingContextEdit",
        decision: str,
        *,
        reason: str | None = None,
    ) -> str:
        """Persist an accept/reject decision and return its feedback ID."""
        from context_edit import append_coach_feedback, new_feedback_entry

        entry = new_feedback_entry(pending, decision, reason=reason)
        append_coach_feedback(self.context_dir, entry)
        return entry.feedback_id

    def _consume_rejection_reason(self, reply_to: dict, text: str) -> bool:
        """Handle an optional rejection-reason reply if it matches a pending prompt."""
        from context_edit import update_coach_feedback_reason

        prompt_id = reply_to.get("message_id")
        if prompt_id is None:
            return False

        with self._lock:
            feedback_id = self._pending_rejection_reasons.pop(prompt_id, None)
        if feedback_id is None:
            return False
        self._save_pending_reason_state()

        updated = update_coach_feedback_reason(self.context_dir, feedback_id, text)
        if not updated:
            logger.warning(
                "No matching coach feedback entry for reason %s", feedback_id
            )
        return True

    def _consume_feedback_reason(self, reply_to: dict, text: str) -> bool:
        """Handle an optional feedback-reason reply if it matches a pending prompt."""
        from store import open_db, update_feedback_reason

        prompt_id = reply_to.get("message_id")
        if prompt_id is None:
            return False

        with self._lock:
            feedback_id = self._pending_feedback_reasons.pop(prompt_id, None)
        if feedback_id is None:
            return False
        self._save_pending_reason_state()

        conn = open_db(self.db)
        update_feedback_reason(conn, feedback_id, text)
        return True

    def _propose_context_edit(self, edit: "ContextEdit", *, source: str) -> None:
        """Send a context edit proposal or auto-apply it.

        Args:
            edit: The validated context edit extracted from the LLM response.
            source: Origin of the proposal, e.g. ``"coach"`` or ``"chat"``.
        """
        from config import AUTO_ACCEPT_CONTEXT_EDITS
        from context_edit import (
            EditPreviewError,
            PendingContextEdit,
            apply_edit,
            build_content_preview,
            build_edit_preview,
        )

        try:
            preview = build_edit_preview(self.context_dir, edit, strict=True)
        except EditPreviewError as exc:
            # Drop silently. Surfacing "Skipped invalid suggestion…" was
            # uglier than the missing edit, and the user still sees the
            # main chat reply that was sent in the same turn. The warning
            # in llm-log is the place to look when prompts drift.
            logger.warning(
                "Dropping invalid %s proposal for %s.md (section=%r): %s",
                source,
                edit.file,
                edit.section,
                exc,
            )
            return

        if AUTO_ACCEPT_CONTEXT_EDITS:
            try:
                self._self_originated_writes.add(
                    (self.context_dir / f"{edit.file}.md").resolve()
                )
                apply_edit(self.context_dir, edit, strict=True)
                pending = PendingContextEdit(edit=edit, source=source, preview=preview)
                self._record_context_feedback(pending, "accepted")
                self._poller.send_reply(
                    f"\u2705 Updated {edit.file}.md\n{edit.summary}\n\n```diff\n{preview}\n```"
                )
            except Exception:
                logger.error("Failed to auto-apply context edit", exc_info=True)
            return

        edit_id = self._pending_edits.store(edit, source=source, preview=preview)
        content_preview = build_content_preview(edit)
        text = (
            f"\U0001f4cb Suggestion — {edit.file}.md\n"
            f"{edit.summary}\n\n"
            f"Proposed content:\n"
            f"```\n{content_preview}\n```"
        )
        buttons = [
            [
                {"text": "\u2705 Accept", "callback_data": f"ctx_accept:{edit_id}"},
                {"text": "\u274c Reject", "callback_data": f"ctx_reject:{edit_id}"},
                {"text": "\U0001f50d Diff", "callback_data": f"ctx_diff:{edit_id}"},
            ]
        ]
        self._poller.send_message_with_keyboard(text, buttons)

    # ------------------------------------------------------------------
    # Scheduled checks
    # ------------------------------------------------------------------

    def _scheduled_check_loop(self) -> None:
        """Background thread: periodic checks for morning reports and evening missed sessions."""
        from notification_prefs import evaluate_nudge_delivery, scheduled_report_due

        while True:
            time.sleep(SCHEDULED_CHECK_INTERVAL_S)
            now = datetime.now().astimezone()
            prefs = self._load_notification_prefs(now=now)

            # Drain or drop queued nudges once the nudge gate changes.
            if self._state.get("quiet_queue"):
                nudge_decision = evaluate_nudge_delivery(prefs, now=now)
                if nudge_decision["status"] == "allowed":
                    self._runners._drain_quiet_queue()
                elif nudge_decision["status"] == "suppressed":
                    logger.info(
                        "Dropping queued nudges due to notification prefs: %s",
                        nudge_decision.get("reason", "unknown"),
                    )
                    self._drop_queued_nudges()

            if scheduled_report_due(prefs, "weekly_insights", now=now):
                self._runners._run_weekly_report()

            if scheduled_report_due(prefs, "midweek_report", now=now):
                self._runners._run_midweek_report()

            # Evening: check for missed sessions on training days
            if EVENING_HOUR_START <= now.hour < EVENING_HOUR_END:
                if now.weekday() not in TRAINING_DAYS:
                    continue
                today_str = date.today().isoformat()
                if self._state.get("last_missed_session_date") == today_str:
                    continue
                try:
                    conn = sqlite3.connect(str(self.db))
                    row = conn.execute(
                        "SELECT COUNT(*) FROM workout WHERE date = ?", (today_str,)
                    ).fetchone()
                    conn.close()
                    has_workout = row[0] > 0 if row else False
                    if not has_workout:
                        logger.info(
                            "Evening check: no workout on training day %s", today_str
                        )
                        self._state["last_missed_session_date"] = today_str
                        _save_state(self._state)
                        self._runners._run_nudge(
                            "missed_session",
                            trigger_context=(
                                f"No workout has been logged today ({today_str}, "
                                f"{date.today().strftime('%A')}), and the evening "
                                "check is firing because today is a scheduled "
                                "training day."
                            ),
                        )
                except Exception as exc:
                    logger.error("Evening check DB query failed: %s", exc)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the observer and scheduled check thread, then block until interrupted."""
        from watchdog.observers import Observer

        # Acquire an exclusive file lock to prevent concurrent daemon instances.
        # The lock is held for the lifetime of the process (released on exit).
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = LOCK_FILE.open("w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.error(
                "Another daemon instance is already running (lock: %s). Exiting.",
                LOCK_FILE,
            )
            sys.exit(1)
        self._lock_file.write(str(os.getpid()))
        self._lock_file.flush()

        if not ICLOUD_HEALTH_DIR.exists():
            logger.warning(
                "iCloud health dir not found: %s — health triggers disabled",
                ICLOUD_HEALTH_DIR,
            )

        logger.info("zdrowskit daemon starting")
        logger.info("Health data dir : %s", ICLOUD_HEALTH_DIR)
        logger.info("Context dir     : %s", self.context_dir)
        logger.info("Database        : %s", self.db)
        logger.info("State file      : %s", STATE_FILE)

        observer = Observer()

        if ICLOUD_HEALTH_DIR.exists():
            observer.schedule(
                _make_health_handler(self._schedule_health, self._schedule_health),
                str(ICLOUD_HEALTH_DIR),
                recursive=True,
            )

        if self.context_dir.exists():
            observer.schedule(
                _make_context_handler(
                    self._schedule_context, self._self_originated_writes
                ),
                str(self.context_dir),
                recursive=False,
            )
        else:
            logger.warning(
                "Context dir not found: %s — context triggers disabled",
                self.context_dir,
            )

        observer.start()

        scheduled_thread = threading.Thread(
            target=self._scheduled_check_loop, daemon=True, name="scheduled-checks"
        )
        scheduled_thread.start()

        self._chat.start()

        logger.info("Daemon running — press Ctrl+C to stop")
        self._record_event("daemon", "start", "Daemon started")
        try:
            while observer.is_alive():
                observer.join(timeout=1)
        except KeyboardInterrupt:
            logger.info("Shutting down daemon")
        finally:
            self._stop_event.set()
            observer.stop()
            observer.join()
            logger.info("Daemon stopped")
            self._record_event("daemon", "stop", "Daemon stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _setup_logging(foreground: bool) -> None:
    """Configure logging for the daemon.

    Rotating file log always active. Console output added when --foreground.

    Args:
        foreground: If True, also log to stderr with colours.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE, when="midnight", backupCount=7, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    if foreground:
        # Reuse the project's coloured stderr handler
        from log import setup_logging as _setup_colour

        _setup_colour()


def main() -> None:
    """Entry point: parse args and start the daemon."""
    import argparse

    from dotenv import load_dotenv

    # Add src/ to path so project modules resolve when run directly
    sys.path.insert(0, str(Path(__file__).parent))
    load_dotenv()

    from config import CONTEXT_DIR
    from store import default_db_path

    parser = argparse.ArgumentParser(
        description="zdrowskit daemon — filesystem watcher and notification dispatcher"
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Log to stderr in addition to the log file (useful for debugging)",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=str(default_db_path()),
        help="Path to SQLite database",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        default="anthropic/claude-opus-4-6",
        help="litellm model string for LLM calls (default: claude-opus-4-6)",
    )
    args = parser.parse_args()

    _setup_logging(args.foreground)

    daemon = ZdrowskitDaemon(
        model=args.model,
        db=Path(args.db),
        context_dir=CONTEXT_DIR,
    )
    daemon.run()


if __name__ == "__main__":
    main()
