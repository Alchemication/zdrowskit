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
import types
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from commands import CoachProposal, CommandResult
    from context_edit import ContextEdit
    from context_edit import PendingContextEdit
    from llm import LLMResult

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


@dataclass
class PendingAdd:
    """In-flight /add manual activity flow state."""

    step: str  # pick_type | confirm_workout | pick_sleep_date | pick_sleep_dur | confirm_sleep
    message_id: int
    created_at: float  # time.monotonic() for TTL cleanup
    type_options: list[dict] | None = None  # [{type, category, count}, ...]
    workout_type: str | None = None
    category: str | None = None
    clone_row: dict | None = None  # full workout column dict from LLM
    date: str | None = None
    sleep_total_h: float | None = None
    sleep_in_bed_h: float | None = None
    saved_id: int | None = None  # row id after save, for undo
    saved_table: str | None = None  # "manual_workout" or "manual_sleep"


_PENDING_ADD_TTL_S = 600  # 10 min


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ICLOUD_HEALTH_DIR = (
    Path.home()
    / "Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents"
)
LOG_FILE = Path.home() / "Library/Logs/zdrowskit.daemon.log"
LOCK_FILE = Path.home() / "Documents/zdrowskit/.daemon.lock"
STATE_FILE = Path.home() / "Documents/zdrowskit/.daemon_state.json"

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------

HEALTH_DEBOUNCE_S = 180  # 3 min: wait for all JSON files to arrive via iCloud
CONTEXT_DEBOUNCE_S = 60  # 1 min: debounce rapid .md edits

MAX_NUDGES_PER_DAY = 3
MIN_NUDGE_INTERVAL_S = 90 * 60  # 90 minutes between nudges

# Training days (all days — user catches up on weekends)
TRAINING_DAYS = {0, 1, 2, 3, 4, 5, 6}

SCHEDULED_CHECK_INTERVAL_S = 30 * 60  # check every 30 min

EVENING_HOUR_START = 20
EVENING_HOUR_END = 21

COACH_SUPPRESSION_S = 3600  # ±1 hour: suppress nudges around scheduled reports

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
        self._pending_notify_proposals: dict[str, PendingNotifyProposal] = {}
        self._pending_notify_clarifications: dict[int, PendingNotifyClarification] = {}
        self._pending_rejection_reasons = self._restore_pending_reason_map(
            self._state.get("pending_rejection_reasons"),
            value_type="str",
        )
        self._pending_feedback_reasons = self._restore_pending_reason_map(
            self._state.get("pending_feedback_reasons"),
            value_type="int",
        )
        self._pending_adds: dict[str, PendingAdd] = {}
        self._add_counter: int = 0
        # Paths the daemon is about to write itself (e.g. accepted coach
        # edits). The watchdog handler consults this set to suppress the
        # follow-up `*_updated` nudge that would otherwise fire from the
        # daemon's own apply_edit call. Genuine user edits to the same file
        # in a separate editor are not in the set and still trigger nudges.
        self._self_originated_writes: set[Path] = set()

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

        conversation = getattr(self, "_conversation", None)
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

    def _run_review(
        self,
        *,
        week: str = "last",
        skip_import: bool = False,
    ) -> None:
        """Run a manual review report and send it via Telegram."""
        if week not in {"current", "last"}:
            raise ValueError(f"Unsupported review week: {week}")

        if not skip_import:
            self._run_import()

        from commands import cmd_insights

        args = types.SimpleNamespace(
            db=str(self.db),
            model=self.model,
            email=False,
            telegram=True,
            week=week,
            months=3,
            no_update_baselines=False,
            no_update_history=False,
            explain=False,
            data_dir=None,
            reasoning_effort="medium",
        )
        with _capture_last_error() as cap:
            try:
                logger.info("Running manual review report (%s)", week)
                result = cmd_insights(args)
                self._attach_feedback_button(result, "insights")
                self._record_report("review" if week == "last" else "progress")
                self._state["last_report_ts"] = datetime.now().isoformat()
                _save_state(self._state)
            except SystemExit:
                # Snapshot before our own logger.error overwrites the capture.
                captured = cap.last_message
                logger.error("Manual review report failed (%s)", week)
                self._notify_user_failure(f"Manual review ({week})", captured)

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

    @staticmethod
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
        before = self._data_snapshot()
        self._run_import()
        after = self._data_snapshot()
        trigger_context = self._format_data_delta(before, after)
        self._state["last_data_snapshot"] = after
        _save_state(self._state)
        self._run_nudge("new_data", trigger_context=trigger_context)

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
        trigger_context = self._format_context_trigger(stem, trigger)
        self._run_nudge(trigger, trigger_context=trigger_context)

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _is_report_imminent(self) -> bool:
        """Check if a scheduled report will fire within COACH_SUPPRESSION_S."""
        from notification_prefs import effective_notification_prefs

        now = datetime.now().astimezone()
        prefs = self._load_notification_prefs(now=now)
        effective = effective_notification_prefs(prefs)

        for report_type in ("weekly_insights", "midweek_report"):
            report = effective[report_type]
            if now.strftime("%A").lower() != report["weekday"]:
                continue
            hour_str, minute_str = report["time"].split(":")
            report_time = now.replace(
                hour=int(hour_str),
                minute=int(minute_str),
                second=0,
                microsecond=0,
            )
            delta = (report_time - now).total_seconds()
            if 0 < delta < COACH_SUPPRESSION_S:
                return True

        return False

    def _can_send_nudge(self) -> bool:
        """Check whether a nudge is allowed under the rate limits.

        Returns:
            True if a nudge may be sent; False if suppressed.
        """
        prefs = self._load_notification_prefs(now=datetime.now().astimezone())
        max_nudges_per_day = (
            prefs.get("overrides", {}).get("nudges", {}).get("max_per_day")
        )
        if not isinstance(max_nudges_per_day, int):
            from notification_prefs import effective_notification_prefs

            max_nudges_per_day = effective_notification_prefs(prefs)["nudges"][
                "max_per_day"
            ]

        # Suppress near scheduled reports (±1 hour)
        last_report_ts = self._state.get("last_report_ts")
        if last_report_ts:
            elapsed = abs(
                (
                    datetime.now() - datetime.fromisoformat(last_report_ts)
                ).total_seconds()
            )
            if elapsed < COACH_SUPPRESSION_S:
                logger.info(
                    "Nudge suppressed: within %.0f min of scheduled report",
                    elapsed / 60,
                )
                return False
        if self._is_report_imminent():
            logger.info("Nudge suppressed: scheduled report imminent")
            return False

        today_str = date.today().isoformat()

        if self._state.get("nudge_date") != today_str:
            self._state["nudge_count_today"] = 0
            self._state["nudge_date"] = today_str

        if self._state.get("nudge_count_today", 0) >= max_nudges_per_day:
            logger.info(
                "Nudge suppressed: daily limit (%d) reached", max_nudges_per_day
            )
            return False

        last_ts = self._state.get("last_nudge_ts")
        if last_ts:
            elapsed = (datetime.now() - datetime.fromisoformat(last_ts)).total_seconds()
            if elapsed < MIN_NUDGE_INTERVAL_S:
                logger.info(
                    "Nudge suppressed: %.0f min since last (min %.0f min)",
                    elapsed / 60,
                    MIN_NUDGE_INTERVAL_S / 60,
                )
                return False

        return True

    def _record_nudge(self, text: str, trigger: str) -> None:
        """Update state after a nudge is sent.

        Args:
            text: The nudge text that was sent.
            trigger: The trigger type that prompted the nudge.
        """
        today_str = date.today().isoformat()
        if self._state.get("nudge_date") != today_str:
            self._state["nudge_count_today"] = 0
            self._state["nudge_date"] = today_str
        self._state["nudge_count_today"] = self._state.get("nudge_count_today", 0) + 1
        now = datetime.now()
        self._state["last_nudge_ts"] = now.isoformat()

        entry = {"ts": now.isoformat(), "trigger": trigger, "text": text}
        recent: list[dict] = self._state.get("recent_nudges", [])
        recent.insert(0, entry)
        self._state["recent_nudges"] = recent[:3]  # Keep last 3

        _save_state(self._state)

    def _can_send_report(self, report_type: str) -> bool:
        """Check whether a report of the given type may be sent today.

        Args:
            report_type: "review" for full-week or "progress" for mid-week.

        Returns:
            True if report may be sent; False if already sent today.
        """
        key = f"last_{report_type}_date"
        skipped_key = f"last_{report_type}_skip_date"
        today_str = date.today().isoformat()
        if self._state.get(key) == today_str:
            logger.info("%s report suppressed: already sent today", report_type)
            return False
        if self._state.get(skipped_key) == today_str:
            logger.info("%s report suppressed: already skipped today", report_type)
            return False
        return True

    def _record_report(self, report_type: str) -> None:
        """Update state after a report is sent.

        Args:
            report_type: "review" for full-week or "progress" for mid-week.
        """
        self._state[f"last_{report_type}_date"] = date.today().isoformat()
        _save_state(self._state)

    # ------------------------------------------------------------------
    # LLM calls
    # ------------------------------------------------------------------

    def _data_snapshot(self) -> dict:
        """Snapshot table-level markers used to compute import deltas.

        Returns:
            A dict with row counts and max-date markers for the daily,
            workout_all, and sleep_all tables. Empty dict on failure.
        """
        try:
            conn = sqlite3.connect(str(self.db))
            cur = conn.cursor()
            snap: dict = {}
            for table, date_col in (
                ("daily", "date"),
                ("workout_all", "start_utc"),
                ("sleep_all", "date"),
            ):
                try:
                    row = cur.execute(
                        f"SELECT COUNT(*), MAX({date_col}) FROM {table}"
                    ).fetchone()
                except sqlite3.Error:
                    continue
                snap[f"{table}_count"] = row[0] if row else 0
                snap[f"{table}_max"] = row[1] if row else None
            conn.close()
            return snap
        except sqlite3.Error as exc:
            logger.warning("Data snapshot failed: %s", exc)
            return {}

    def _format_data_delta(self, before: dict, after: dict) -> str:
        """Describe what records arrived between two data snapshots.

        Args:
            before: Snapshot taken before the import ran.
            after: Snapshot taken after the import ran.

        Returns:
            Human-readable text the LLM can use to know what is actually new.
            Falls back to a generic line when nothing identifiable changed.
        """
        if not after:
            return "New health data synced (delta unavailable)."

        lines: list[str] = []

        # New workouts: rows with start_utc strictly greater than the prior max.
        prev_workout_max = before.get("workout_all_max")
        try:
            conn = sqlite3.connect(str(self.db))
            conn.row_factory = sqlite3.Row
            if prev_workout_max:
                rows = conn.execute(
                    "SELECT start_utc, date, type, category, duration_min, "
                    "gpx_distance_km FROM workout_all "
                    "WHERE start_utc > ? ORDER BY start_utc",
                    (prev_workout_max,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT start_utc, date, type, category, duration_min, "
                    "gpx_distance_km FROM workout_all "
                    "ORDER BY start_utc DESC LIMIT 3"
                ).fetchall()
            for r in rows:
                dur = r["duration_min"]
                dur_s = f"{dur:.0f} min" if dur is not None else "?"
                dist = r["gpx_distance_km"]
                dist_s = f", {dist:.2f} km" if dist is not None else ""
                lines.append(
                    f"- New workout: {r['type']} ({r['category']}), "
                    f"{dur_s}{dist_s} on {r['date']}"
                )

            # New sleep nights: rows with date strictly greater than prior max.
            prev_sleep_max = before.get("sleep_all_max")
            if prev_sleep_max:
                rows = conn.execute(
                    "SELECT date, sleep_total_h, sleep_efficiency_pct "
                    "FROM sleep_all WHERE date > ? ORDER BY date",
                    (prev_sleep_max,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT date, sleep_total_h, sleep_efficiency_pct "
                    "FROM sleep_all ORDER BY date DESC LIMIT 2"
                ).fetchall()
            for r in rows:
                h = r["sleep_total_h"]
                eff = r["sleep_efficiency_pct"]
                h_s = f"{h:.1f}h" if h is not None else "?h"
                eff_s = f", {eff:.0f}% efficiency" if eff is not None else ""
                lines.append(f"- New sleep night: {r['date']} — {h_s}{eff_s}")

            # New daily metric rows for dates beyond the previous max.
            prev_daily_max = before.get("daily_max")
            if prev_daily_max:
                rows = conn.execute(
                    "SELECT date, steps, hrv_ms, resting_hr FROM daily "
                    "WHERE date > ? ORDER BY date",
                    (prev_daily_max,),
                ).fetchall()
                for r in rows:
                    parts = []
                    if r["steps"] is not None:
                        parts.append(f"steps {r['steps']}")
                    if r["hrv_ms"] is not None:
                        parts.append(f"HRV {r['hrv_ms']:.0f} ms")
                    if r["resting_hr"] is not None:
                        parts.append(f"RHR {r['resting_hr']:.0f} bpm")
                    detail = ", ".join(parts) if parts else "(no metrics yet)"
                    lines.append(f"- New daily row: {r['date']} — {detail}")
            conn.close()
        except sqlite3.Error as exc:
            logger.warning("Delta query failed: %s", exc)
            return "New health data synced (delta query failed)."

        if not lines:
            # No new identifiable rows — most likely an in-place refresh
            # of today's metrics (e.g. a late HRV reading landing).
            return (
                "Health data refreshed but no new completed activities or sleep "
                "nights since the previous sync. Today's metrics may have "
                "updated in place."
            )

        return "Records added in this import:\n" + "\n".join(lines)

    def _format_context_trigger(self, stem: str, trigger: str) -> str:
        """Describe a context-file edit so the LLM knows where to look.

        Args:
            stem: File stem that was edited (``log``, ``strategy``, ``me``).
            trigger: The mapped trigger type string.

        Returns:
            One-line description pointing the LLM to the relevant section.
        """
        section_map = {
            "log": ("Recent User Notes", "log.md"),
            "strategy": ("Strategy", "strategy.md"),
            "me": ("About the User", "me.md"),
        }
        section, filename = section_map.get(stem, ("Recent User Notes", f"{stem}.md"))
        return (
            f"The user just edited {filename} (trigger: {trigger}). "
            f"The current contents are in the '{section}' section above — "
            "respond to what changed there."
        )

    def _run_import(self) -> None:
        """Import the latest health data from the iCloud directory into the DB."""
        from commands import cmd_import

        args = types.SimpleNamespace(
            data_dir=str(ICLOUD_HEALTH_DIR),
            source="autoexport",
            db=str(self.db),
        )
        try:
            logger.info("Importing health data from %s", ICLOUD_HEALTH_DIR)
            cmd_import(args)
        except SystemExit:
            logger.error("Import failed — proceeding with existing DB data")

    def _run_weekly_report(self) -> None:
        """Run the full weekly insights report and send via Telegram."""
        from notification_prefs import evaluate_report_delivery

        now = datetime.now().astimezone()
        prefs = self._load_notification_prefs(now=now)
        decision = evaluate_report_delivery(prefs, "weekly_insights", now=now)
        if decision["status"] != "allowed":
            self._state["last_review_skip_date"] = date.today().isoformat()
            _save_state(self._state)
            logger.info(
                "Weekly insights suppressed: %s",
                decision.get("reason", "unknown"),
            )
            return
        if not self._can_send_report("review"):
            return

        self._run_import()

        from commands import cmd_insights

        args = types.SimpleNamespace(
            db=str(self.db),
            model=self.model,
            email=False,
            telegram=True,
            week="last",
            months=3,
            no_update_baselines=False,
            no_update_history=False,
            explain=False,
            data_dir=None,
            reasoning_effort="medium",
        )
        with _capture_last_error() as cap:
            try:
                logger.info("Running weekly review report")
                result = cmd_insights(args)
                self._attach_feedback_button(result, "insights")
                self._record_report("review")
                self._state["last_report_ts"] = datetime.now().isoformat()
                _save_state(self._state)
                self._run_coach(week="last", skip_import=True)
            except SystemExit:
                captured = cap.last_message
                logger.error("Weekly review report failed")
                self._notify_user_failure("Weekly review", captured)

    def _run_midweek_report(self) -> None:
        """Run a mid-week progress report and send via Telegram."""
        from notification_prefs import evaluate_report_delivery

        now = datetime.now().astimezone()
        prefs = self._load_notification_prefs(now=now)
        decision = evaluate_report_delivery(prefs, "midweek_report", now=now)
        if decision["status"] != "allowed":
            self._state["last_progress_skip_date"] = date.today().isoformat()
            _save_state(self._state)
            logger.info(
                "Midweek report suppressed: %s",
                decision.get("reason", "unknown"),
            )
            return
        if not self._can_send_report("progress"):
            return

        self._run_import()

        from commands import cmd_insights

        args = types.SimpleNamespace(
            db=str(self.db),
            model=self.model,
            email=False,
            telegram=True,
            week="current",
            months=3,
            no_update_baselines=False,
            no_update_history=False,
            explain=False,
            data_dir=None,
            reasoning_effort="medium",
        )
        with _capture_last_error() as cap:
            try:
                logger.info("Running mid-week progress report")
                result = cmd_insights(args)
                self._attach_feedback_button(result, "insights")
                self._record_report("progress")
                self._state["last_report_ts"] = datetime.now().isoformat()
                _save_state(self._state)
            except SystemExit:
                captured = cap.last_message
                logger.error("Mid-week progress report failed")
                self._notify_user_failure("Mid-week progress", captured)

    def _run_nudge(
        self,
        trigger: str,
        *,
        trigger_context: str | None = None,
        _from_drain: bool = False,
    ) -> None:
        """Run a nudge and send via Telegram.

        Passes recent nudge history so the LLM can decide whether there is
        anything new worth saying (SKIP if not).

        Args:
            trigger: Trigger type string passed to cmd_nudge.
            trigger_context: Optional human-readable description of *what*
                the trigger refers to (e.g. which records were imported,
                which file was edited). When None, a generic placeholder is
                used.
            _from_drain: Internal flag — True when called from the deferred
                queue drain path.
        """
        from notification_prefs import evaluate_nudge_delivery

        now = datetime.now().astimezone()
        prefs = self._load_notification_prefs(now=now)
        decision = evaluate_nudge_delivery(prefs, now=now)
        if decision["status"] == "suppressed":
            logger.info(
                "Nudge suppressed by notification prefs: %s",
                decision.get("reason", "unknown"),
            )
            return
        if decision["status"] == "deferred":
            if _from_drain:
                logger.info("Deferred nudge still blocked at drain time; skipping")
                return
            self._queue_nudge_trigger(trigger, now=now)
            logger.info(
                "Nudge deferred until %s (trigger: %s, queue size: %d)",
                decision.get("until", "later"),
                trigger,
                len(self._state.get("quiet_queue", [])),
            )
            return

        if not self._can_send_nudge():
            return

        from commands import cmd_nudge

        args = types.SimpleNamespace(
            db=str(self.db),
            model=self.model,
            email=False,
            telegram=True,
            trigger=trigger,
            months=1,
            recent_nudges=self._state.get("recent_nudges", []),
            last_coach_summary=self._state.get("last_coach_summary", ""),
            last_coach_summary_date=self._state.get("last_coach_summary_date", ""),
            trigger_context=trigger_context or "",
        )
        with _capture_last_error() as cap:
            try:
                logger.info("Running nudge (trigger: %s)", trigger)
                result = cmd_nudge(args)
                if result.text:
                    self._record_nudge(result.text, trigger)
                    self._attach_feedback_button(result, "nudge")
            except SystemExit:
                captured = cap.last_message
                logger.error("Nudge failed (trigger: %s)", trigger)
                self._notify_user_failure(f"Nudge ({trigger})", captured)

    def _drain_quiet_queue(self) -> None:
        """Process deferred triggers as a single consolidated nudge."""
        queue: list[dict] = self._state.get("quiet_queue", [])
        if not queue:
            return

        # Clear the queue before sending to avoid re-processing on failure
        self._state["quiet_queue"] = []
        _save_state(self._state)

        # Pick the most "interesting" trigger (user-initiated > system)
        priority = {
            "strategy_updated": 4,
            "log_update": 2,
            "profile_updated": 1,
            "new_data": 0,
        }
        best = max(queue, key=lambda e: priority.get(e["trigger"], 0))

        logger.info(
            "Draining quiet queue: %d triggers, sending consolidated nudge (trigger: %s)",
            len(queue),
            best["trigger"],
        )
        # Compose a consolidated trigger_context from every queued event so
        # the nudge has the full picture of what accumulated during quiet hours.
        parts = [
            f"- {e['trigger']} at {e['ts'][:16]}" for e in queue if e.get("trigger")
        ]
        trigger_context = (
            "Multiple triggers accumulated during quiet hours; choosing the "
            "highest-priority one to drive the message:\n" + "\n".join(parts)
            if len(queue) > 1
            else None
        )
        self._run_nudge(
            best["trigger"], trigger_context=trigger_context, _from_drain=True
        )

    def _run_coach(
        self,
        *,
        week: str = "last",
        skip_import: bool = False,
        force: bool = False,
    ) -> None:
        """Run a coaching review and send proposals via Telegram.

        Proposes concrete edits to strategy.md based on the
        week's data. Each proposal is sent as an inline Approve/Reject
        button. When the model returns SKIP (no strategy changes
        warranted), nothing is sent — the coach is silent on no-change
        weeks, mirroring the nudge SKIP behavior.

        Args:
            week: Which week to review (``"last"`` or ``"current"``).
            skip_import: Skip the pre-run import pass (used when a caller
                has already imported, e.g. the weekly report path).
            force: Bypass the "already ran today" guard. Set by manual
                triggers like the ``/coach`` Telegram command so the user
                can re-run on demand.
        """
        last_coach = self._state.get("last_coach_date", "")
        today_str = date.today().isoformat()
        if last_coach == today_str and not force:
            logger.debug("Coach already ran today, skipping")
            return

        if not skip_import:
            self._run_import()

        from commands import cmd_coach

        args = types.SimpleNamespace(
            db=str(self.db),
            model=self.model,
            week=week,
            months=3,
            recent_nudges=self._state.get("recent_nudges", []),
            reasoning_effort="medium",
        )
        with _capture_last_error() as cap:
            try:
                logger.info("Running coaching review")
                cmd_result, proposals = cmd_coach(args)
                self._send_coach_bundle(cmd_result, proposals, force=force)
                self._state["last_coach_date"] = today_str
                if cmd_result.text:
                    self._state["last_coach_summary"] = cmd_result.text[:500]
                    self._state["last_coach_summary_date"] = today_str
                _save_state(self._state)
            except SystemExit:
                captured = cap.last_message
                logger.error("Coaching review failed")
                self._notify_user_failure("Coaching review", captured)

    def _send_coach_bundle(
        self,
        cmd_result: "CommandResult",
        proposals: list["CoachProposal"],
        *,
        force: bool,
    ) -> None:
        """Deliver a coach review as one bundled Telegram message.

        Composes the narrative + per-edit diff blocks (already in
        ``cmd_result.text``) and an inline keyboard with one Accept / Reject
        / Diff row per proposal plus a final feedback row. Long bundles are
        chunked by :meth:`TelegramPoller.send_message_with_keyboard`, which
        attaches the keyboard to the final chunk so the user always sees
        the buttons at the bottom of the conversation.

        SKIP handling: when ``cmd_result.text`` is None and ``force`` is
        True (manual ``/coach``), send a short acknowledgment so the
        "Running coaching review…" placeholder doesn't dangle. Scheduled
        weekly runs stay silent on SKIP to avoid noise after the insights
        report.

        Args:
            cmd_result: Result returned by :func:`commands.cmd_coach`.
            proposals: Validated proposals returned alongside it.
            force: Whether this run was user-initiated (``/coach``) and
                therefore deserves a SKIP acknowledgment.
        """
        from telegram_bot import feedback_keyboard

        if self._poller is None:
            return

        # SKIP path.
        if not cmd_result.text:
            if not force:
                return
            skip_text = (
                "Coach reviewed the week — no strategy changes warranted. "
                "Current strategy is working."
            )
            skip_msg_id = self._poller.send_reply(skip_text)
            if skip_msg_id is not None and cmd_result.llm_call_id is not None:
                self._poller.edit_message_reply_markup(
                    skip_msg_id,
                    feedback_keyboard(cmd_result.llm_call_id, "coach"),
                )
            return

        # No proposals, but coach still has narrative to deliver — rare
        # (the prompt forces SKIP otherwise) but possible from the
        # iteration-cap synthesis path. Send as a regular reply with the
        # feedback keyboard attached.
        if not proposals:
            msg_id = self._poller.send_reply(cmd_result.text)
            if msg_id is not None and cmd_result.llm_call_id is not None:
                self._poller.edit_message_reply_markup(
                    msg_id,
                    feedback_keyboard(cmd_result.llm_call_id, "coach"),
                )
            return

        # Bundled path. Mint one PendingEdit per proposal so the inline
        # buttons can route accept/reject callbacks back to the right edit.
        accept_rows: list[list[dict[str, str]]] = []
        for i, proposal in enumerate(proposals, start=1):
            edit_id = self._pending_edits.store(
                proposal.edit, source="coach", preview=proposal.preview
            )
            accept_rows.append(
                [
                    {
                        "text": f"\u2705 Accept #{i}",
                        "callback_data": f"ctx_accept:{edit_id}",
                    },
                    {
                        "text": f"\u274c Reject #{i}",
                        "callback_data": f"ctx_reject:{edit_id}",
                    },
                    {
                        "text": f"\U0001f50d Diff #{i}",
                        "callback_data": f"ctx_diff:{edit_id}",
                    },
                ]
            )

        # Append a feedback row reusing the existing thumbs-down keyboard
        # so the user can flag the whole review without losing the per-edit
        # buttons. feedback_keyboard returns rows (list[list[button]]).
        keyboard_rows = list(accept_rows)
        if cmd_result.llm_call_id is not None:
            keyboard_rows.extend(feedback_keyboard(cmd_result.llm_call_id, "coach"))

        self._poller.send_message_with_keyboard(cmd_result.text, keyboard_rows)

    # ------------------------------------------------------------------
    # Telegram interactive chat
    # ------------------------------------------------------------------

    def _start_telegram_poller(self) -> None:
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
            args=(self._handle_telegram_message, self._stop_event),
            kwargs={"on_callback": self._handle_telegram_callback},
            daemon=True,
            name="telegram-poller",
        )
        thread.start()
        logger.info("Telegram chat listener started")

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
        poller = getattr(self, "_poller", None)
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
        if reply_to and self._consume_rejection_reason(reply_to, text):
            self._poller.send_reply(
                "Saved the rejection reason.",
                reply_to_message_id=message["message_id"],
            )
            return

        # Capture optional free-text feedback reason.
        if reply_to and self._consume_feedback_reason(reply_to, text):
            self._poller.send_reply(
                "\u2713 Feedback saved, thanks!",
                reply_to_message_id=message["message_id"],
            )
            return

        if reply_to and self._consume_notify_clarification(reply_to, text, message):
            return

        message_id = message["message_id"]

        # Handle bot commands before the LLM.
        if text.startswith("/"):
            self._handle_command(text, message_id)
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

            conn = open_db(self.db)
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
        from charts import extract_charts, render_chart, strip_charts

        chart_blocks = extract_charts(reply)
        if chart_blocks:
            extra_ns = {"rows": query_rows} if query_rows else None
            for block in chart_blocks:
                try:
                    img = render_chart(block.code, {}, extra_namespace=extra_ns)
                    if img:
                        self._poller.send_photo(img, caption=block.title)
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
            self._propose_context_edit(edit, source="chat")
            break  # At most one context update per response

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
                self._run_review(week=week, skip_import=False)
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
                self._run_coach(week=week, skip_import=False, force=True)
            finally:
                self._stop_placeholder_animation(stop_anim, anim_thread)
                if status_id is not None:
                    self._poller.edit_message(
                        status_id, f"\u2713 Coaching review for {label} done."
                    )
        elif cmd == "/notify":
            args = text.split(maxsplit=1)
            request_text = args[1].strip() if len(args) > 1 else ""
            self._handle_notify_command(request_text, message_id)
        elif cmd == "/status":
            self._poller.send_reply(
                "\n".join(self._build_status_lines()),
                reply_to_message_id=message_id,
            )
        elif cmd == "/context":
            parts = text.split()
            file_arg = parts[1] if len(parts) > 1 else None
            self._send_context_overview(message_id, file_arg)
        elif cmd == "/add":
            self._handle_add_command(message_id)
        elif cmd == "/help":
            from commands import TELEGRAM_BOT_COMMANDS
            from config import CONTEXT_DIR, PROMPTS_DIR

            ctx_names = sorted(
                f.stem
                for d in (CONTEXT_DIR, PROMPTS_DIR)
                for f in d.glob("*.md")
                if f.stat().st_size > 0
            )
            ctx_opts = ", ".join(ctx_names) if ctx_names else "none found"
            lines = []
            for command in TELEGRAM_BOT_COMMANDS:
                if command["command"] == "review":
                    lines.append(
                        f"/review [current|last] — {command['description']} (default: last)"
                    )
                elif command["command"] == "coach":
                    lines.append(
                        f"/coach [current|last] — {command['description']} (default: last)"
                    )
                elif command["command"] == "context":
                    lines.append(f"/context [name] — {command['description']}")
                elif command["command"] == "add":
                    lines.append(f"/add — {command['description']} (workouts, sleep)")
                else:
                    lines.append(f"/{command['command']} — {command['description']}")
            lines.append(f"\nAvailable context files: {ctx_opts}")
            self._poller.send_reply("\n".join(lines), reply_to_message_id=message_id)
        else:
            self._poller.send_reply(
                "Unknown command. Try /help",
                reply_to_message_id=message_id,
            )

    def _handle_notify_command(self, request_text: str, message_id: int) -> None:
        """Handle the Telegram /notify command."""
        from commands import interpret_notify_request
        from notification_prefs import (
            format_notification_summary,
            format_proposed_changes,
        )

        now = datetime.now().astimezone()
        prefs = self._load_notification_prefs(now=now)

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
                db=self.db,
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
                    self._pending_notify_clarifications[prompt_id] = (
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
        self._pending_notify_proposals[proposal_id] = PendingNotifyProposal(
            request_text=request_text,
            preview=preview,
            summary=summary,
            changes=payload["changes"],
        )
        self._poller.send_message_with_keyboard(
            f"{summary}\n\n{preview}",
            self._notify_keyboard(proposal_id),
            reply_to_message_id=message_id,
        )

    def _consume_notify_clarification(
        self,
        reply_to: dict,
        text: str,
        message: dict,
    ) -> bool:
        """Handle a free-text clarification reply for /notify."""
        from commands import interpret_notify_request
        from notification_prefs import (
            format_notification_summary,
            format_proposed_changes,
        )

        prompt_id = reply_to.get("message_id")
        if prompt_id is None:
            return False

        with self._lock:
            pending = self._pending_notify_clarifications.pop(prompt_id, None)
        if pending is None:
            return False

        now = datetime.now().astimezone()
        prefs = self._load_notification_prefs(now=now)
        try:
            payload = interpret_notify_request(
                pending.request_text,
                db=self.db,
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
                    self._pending_notify_clarifications[next_prompt_id] = pending
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
        self._pending_notify_proposals[proposal_id] = PendingNotifyProposal(
            request_text=pending.request_text,
            preview=preview,
            summary=summary,
            changes=payload["changes"],
        )
        self._poller.send_message_with_keyboard(
            f"{summary}\n\n{preview}",
            self._notify_keyboard(proposal_id),
            reply_to_message_id=message["message_id"],
        )
        return True

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
            header = f"📄 {path.name}"
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
                lines.append(f"📄 {f.stem} — {line_count} lines ({size} B)")
            except OSError:
                lines.append(f"📄 {f.stem} — (unreadable)")

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
                    self._self_originated_writes.add(
                        (self.context_dir / f"{pending.edit.file}.md").resolve()
                    )
                    apply_edit(self.context_dir, pending.edit, strict=True)
                    self._record_context_feedback(pending, "accepted")
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

        elif data.startswith("notify_accept:"):
            proposal_id = data.split(":", 1)[1]
            pending = self._pending_notify_proposals.pop(proposal_id, None)
            if not pending:
                self._poller.answer_callback_query(cb_id, "This proposal expired.")
                if msg_id:
                    self._poller.edit_message(
                        msg_id, "This notification proposal has expired."
                    )
                return

            from notification_prefs import apply_notification_changes

            now = datetime.now().astimezone()
            prefs = self._load_notification_prefs(now=now)
            updated = apply_notification_changes(prefs, pending.changes)
            self._save_notification_prefs(updated)
            self._poller.answer_callback_query(cb_id, "Applied!")
            if msg_id:
                self._poller.edit_message(
                    msg_id,
                    f"\u2705 Applied notification changes.\n\n{pending.preview}",
                )

        elif data.startswith("notify_reject:"):
            proposal_id = data.split(":", 1)[1]
            pending = self._pending_notify_proposals.pop(proposal_id, None)
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
                feedback_id = self._record_context_feedback(pending, "rejected")
                prompt_id = self._poller.send_reply(
                    "Optional: reply with why you rejected this suggestion.",
                    reply_to_message_id=msg_id,
                    force_reply=True,
                )
                if prompt_id is not None:
                    with self._lock:
                        self._pending_rejection_reasons[prompt_id] = feedback_id
                    self._save_pending_reason_state()

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

            conn = open_db(self.db)
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
                with self._lock:
                    self._pending_feedback_reasons[prompt_id] = fb_id
                self._save_pending_reason_state()

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

            conn = open_db(self.db)
            deleted = delete_feedback(conn, feedback_id)
            self._drop_feedback_reason_prompts(feedback_id)
            if not deleted:
                self._poller.answer_callback_query(cb_id, "Feedback already removed.")
                return

            self._poller.answer_callback_query(cb_id, "Feedback removed.")
            if msg_id:
                label = FEEDBACK_CATEGORIES.get(category, category)
                restored = self._strip_feedback_label(msg.get("text", ""), label)
                buttons = feedback_keyboard(llm_call_id, message_type)
                self._poller.edit_message_with_keyboard(msg_id, restored, buttons)

        elif data.startswith("add_"):
            self._handle_add_callback(cb_id, data, msg_id)

    # ------------------------------------------------------------------
    # /add — manual activity entry
    # ------------------------------------------------------------------

    def _new_add_id(self) -> str:
        """Generate a short unique id for a PendingAdd entry."""
        self._add_counter += 1
        return f"a{self._add_counter}"

    def _cleanup_pending_adds(self) -> None:
        """Remove expired PendingAdd entries. Must hold self._lock."""
        now = time.monotonic()
        expired = [
            k
            for k, v in self._pending_adds.items()
            if now - v.created_at > _PENDING_ADD_TTL_S
        ]
        for k in expired:
            del self._pending_adds[k]

    def _handle_add_command(self, message_id: int) -> None:
        """Start the /add flow: show personalized workout type buttons."""
        from store import get_frequent_workout_types, open_db

        conn = open_db(self.db)
        try:
            types = get_frequent_workout_types(conn, limit=4)
        finally:
            conn.close()

        with self._lock:
            self._cleanup_pending_adds()
            add_id = self._new_add_id()

        # Build buttons: one per frequent type + Sleep + Cancel.
        rows: list[list[dict[str, str]]] = []
        type_row: list[dict[str, str]] = []
        for i, t in enumerate(types):
            label = f"{t['type']} ({t['count']}x)"
            type_row.append({"text": label, "callback_data": f"add_type:{add_id}:{i}"})
            if len(type_row) == 2:
                rows.append(type_row)
                type_row = []
        if type_row:
            rows.append(type_row)
        rows.append(
            [
                {"text": "\U0001f634 Sleep", "callback_data": f"add_sleep:{add_id}"},
                {"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"},
            ]
        )

        sent_id = self._poller.send_message_with_keyboard(
            "What would you like to add?",
            rows,
            reply_to_message_id=message_id,
        )

        pending = PendingAdd(
            step="pick_type",
            message_id=sent_id or 0,
            created_at=time.monotonic(),
            type_options=types,
        )
        with self._lock:
            self._pending_adds[add_id] = pending

    def _handle_add_callback(self, cb_id: str, data: str, msg_id: int | None) -> None:
        """Dispatch /add inline keyboard callbacks."""
        parts = data.split(":")
        action = parts[0]  # add_type, add_sleep, add_ok, etc.
        add_id = parts[1] if len(parts) > 1 else ""
        param = parts[2] if len(parts) > 2 else ""

        with self._lock:
            self._cleanup_pending_adds()
            pending = self._pending_adds.get(add_id)

        if pending is None:
            self._poller.answer_callback_query(cb_id, "This flow expired.")
            if msg_id:
                self._poller.edit_message(msg_id, "This /add flow has expired.")
            return

        if action == "add_type":
            self._add_handle_type(cb_id, add_id, pending, int(param), msg_id)
        elif action == "add_sleep":
            self._add_handle_sleep_start(cb_id, add_id, pending, msg_id)
        elif action == "add_ok":
            self._add_handle_confirm(cb_id, add_id, pending, msg_id)
        elif action == "add_dur":
            self._add_show_duration_picker(cb_id, add_id, pending, msg_id)
        elif action == "add_d":
            self._add_handle_duration(cb_id, add_id, pending, float(param), msg_id)
        elif action == "add_dt":
            self._add_handle_date(cb_id, add_id, pending, param, msg_id)
        elif action == "add_sd":
            self._add_handle_sleep_duration(
                cb_id, add_id, pending, float(param), msg_id
            )
        elif action == "add_undo":
            self._add_handle_undo(cb_id, add_id, pending, msg_id)
        elif action == "add_x":
            self._add_handle_cancel(cb_id, add_id, msg_id)
        else:
            self._poller.answer_callback_query(cb_id, "Unknown action.")

    def _add_handle_type(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        type_index: int,
        msg_id: int | None,
    ) -> None:
        """User picked a workout type — run LLM clone and show confirmation."""
        self._poller.answer_callback_query(cb_id)
        if not pending.type_options or type_index >= len(pending.type_options):
            return

        chosen = pending.type_options[type_index]
        today = date.today().isoformat()

        # Show a "finding match" message while the LLM works.
        if msg_id:
            self._poller.edit_message(
                msg_id, f"\u23f3 Finding best match for {chosen['type']}..."
            )

        from store import open_db

        conn = open_db(self.db)
        try:
            clone = self._find_workout_clone(conn, chosen["type"], chosen["category"])
        finally:
            conn.close()

        pending.workout_type = chosen["type"]
        pending.category = chosen["category"]
        pending.clone_row = clone
        pending.date = today
        pending.step = "confirm_workout"

        self._add_show_workout_confirm(add_id, pending, msg_id)

    def _check_existing_workout(self, target_date: str, workout_type: str) -> str:
        """Check for an existing workout of the same type on the target date.

        Returns:
            A warning string if a duplicate exists, empty string otherwise.
        """
        from store import open_db

        conn = open_db(self.db)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM workout_all WHERE date = ? AND type = ?",
                (target_date, workout_type),
            ).fetchone()
            if row and row["n"] > 0:
                return (
                    f"\u26a0\ufe0f A {workout_type} already exists for {target_date}.\n"
                )
            return ""
        finally:
            conn.close()

    def _check_existing_sleep(self, target_date: str) -> str:
        """Check for existing sleep data on the target date.

        Returns:
            A warning string if sleep data exists, empty string otherwise.
        """
        from store import open_db

        conn = open_db(self.db)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sleep_all WHERE date = ?",
                (target_date,),
            ).fetchone()
            if row and row["n"] > 0:
                return f"\u26a0\ufe0f Sleep data already exists for {target_date} — saving will replace it.\n"
            return ""
        finally:
            conn.close()

    def _add_show_workout_confirm(
        self, add_id: str, pending: PendingAdd, msg_id: int | None
    ) -> None:
        """Display the workout confirmation screen with Save/Adjust/Date buttons."""
        clone = pending.clone_row or {}
        dur = clone.get("duration_min", 0)
        energy = clone.get("active_energy_kj")
        dist = clone.get("gpx_distance_km")
        note = clone.get("source_note", "")

        parts = [f"**{pending.workout_type}** — {dur:.0f} min"]
        if dist:
            parts.append(f"{dist:.1f} km")
        if energy:
            parts.append(f"~{energy:.0f} kJ")
        summary = ", ".join(parts)

        warning = self._check_existing_workout(
            pending.date or "", pending.workout_type or ""
        )
        text = f"{warning}{summary}\nDate: {pending.date}"
        if note:
            text += f"\n_{note}_"

        buttons = [
            [
                {"text": "\u2705 Save", "callback_data": f"add_ok:{add_id}"},
                {"text": "\u23f1 Duration", "callback_data": f"add_dur:{add_id}"},
            ],
            [
                {"text": "\U0001f4c5 Date", "callback_data": f"add_dt:{add_id}:pick"},
                {"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"},
            ],
        ]
        if msg_id:
            self._poller.edit_message_with_keyboard(msg_id, text, buttons)

    def _add_show_duration_picker(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        msg_id: int | None,
    ) -> None:
        """Show duration adjustment buttons based on workout category."""
        self._poller.answer_callback_query(cb_id)
        cat = pending.clone_row.get("category", "") if pending.clone_row else ""
        if cat in ("run", "walk", "cycle"):
            durations = [15, 20, 30, 45, 60, 90]
        else:
            durations = [20, 30, 45, 60, 75, 90]

        rows: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for d in durations:
            row.append({"text": f"{d} min", "callback_data": f"add_d:{add_id}:{d}"})
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"}])

        if msg_id:
            self._poller.edit_message_with_keyboard(
                msg_id, f"Pick duration for {pending.workout_type}:", rows
            )

    def _add_handle_duration(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        new_dur: float,
        msg_id: int | None,
    ) -> None:
        """User picked a new duration — scale energy/distance proportionally."""
        self._poller.answer_callback_query(cb_id)
        clone = pending.clone_row
        if clone:
            old_dur = clone.get("duration_min") or new_dur
            if old_dur and old_dur > 0:
                ratio = new_dur / old_dur
                if clone.get("active_energy_kj"):
                    clone["active_energy_kj"] = round(
                        clone["active_energy_kj"] * ratio, 1
                    )
                if clone.get("gpx_distance_km"):
                    clone["gpx_distance_km"] = round(
                        clone["gpx_distance_km"] * ratio, 2
                    )
            clone["duration_min"] = new_dur
            # Strip any previous adjustment suffix before adding the new one.
            note = clone.get("source_note", "")
            if ", adjusted to " in note:
                note = note[: note.index(", adjusted to ")]
            clone["source_note"] = (
                f"{note}, adjusted to {new_dur:.0f} min"
                if note
                else f"adjusted to {new_dur:.0f} min"
            )

        pending.step = "confirm_workout"
        self._add_show_workout_confirm(add_id, pending, msg_id)

    def _add_handle_date(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        param: str,
        msg_id: int | None,
    ) -> None:
        """Handle date selection — either show picker or apply a choice."""
        self._poller.answer_callback_query(cb_id)
        from datetime import timedelta

        today = date.today()
        if param == "pick":
            # Show date picker buttons.
            buttons = [
                [
                    {"text": "Today", "callback_data": f"add_dt:{add_id}:today"},
                    {"text": "Yesterday", "callback_data": f"add_dt:{add_id}:yest"},
                    {"text": "2 days ago", "callback_data": f"add_dt:{add_id}:bfr"},
                ],
                [{"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"}],
            ]
            if msg_id:
                self._poller.edit_message_with_keyboard(
                    msg_id, "When did you do it?", buttons
                )
            return

        if param == "today":
            pending.date = today.isoformat()
        elif param == "yest":
            pending.date = (today - timedelta(days=1)).isoformat()
        elif param == "bfr":
            pending.date = (today - timedelta(days=2)).isoformat()

        # Return to the appropriate confirm screen.
        if pending.step in ("confirm_workout", "pick_type"):
            pending.step = "confirm_workout"
            self._add_show_workout_confirm(add_id, pending, msg_id)
        else:
            self._add_show_sleep_duration_picker(add_id, pending, msg_id)

    def _add_handle_sleep_start(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        msg_id: int | None,
    ) -> None:
        """User chose Sleep — show date picker."""
        self._poller.answer_callback_query(cb_id)
        pending.step = "pick_sleep_date"

        # Sleep is stored under the night-start date (DB convention):
        # "Last night" on a Tuesday = Monday night = date Monday = yesterday.
        buttons = [
            [
                {"text": "Last night", "callback_data": f"add_dt:{add_id}:yest"},
                {"text": "Night before", "callback_data": f"add_dt:{add_id}:bfr"},
            ],
            [{"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"}],
        ]
        if msg_id:
            self._poller.edit_message_with_keyboard(
                msg_id,
                "When was this sleep?\n_(date = night-start, e.g. Mon night = Mon)_",
                buttons,
            )

    def _add_show_sleep_duration_picker(
        self, add_id: str, pending: PendingAdd, msg_id: int | None
    ) -> None:
        """Show sleep duration range buttons."""
        pending.step = "pick_sleep_dur"
        durations = [5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0]
        rows: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for h in durations:
            label = f"{h:.1f}h" if h != int(h) else f"{int(h)}h"
            row.append({"text": label, "callback_data": f"add_sd:{add_id}:{h}"})
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"}])

        if msg_id:
            self._poller.edit_message_with_keyboard(
                msg_id, f"Roughly how long did you sleep? (date: {pending.date})", rows
            )

    def _add_handle_sleep_duration(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        hours: float,
        msg_id: int | None,
    ) -> None:
        """User picked sleep duration — show confirmation."""
        self._poller.answer_callback_query(cb_id)
        pending.sleep_total_h = hours
        pending.sleep_in_bed_h = round(hours * 1.08, 2)
        pending.step = "confirm_sleep"

        warning = self._check_existing_sleep(pending.date or "")
        text = f"{warning}**Sleep** — {hours}h\nDate: {pending.date}"
        buttons = [
            [
                {"text": "\u2705 Save", "callback_data": f"add_ok:{add_id}"},
                {"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"},
            ]
        ]
        if msg_id:
            self._poller.edit_message_with_keyboard(msg_id, text, buttons)

    def _add_handle_confirm(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        msg_id: int | None,
    ) -> None:
        """Save the manual activity and show confirmation with undo."""
        self._poller.answer_callback_query(cb_id, "Saved!")
        from store import insert_manual_sleep, insert_manual_workout, open_db

        conn = open_db(self.db)
        try:
            if pending.step == "confirm_workout" and pending.clone_row:
                row_id = insert_manual_workout(
                    conn,
                    clone_row=pending.clone_row,
                    date=pending.date or date.today().isoformat(),
                    source_note=pending.clone_row.get("source_note"),
                )
                pending.saved_id = row_id
                pending.saved_table = "manual_workout"

                clone = pending.clone_row
                dur = clone.get("duration_min", 0)
                text = f"\u2705 Saved: {pending.workout_type} \u00b7 {dur:.0f} min \u00b7 {pending.date}"
            elif pending.step == "confirm_sleep" and pending.sleep_total_h:
                row_id = insert_manual_sleep(
                    conn,
                    date=pending.date or date.today().isoformat(),
                    sleep_total_h=pending.sleep_total_h,
                    sleep_in_bed_h=pending.sleep_in_bed_h,
                )
                pending.saved_id = row_id
                pending.saved_table = "manual_sleep"
                text = f"\u2705 Saved: Sleep \u00b7 {pending.sleep_total_h}h \u00b7 {pending.date}"
            else:
                self._poller.answer_callback_query(cb_id, "Nothing to save.")
                return
        finally:
            conn.close()

        buttons = [[{"text": "\u21a9 Undo", "callback_data": f"add_undo:{add_id}"}]]
        if msg_id:
            self._poller.edit_message_with_keyboard(msg_id, text, buttons)

    def _add_handle_undo(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        msg_id: int | None,
    ) -> None:
        """Undo a saved manual activity."""
        from store import delete_manual_sleep, delete_manual_workout, open_db

        deleted = False
        conn = open_db(self.db)
        try:
            if pending.saved_table == "manual_workout" and pending.saved_id:
                deleted = delete_manual_workout(conn, pending.saved_id)
            elif pending.saved_table == "manual_sleep" and pending.date:
                deleted = delete_manual_sleep(conn, pending.date)
        finally:
            conn.close()

        with self._lock:
            self._pending_adds.pop(add_id, None)

        if deleted:
            self._poller.answer_callback_query(cb_id, "Undone!")
            if msg_id:
                self._poller.edit_message(msg_id, "\u21a9 Undone.")
                self._poller.edit_message_reply_markup(msg_id, None)
        else:
            self._poller.answer_callback_query(cb_id, "Nothing to undo.")
            if msg_id:
                self._poller.edit_message(msg_id, "Nothing to undo — already removed.")
                self._poller.edit_message_reply_markup(msg_id, None)

    def _add_handle_cancel(self, cb_id: str, add_id: str, msg_id: int | None) -> None:
        """Cancel the /add flow."""
        with self._lock:
            self._pending_adds.pop(add_id, None)
        self._poller.answer_callback_query(cb_id, "Cancelled.")
        if msg_id:
            self._poller.edit_message(msg_id, "Cancelled.")
            self._poller.edit_message_reply_markup(msg_id, None)

    def _find_workout_clone(
        self,
        conn: sqlite3.Connection,
        workout_type: str,
        category: str,
    ) -> dict:
        """Find the best historical workout to clone via LLM.

        Queries recent workouts and asks a lightweight LLM to pick (or
        synthesize) the best match for the requested type.  Falls back to
        the most recent same-type workout on LLM failure.

        Args:
            conn: Open database connection.
            workout_type: Requested workout type name (e.g. "Outdoor Run").
            category: Workout category (e.g. "run").

        Returns:
            Dict with workout column values suitable for ``insert_manual_workout``.
        """
        from llm import call_llm
        from store import _WORKOUT_CLONE_COLUMNS

        # Fetch recent workouts for context.
        rows = conn.execute(
            """
            SELECT * FROM workout_all
            ORDER BY date DESC
            LIMIT 20
            """,
        ).fetchall()

        if not rows:
            return {
                "type": workout_type,
                "category": category,
                "duration_min": 30,
                "source_note": "default (no history)",
            }

        # Format workout history compactly for the LLM.
        history = []
        for r in rows:
            entry: dict = {}
            for col in _WORKOUT_CLONE_COLUMNS:
                val = r[col] if col in r.keys() else None
                if val is not None:
                    entry[col] = val
            entry["date"] = r["date"]
            history.append(entry)

        import json as _json

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a fitness data assistant. The user wants to manually "
                    "log a workout. Based on their recent history, pick the single "
                    "best workout to clone as a template, or synthesize one from "
                    "partial matches if no exact type match exists.\n\n"
                    "Return ONLY a JSON object with these fields:\n"
                    + ", ".join(_WORKOUT_CLONE_COLUMNS)
                    + ", source_note\n\n"
                    "source_note should briefly explain your choice "
                    '(e.g. "cloned from Apr 1 Outdoor Run" or '
                    '"scaled from 5K tempo to 2K distance").\n\n'
                    "Rules:\n"
                    "- Copy HR and sensor fields from the source workout as-is.\n"
                    "- If scaling duration/distance, scale active_energy_kj proportionally.\n"
                    "- counts_as_lift should be 1 for strength workouts, 0 otherwise.\n"
                    "- Return valid JSON only, no markdown fences."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Log this workout: {workout_type} (category: {category})\n\n"
                    f"Recent history ({len(history)} workouts):\n"
                    f"{_json.dumps(history, default=str)}"
                ),
            },
        ]

        try:
            result = call_llm(
                messages,
                model="anthropic/claude-haiku-4-5-20251001",
                max_tokens=512,
                temperature=0.2,
                conn=conn,
                request_type="add_clone",
            )
            # Parse JSON from response (strip markdown fences if present).
            text = result.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            clone = _json.loads(text)
            # Ensure required fields are present.
            clone.setdefault("type", workout_type)
            clone.setdefault("category", category)
            clone.setdefault("duration_min", 30)
            return clone
        except Exception:
            logger.warning(
                "LLM clone failed for %s, falling back to most recent",
                workout_type,
                exc_info=True,
            )
            # Fallback: most recent same-type, then same-category, then any.
            for r in rows:
                if r["type"] == workout_type:
                    return {
                        col: r[col] if col in r.keys() else None
                        for col in _WORKOUT_CLONE_COLUMNS
                    } | {"source_note": f"most recent {workout_type}"}
            for r in rows:
                if r["category"] == category:
                    return {
                        col: r[col] if col in r.keys() else None
                        for col in _WORKOUT_CLONE_COLUMNS
                    } | {
                        "type": workout_type,
                        "source_note": f"most recent {category} workout",
                    }
            first = rows[0]
            return {
                col: first[col] if col in first.keys() else None
                for col in _WORKOUT_CLONE_COLUMNS
            } | {
                "type": workout_type,
                "category": category,
                "source_note": "most recent workout",
            }

    def _chat_reply(
        self, conn: sqlite3.Connection
    ) -> tuple["LLMResult", list, list[dict]]:
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
        from llm import (
            build_llm_data,
            build_messages,
            call_llm,
            load_context,
            slim_for_prompt,
        )
        from tools import all_chat_tools, execute_tool

        ctx = load_context(self.context_dir, prompt_file="chat_prompt")

        # Inject recent nudge history so the LLM knows what it recently sent.
        recent = self._state.get("recent_nudges", [])
        if recent:
            ctx["recent_nudges"] = "\n".join(
                f"{i + 1}. [{e['ts'][:16]} / {e['trigger']}] {e['text']}"
                for i, e in enumerate(recent)
            )

        # Inject last coach review for cross-message awareness.
        coach_summary = self._state.get("last_coach_summary", "")
        coach_date = self._state.get("last_coach_summary_date", "")
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
            health_data_json=_json.dumps(slim_for_prompt(health_data), default=str),
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

        for _iteration in range(MAX_TOOL_ITERATIONS):
            result = call_llm(
                messages,
                model=self.model,
                tools=tools,
                conn=conn,
                request_type="chat",
                max_tokens=1024,
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
                    tool_result = execute_tool(fn_name, args, self.db)
                    # Accumulate query rows for chart rendering.
                    if fn_name == "run_sql":
                        try:
                            parsed = _json.loads(tool_result)
                            if isinstance(parsed, list):
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
                                {"error": "tool budget exhausted, answer now"}
                            ),
                        }
                    )
            result = call_llm(
                messages,
                model=self.model,
                tools=None,
                conn=conn,
                request_type="chat",
                max_tokens=1024,
                metadata={"iteration": "final_synthesis"},
            )

        return result, deferred_edits, query_rows

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
                    self._drain_quiet_queue()
                elif nudge_decision["status"] == "suppressed":
                    logger.info(
                        "Dropping queued nudges due to notification prefs: %s",
                        nudge_decision.get("reason", "unknown"),
                    )
                    self._drop_queued_nudges()

            if scheduled_report_due(prefs, "weekly_insights", now=now):
                self._run_weekly_report()

            if scheduled_report_due(prefs, "midweek_report", now=now):
                self._run_midweek_report()

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
                        self._run_nudge(
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

        self._start_telegram_poller()

        logger.info("Daemon running — press Ctrl+C to stop")
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
