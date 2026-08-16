"""Shared paths, limits, model routing, and daemon configuration.

Every operational tunable lives here: thresholds, intervals, windows, size and
count limits, model routes, and the paths under the app home. Nothing is
inlined at its point of use, so this file is the one place to look for what a
value is and why it is that value.

Constants are grouped by subsystem in file order — paths, HTTP ingest, ingest
health, baselines, context and prompt limits, token budgets, location lookup,
evals, verification, model routes, daemon timing — and each carries a docstring
covering how it was chosen. Where a value came from a measurement, that
docstring records the measurement, because nothing else does.

Most values accept a ``ZDROWSKIT_*`` environment override, read once at import.
Model routing has a second, authoritative layer: a profile's saved
``model_prefs.json`` wins over anything set here. Prefer ``main.py models`` for
routing changes.

Example:
    from config import CONTEXT_DIR, resolve_data_dir
    data = resolve_data_dir(args.data_dir)
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


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
GOOGLE_DRIVE_SERVICE_ACCOUNT: str | None = os.environ.get(
    "ZDROWSKIT_GOOGLE_DRIVE_SERVICE_ACCOUNT"
)
"""Shared service-account key path. Per-profile folder IDs live in the roster.

The legacy ``ZDROWSKIT_IMPORT_SOURCE`` and per-folder variables are deliberately
absent: import source and folder IDs are per-profile roster fields now, and
``profiles.adopt`` reads the old variables directly when migrating a legacy
install. A module-level copy here would be read at import time and would shadow
the roster with a stale answer.
"""
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
HTTP_INGEST_MAX_METRICS: int = 200
"""Distinct metric types accepted in one Metrics payload.

Auto Export offers well under a hundred metric types even with everything
switched on, so this is roughly double the widest real export. It bounds the
per-payload work without ever rejecting a legitimate phone.
"""
HTTP_INGEST_MAX_METRIC_ENTRIES: int = 400
"""Daily entries accepted for a single metric type in one payload.

A rolling export carries days, not years: 400 covers well over a year of daily
values for one metric, which no scheduled automation should ever send. A
historical backfill that needs more belongs on the iCloud or Drive transport,
which has no request ceiling.
"""
HTTP_INGEST_MAX_WORKOUTS: int = 500
"""Workouts accepted in one payload.

Sized for a wide manual backfill rather than a scheduled export, which carries
a handful. Beyond this the request is large enough that the file transports are
the better tool.
"""
HTTP_INGEST_MAX_ROUTE_POINTS: int = 250_000
"""GPS route points accepted for a single workout.

At one sample per second this is roughly 69 hours of continuous recording, so
it cannot reject a real activity, including an ultra. It exists because route
arrays dominate payload size and a corrupt one should fail at the door rather
than during parsing.
"""
HTTP_INGEST_MAX_JSON_DEPTH: int = 16
"""Maximum nesting depth accepted in an upload body.

Auto Export payloads nest about five levels deep. The ceiling is a guard
against hostile or corrupt JSON exhausting the stack during traversal, not a
format constraint.
"""
HTTP_INGEST_MAX_JSON_NODES: int = 1_000_000
"""Maximum total JSON nodes traversed while validating an upload body.

Bounds validation cost for a body that is within the byte limit but pathological
in shape — millions of tiny values rather than a few large ones.
"""
HTTP_INGEST_MAX_RECEIPTS: int = 100
"""Upload receipts retained in a profile's ingest state file.

Enough history for ``ingest status`` to show a meaningful recent picture while
keeping the state file small, since it is rewritten on every upload.
"""
HTTP_INGEST_BATCH_CAPTURE_MAX_FILES: int = 200
"""Captured batch requests retained per profile before the oldest are dropped.

The batch endpoint exists to observe what Auto Export sends when batch export is
switched on, so the useful unit is one whole export run. A three-month run is
expected to arrive as tens of requests, not hundreds; two hundred holds several
such runs for comparison while keeping the directory inspectable by hand and
bounding what an accidentally-scheduled automation can leave on disk.
"""
PUBLIC_DNS_RESOLVER_URL: str = "https://cloudflare-dns.com/dns-query"
"""DNS-over-HTTPS endpoint used to resolve the Funnel hostname from outside.

The host's own resolver is useless for this: MagicDNS answers for every tailnet
member, so the Funnel name resolves locally to a 100.64.0.0/10 address even
when its public record has disappeared and no phone can reach it. A resolver
outside the tailnet is the only way to see what Auto Export sees. DoH is used
rather than shelling out to ``dig`` so the check needs nothing but stdlib.
"""
PUBLIC_DNS_TIMEOUT_S: float = 5
"""Seconds to wait for the public resolver before giving up.

Generous for a single JSON lookup, short enough that a diagnostic command still
returns promptly on a flaky network. A timeout reports "unknown", never
"broken" — an offline laptop must not look like a missing DNS record.
"""
DATA_HEALTH_REALERT_S: int = 24 * 60 * 60
"""How long before an unresolved ingest problem is reported again."""
DATA_HEALTH_STALE_AFTER_DAYS: int = 2
"""Complete days missing daily metrics before the profile is warned.

Deliberately counts days of *stored data*, not hours since the last upload. Auto
Export arrives in bursts: across 70 complete pairs over twelve days of one real
profile, the gap between imports ran a 1.7h median, a 10.1h p90 and a 34.9h
maximum, so any hours-based silence threshold low enough to catch a dead phone
also fires on the tail of normal operation — and a long gap that lands as a
complete backfill has cost the user nothing worth a message. What matters is
whether a day is actually missing, which stays true no matter how the uploads
were batched.

Two days rather than one because of a second, narrower sample: over the eight
days whose per-day arrival times were retained, a healthy Metrics upload first
arrived after 10:00 twice, at 12:40 and as late as 20:03. A one-day threshold
would have repeated the false alert this check replaces. The Metrics payload
itself carried a rolling 7-8 days there, so two days of grace still leaves
several days in which a delayed export can backfill the gap.
"""
DATA_HEALTH_STALE_CUTOFF_HOUR: int = 10
"""Hour at which another completed metric day becomes owed.

The two-day staleness threshold absorbs the observed same-day arrival variance;
this cutoff only decides when the calendar advances for that count. It is kept
independent from sleep classification because a completed Metrics export and a
single sleep night have different arrival behavior.
"""
SLEEP_SYNC_CUTOFF_HOUR: int = 10
"""Hour after which yesterday's absent sleep is considered not tracked.

Ten is the existing product boundary between an overnight sync still being
plausibly pending and a night the reports should treat as absent. It is separate
from data-health staleness so either policy can change without moving the other.
"""
DATA_HEALTH_SPLIT_AFTER_H: float = 6
"""Hours of uploads arriving without importing before the profile is warned.

Measured in hours rather than days, unlike the staleness threshold: uploads
still arriving prove the phone is reachable, so a stalled import is a definite
fault on this end rather than a quiet evening, and waiting a day to say so
wastes the window in which the pairing can still be fixed.
"""
FUNNEL_DNS_CHECK_AFTER_H: float = 3
"""Upload silence before the Funnel's public DNS record is checked.

While uploads are arriving the record demonstrably resolves, so checking it is
a wasted network call on every healthy cycle. Silence is the only state where
the answer can differ from what the last upload already proved.

Three hours sits below the shortest observed real outage (26h) and above the
gap between daytime exports, so a genuine outage is caught in its first hours
while an ordinary lull between automations does not trigger a lookup. Overnight
quiet does cause a few lookups that return healthy; that costs one DoH request
each and alerts nobody, which is the right trade against detecting a dead pipe
a day late.
"""
DATA_HEALTH_QUIET_START_HHMM: str = "22:00"
"""Local time after which ingest-health alerts are held until morning.

A stalled import is never fixable while the operator is asleep, so waking them
is pure noise: the fault persists until they act either way. The alert is held,
not dropped — it fires at DATA_HEALTH_QUIET_END_HHMM. Paired with the end time
this brackets a 22:00-08:00 do-not-disturb window.

Staleness alerts cannot reach this window on their own, since they only become
true after DATA_HEALTH_STALE_CUTOFF_HOUR. It still guards the hours-based split
and error conditions, which can turn true at any time of night."""
DATA_HEALTH_QUIET_END_HHMM: str = "08:00"
"""Local time at which a held ingest-health alert is released.

Set to a normal waking hour so an overnight pipe fault surfaces first thing,
while the phone can still resend its rolling export window."""
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
WORKOUT_SPLIT_MIN_SAMPLE_COVERAGE: float = 0.6
"""Fraction of a kilometre split that must carry samples to report a value for it.

Governs every per-minute series attached to a split — heart rate and step
cadence — because both arrive as the same one-minute bins from the same watch
and fail the same way: sampling routinely starts after the workout begins or
drops out mid-session. One knob rather than one per series, since the physical
reason for the floor is identical.

Measured over 1640 route-bearing runs and walks on 2026-08-15: 68.1% of workouts
have at least 95% of their elapsed time covered by heart-rate bins, 6.5% fall
between 50% and 80%, and 13.1% sit under 50% — one 30-minute run recorded
nothing for its first 12 minutes. The distribution is bimodal, so the cut lands
in the sparse middle rather than splitting a dense region.

Six-tenths keeps splits where most of the kilometre was actually measured. At a
5-6 min/km pace a split spans 5-6 bins, so this needs roughly 4 of them; below
that the value is drawn from a minority of the split and can miss an entire
surge or recovery while still reading as a normal number. Splits under the floor
store their coverage and a null value, so a partial kilometre is visibly unknown
rather than confidently wrong.
"""


MAX_HISTORY_ENTRIES: int = 10
"""Weekly memory entries retained in history.md and injected into prompts.

One entry per week, so this is a window in weeks: ten covers roughly a training
quarter, long enough for a season's worth of threads — a recurring injury, a
drought in one session type, a behavioural pattern like training always landing
late in the week — to still be visible when they matter.

Kept deliberately modest because each entry is at most two bullets by contract
and the older ones age into noise: a thread that has been open for ten weeks is
either resolved or is being carried by the newer entries anyway. This bounds
both the prompt and, since ``append_history`` trims to it, the file.
"""

MAX_LOG_ENTRIES: int = 10
"""Maximum number of entries to inject from log.md into LLM prompts.

The limit went unenforced for a long time — log.md is a flat list of dated
bullets, and the trimmer applied to it split on ``## `` headings, so it matched
nothing and every prompt received the whole journal. Any value predating
``_recent_log`` was therefore never actually exercised.

Ten because the two files divide the work. log.md carries recent day-to-day
detail, which at the observed cadence is about five weeks; history.md carries
the durable threads, so the context that has to survive months — an indefinite
caring arrangement, a bereavement, an illness explaining a missing week — is
already held there and does not need the log to reach back for it. Entries are
also single-line and token-dense by contract, so ten of them is roughly 300
tokens: cheap enough that the ceiling is set by usefulness, not budget.
"""

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

INSIGHTS_MAX_VISIBLE_CHARS: int = 1024
"""Hard ceiling on the weekly report body the user actually receives.

The size of a message read on a phone without scrolling, and the ceiling a
Telegram photo caption allows. Enforced deterministically rather than by the
verifier: it is a character count, and asking an LLM to do arithmetic spends
one of its issue slots on something a regex cannot get wrong. The same number
appears in prose in `insights_prompt.md` and in the Insights row of
`docs/notifications.md` — update both if this changes. It is deliberately
absent from `verify_insights_prompt.md`, which tells the verifier to ignore
length precisely because this check runs before it."""

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

OPENAI_LUNA_MODEL: str = os.environ.get(
    "ZDROWSKIT_OPENAI_LUNA_MODEL",
    "openai/gpt-5.6-luna",
)
"""Budget-tier OpenAI model used for chat, nudges, and weekly memory."""

DEFAULT_INSIGHTS_MODEL: str = os.environ.get(
    "ZDROWSKIT_INSIGHTS_MODEL",
    OPENAI_LUNA_MODEL,
)
"""Default model for weekly insights reports.

Moved off Opus 5 on cost: roughly 60x the price of Luna per covered eval run,
for a quality lead the three insights cases were too few to confirm. See
``model_prefs._feature_defaults``.
"""

DEFAULT_COACH_MODEL: str = os.environ.get(
    "ZDROWSKIT_COACH_MODEL",
    OPENAI_LUNA_MODEL,
)
"""Default model for coaching review/proposal generation.

Moved off Opus 5 on 2026-08-09, and unlike reports and nudges this move was
**not measured**: coach is the one surface with no eval cases. The reasoning is
symmetry rather than evidence — there was no result showing Opus 5 was worth
23x Luna here ($0.0956 against $0.0041 per review), so keeping it was as
unfounded as moving it, only dearer. Coach output is verified (``VERIFY_COACH``)
which is a real net under it. See ``model_prefs._feature_defaults``.
"""

DEFAULT_NUDGE_MODEL: str = os.environ.get(
    "ZDROWSKIT_NUDGE_MODEL",
    OPENAI_LUNA_MODEL,
)
"""Default model for proactive nudges.

Measured 2026-08-08 across the nudge eval cases at five runs per model. Luna
took 80% against 40% for DeepSeek Flash, DeepSeek Pro and Opus 5 alike, swept
the week-totals case 5/5, and answered in 4.6 s against 13-28 s.

Opus 5 is the reason this default changed. It cost $0.098 a nudge — 80x Luna —
and scored no better than the cheapest model in the lineup, including 0/5 on
the case that guards against stating metric values the data does not contain.
At the 2/day cap that is $1.37 a week against $0.017 for a worse result.

No model passes that invented-metric case reliably; the best is 3/5. That is a
prompt defect rather than a routing one, and picking a model cannot fix it.
"""

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
    PRIMARY_FLASH_MODEL,
)
"""Model used for evidence-bound verifier passes.

Flash, not Pro, on accuracy rather than price: over the seven
verification_judge cases at five runs each it took 85.7% strict against Pro's
57.1%, catching every seeded defect while still passing both sound drafts.
Pro is marginally cheaper and faster here; the verifier is the trust backstop,
so accuracy wins. See ``model_prefs`` for the fallback rationale.
"""

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
