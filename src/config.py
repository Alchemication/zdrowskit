"""Shared paths, limits, model routing, and daemon configuration.

This module is the central place for runtime knobs. Most model and verification
settings can be overridden with environment variables; see README.md for the
user-facing list.

Public groups:
    Paths: AUTOEXPORT_DATA_DIR, CONTEXT_DIR, NOTIFICATION_PREFS_PATH,
        PROMPTS_DIR, REPORTS_DIR, NUDGES_DIR.
    Prompt/context limits: MAX_HISTORY_ENTRIES, MAX_LOG_ENTRIES,
        MAX_COACH_FEEDBACK_ENTRIES, MAX_CONVERSATION_MESSAGES,
        MAX_TOOL_ITERATIONS*, MAX_TOKENS*.
    Model routing: DEEPSEEK_*_MODEL, ANTHROPIC_*_MODEL, PRIMARY_*_MODEL,
        FALLBACK_*_MODEL, DEFAULT_*_MODEL, FALLBACK_MODEL.
    Verification: ENABLE_LLM_VERIFICATION, VERIFY_*, VERIFICATION_MODEL,
        VERIFICATION_REWRITE_MODEL, MAX_VERIFICATION_REVISIONS.
    Daemon: LOG_FILE, LOCK_FILE, STATE_FILE, debounce windows, nudge limits,
        report cadence, and suppression timing.
    Helpers: resolve_data_dir.

Example:
    from config import CONTEXT_DIR, resolve_data_dir
    data = resolve_data_dir(args.data_dir)
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

APP_HOME: Path = Path(
    os.environ.get("ZDROWSKIT_HOME", "~/Documents/zdrowskit")
).expanduser()
"""Root directory for user-owned zdrowskit state and context files."""

AUTOEXPORT_DATA_DIR: Path = (
    Path.home()
    / "Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents"
)
"""iCloud path where Auto Export app exports land."""
CONTEXT_DIR: Path = APP_HOME / "ContextFiles"
NOTIFICATION_PREFS_PATH: Path = APP_HOME / "notification_prefs.json"
MODEL_PREFS_PATH: Path = APP_HOME / "model_prefs.json"
PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"
REPORTS_DIR: Path = APP_HOME / "Reports"
NUDGES_DIR: Path = APP_HOME / "Nudges"


def _env_bool(name: str, default: bool) -> bool:
    """Return a bool from an environment variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Return an int from an environment variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return int(raw.strip())


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

MAX_TOOL_ITERATIONS: int = 8
"""Maximum tool-call loop iterations for the chat path. Chat is conversational
and often needs a few drill-down queries or chart repair turns in a single
conversation."""

MAX_TOOL_ITERATIONS_INSIGHTS: int = 12
"""Maximum tool-call loop iterations for the weekly insights report. Multi-step
analysis (per-day pulls + cross-checks) is async and legitimately needs more
headroom."""

MAX_TOOL_ITERATIONS_COACH: int = 12
"""Maximum tool-call loop iterations for the coaching review. Same multi-step
analysis pattern as insights — pull data, spot outlier, verify."""

MAX_TOOL_ITERATIONS_NUDGE: int = 5
"""Maximum tool-call loop iterations for nudges. Nudges are async, but should
still stay focused on one small actionable observation."""

MAX_TOKENS_DEFAULT: int = _env_int("ZDROWSKIT_MAX_TOKENS_DEFAULT", 4096)
"""Default output token budget for uncategorised LLM calls."""

MAX_TOKENS_INSIGHTS: int = _env_int("ZDROWSKIT_MAX_TOKENS_INSIGHTS", 8192)
"""Output token budget for weekly insights reports. Reports are async and can
include chart code, so the cap is higher than interactive chat."""

MAX_TOKENS_COACH: int = _env_int("ZDROWSKIT_MAX_TOKENS_COACH", 8192)
"""Output token budget for coaching reviews. Coach runs are async and may need
room for narrative plus context-edit proposals."""

MAX_TOKENS_CHAT: int = _env_int("ZDROWSKIT_MAX_TOKENS_CHAT", 4096)
"""Output token budget for interactive chat. Kept responsive, but high enough
for chart-generating answers."""

MAX_TOKENS_NUDGE: int = _env_int("ZDROWSKIT_MAX_TOKENS_NUDGE", 4096)
"""Output token budget for nudges. Nudges should be short, but tool-repair
turns need enough room to finish cleanly."""

MAX_TOKENS_NOTIFY: int = _env_int("ZDROWSKIT_MAX_TOKENS_NOTIFY", 512)
"""Output token budget for /notify preference interpretation."""

MAX_TOKENS_LOG_FLOW: int = _env_int("ZDROWSKIT_MAX_TOKENS_LOG_FLOW", 4096)
"""Output token budget for /log tap-flow generation and follow-up steps."""

MAX_TOKENS_ADD_CLONE: int = _env_int("ZDROWSKIT_MAX_TOKENS_ADD_CLONE", 512)
"""Output token budget for /add historical workout clone selection."""

MAX_TOKENS_VERIFICATION: int = _env_int("ZDROWSKIT_MAX_TOKENS_VERIFICATION", 4096)
"""Output token budget for evidence-bound verifier passes."""

MAX_TOKENS_VERIFICATION_REWRITE: int = _env_int(
    "ZDROWSKIT_MAX_TOKENS_VERIFICATION_REWRITE",
    4096,
)
"""Output token budget for bounded verification rewrites."""

DEEPSEEK_PRO_MODEL: str = os.environ.get(
    "ZDROWSKIT_DEEPSEEK_PRO_MODEL",
    "deepseek/deepseek-v4-pro",
)
"""Primary high-capability DeepSeek model used by feature defaults."""

DEEPSEEK_FLASH_MODEL: str = os.environ.get(
    "ZDROWSKIT_DEEPSEEK_FLASH_MODEL",
    "deepseek/deepseek-v4-flash",
)
"""Lower-cost DeepSeek model used by lightweight feature defaults."""

DEEPSEEK_THINKING: str = os.environ.get(
    "ZDROWSKIT_DEEPSEEK_THINKING",
    "enabled",
).strip()
"""Default DeepSeek V4 thinking mode for app calls: 'disabled', 'enabled', or empty."""

DEEPSEEK_EXTRA_BODY: dict[str, object] | None = (
    {"thinking": {"type": DEEPSEEK_THINKING}}
    if DEEPSEEK_THINKING in {"enabled", "disabled"}
    else None
)
"""Default provider-specific request body extras for DeepSeek model attempts."""

ANTHROPIC_OPUS_MODEL: str = os.environ.get(
    "ZDROWSKIT_ANTHROPIC_OPUS_MODEL",
    "anthropic/claude-opus-4-6",
)
"""High-capability Anthropic fallback paired with DeepSeek Pro."""

ANTHROPIC_OPUS_4_7_MODEL: str = os.environ.get(
    "ZDROWSKIT_ANTHROPIC_OPUS_4_7_MODEL",
    "anthropic/claude-opus-4-7",
)
"""Low-latency high-capability Anthropic model used by the chat preset."""

ANTHROPIC_HAIKU_MODEL: str = os.environ.get(
    "ZDROWSKIT_ANTHROPIC_HAIKU_MODEL",
    "anthropic/claude-haiku-4-5",
)
"""Low-cost Anthropic fallback paired with DeepSeek Flash."""

PRIMARY_PRO_MODEL: str = os.environ.get(
    "ZDROWSKIT_PRIMARY_PRO_MODEL",
    DEEPSEEK_PRO_MODEL,
)
"""Primary high-capability model for Pro-class LLM tasks."""

FALLBACK_PRO_MODEL: str = os.environ.get(
    "ZDROWSKIT_FALLBACK_PRO_MODEL",
    ANTHROPIC_OPUS_MODEL,
)
"""Fallback high-capability model for Pro-class LLM tasks."""

PRIMARY_FLASH_MODEL: str = os.environ.get(
    "ZDROWSKIT_PRIMARY_FLASH_MODEL",
    DEEPSEEK_FLASH_MODEL,
)
"""Primary lower-cost model for Flash-class LLM tasks."""

FALLBACK_FLASH_MODEL: str = os.environ.get(
    "ZDROWSKIT_FALLBACK_FLASH_MODEL",
    ANTHROPIC_HAIKU_MODEL,
)
"""Fallback lower-cost model for Flash-class LLM tasks."""

DEFAULT_MODEL: str = os.environ.get("ZDROWSKIT_DEFAULT_MODEL", PRIMARY_PRO_MODEL)
"""General default model for uncategorised LLM calls."""

FALLBACK_MODEL: str = os.environ.get("ZDROWSKIT_FALLBACK_MODEL", FALLBACK_PRO_MODEL)
"""General fallback model paired with DEFAULT_MODEL."""

DEFAULT_INSIGHTS_MODEL: str = os.environ.get(
    "ZDROWSKIT_INSIGHTS_MODEL",
    ANTHROPIC_OPUS_4_7_MODEL,
)
"""Default model for weekly insights reports."""

DEFAULT_COACH_MODEL: str = os.environ.get(
    "ZDROWSKIT_COACH_MODEL",
    ANTHROPIC_OPUS_4_7_MODEL,
)
"""Default model for coaching review/proposal generation."""

DEFAULT_NUDGE_MODEL: str = os.environ.get(
    "ZDROWSKIT_NUDGE_MODEL",
    ANTHROPIC_OPUS_4_7_MODEL,
)
"""Default model for proactive nudges."""

DEFAULT_CHAT_MODEL: str = os.environ.get(
    "ZDROWSKIT_CHAT_MODEL",
    PRIMARY_FLASH_MODEL,
)
"""Default model for interactive Telegram chat."""

DEFAULT_NOTIFY_MODEL: str = os.environ.get(
    "ZDROWSKIT_NOTIFY_MODEL",
    PRIMARY_FLASH_MODEL,
)
"""Default model for /notify intent interpretation."""

DEFAULT_LOG_FLOW_MODEL: str = os.environ.get(
    "ZDROWSKIT_LOG_FLOW_MODEL",
    ANTHROPIC_HAIKU_MODEL,
)
"""Default model for /log tap-flow generation."""

DEFAULT_ADD_CLONE_MODEL: str = os.environ.get(
    "ZDROWSKIT_ADD_CLONE_MODEL",
    PRIMARY_FLASH_MODEL,
)
"""Default model for /add workout clone selection."""

ENABLE_LLM_VERIFICATION: bool = _env_bool("ZDROWSKIT_ENABLE_LLM_VERIFICATION", True)
"""Global feature flag for post-generation LLM verification.

Enabled by default for async LLM surfaces (insights, coach, nudges), where
latency is less important than avoiding weak or unsupported outputs. Set
``ZDROWSKIT_ENABLE_LLM_VERIFICATION=0`` to disable locally.
"""

VERIFY_INSIGHTS: bool = _env_bool("ZDROWSKIT_VERIFY_INSIGHTS", True)
"""When LLM verification is enabled, verify weekly insights reports."""

VERIFY_COACH: bool = _env_bool("ZDROWSKIT_VERIFY_COACH", True)
"""When LLM verification is enabled, verify coaching review bundles."""

VERIFY_NUDGE: bool = _env_bool("ZDROWSKIT_VERIFY_NUDGE", True)
"""When LLM verification is enabled, verify nudges before sending."""

VERIFICATION_MODEL: str = os.environ.get(
    "ZDROWSKIT_VERIFICATION_MODEL",
    PRIMARY_PRO_MODEL,
)
"""Model used for evidence-bound verifier passes."""

VERIFICATION_REWRITE_MODEL: str = os.environ.get(
    "ZDROWSKIT_VERIFICATION_REWRITE_MODEL",
    PRIMARY_FLASH_MODEL,
)
"""Model used for bounded rewrites after verifier findings."""

MAX_VERIFICATION_REVISIONS: int = int(
    os.environ.get("ZDROWSKIT_MAX_VERIFICATION_REVISIONS", "1")
)
"""Maximum bounded rewrite attempts after a verifier returns revise."""

VERIFY_DEEPSEEK_THINKING: str = os.environ.get(
    "ZDROWSKIT_VERIFY_DEEPSEEK_THINKING",
    DEEPSEEK_THINKING,
).strip()
"""DeepSeek thinking mode for verifier calls: 'disabled', 'enabled', or empty."""

VERIFICATION_EXTRA_BODY: dict[str, object] | None = (
    {"thinking": {"type": VERIFY_DEEPSEEK_THINKING}}
    if VERIFY_DEEPSEEK_THINKING in {"enabled", "disabled"}
    else None
)
"""Provider-specific request body extras for verifier calls."""


# ---------------------------------------------------------------------------
# Daemon paths and timing
# ---------------------------------------------------------------------------

LOG_FILE: Path = Path.home() / "Library/Logs/zdrowskit.daemon.log"
"""Daemon log file (stderr/stdout sink under launchd)."""

LOCK_FILE: Path = APP_HOME / ".daemon.lock"
"""Single-instance lock file held by the daemon while running."""

STATE_FILE: Path = APP_HOME / ".daemon_state.json"
"""Persistent rate-limit and queue state for the daemon."""

HEALTH_DEBOUNCE_S: int = 180
"""Health-data debounce window: wait this long after the last .json modify
event before importing, so all sibling files have time to land via iCloud."""

CONTEXT_DEBOUNCE_S: int = 60
"""Context-file (.md) debounce window: collapse rapid edits into one fire."""

MAX_NUDGES_PER_DAY: int = 2
"""Hard cap on nudges per calendar day."""

MIN_NUDGE_INTERVAL_S: int = 3 * 60 * 60
"""Minimum gap between consecutive nudges."""

SCHEDULED_CHECK_INTERVAL_S: int = 30 * 60
"""How often the scheduled-check loop wakes to evaluate report cadence."""

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
