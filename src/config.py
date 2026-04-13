"""Shared paths and configuration resolution.

Public API:
    AUTOEXPORT_DATA_DIR   — iCloud path for Auto Export app automation exports.
    CONTEXT_DIR           — directory containing user context files (me, strategy, log).
    NOTIFICATION_PREFS_PATH — JSON file storing notification preference overrides.
    PROMPTS_DIR           — directory containing prompt templates (prompt.md, soul.md, etc.).
    REPORTS_DIR           — directory where generated reports are saved.
    NUDGES_DIR            — directory where sent nudges are saved.
    MAX_HISTORY_ENTRIES   — max entries kept in history.md.
    MAX_CONVERSATION_MESSAGES — max messages in the Telegram chat buffer.
    resolve_data_dir      — resolve data directory from CLI arg, env var, or default.

Example:
    from config import resolve_data_dir, CONTEXT_DIR
    data = resolve_data_dir(args.data_dir)
"""

from __future__ import annotations

import os
from pathlib import Path

AUTOEXPORT_DATA_DIR: Path = (
    Path.home()
    / "Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents"
)
"""iCloud path where Auto Export app exports land."""
CONTEXT_DIR: Path = Path.home() / "Documents" / "zdrowskit" / "ContextFiles"
NOTIFICATION_PREFS_PATH: Path = (
    Path.home() / "Documents" / "zdrowskit" / "notification_prefs.json"
)
PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"
REPORTS_DIR: Path = Path.home() / "Documents" / "zdrowskit" / "Reports"
NUDGES_DIR: Path = Path.home() / "Documents" / "zdrowskit" / "Nudges"

MAX_HISTORY_ENTRIES: int = 8
"""Maximum number of entries to retain in history.md."""

MAX_LOG_ENTRIES: int = 5
"""Maximum number of entries to inject from log.md into LLM prompts."""

MAX_COACH_FEEDBACK_ENTRIES: int = 8
"""Maximum number of entries to inject from coach_feedback.md into prompts."""

MAX_CONVERSATION_MESSAGES: int = 20
"""Maximum number of messages to keep in the in-memory chat conversation buffer."""

EDITABLE_CONTEXT_FILES: set[str] = {"me", "strategy", "log"}
"""Context file stems that may be updated via chat."""

AUTO_ACCEPT_CONTEXT_EDITS: bool = (
    os.environ.get("ZDROWSKIT_AUTO_ACCEPT_EDITS", "") == "1"
)
"""When True, apply context edits without confirmation."""

CHART_THEME: str = os.environ.get("ZDROWSKIT_CHART_THEME", "plotly_dark")
"""Plotly template for chart rendering (e.g. 'plotly_dark', 'plotly_white')."""

MAX_TOOL_ITERATIONS: int = 6
"""Maximum tool-call loop iterations for the chat path. Chat is conversational
and often needs a few drill-down queries in a single turn."""

MAX_TOOL_ITERATIONS_INSIGHTS: int = 8
"""Maximum tool-call loop iterations for the weekly insights report. Multi-step
analysis (per-day pulls + cross-checks) legitimately needs more headroom."""

MAX_TOOL_ITERATIONS_COACH: int = 8
"""Maximum tool-call loop iterations for the coaching review. Same multi-step
analysis pattern as insights — pull data, spot outlier, verify."""

MAX_TOOL_ITERATIONS_NUDGE: int = 3
"""Maximum tool-call loop iterations for nudges. Kept tight on purpose — nudges
must be quick and a single targeted query is usually enough."""


# ---------------------------------------------------------------------------
# Daemon paths and timing
# ---------------------------------------------------------------------------

LOG_FILE: Path = Path.home() / "Library/Logs/zdrowskit.daemon.log"
"""Daemon log file (stderr/stdout sink under launchd)."""

LOCK_FILE: Path = Path.home() / "Documents/zdrowskit/.daemon.lock"
"""Single-instance lock file held by the daemon while running."""

STATE_FILE: Path = Path.home() / "Documents/zdrowskit/.daemon_state.json"
"""Persistent rate-limit and queue state for the daemon."""

HEALTH_DEBOUNCE_S: int = 180
"""Health-data debounce window: wait this long after the last .json modify
event before importing, so all sibling files have time to land via iCloud."""

CONTEXT_DEBOUNCE_S: int = 60
"""Context-file (.md) debounce window: collapse rapid edits into one fire."""

MAX_NUDGES_PER_DAY: int = 3
"""Hard cap on nudges per calendar day."""

MIN_NUDGE_INTERVAL_S: int = 90 * 60
"""Minimum gap between consecutive nudges."""

TRAINING_DAYS: set[int] = {0, 1, 2, 3, 4, 5, 6}
"""Weekdays (Mon=0..Sun=6) eligible for nudges. Currently every day —
the user catches up on weekends."""

SCHEDULED_CHECK_INTERVAL_S: int = 30 * 60
"""How often the scheduled-check loop wakes to evaluate report cadence."""

EVENING_HOUR_START: int = 20
"""Inclusive lower bound (24h) of the evening nudge window."""

EVENING_HOUR_END: int = 21
"""Exclusive upper bound (24h) of the evening nudge window."""

COACH_SUPPRESSION_S: int = 3600
"""±1 hour suppression around scheduled reports — no nudges fire inside
this window so the report itself can land first."""


def resolve_data_dir(arg: str | None) -> Path:
    """Resolve the data directory from CLI arg, env var, or default.

    Priority: CLI --data-dir > HEALTH_DATA_DIR env var > AUTOEXPORT_DATA_DIR.

    Args:
        arg: Value of the --data-dir CLI argument, or None if not provided.

    Returns:
        An absolute Path to the resolved data directory.
    """
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("HEALTH_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return AUTOEXPORT_DATA_DIR
