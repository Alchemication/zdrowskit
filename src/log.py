"""Console logging setup for zdrowskit.

Public API:
    setup_logging(verbose) -- configure root logger with a stderr handler
    quiet_noisy_loggers()  -- cap chatty third-party loggers at WARNING
    LevelFormatter         -- pads + optionally colorizes the level field
    LOG_FORMAT             -- shared format string used by CLI and daemon

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

LOG_FORMAT: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"
"""Shared log format. Levelname is padded to 8 chars inside the formatter so
the field stays aligned with or without color escape codes."""

_RESET = "\033[0m"

_LEVEL_COLORS: dict[int, str] = {
    logging.DEBUG: "\033[2;37m",  # dim white
    logging.INFO: "\033[36m",  # cyan
    logging.WARNING: "\033[33m",  # yellow
    logging.ERROR: "\033[31m",  # red
    logging.CRITICAL: "\033[1;31m",  # bold red
}

_NOISY_LOGGERS: tuple[str, ...] = (
    "urllib3",
    "urllib",
    "httpx",
    "httpcore",
    "watchdog",
    "anthropic",
    "openai",
)


class LevelFormatter(logging.Formatter):
    """Formatter that pads the level field and optionally colorizes it."""

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        use_color: bool = True,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        original_levelname = record.levelname
        padded = f"{record.levelname:<8}"
        if self._use_color:
            color = _LEVEL_COLORS.get(record.levelno, "")
            record.levelname = f"{color}{padded}{_RESET}"
        else:
            record.levelname = padded
        try:
            return super().format(record)
        finally:
            record.levelname = original_levelname


def quiet_noisy_loggers() -> None:
    """Cap chatty third-party loggers at WARNING.

    Stops urllib/watchdog/httpx/etc. from drowning out application output,
    especially at DEBUG level (e.g. ``--verbose``).
    """
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_logging(verbose: bool = False) -> None:
    """Configure the root logger with a stderr handler.

    Colors are emitted only when stderr is a TTY, so redirected output stays
    free of ANSI escape codes.

    Args:
        verbose: If True, set level to DEBUG; otherwise INFO.
    """
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(LevelFormatter(LOG_FORMAT, use_color=sys.stderr.isatty()))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    quiet_noisy_loggers()
