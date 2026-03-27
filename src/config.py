"""Shared paths and configuration resolution.

Public API:
    SHORTCUTS_DATA_DIR    — iCloud path for iOS Shortcuts health exports.
    AUTOEXPORT_DATA_DIR   — iCloud path for Auto Export app automation exports.
    CONTEXT_DIR           — directory containing user context files (me, goals, etc.).
    PROMPTS_DIR           — directory containing prompt templates (prompt.md, soul.md, etc.).
    REPORTS_DIR           — directory where generated reports are saved.
    NUDGES_DIR            — directory where sent nudges are saved.
    MAX_HISTORY_ENTRIES   — max entries kept in history.md.
    MAX_CONVERSATION_MESSAGES — max messages in the Telegram chat buffer.
    resolve_data_dir      — resolve data directory from CLI arg, env var, or source default.

Example:
    from config import resolve_data_dir, CONTEXT_DIR
    data = resolve_data_dir(args.data_dir, source="autoexport")
"""

from __future__ import annotations

import os
from pathlib import Path

SHORTCUTS_DATA_DIR: Path = (
    Path.home()
    / "Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/MyHealth"
)
"""iCloud path where iOS Shortcuts exports land (historical backfill)."""

AUTOEXPORT_DATA_DIR: Path = (
    Path.home()
    / "Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents"
)
"""iCloud path where Auto Export app automation exports land (ongoing sync)."""

_SOURCE_DEFAULTS: dict[str, Path] = {
    "shortcuts": SHORTCUTS_DATA_DIR,
    "autoexport": AUTOEXPORT_DATA_DIR,
}
CONTEXT_DIR: Path = Path.home() / "Documents" / "zdrowskit" / "ContextFiles"
PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"
REPORTS_DIR: Path = Path.home() / "Documents" / "zdrowskit" / "Reports"
NUDGES_DIR: Path = Path.home() / "Documents" / "zdrowskit" / "Nudges"

MAX_HISTORY_ENTRIES: int = 8
"""Maximum number of entries to retain in history.md."""

MAX_LOG_ENTRIES: int = 5
"""Maximum number of entries to inject from log.md into LLM prompts."""

MAX_CONVERSATION_MESSAGES: int = 20
"""Maximum number of messages to keep in the in-memory chat conversation buffer."""

EDITABLE_CONTEXT_FILES: set[str] = {"me", "goals", "plan", "log"}
"""Context file stems that may be updated via chat."""

AUTO_ACCEPT_CONTEXT_EDITS: bool = (
    os.environ.get("ZDROWSKIT_AUTO_ACCEPT_EDITS", "") == "1"
)
"""When True, apply context edits without confirmation."""

CHART_THEME: str = os.environ.get("ZDROWSKIT_CHART_THEME", "plotly_dark")
"""Plotly template for chart rendering (e.g. 'plotly_dark', 'plotly_white')."""

MAX_TOOL_ITERATIONS: int = 5
"""Maximum tool-call loop iterations in a single chat turn."""


def resolve_data_dir(arg: str | None, source: str = "autoexport") -> Path:
    """Resolve the data directory from CLI arg, env var, or source default.

    Priority: CLI --data-dir > HEALTH_DATA_DIR env var > source default.

    Args:
        arg: Value of the --data-dir CLI argument, or None if not provided.
        source: Data source format — "shortcuts" or "autoexport".

    Returns:
        An absolute Path to the resolved data directory.
    """
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("HEALTH_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return _SOURCE_DEFAULTS.get(source, AUTOEXPORT_DATA_DIR)
