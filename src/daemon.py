"""Health import and notification daemon for zdrowskit.

Polls Google Drive or monitors local health data files, watches context .md
files, and triggers LLM-powered notifications when meaningful changes arrive.
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
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from config import (
    CONTEXT_DEBOUNCE_S,
    GOOGLE_DRIVE_POLL_INTERVAL_S,
    GOOGLE_DRIVE_SERVICE_ACCOUNT,
    HEALTH_DEBOUNCE_S,
    LOCK_FILE,
    LOG_FILE,
    SCHEDULED_CHECK_INTERVAL_S,
    resolve_data_dir,
    resolve_google_drive_data_dir,
)

# Re-exported so tests and external callers can keep importing these names
# from ``daemon`` after the /notify flow moved into ``daemon_notify_flow``.
from daemon_notify_flow import (  # noqa: F401
    PendingNotifyClarification,
    PendingNotifyProposal,
)
from llm_verify import (
    VerificationSuppression,
    register_suppression_listener,
    unregister_suppression_listener,
)

if TYPE_CHECKING:
    from cmd_llm_common import CommandResult
    from commands import ImportResult
    from context_edit import ContextEdit
    from context_edit import PendingContextEdit
    from profiles import Profile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def _load_state(path: Path) -> dict:
    """Load rate-limit state from the JSON state file.

    Returns:
        A dict with rate-limit keys, or an empty dict on first run / parse error.
    """
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read state file %s: %s", path, exc)
    return {}


def _save_state(state: dict, path: Path) -> None:
    """Persist rate-limit state to the JSON state file.

    Args:
        state: The state dict to serialise.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


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

    Also captures the most recent :class:`VerificationSuppression` emitted
    by the verifier during the wrapped block, so the daemon can attach
    rich detail buttons to the failure notice without re-querying the
    events table (which would race across categories).
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.last_message: str | None = None
        self.last_suppression: VerificationSuppression | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.last_message = record.getMessage()
        except Exception:
            # Never let logging-side failures break command execution.
            pass

    def _record_suppression(self, snapshot: VerificationSuppression) -> None:
        self.last_suppression = snapshot


@contextmanager
def _capture_last_error() -> Iterator[_LastErrorCapture]:
    """Capture errors and verifier suppressions during the wrapped block.

    The handler is attached to the root logger so it sees errors emitted
    by any module the wrapped command touches (commands, llm, store, ...).
    A verifier suppression listener is registered alongside so the daemon
    can show the user *why* a report was blocked without a time-window
    DB query. Both are released unconditionally on exit.
    """
    capture = _LastErrorCapture()
    root = logging.getLogger()
    root.addHandler(capture)
    register_suppression_listener(capture._record_suppression)
    try:
        yield capture
    finally:
        unregister_suppression_listener(capture._record_suppression)
        root.removeHandler(capture)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class ProfileRuntime:
    """Watches health data and context files, fires LLM notifications.

    Attributes:
        model: litellm model string for LLM calls.
        db: Path to the SQLite database.
        context_dir: Path to the context .md files directory.
    """

    def __init__(
        self,
        model: str | None,
        db: Path,
        context_dir: Path,
        health_dir: Path | None = None,
        import_source: str = "local",
        google_drive_service_account: Path | None = None,
        google_drive_metrics_folder_id: str | None = None,
        google_drive_workouts_folder_id: str | None = None,
        google_drive_poll_interval_s: int = 5 * 60,
        *,
        profile: "Profile | None" = None,
        sender: object | None = None,
        telegram_poller: object | None = None,
        state_path: Path | None = None,
    ) -> None:
        """Initialise the daemon.

        Args:
            model: Optional litellm model override for LLM commands.
            db: Path to the SQLite database.
            context_dir: Path to the ContextFiles directory.
            health_dir: Path to the Auto Export data directory.
            import_source: ``local`` filesystem events or ``google-drive`` polling.
            google_drive_service_account: Service-account JSON path for Drive.
            google_drive_metrics_folder_id: Auto Export Metrics folder ID.
            google_drive_workouts_folder_id: Auto Export Workouts folder ID.
            google_drive_poll_interval_s: Seconds between Drive API polls.

        Raises:
            ValueError: If the import source or Drive configuration is invalid.
        """
        if import_source not in {"local", "google-drive"}:
            raise ValueError(
                f"Unknown import source {import_source!r}; use local or google-drive."
            )
        if google_drive_poll_interval_s <= 0:
            raise ValueError("Google Drive poll interval must be greater than zero.")
        if import_source == "google-drive":
            missing = [
                name
                for name, value in (
                    (
                        "ZDROWSKIT_GOOGLE_DRIVE_SERVICE_ACCOUNT",
                        google_drive_service_account,
                    ),
                    (
                        "ZDROWSKIT_GOOGLE_DRIVE_METRICS_FOLDER_ID",
                        google_drive_metrics_folder_id,
                    ),
                    (
                        "ZDROWSKIT_GOOGLE_DRIVE_WORKOUTS_FOLDER_ID",
                        google_drive_workouts_folder_id,
                    ),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "Google Drive daemon is missing configuration: "
                    + ", ".join(missing)
                )

        self.profile = profile
        self.name = profile.name if profile is not None else "default"
        self.operator = profile.operator if profile is not None else True
        self.model = model
        self.db = db
        self.context_dir = context_dir
        self.health_dir = health_dir or (
            resolve_google_drive_data_dir(None)
            if import_source == "google-drive"
            else resolve_data_dir(None)
        )
        self.import_source = import_source
        self.google_drive_service_account = google_drive_service_account
        self.google_drive_metrics_folder_id = google_drive_metrics_folder_id
        self.google_drive_workouts_folder_id = google_drive_workouts_folder_id
        self.google_drive_poll_interval_s = google_drive_poll_interval_s

        from config import (
            MODEL_PREFS_PATH,
            NOTIFICATION_PREFS_PATH,
            NUDGES_DIR,
            REPORTS_DIR,
        )

        self.state_path = state_path or (
            profile.state
            if profile is not None
            else self.db.parent / "daemon_state.json"
        )
        self._state = _load_state(self.state_path)
        self._notification_prefs_path = (
            profile.notification_prefs
            if profile is not None
            else NOTIFICATION_PREFS_PATH
        )
        self.model_prefs_path = (
            profile.model_prefs if profile is not None else MODEL_PREFS_PATH
        )
        self.reports_dir = profile.reports if profile is not None else REPORTS_DIR
        self.nudges_dir = profile.nudges if profile is not None else NUDGES_DIR
        self.telegram_poller = telegram_poller
        self._lock = threading.Lock()
        self._import_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._health_timer: threading.Timer | None = None
        self._health_debounce_count = 0
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
        from daemon_drive import GoogleDrivePollHandler
        from daemon_model_flow import ModelFlowHandler
        from daemon_notify_flow import NotifyFlowHandler
        from daemon_runners import DaemonRunnerHandler
        from daemon_telegram_chat import TelegramChatHandler

        self._add_flow = AddFlowHandler(self)
        self._notify_flow = NotifyFlowHandler(self)
        self._model_flow = ModelFlowHandler(self)
        self._chat = TelegramChatHandler(self)
        self._runners = DaemonRunnerHandler(self)
        self._drive = GoogleDrivePollHandler(self)
        # Paths the daemon is about to write itself (e.g. accepted coach
        # edits). The watchdog handler consults this set to suppress the
        # follow-up `*_updated` nudge that would otherwise fire from the
        # daemon's own apply_edit call. Genuine user edits to the same file
        # in a separate editor are not in the set and still trigger nudges.
        self._self_originated_writes: set[Path] = set()
        self._dispatch_lock = threading.RLock()
        if sender is not None:
            self._chat.start(sender)

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
            (
                "- Health import: Google Drive "
                f"(every {self.google_drive_poll_interval_s}s)"
                if self.import_source == "google-drive"
                else "- Health import: local filesystem watcher"
            ),
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

        telegram = self._chat.telegram_status()
        if telegram.get("configured"):
            poller = telegram.get("poller")
            handler = telegram.get("handler")
            poller = poller if isinstance(poller, dict) else {}
            handler = handler if isinstance(handler, dict) else {}

            last_poll_count = poller.get("last_poll_update_count")
            count_label = (
                str(last_poll_count) if last_poll_count is not None else "unknown"
            )
            lines.append(
                "- Telegram: on; "
                f"last poll: {self._format_status_timestamp(poller.get('last_poll_at'))} "
                f"({count_label} updates)"
            )
            lines.append(
                "- Telegram last message: "
                f"{poller.get('last_message_id') or 'none'} at "
                f"{self._format_status_timestamp(poller.get('last_message_at'))}"
            )
            callback_label = poller.get("last_callback_data") or "none"
            lines.append(
                "- Telegram last callback: "
                f"{callback_label} at "
                f"{self._format_status_timestamp(poller.get('last_callback_at'))}"
            )
            active_handlers = handler.get("active_handlers") or 0
            last_handler_kind = handler.get("last_handler_kind") or "none"
            last_handler_id = handler.get("last_handler_id") or ""
            lines.append(
                "- Telegram handler: "
                f"active {active_handlers}; "
                f"last start {last_handler_kind} {last_handler_id} at "
                f"{self._format_status_timestamp(handler.get('last_handler_start_at'))}; "
                f"done {self._format_status_timestamp(handler.get('last_handler_done_at'))}"
            )
            poll_error = poller.get("last_poll_error")
            if poll_error:
                lines.append(
                    "- Telegram poll error: "
                    f"{self._format_status_timestamp(poller.get('last_poll_error_at'))} "
                    f"{poll_error}"
                )
            handler_error = handler.get("last_handler_error")
            if handler_error:
                lines.append(
                    "- Telegram handler error: "
                    f"{self._format_status_timestamp(handler.get('last_handler_error_at'))} "
                    f"{handler_error}"
                )
            else:
                lines.append("- Telegram handler error: never")
        else:
            lines.append("- Telegram: off")

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
        self._save_state()

    def _save_state(self) -> None:
        """Persist daemon state."""
        _save_state(self._state, self.state_path)

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
        self._save_state()

    def _drop_queued_nudges(self) -> None:
        """Drop any queued nudges without sending them."""
        if self._state.get("quiet_queue"):
            self._state["quiet_queue"] = []
            self._save_state()

    # ------------------------------------------------------------------
    # Scheduling / debounce
    # ------------------------------------------------------------------

    def _schedule_health(self) -> None:
        """Schedule a health trigger, debouncing rapid file events."""
        record_detected = False
        with self._lock:
            if not self._health_timer:
                record_detected = True
            if self._health_timer:
                self._health_timer.cancel()
            self._health_debounce_count += 1
            self._health_timer = threading.Timer(HEALTH_DEBOUNCE_S, self._fire_health)
            self._health_timer.daemon = True
            self._health_timer.start()
        if record_detected:
            self._record_event(
                "import",
                "detected",
                f"Health data change detected; import scheduled in {HEALTH_DEBOUNCE_S}s",
                {"debounce_s": HEALTH_DEBOUNCE_S},
            )
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
        with self._lock:
            file_events = self._health_debounce_count or 1
            self._health_debounce_count = 0
            self._health_timer = None
        self._record_event(
            "import",
            "started",
            "Health data debounce settled; running import",
            {"debounce_s": HEALTH_DEBOUNCE_S, "file_events": file_events},
        )
        with self._import_lock:
            before = self._runners._data_snapshot()
            result = self._runners._run_import()
            after = self._runners._data_snapshot()
        if result is None:
            return
        trigger_context = self._runners._format_data_delta(before, after)
        self._state["last_data_snapshot"] = after
        self._save_state()
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

    def _run_import(self) -> "ImportResult | None":
        """Delegate to the runner handler."""
        return self._runners._run_import()

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

    @staticmethod
    def _truncate_notice(text: str, limit: int) -> str:
        """Return *text* trimmed for a Telegram notice, preserving newlines."""
        stripped = text.strip()
        if len(stripped) <= limit:
            return stripped
        return stripped[: limit - 3].rstrip() + "..."

    def _notify_user_failure(
        self,
        operation: str,
        error_text: str | None,
        *,
        detail: VerificationSuppression | None = None,
    ) -> None:
        """Send a brief failure notice to Telegram so the user knows.

        Background runs (scheduled reports, manual /review, nudges, coach)
        used to fail silently — only the daemon log recorded the error. This
        forwards the most recent ERROR-level log message to Telegram so the
        user sees what broke without having to read daemon logs.

        Args:
            operation: Short human-readable label, e.g. "Weekly review".
            error_text: The captured error message, or None if nothing was
                captured (rare — falls back to a generic notice).
            detail: Verifier suppression snapshot captured during the
                failing run; when present, the notice gets a richer
                summary plus inline Details / Trace buttons.
        """
        poller = self._chat._poller
        if poller is None:
            # Telegram not configured — nothing to notify.
            return

        buttons: list[list[dict[str, str]]] = []
        if detail is not None and detail.source_llm_call_id is not None:
            text = self._format_verifier_failure_text(operation, detail)
            buttons.append(self._verifier_failure_buttons(detail))
        elif error_text:
            text = f"**{operation} failed**\n\n{self._truncate_notice(error_text, 600)}"
        else:
            text = f"**{operation} failed**\n\nCheck daemon logs."

        try:
            poller.send_message_with_keyboard(text, buttons)
        except Exception:
            logger.warning("Failed to send failure notice to Telegram", exc_info=True)

    def _format_verifier_failure_text(
        self,
        operation: str,
        detail: VerificationSuppression,
    ) -> str:
        """Build the user-facing text for a verifier-suppressed failure."""
        lines = [f"**{operation} failed**", "", "Verifier blocked it."]
        if detail.first_issue is not None and detail.first_issue.problem:
            lines.extend(["", self._truncate_notice(detail.first_issue.problem, 180)])
        lines.extend(["", "The report was not sent."])
        return "\n".join(lines)

    @staticmethod
    def _verifier_failure_buttons(
        detail: VerificationSuppression,
    ) -> list[dict[str, str]]:
        """Inline buttons for a verifier-suppressed failure notice."""
        row: list[dict[str, str]] = [
            {
                "text": "Details",
                "callback_data": f"faildetail:{detail.source_llm_call_id}",
            }
        ]
        if detail.trace_id is not None:
            row.append(
                {
                    "text": f"Trace {detail.trace_id}",
                    "callback_data": f"llmlog:trace:{detail.trace_id}",
                }
            )
        return row

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
        """Background thread: periodic checks for scheduled reports."""
        while not self._stop_event.wait(SCHEDULED_CHECK_INTERVAL_S):
            self._scheduled_check_once()

    def _scheduled_check_once(self) -> None:
        """Run one profile-scoped scheduled report and queue check."""
        from notification_prefs import evaluate_nudge_delivery, scheduled_report_due

        now = datetime.now().astimezone()
        prefs = self._load_notification_prefs(now=now)
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

    def _poll_google_drive_once(self, *, force_import: bool) -> bool:
        """Delegate one Google Drive poll to the Drive handler.

        Args:
            force_import: Whether to parse an unchanged cache on this poll.

        Returns:
            Whether the poll and any required import succeeded.
        """
        return self._drive.poll_once(force_import=force_import)


class Daemon:
    """Process-wide orchestration for isolated profile runtimes."""

    def __init__(
        self,
        profiles: dict[str, "Profile"],
        *,
        model: str | None = None,
        google_drive_service_account: Path | None = None,
        google_drive_poll_interval_s: int = GOOGLE_DRIVE_POLL_INTERVAL_S,
        local_health_dir: Path | None = None,
    ) -> None:
        from profiles import enabled_profiles
        from telegram_bot import TelegramPoller, TelegramSender

        self.profiles = profiles
        self.enabled = enabled_profiles(profiles)
        self._stop_event = threading.Event()
        self._executor = ThreadPoolExecutor(
            max_workers=max(4, min(12, len(self.enabled) * 2)),
            thread_name_prefix="profile-worker",
        )
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self._poller = TelegramPoller(token) if token else None
        self.runtimes: dict[str, ProfileRuntime] = {}
        for profile in self.enabled.values():
            if not profile.db.is_file():
                logger.error(
                    "Profile %s disabled at runtime: database is missing (%s). "
                    "Restore it or recreate the profile explicitly.",
                    profile.name,
                    profile.db,
                )
                continue
            missing_context = [
                name
                for name in ("me.md", "strategy.md")
                if not (profile.context / name).is_file()
            ]
            if missing_context:
                logger.error(
                    "Profile %s disabled at runtime: missing context files: %s",
                    profile.name,
                    ", ".join(missing_context),
                )
                continue
            sender = TelegramSender(token, str(profile.telegram_id)) if token else None
            health_dir = (
                local_health_dir or resolve_data_dir(None)
                if profile.import_source == "local"
                else profile.drive_cache
            )
            try:
                self.runtimes[profile.name] = ProfileRuntime(
                    model=model,
                    db=profile.db,
                    context_dir=profile.context,
                    health_dir=health_dir,
                    import_source=profile.import_source,
                    google_drive_service_account=google_drive_service_account,
                    google_drive_metrics_folder_id=profile.drive_metrics_folder_id,
                    google_drive_workouts_folder_id=profile.drive_workouts_folder_id,
                    google_drive_poll_interval_s=google_drive_poll_interval_s,
                    profile=profile,
                    sender=sender,
                    telegram_poller=self._poller,
                )
            except ValueError as exc:
                logger.error("Profile %s disabled at runtime: %s", profile.name, exc)
        self._operator_sender = next(
            (
                runtime._poller
                for runtime in self.runtimes.values()
                if runtime.operator and runtime._poller is not None
            ),
            None,
        )

    def _run_profile(
        self,
        runtime: ProfileRuntime,
        fn: callable,
        *args: object,
        **kwargs: object,
    ) -> None:
        """Serialize one profile's mutable work and isolate failures."""
        try:
            with runtime._dispatch_lock:
                fn(*args, **kwargs)
        except Exception:
            logger.exception("Profile %s operation failed", runtime.name)

    def handle_update(self, update: dict) -> None:
        """Authorize and route one raw Telegram update."""
        from profiles import resolve_profile

        profile = resolve_profile(update, self.profiles)
        if profile is None:
            sender = update.get("message", {}).get("from", {})
            if not sender:
                sender = update.get("callback_query", {}).get("from", {})
            user_id = sender.get("id", "unknown")
            username = sender.get("username")
            logger.warning(
                "Rejected unknown or non-private Telegram update from %s (@%s)",
                user_id,
                username or "",
            )
            if self._operator_sender is not None:
                suffix = f" (@{username})" if username else ""
                self._operator_sender.send_reply(
                    f"Unknown user {user_id}{suffix} messaged the bot."
                )
            return

        runtime = self.runtimes.get(profile.name)
        if runtime is None:
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._run_profile(
                runtime,
                runtime._chat._handle_telegram_callback_monitored,
                callback,
            )
            return
        message = update.get("message")
        if isinstance(message, dict) and message.get("text"):
            self._run_profile(
                runtime,
                runtime._chat._handle_telegram_message_monitored,
                message,
            )

    def _scheduler_loop(self) -> None:
        """Submit isolated scheduled and Drive work to the bounded pool."""
        next_scheduled = 0.0
        next_drive = {name: 0.0 for name in self.runtimes}
        drive_initial = set(self.runtimes)
        while not self._stop_event.wait(1):
            now = time.monotonic()
            if now >= next_scheduled:
                for runtime in self.runtimes.values():
                    self._executor.submit(
                        self._run_profile,
                        runtime,
                        runtime._scheduled_check_once,
                    )
                next_scheduled = now + SCHEDULED_CHECK_INTERVAL_S
            for name, runtime in self.runtimes.items():
                if runtime.import_source != "google-drive" or now < next_drive[name]:
                    continue
                force = name in drive_initial
                drive_initial.discard(name)
                self._executor.submit(
                    self._run_profile,
                    runtime,
                    runtime._poll_google_drive_once,
                    force_import=force,
                )
                next_drive[name] = now + runtime.google_drive_poll_interval_s

    def run(self) -> None:
        """Start shared observers, scheduler, and Telegram poller."""
        from watchdog.observers import Observer

        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = LOCK_FILE.open("w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.error("Another daemon instance is running (lock: %s).", LOCK_FILE)
            raise SystemExit(1)
        self._lock_file.write(str(os.getpid()))
        self._lock_file.flush()

        observer = Observer()
        for runtime in self.runtimes.values():
            logger.info(
                "Profile %s: db=%s source=%s context=%s",
                runtime.name,
                runtime.db,
                runtime.import_source,
                runtime.context_dir,
            )
            if runtime.import_source == "local" and runtime.health_dir.exists():
                observer.schedule(
                    _make_health_handler(
                        lambda runtime=runtime: self._executor.submit(
                            self._run_profile, runtime, runtime._schedule_health
                        ),
                        lambda runtime=runtime: self._executor.submit(
                            self._run_profile, runtime, runtime._schedule_health
                        ),
                    ),
                    str(runtime.health_dir),
                    recursive=True,
                )
            if runtime.context_dir.exists():
                observer.schedule(
                    _make_context_handler(
                        lambda stem, runtime=runtime: self._executor.submit(
                            self._run_profile,
                            runtime,
                            runtime._schedule_context,
                            stem,
                        ),
                        runtime._self_originated_writes,
                    ),
                    str(runtime.context_dir),
                    recursive=False,
                )
            runtime._record_event("daemon", "start", "Profile runtime started")

        observer.start()
        scheduler = threading.Thread(
            target=self._scheduler_loop,
            daemon=True,
            name="profile-scheduler",
        )
        scheduler.start()
        if self._poller is not None:
            telegram = threading.Thread(
                target=self._poller.poll_loop,
                args=(self.handle_update, self._stop_event),
                daemon=True,
                name="telegram-poller",
            )
            telegram.start()
        else:
            logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram disabled")

        logger.info("Daemon running with %d enabled profile(s)", len(self.runtimes))
        try:
            while observer.is_alive():
                observer.join(timeout=1)
        except KeyboardInterrupt:
            logger.info("Shutting down daemon")
        finally:
            self._stop_event.set()
            for runtime in self.runtimes.values():
                runtime._stop_event.set()
            observer.stop()
            observer.join()
            self._executor.shutdown(wait=True, cancel_futures=True)
            for runtime in self.runtimes.values():
                runtime._record_event("daemon", "stop", "Profile runtime stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _setup_logging(foreground: bool) -> None:
    """Configure logging for the daemon.

    Rotating file log always active. Console output added when --foreground.

    Args:
        foreground: If True, also log to stderr with colours.
    """
    from log import LOG_FORMAT, LevelFormatter, quiet_noisy_loggers

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE, when="midnight", backupCount=7, encoding="utf-8"
    )
    file_handler.setFormatter(LevelFormatter(LOG_FORMAT, use_color=False))
    root.addHandler(file_handler)
    quiet_noisy_loggers()

    if foreground:
        # Add a coloured stderr handler alongside the file handler.
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(
            LevelFormatter(LOG_FORMAT, use_color=sys.stderr.isatty())
        )
        root.addHandler(stderr_handler)


def main() -> None:
    """Entry point: parse args and start the daemon."""
    import argparse

    # Add src/ to path so project modules resolve when run directly
    sys.path.insert(0, str(Path(__file__).parent))

    parser = argparse.ArgumentParser(
        description="zdrowskit health import and notification daemon"
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Log to stderr in addition to the log file (useful for debugging)",
    )
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        help="Operator's local Auto Export folder override",
    )
    parser.add_argument(
        "--google-drive-service-account",
        metavar="PATH",
        default=GOOGLE_DRIVE_SERVICE_ACCOUNT,
        help="Service-account JSON path",
    )
    parser.add_argument(
        "--google-drive-poll-interval",
        type=int,
        metavar="SECONDS",
        default=GOOGLE_DRIVE_POLL_INTERVAL_S,
        help="Drive polling interval in seconds",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        default=None,
        help="Legacy global litellm model override for all LLM calls",
    )
    args = parser.parse_args()

    _setup_logging(args.foreground)

    try:
        from profiles import load_profiles

        daemon = Daemon(
            load_profiles(),
            model=args.model,
            google_drive_service_account=(
                Path(args.google_drive_service_account).expanduser().resolve()
                if args.google_drive_service_account
                else None
            ),
            google_drive_poll_interval_s=args.google_drive_poll_interval,
            local_health_dir=(
                resolve_data_dir(args.data_dir) if args.data_dir else None
            ),
        )
    except ValueError as exc:
        parser.error(str(exc))
    daemon.run()


if __name__ == "__main__":
    main()
