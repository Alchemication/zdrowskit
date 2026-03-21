"""Shared paths and configuration resolution.

Public API:
    DEFAULT_DATA_DIR      — default Apple Health export directory.
    CONTEXT_DIR           — directory containing LLM context files.
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

DEFAULT_DATA_DIR: Path = Path.home() / "Documents" / "zdrowskit" / "MyHealth"
CONTEXT_DIR: Path = Path.home() / "Documents" / "zdrowskit" / "ContextFiles"
REPORTS_DIR: Path = Path.home() / "Documents" / "zdrowskit" / "Reports"
NUDGES_DIR: Path = Path.home() / "Documents" / "zdrowskit" / "Nudges"

MAX_HISTORY_ENTRIES: int = 8
"""Maximum number of entries to retain in history.md."""

MAX_CONVERSATION_MESSAGES: int = 20
"""Maximum number of messages to keep in the in-memory chat conversation buffer."""

EDITABLE_CONTEXT_FILES: set[str] = {"me", "goals", "plan", "log"}
"""Context file stems that may be updated via chat."""

AUTO_ACCEPT_CONTEXT_EDITS: bool = (
    os.environ.get("ZDROWSKIT_AUTO_ACCEPT_EDITS", "") == "1"
)
"""When True, apply context edits without confirmation."""


def resolve_data_dir(arg: str | None) -> Path:
    """Resolve the data directory from CLI arg, env var, or default path.

    Priority: CLI --data-dir > HEALTH_DATA_DIR env var > DEFAULT_DATA_DIR.

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
    return DEFAULT_DATA_DIR
