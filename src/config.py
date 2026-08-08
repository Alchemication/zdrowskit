"""Shared paths, limits, model routing, and daemon configuration.

This module is the central place for runtime knobs. Most model and verification
settings can be overridden with environment variables; see README.md for the
user-facing list.

Public groups:
    Paths: AUTOEXPORT_DATA_DIR, CONTEXT_DIR, NOTIFICATION_PREFS_PATH,
        GOOGLE_DRIVE_DATA_DIR, HTTP_INGEST_TOKEN_FILE, PROMPTS_DIR, REPORTS_DIR,
        NUDGES_DIR.
    Prompt/context limits: MAX_HISTORY_ENTRIES, MAX_LOG_ENTRIES,
        MAX_COACH_FEEDBACK_ENTRIES, MAX_CONVERSATION_MESSAGES,
        MAX_TOOL_ITERATIONS*, MAX_TOKENS*.
    Model routing: DEEPSEEK_*_MODEL, ANTHROPIC_*_MODEL, PRIMARY_*_MODEL,
        FALLBACK_*_MODEL, DEFAULT_*_MODEL, FALLBACK_MODEL.
    Verification: ENABLE_LLM_VERIFICATION, VERIFY_*, VERIFICATION_MODEL,
        VERIFICATION_REWRITE_MODEL, MAX_VERIFICATION_REVISIONS.
    Daemon: LOG_FILE, LOCK_FILE, debounce windows, nudge limits,
        report cadence, Google Drive polling, and suppression timing.
    Helpers: resolve_data_dir, resolve_google_drive_data_dir.

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
GOOGLE_DRIVE_DATA_DIR: Path = APP_HOME / "Imports" / "google-drive"
"""Default local cache for Google Drive Auto Export files."""
IMPORT_SOURCE: str = os.environ.get("ZDROWSKIT_IMPORT_SOURCE", "local").strip()
"""Health import transport: ``http``, ``local``, or ``google-drive``."""
GOOGLE_DRIVE_SERVICE_ACCOUNT: str | None = os.environ.get(
    "ZDROWSKIT_GOOGLE_DRIVE_SERVICE_ACCOUNT"
)
GOOGLE_DRIVE_METRICS_FOLDER_ID: str | None = os.environ.get(
    "ZDROWSKIT_GOOGLE_DRIVE_METRICS_FOLDER_ID"
)
GOOGLE_DRIVE_WORKOUTS_FOLDER_ID: str | None = os.environ.get(
    "ZDROWSKIT_GOOGLE_DRIVE_WORKOUTS_FOLDER_ID"
)
CONTEXT_DIR: Path = APP_HOME / "ContextFiles"
NOTIFICATION_PREFS_PATH: Path = APP_HOME / "notification_prefs.json"
MODEL_PREFS_PATH: Path = APP_HOME / "model_prefs.json"
PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"
REPORTS_DIR: Path = APP_HOME / "Reports"
NUDGES_DIR: Path = APP_HOME / "Nudges"
HTTP_INGEST_TOKEN_FILE: Path = APP_HOME / "ingest_tokens.json"
"""Hash-only bearer-token registry for HTTP Auto Export uploads."""
HTTP_INGEST_HOST = "127.0.0.1"
"""Loopback interface used by the HTTP receiver behind Tailscale Funnel."""
HTTP_INGEST_PORT: int = int(os.environ.get("ZDROWSKIT_HTTP_INGEST_PORT", "8787"))
"""Local TCP port used by the HTTP receiver."""
HTTP_INGEST_MAX_BYTES: int = 64 * 1024 * 1024
"""Maximum accepted Auto Export request body size."""
HTTP_INGEST_PAIR_WINDOW_S: int = 60 * 60
"""Maximum arrival gap between the Metrics and Workouts halves of an export.

Pairing no longer protects the data — either half can now land alone without
erasing the other. It only keeps a nudge from reacting to half a day, so a
missed pair costs a delayed import rather than lost data. An hour is wide
enough to absorb a slow multi-megabyte route upload while an hour-old Workouts
payload still describes today.
"""
DATA_HEALTH_REALERT_S: int = 24 * 60 * 60
"""How long before an unresolved ingest problem is reported again."""
DATA_HEALTH_SILENT_AFTER_H: float = 16
"""Hours of total ingest silence before the profile is warned.

Long enough to clear a night's sleep — an overnight gap runs about 9-10h — and
short enough to fire well inside the ~48h Auto Export window, after which the
missed days can no longer be recovered by simply fixing the phone.
"""
DATA_HEALTH_SPLIT_AFTER_H: float = 6
"""Hours of uploads arriving without importing before the profile is warned.

Far shorter than the silence threshold: uploads still arriving prove the phone
is reachable, so a stalled import is a real fault rather than a quiet evening.
"""
BASELINE_MIN_SAMPLES: int = 5
"""Observations required before a rolling window is reported as an average.

Below five readings one unusual day dominates the mean, so the number says more
about that day than about the person — and it is printed under a "30-day avg"
heading that claims otherwise. Sporadic metrics like VO2max still clear five
within a normal month of activity, so the floor suppresses fabricated norms
rather than legitimately infrequent ones.
"""
METRIC_TRUST_WINDOW_DAYS: int = 30
"""Window used to decide whether a metric is currently knowable.

Matches the shortest rolling baseline, so "established" here means exactly
"has a 30-day average" — one definition of trust rather than two that can
disagree with each other in the same prompt.
"""
MILESTONE_LIFETIME_MIN_DAYS: int = 180
"""History required before a recorded best may be called a lifetime record.

Under roughly half a year the best recorded run is mostly a statement about how
little has been recorded, and presenting it as a lifetime PR invites a report
to congratulate someone on an achievement that has not happened yet. Half a
year spans enough of a training cycle that a best within it is a real one.
"""
BASELINE_MIN_WINDOW_COVERAGE: float = 0.8
"""Fraction of a training-volume window that must contain data to divide by it.

Per-week volume divides a total by the window's nominal length, so a profile
holding three days of data reports a twelve-week average built from three days
— and a beginner's first week reads as a twelve-week habit of barely training.
Requiring most of the window to be present makes the average mean what its
label says.
"""


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


def _env_float(name: str, default: float) -> float:
    """Return a float from an environment variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return float(raw.strip())


MAX_HISTORY_ENTRIES: int = 8
"""Maximum number of entries to retain in history.md."""

MAX_LOG_ENTRIES: int = 5
"""Maximum number of entries to inject from log.md into LLM prompts."""

MAX_COACH_FEEDBACK_ENTRIES: int = 8
"""Maximum strategy/coach feedback entries to inject into prompts."""

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

MAX_TOKENS_ADD_CLONE: int = _env_int("ZDROWSKIT_MAX_TOKENS_ADD_CLONE", 512)
"""Output token budget for /add historical workout clone selection."""

MAX_TOKENS_MEMORY: int = _env_int("ZDROWSKIT_MAX_TOKENS_MEMORY", 1024)
"""Output token budget for the weekly memory extraction call.

Two bullets cost 60-100 tokens on the routed model, so this is roughly ten
times the observed need. That margin is deliberate: an exhausted budget returns
no block, which is indistinguishable downstream from a week genuinely worth
carrying nothing."""

LOCATION_GEOCODER: str = os.environ.get("ZDROWSKIT_LOCATION_GEOCODER", "nominatim")
"""Reverse geocoder for workout localities. Use ``off`` to disable."""

LOCATION_COORD_DECIMALS: int = _env_int("ZDROWSKIT_LOCATION_COORD_DECIMALS", 2)
"""Decimal places for locality lookup/cache (~1 km at the default)."""

LOCATION_HTTP_TIMEOUT_S: float = _env_float("ZDROWSKIT_LOCATION_HTTP_TIMEOUT_S", 8.0)
"""Network timeout for sparse reverse-geocoding cache misses."""

LOCATION_MIN_REQUEST_INTERVAL_S: float = _env_float(
    "ZDROWSKIT_LOCATION_MIN_REQUEST_INTERVAL_S",
    1.1,
)
"""Minimum delay between public reverse-geocoder requests."""

LOCATION_USER_AGENT: str = os.environ.get(
    "ZDROWSKIT_LOCATION_USER_AGENT",
    "zdrowskit/0.1 personal health import",
)
"""User-Agent sent to public reverse-geocoding services."""

EVAL_EXECUTION_ATTEMPTS: int = _env_int("ZDROWSKIT_EVAL_EXECUTION_ATTEMPTS", 3)
"""Attempts an eval makes to obtain a model response before recording an error.

Providers intermittently return a truncated or malformed structured payload,
which is a transport fault rather than a judgement the case is measuring.
Counting one as a failed case understates a model in exactly the comparison
evals exist to inform, so execution is retried while assertions are not. Three
attempts clears observed one-off malformed emissions without masking a model
that cannot produce valid output at all.
"""

MAX_TOKENS_VERIFICATION: int = _env_int("ZDROWSKIT_MAX_TOKENS_VERIFICATION", 16384)
"""Output token budget for evidence-bound verifier passes.

A verifier emits one structured issue per finding, with quote, problem,
correction, and evidence, on top of its own reasoning tokens. Audits of a
full-length report were reaching the previous 8192 ceiling exactly and
returning nothing, which reads downstream as a verifier failure rather than as
a truncated one. Doubling it puts the observed worst case at half the budget.
"""

MAX_TOKENS_VERIFICATION_REWRITE: int = _env_int(
    "ZDROWSKIT_MAX_TOKENS_VERIFICATION_REWRITE",
    2 * max(MAX_TOKENS_INSIGHTS, MAX_TOKENS_COACH),
)
"""Output token budget for bounded verification rewrites.

A rewrite reproduces the whole draft and applies corrections to it, so its job
is strictly larger than the writer's and it needs room for reasoning on top.
The previous fixed 4096 was half the writer's ceiling: a full-length report
could not fit even before corrections, so the rewriter burned the budget and
returned empty, and a fixable "revise" became a suppressed report.

Derived from the writer budgets rather than set independently so raising a
report ceiling cannot silently strand the rewriter below it again.
"""

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

ANTHROPIC_OPUS_MODEL: str = os.environ.get(
    "ZDROWSKIT_ANTHROPIC_OPUS_MODEL",
    "anthropic/claude-opus-5",
)
"""High-capability Anthropic model: premium primary and DeepSeek Pro fallback."""

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
    ANTHROPIC_OPUS_MODEL,
)
"""Default model for weekly insights reports."""

DEFAULT_COACH_MODEL: str = os.environ.get(
    "ZDROWSKIT_COACH_MODEL",
    ANTHROPIC_OPUS_MODEL,
)
"""Default model for coaching review/proposal generation."""

DEFAULT_NUDGE_MODEL: str = os.environ.get(
    "ZDROWSKIT_NUDGE_MODEL",
    ANTHROPIC_OPUS_MODEL,
)
"""Default model for proactive nudges."""

OPENAI_LUNA_MODEL: str = os.environ.get(
    "ZDROWSKIT_OPENAI_LUNA_MODEL",
    "openai/gpt-5.6-luna",
)
"""Budget-tier OpenAI model used for interactive chat and weekly memory."""

DEFAULT_CHAT_MODEL: str = os.environ.get(
    "ZDROWSKIT_CHAT_MODEL",
    OPENAI_LUNA_MODEL,
)
"""Default model for interactive Telegram chat.

Chat is the one surface with no verifier, so a model that claims an action it
never took — "Plan's updated" with no `update_context` call, "Figure 1 shows
it" with no chart block — goes uncaught. DeepSeek Flash did that in two of its
three failures across eleven cases, which is why chat moved off it.

The comparison that picked Luna over Flash did not measure Luna. Chat sends
tools on every turn, and until litellm 1.95.0 a tool-carrying Luna request was
rejected outright, so every one of those runs scored the fallback instead. The
first honest measurement, 2026-08-08 at three runs per case, puts Luna at 27 of
33 with one case failing 0/3 (`chat_tempo_short_warmup_negative`) and two
flaky. That is not the clean sweep this docstring used to claim.

Luna stays for now because the alternative is the model that fabricates
completed actions, and it costs about $0.11 a month at this call volume. Effort
stays high. Both of those are due a rerun against Flash on equal footing.
"""

DEFAULT_NOTIFY_MODEL: str = os.environ.get(
    "ZDROWSKIT_NOTIFY_MODEL",
    PRIMARY_FLASH_MODEL,
)
"""Default model for /notify intent interpretation."""

DEFAULT_ADD_CLONE_MODEL: str = os.environ.get(
    "ZDROWSKIT_ADD_CLONE_MODEL",
    PRIMARY_FLASH_MODEL,
)
"""Default model for /add workout clone selection."""

DEFAULT_MEMORY_MODEL: str = os.environ.get(
    "ZDROWSKIT_MEMORY_MODEL",
    OPENAI_LUNA_MODEL,
)
"""Default model for weekly memory extraction.

Memory used to be a section of the report, written by the same premium model
under the same breath as the analysis. Extracting two bullets from a finished
1024-character report is a much smaller job, and it is the one the writer did
worst — the block is invisible to the user, so nobody caught a bad line until
it had been replayed into prompts for weeks.

Luna rather than the usual Flash primary, and with reasoning off. DeepSeek
Flash cannot terminate on this prompt: it emits a reasoning trace whether or
not thinking is requested, and across eight samples it exhausted the whole
budget and returned empty text at 1024, at 4096, and once even at 8192. Luna
returned a clean block in 6 of 6 at 1024 in roughly 200 tokens, with or without
effort, and its bullets stay qualitative where Haiku's kept quoting figures the
database already holds. The task is selecting two lines against a stated rule
list — extended thinking was never what made it work."""

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


# ---------------------------------------------------------------------------
# Daemon paths and timing
# ---------------------------------------------------------------------------

LOG_FILE: Path = Path.home() / "Library/Logs/zdrowskit.daemon.log"
"""Daemon log file (stderr/stdout sink under launchd)."""

LOCK_FILE: Path = APP_HOME / ".daemon.lock"
"""Single-instance lock file held by the daemon while running."""

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

GOOGLE_DRIVE_POLL_INTERVAL_S: int = _env_int(
    "ZDROWSKIT_GOOGLE_DRIVE_POLL_INTERVAL_S", 5 * 60
)
"""How often a Google Drive daemon checks for new or changed export files."""

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


def resolve_google_drive_data_dir(arg: str | None) -> Path:
    """Resolve the local cache used for Google Drive imports.

    Priority: CLI ``--data-dir`` > ``HEALTH_DATA_DIR`` environment variable >
    ``GOOGLE_DRIVE_DATA_DIR``.

    Args:
        arg: Value of the ``--data-dir`` CLI argument, or None.

    Returns:
        Absolute path to the local Google Drive import cache.
    """
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("HEALTH_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return GOOGLE_DRIVE_DATA_DIR.resolve()
