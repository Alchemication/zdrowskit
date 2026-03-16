"""Colored console logging setup for zdrowskit.

Public API:
    setup_logging(verbose) -- configure root logger with a colored stderr handler

Example:
    from log import setup_logging
    import logging

    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("Loading data from %s", path)
"""

from __future__ import annotations
import logging
import sys

_RESET = "\033[0m"

_LEVEL_COLORS: dict[int, str] = {
    logging.DEBUG: "\033[2;37m",  # dim white
    logging.INFO: "\033[36m",  # cyan
    logging.WARNING: "\033[33m",  # yellow
    logging.ERROR: "\033[31m",  # red
    logging.CRITICAL: "\033[1;31m",  # bold red
}


class _ColorFormatter(logging.Formatter):
    """Formatter that prepends a colored level label to each log line."""

    def format(self, record: logging.LogRecord) -> str:
        color = _LEVEL_COLORS.get(record.levelno, "")
        original_levelname = record.levelname
        record.levelname = f"{color}{record.levelname:<8}{_RESET}"
        result = super().format(record)
        record.levelname = original_levelname
        return result


def setup_logging(verbose: bool = False) -> None:
    """Configure the root logger with a colored stderr handler.

    Args:
        verbose: If True, set level to DEBUG; otherwise INFO.
    """
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ColorFormatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
