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
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from commands import CommandResult
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


def _make_context_handler(on_file_changed):  # type: ignore[no-untyped-def]
    """Build a watchdog FileSystemEventHandler for the context .md files dir.

    Triggers on modifications to user-editable context files: me.md,
    log.md, goals.md, and plan.md. Ignores auto-managed files
    (baselines.md, history.md) and prompt templates.

    Args:
        on_file_changed: Callable(stem: str) called with the file stem
            (e.g. "log", "goals", "plan", "me").

    Returns:
        A watchdog FileSystemEventHandler instance.
    """
    from watchdog.events import FileSystemEventHandler

    WATCHED_STEMS = {"me", "log", "goals", "plan"}

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event) -> None:  # type: ignore[override]
            if event.is_directory:
                return
            path = Path(event.src_path)
            if path.suffix != ".md":
                return
            if path.stem in WATCHED_STEMS:
                on_file_changed(path.stem)

    return _Handler()


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
            stem: File stem that changed (e.g. "log", "goals", "plan").
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
        self._run_import()
        self._run_nudge("new_data")

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
            "goals": "goal_updated",
            "plan": "plan_updated",
        }
        trigger = trigger_map.get(stem, "log_update")
        logger.info("Context trigger fired: %s.md → %s", stem, trigger)
        self._run_nudge(trigger)

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
        max_nudges_per_day = prefs.get("overrides", {}).get("nudges", {}).get(
            "max_per_day"
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
        )
        try:
            logger.info("Running weekly review report")
            result = cmd_insights(args)
            self._attach_feedback_button(result, "insights")
            self._record_report("review")
            self._state["last_report_ts"] = datetime.now().isoformat()
            _save_state(self._state)
            self._run_coach(week="last", skip_import=True)
        except SystemExit:
            logger.error("Weekly review report failed")

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
        )
        try:
            logger.info("Running mid-week progress report")
            result = cmd_insights(args)
            self._attach_feedback_button(result, "insights")
            self._record_report("progress")
            self._state["last_report_ts"] = datetime.now().isoformat()
            _save_state(self._state)
        except SystemExit:
            logger.error("Mid-week progress report failed")

    def _run_nudge(self, trigger: str, *, _from_drain: bool = False) -> None:
        """Run a nudge and send via Telegram.

        Passes recent nudge history so the LLM can decide whether there is
        anything new worth saying (SKIP if not).

        Args:
            trigger: Trigger type string passed to cmd_nudge.
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
        )
        try:
            logger.info("Running nudge (trigger: %s)", trigger)
            result = cmd_nudge(args)
            if result.text:
                self._record_nudge(result.text, trigger)
                self._attach_feedback_button(result, "nudge")
        except SystemExit:
            logger.error("Nudge failed (trigger: %s)", trigger)

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
            "goal_updated": 4,
            "plan_updated": 3,
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
        self._run_nudge(best["trigger"], _from_drain=True)

    def _run_coach(
        self,
        *,
        week: str = "last",
        skip_import: bool = False,
        force: bool = False,
    ) -> None:
        """Run a coaching review and send proposals via Telegram.

        Proposes concrete edits to plan.md / goals.md based on the
        week's data. Each proposal is sent as an inline Approve/Reject
        button. If the LLM proposes no changes, only the reasoning text
        is sent.
        """
        last_coach = self._state.get("last_coach_date", "")
        today_str = date.today().isoformat()
        if not force and last_coach == today_str:
            logger.debug("Coach already ran today, skipping")
            return

        if not skip_import:
            self._run_import()

        from commands import cmd_coach

        args = types.SimpleNamespace(
            db=str(self.db),
            model=self.model,
            email=False,
            telegram=True,
            week=week,
            months=3,
            recent_nudges=self._state.get("recent_nudges", []),
        )
        try:
            logger.info("Running coaching review")
            cmd_result, edits = cmd_coach(args)
            self._attach_feedback_button(cmd_result, "coach")
            for edit in edits:
                self._propose_context_edit(edit, source="coach")
            self._state["last_coach_date"] = today_str
            if cmd_result.text:
                self._state["last_coach_summary"] = cmd_result.text[:500]
                self._state["last_coach_summary_date"] = today_str
            _save_state(self._state)
        except SystemExit:
            logger.error("Coaching review failed")

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
        if message_type == "insights":
            self._poller.send_message_with_keyboard(
                "_Report feedback_: tap 👎 if something was off.",
                kb,
                reply_to_message_id=msg_id,
            )
            return
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

        # Send a placeholder so the user sees immediate feedback.
        placeholder_id = self._poller.send_placeholder(reply_to_message_id=message_id)

        try:
            from store import open_db

            conn = open_db(self.db)
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
        elif cmd == "/notify":
            args = text.split(maxsplit=1)
            request_text = args[1].strip() if len(args) > 1 else ""
            self._handle_notify_command(request_text, message_id)
        elif cmd == "/status":
            nudge_count = self._state.get("nudge_count_today", 0)
            prefs = self._load_notification_prefs(now=datetime.now().astimezone())
            from notification_prefs import effective_notification_prefs

            max_nudges = effective_notification_prefs(prefs)["nudges"]["max_per_day"]
            buf_len = len(self._conversation)
            lines = [
                f"Buffer: {buf_len} messages",
                f"Nudges today: {nudge_count}/{max_nudges}",
            ]
            last_nudge = self._state.get("last_nudge_ts")
            if last_nudge:
                lines.append(f"Last nudge: {last_nudge[:16]}")
            self._poller.send_reply("\n".join(lines), reply_to_message_id=message_id)
        elif cmd == "/context":
            parts = text.split()
            file_arg = parts[1] if len(parts) > 1 else None
            self._send_context_overview(message_id, file_arg)
        elif cmd == "/coach":
            self._poller.send_reply(
                "Running coaching review…", reply_to_message_id=message_id
            )
            self._run_coach(week="last", skip_import=False, force=True)
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
            lines = [
                f"/{c['command']} — {c['description']}" for c in TELEGRAM_BOT_COMMANDS
            ]
            lines.append(f"\n/context <name> — Show full file ({ctx_opts})")
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
            text = (
                f"{reason}\n\n"
                f"{format_notification_summary(
                    prefs,
                    now=now,
                    include_examples=True,
                    max_nudges_per_day=MAX_NUDGES_PER_DAY,
                )}"
            )
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
            logger.warning("Rejected invalid %s proposal: %s", source, exc)
            self._poller.send_reply(
                f"Skipped invalid {source} suggestion for {edit.file}.md: {exc}"
            )
            return

        if AUTO_ACCEPT_CONTEXT_EDITS:
            try:
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

        # If we exhausted iterations, return the last result.
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
                        self._run_nudge("missed_session")
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
                _make_context_handler(self._schedule_context),
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
