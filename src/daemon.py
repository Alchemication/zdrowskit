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
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from context_edit import ContextEdit
    from llm import LLMResult

logger = logging.getLogger(__name__)

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

MORNING_REPORT_HOUR_START = 8
MORNING_REPORT_HOUR_END = 9

MIDWEEK_REPORT_HOUR_START = 9
MIDWEEK_REPORT_HOUR_END = 10

COACH_DAY = 6  # Sunday
COACH_HOUR_START = 19
COACH_HOUR_END = 20


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
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._health_timer: threading.Timer | None = None
        self._context_timers: dict[str, threading.Timer] = {}
        self._context_fire_times: dict[str, float] = {}

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

    def _can_send_report(self, report_type: str) -> bool:
        """Check whether a report of the given type may be sent today.

        Args:
            report_type: "review" for full-week or "progress" for mid-week.

        Returns:
            True if report may be sent; False if already sent today.
        """
        key = f"last_{report_type}_date"
        today_str = date.today().isoformat()
        if self._state.get(key) == today_str:
            logger.info("%s report suppressed: already sent today", report_type)
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
            cmd_insights(args)
            self._record_report("review")
        except SystemExit:
            logger.error("Weekly review report failed")

    def _run_midweek_report(self) -> None:
        """Run a mid-week progress report and send via Telegram."""
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
            cmd_insights(args)
            self._record_report("progress")
        except SystemExit:
            logger.error("Mid-week progress report failed")

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

    def _run_coach(self) -> None:
        """Run a coaching review and send proposals via Telegram.

        Proposes concrete edits to plan.md / goals.md based on the
        week's data. Each proposal is sent as an inline Approve/Reject
        button. If the LLM proposes no changes, only the reasoning text
        is sent.
        """
        last_coach = self._state.get("last_coach_date", "")
        today_str = date.today().isoformat()
        if last_coach == today_str:
            logger.debug("Coach already ran today, skipping")
            return

        self._run_import()

        from commands import cmd_coach

        args = types.SimpleNamespace(
            db=str(self.db),
            model=self.model,
            email=False,
            telegram=True,
            week="current",
            months=3,
        )
        try:
            logger.info("Running coaching review")
            _text, edits = cmd_coach(args)
            for edit in edits:
                self._propose_context_edit(edit)
            self._state["last_coach_date"] = today_str
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

        message_id = message["message_id"]

        # Handle bot commands before the LLM.
        if text.startswith("/"):
            self._handle_command(text, message_id)
            return

        # If the user replied to a specific bot message, inject its text
        # so the LLM has the context of what they're responding to.
        reply_to = message.get("reply_to_message")
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
        if placeholder_id:
            self._poller.edit_message(placeholder_id, reply)
        else:
            self._poller.send_reply(reply, reply_to_message_id=message_id)

        # Propose any deferred context edits from the tool-calling loop.
        for edit in deferred_edits:
            self._propose_context_edit(edit)
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
        elif cmd == "/status":
            nudge_count = self._state.get("nudge_count_today", 0)
            buf_len = len(self._conversation)
            lines = [
                f"Buffer: {buf_len} messages",
                f"Nudges today: {nudge_count}/{MAX_NUDGES_PER_DAY}",
            ]
            last_nudge = self._state.get("last_nudge_ts")
            if last_nudge:
                lines.append(f"Last nudge: {last_nudge[:16]}")
            self._poller.send_reply("\n".join(lines), reply_to_message_id=message_id)
        elif cmd == "/context":
            parts = text.split()
            file_arg = parts[1] if len(parts) > 1 else None
            self._send_context_overview(message_id, file_arg)
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

    def _propose_context_edit(self, edit: "ContextEdit") -> None:
        """Send a context edit proposal or auto-apply it.

        Args:
            edit: The validated context edit extracted from the LLM response.
        """
        from config import AUTO_ACCEPT_CONTEXT_EDITS
        from context_edit import apply_edit

        if AUTO_ACCEPT_CONTEXT_EDITS:
            try:
                apply_edit(self.context_dir, edit)
                self._poller.send_reply(
                    f"\u2705 Updated {edit.file}.md: {edit.summary}"
                )
            except Exception:
                logger.error("Failed to auto-apply context edit", exc_info=True)
            return

        edit_id = self._pending_edits.store(edit)
        action_label = (
            "append to" if edit.action == "append" else f"replace {edit.section} in"
        )
        text = f"📝 {action_label} {edit.file}.md\n{edit.summary}"
        buttons = [
            [
                {"text": "\u2705 Accept", "callback_data": f"ctx_accept:{edit_id}"},
                {"text": "\u274c Reject", "callback_data": f"ctx_reject:{edit_id}"},
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
            edit = self._pending_edits.pop(edit_id)
            if edit:
                try:
                    apply_edit(self.context_dir, edit)
                    self._poller.answer_callback_query(cb_id, "Applied!")
                    if msg_id:
                        self._poller.edit_message(
                            msg_id, f"\u2705 Applied: {edit.summary}"
                        )
                except Exception:
                    logger.error("Failed to apply context edit", exc_info=True)
                    self._poller.answer_callback_query(cb_id, "Error applying edit.")
            else:
                self._poller.answer_callback_query(cb_id, "Expired or already handled.")
                if msg_id:
                    self._poller.edit_message(msg_id, "This edit has expired.")

        elif data.startswith("ctx_reject:"):
            edit_id = data.split(":", 1)[1]
            edit = self._pending_edits.pop(edit_id)
            self._poller.answer_callback_query(cb_id, "Discarded.")
            if msg_id:
                summary = edit.summary if edit else "unknown"
                self._poller.edit_message(msg_id, f"\u274c Discarded: {summary}")

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

        health_data = build_llm_data(conn, months=3)

        try:
            baselines = compute_baselines(conn)
        except Exception:
            logger.warning("Baselines computation failed", exc_info=True)
            baselines = None

        import json as _json

        messages = build_messages(
            ctx,
            health_data_json=_json.dumps(health_data, default=str),
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
        while True:
            time.sleep(SCHEDULED_CHECK_INTERVAL_S)
            now = datetime.now()

            # Monday morning: full week review (previous Mon–Sun)
            if (
                now.weekday() == 0
                and MORNING_REPORT_HOUR_START <= now.hour < MORNING_REPORT_HOUR_END
            ):
                self._run_weekly_report()

            # Thursday morning: mid-week progress check (current Mon–Thu)
            if (
                now.weekday() == 3
                and MIDWEEK_REPORT_HOUR_START <= now.hour < MIDWEEK_REPORT_HOUR_END
            ):
                self._run_midweek_report()

            # Sunday evening: coaching review — propose plan/goal updates
            if (
                now.weekday() == COACH_DAY
                and COACH_HOUR_START <= now.hour < COACH_HOUR_END
            ):
                self._run_coach()

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
