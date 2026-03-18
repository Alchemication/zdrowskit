"""Filesystem watcher daemon for zdrowskit.

Monitors iCloud health data files and context .md files, triggering
LLM-powered notifications when meaningful changes are detected.

Public API:
    main  — parse args and run the daemon loop

Example:
    uv run python src/daemon.py
    uv run python src/daemon.py --foreground
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sqlite3
import sys
import threading
import time
import types
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ICLOUD_HEALTH_DIR = (
    Path.home()
    / "Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/MyHealth"
)
LOG_FILE = Path.home() / "Library/Logs/zdrowskit.daemon.log"
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

MORNING_REPORT_HOUR_START = 8
MORNING_REPORT_HOUR_END = 9


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

    Triggers on modifications to log.md, goals.md, and plan.md only.
    Ignores me.md (auto-rewritten by baselines), soul.md, history.md,
    prompt.md, and nudge_prompt.md.

    Args:
        on_file_changed: Callable(stem: str) called with the file stem
            (e.g. "log", "goals", "plan").

    Returns:
        A watchdog FileSystemEventHandler instance.
    """
    from watchdog.events import FileSystemEventHandler

    WATCHED_STEMS = {"log", "goals", "plan"}

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
        self._lock = threading.Lock()
        self._health_timer: threading.Timer | None = None
        self._context_timers: dict[str, threading.Timer] = {}

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

        Args:
            stem: File stem that changed.
        """
        trigger_map = {
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

    def _can_send_nudge(self) -> bool:
        """Check whether a nudge is allowed under the rate limits.

        Returns:
            True if a nudge may be sent; False if suppressed.
        """
        today_str = date.today().isoformat()

        if self._state.get("nudge_date") != today_str:
            self._state["nudge_count_today"] = 0
            self._state["nudge_date"] = today_str

        if self._state.get("nudge_count_today", 0) >= MAX_NUDGES_PER_DAY:
            logger.info(
                "Nudge suppressed: daily limit (%d) reached", MAX_NUDGES_PER_DAY
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

    def _can_send_weekly_report(self) -> bool:
        """Check whether a weekly report is allowed (once per ISO week).

        Returns:
            True if report may be sent; False if already sent this week.
        """
        today = date.today()
        iso_week = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
        if self._state.get("last_weekly_report_week") == iso_week:
            logger.info("Weekly report suppressed: already sent for %s", iso_week)
            return False
        return True

    def _record_weekly_report(self) -> None:
        """Update state after a weekly report is sent."""
        today = date.today()
        iso_week = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
        self._state["last_weekly_report_week"] = iso_week
        _save_state(self._state)

    # ------------------------------------------------------------------
    # LLM calls
    # ------------------------------------------------------------------

    def _run_import(self) -> None:
        """Import the latest health data from the iCloud directory into the DB."""
        from commands import cmd_import

        args = types.SimpleNamespace(
            data_dir=str(ICLOUD_HEALTH_DIR),
            db=str(self.db),
        )
        try:
            logger.info("Importing health data from %s", ICLOUD_HEALTH_DIR)
            cmd_import(args)
        except SystemExit:
            logger.error("Import failed — proceeding with existing DB data")

    def _run_weekly_report(self) -> None:
        """Run the full weekly insights report and send via Telegram."""
        if not self._can_send_weekly_report():
            return

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
            logger.info("Running weekly report")
            cmd_insights(args)
            self._record_weekly_report()
        except SystemExit:
            logger.error("Weekly report command failed")

    def _run_nudge(self, trigger: str) -> None:
        """Run a nudge and send via Telegram.

        Passes recent nudge history so the LLM can decide whether there is
        anything new worth saying (SKIP if not).

        Args:
            trigger: Trigger type string passed to cmd_nudge.
        """
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
        )
        try:
            logger.info("Running nudge (trigger: %s)", trigger)
            result_text = cmd_nudge(args)
            if result_text:
                self._record_nudge(result_text, trigger)
        except SystemExit:
            logger.error("Nudge failed (trigger: %s)", trigger)

    # ------------------------------------------------------------------
    # Scheduled checks
    # ------------------------------------------------------------------

    def _scheduled_check_loop(self) -> None:
        """Background thread: periodic checks for morning reports and evening missed sessions."""
        while True:
            time.sleep(SCHEDULED_CHECK_INTERVAL_S)
            now = datetime.now()

            # Monday morning: send weekly report (uses Sunday's data)
            if (
                now.weekday() == 0
                and MORNING_REPORT_HOUR_START <= now.hour < MORNING_REPORT_HOUR_END
            ):
                self._run_weekly_report()

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

        logger.info("Daemon running — press Ctrl+C to stop")
        try:
            while observer.is_alive():
                observer.join(timeout=1)
        except KeyboardInterrupt:
            logger.info("Shutting down daemon")
        finally:
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
