# LLM Setup

zdrowskit relies on capable models. The coach writes personalised reports, decides when to stay quiet, generates SQL queries against your data, and produces chart code.

Default: Anthropic Opus 5 for async judgement surfaces, with high reasoning and temperature omitted. Telegram chat defaults to DeepSeek V4 Flash with DeepSeek thinking enabled for lower latency and cost.

Minimum: Claude Sonnet 4.6 or equivalent. Anything below that and the reports get generic, the queries get unreliable, and the charts break.

Any model provider works through [litellm](https://github.com/BerriAI/litellm), so you can swap in OpenAI, Google, or any compatible API.

## Model Defaults and Fallback Policy

Model routing is managed in:

```text
~/Documents/zdrowskit/profiles/<name>/model_prefs.json
```

You can change routing with:

```bash
uv run python main.py models
uv run python main.py models --profile anna
```

or through Telegram `/models`. Telegram always uses the sender's routed
profile. CLI commands default to the operator profile.

The Telegram panel groups features as Chat / Reports / Coach / Nudges / Utilities and tags every model button with its capability tier: premium / pro / flash / lite. Chat exposes Reasoning and Temperature controls. `reasoning_effort` is the single reasoning knob: Anthropic gets it natively, and on DeepSeek, `high`/`max` translate into thinking mode (`extra_body={"thinking": {"type": "enabled"}}`) while `low`/`medium`/`none` leave thinking off.

A `Reset all` button on the main panel and `uv run python main.py models reset --all` restore everything to built-in defaults. Picking the `Auto` fallback, or `--fallback auto` from the CLI, defers to the profile's fallback so future profile changes propagate.

Coach defaults to `anthropic/claude-opus-5` with `reasoning_effort=high`, temperature omitted, and `deepseek/deepseek-v4-pro` fallback. Insights moved to `openai/gpt-5.6-luna` with `reasoning_effort=high`, temperature omitted, and `deepseek/deepseek-v4-flash` fallback. Chat and nudges default to `openai/gpt-5.6-luna` with `reasoning_effort=high` and temperature omitted — chat falling back to `anthropic/claude-haiku-4-5`, nudges to `deepseek/deepseek-v4-flash`.

Lightweight utility surfaces, including `/notify` interpretation and `/add` workout clone selection, default to `deepseek/deepseek-v4-flash` with `anthropic/claude-haiku-4-5` fallback. `/add` and verifier rewrites use `reasoning_effort=high` with temperature omitted; `/notify` stays plain Flash. Weekly memory routes to `openai/gpt-5.6-luna` with reasoning off.

Weekly memory is a call of its own rather than a `<memory>` section of the report. Splitting it shortened the insights prompt, made the block scorable as an eval feature on its own, and moved it off the premium model — deciding which two lines to carry forward from a finished 1024-character report is a much smaller job than writing the report. It runs on Luna with reasoning off, in about 200 tokens against a 1024 budget. DeepSeek Flash cannot do this job at all: it emits a reasoning trace whether or not thinking is requested, and exhausted the whole budget returning empty text at 1024, at 4096, and once at 8192 — and an empty block is indistinguishable from a week worth carrying nothing.

Logged LLM calls record the effective model, and fallback calls include `requested_model` and `fallback_used` in params/metadata.

## Cost Projection

Providers bill in USD per million tokens. Logged call costs use LiteLLM's pricing data, with provider-reported cost as a fallback. Consult [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/) and [Anthropic pricing](https://www.anthropic.com/pricing) for current rates; the projections below use recent logged token sizes from this app.

Current default routes:

| Feature | Primary | Normal cadence |
|---|---|---:|
| Weekly report | `openai/gpt-5.6-luna` | 1/week |
| Coach review | `anthropic/claude-opus-5` | 1/week |
| Nudges | `openai/gpt-5.6-luna` | up to 2/day |
| Verification | `deepseek/deepseek-v4-flash` | reports, coach, nudges; Luna fallback |
| Verification rewrites | `deepseek/deepseek-v4-flash` | only when verifier asks |
| Weekly memory | `openai/gpt-5.6-luna` | 1/week, after the report is sent |
| Chat | `openai/gpt-5.6-luna` | on demand |

Six weeks of this app's own logged traffic (2026-06-29 to 2026-08-09), repriced
at the routes above and with the removed mid-week report cycles excluded, lands
around **$1 a month** — roughly $0.15-$0.35 in a given week:

| Workload | Unit cost | Share of spend |
|---|---:|---:|
| Weekly report cycle: writer, verifier, rewriter, memory | ~$0.023/run | 9% |
| Coach review | ~$0.097/run | 25% |
| Nudge, including verification | ~$0.0039/nudge | 44% |
| Chat | ~$0.0034/turn | 22% |
| **Total** | | **~$0.26/week** |

A bottom-up estimate agrees: one report, one coach review, two nudges a day,
fifteen chat turns and a few logs a week comes to ~$0.23/week.

Two things move that number more than the model prices do. **Usage intensity:**
chat is user-driven, and the light-to-heavy span above is $0.17 to $0.31 a week.
**Which models you configure:** the coach is the last surface on Opus 5 and is a
quarter of all spend on its own, while the report — previously the largest line —
fell to about 6% when it moved to Luna. Routing the coach the same way would put
the total under $0.20/week; it has no eval coverage, so that change would be
unmeasured rather than merely cheaper.

Note that nudge *calls* ran at about 4.3/day against `MAX_NUDGES_PER_DAY = 2`.
The cap governs delivered nudges; a trigger that the coach evaluates and then
decides to stay quiet about still costs a call. Estimating nudges from the cap
alone understates them roughly twofold.

Verification normally succeeds on DeepSeek Flash with thinking engaged (via
`reasoning_effort=high`) and rewrite calls remain rare. Verification falls back
to Luna with the same `reasoning_effort=high` and omitted temperature.

Nudges moved off Opus 5 on 2026-08-08. Measured across the nudge eval cases at five runs per model, Luna took 80% while DeepSeek Flash, DeepSeek Pro and Opus 5 all sat at 40%; Luna also swept the week-totals case 5/5 and answered in 4.6 s against 13-28 s. Opus 5 cost $0.098 a nudge — 80x Luna — for no measured gain, which is why the fallback is not the profile's premium default. It moved from DeepSeek Pro to DeepSeek Flash on 2026-08-09 — in `model_prefs` and, more importantly, in `_fallback_chain`, which had no OpenAI branch and sent every Luna call to `DEFAULT_MODEL` (DeepSeek Pro) regardless of what the route configured: all three scored the same 40%, and Luna's observed failure is `max_output_tokens` — it spends the output budget on reasoning and emits nothing — so a reasoning model inheriting the same effort and ceiling is the tier most likely to repeat it. Note that no model passes the invented-metric case reliably (best 3/5): that is a prompt defect, not a routing one.

Chat is separate because it is user-driven. Chat routes to GPT-5.6 Luna at about $0.0014 a turn. DeepSeek Flash is cheaper still, but measured against the chat eval suite it failed three of eleven cases, twice by claiming an action it never took — reporting a plan as updated with no `update_context` call, and citing a figure with no chart block. Chat has no verifier, so nothing catches that.

Note that the run which picked Luna over Flash did not actually measure Luna: chat sends tools every turn, and until litellm 1.95.0 a tool-carrying Luna request was rejected outright and scored the fallback. Measured properly on 2026-08-08 at three runs per case, Luna scores 27 of 33 with one 0/3 and two flaky cases. Luna stays for now, but the Luna-versus-Flash comparison is owed a rerun on equal footing.

Inspect actual spend from your local DB:

```bash
uv run python main.py llm-log --stats
uv run python main.py llm-log --profile anna --stats
```

LLM call IDs are local to each profile database. Always select the profile
before investigating another person's call ID.

## Environment Overrides

The defaults live in `src/config.py` and can be overridden from `.env`:

```env
ZDROWSKIT_PRIMARY_PRO_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_FALLBACK_PRO_MODEL=anthropic/claude-opus-5
ZDROWSKIT_PRIMARY_FLASH_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_FALLBACK_FLASH_MODEL=anthropic/claude-haiku-4-5
ZDROWSKIT_ANTHROPIC_OPUS_MODEL=anthropic/claude-opus-5

ZDROWSKIT_INSIGHTS_MODEL=anthropic/claude-opus-5
ZDROWSKIT_COACH_MODEL=anthropic/claude-opus-5
ZDROWSKIT_NUDGE_MODEL=openai/gpt-5.6-luna
ZDROWSKIT_CHAT_MODEL=openai/gpt-5.6-luna
ZDROWSKIT_NOTIFY_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_ADD_CLONE_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_MEMORY_MODEL=openai/gpt-5.6-luna

ZDROWSKIT_MAX_TOKENS_DEFAULT=4096
ZDROWSKIT_MAX_TOKENS_INSIGHTS=8192
ZDROWSKIT_MAX_TOKENS_COACH=8192
ZDROWSKIT_MAX_TOKENS_CHAT=4096
ZDROWSKIT_MAX_TOKENS_NUDGE=4096
ZDROWSKIT_MAX_TOKENS_NOTIFY=512
ZDROWSKIT_MAX_TOKENS_ADD_CLONE=512
ZDROWSKIT_MAX_TOKENS_MEMORY=1024
ZDROWSKIT_MAX_TOKENS_VERIFICATION=16384
ZDROWSKIT_MAX_TOKENS_VERIFICATION_REWRITE=16384
```

## API Keys

The default configuration expects DeepSeek, Anthropic, and OpenAI keys:

```env
DEEPSEEK_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

Set additional provider keys as needed for your chosen litellm model strings.

## Verification

Post-generation verification runs by default for async LLM outputs: reports, coach reviews, and nudges. This adds a separate verifier call and, when the issue is fixable, one bounded rewrite call before the output is saved or sent.

Chat remains unverified because it is interactive and latency-sensitive.

The default verifier uses `deepseek/deepseek-v4-pro` with `reasoning_effort=high` (engages DeepSeek thinking) and falls back to Opus 5 with the same effort sent natively, no temperature. Bounded rewrites stay on Flash by default, also with `reasoning_effort=high` and no temperature — DeepSeek translates `high` into thinking mode via `extra_body` while Anthropic uses it natively, so the same per-feature setting works across both providers.

A rewrite reproduces the entire draft and applies corrections to it, so its
token budget is derived as twice the largest writer budget rather than set on
its own. When it was a fixed 4096 against an 8192 writer, full-length reports
could not fit before corrections: the rewriter exhausted its budget, returned
nothing, and a fixable `revise` verdict became a suppressed report.

```env
# Optional overrides:
ZDROWSKIT_ENABLE_LLM_VERIFICATION=0
ZDROWSKIT_VERIFICATION_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_VERIFICATION_REWRITE_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_MAX_TOKENS_VERIFICATION=16384
ZDROWSKIT_MAX_TOKENS_VERIFICATION_REWRITE=16384
ZDROWSKIT_MAX_VERIFICATION_REVISIONS=1
ZDROWSKIT_VERIFY_INSIGHTS=1
ZDROWSKIT_VERIFY_COACH=1
ZDROWSKIT_VERIFY_NUDGE=1
```

Each product operation creates an `llm_trace` row. Related provider calls share the same trace: tool-loop iterations, final synthesis retries, verification, and rewrites. `uv run python main.py llm-log --id N` shows the selected call plus its trace; `uv run python main.py llm-log --trace N` shows the trace call list directly.

Verification calls are logged as `insights_verify`, `insights_rewrite`, `coach_verify`, `coach_rewrite`, `nudge_verify`, and `nudge_rewrite`. The original source call metadata also records the verifier verdict, issue counts, issue details, and verifier/rewrite call IDs.

Reports left Opus 5 on 2026-08-09, on price rather than on a measured quality win. Over the three insights eval cases at five runs per model, Opus took 86.7% attempt-weighted against Luna's 73.3% and DeepSeek Flash's 66.7% — but cost $0.5852 per covered run against $0.0117 and $0.0076, roughly 60x. Luna and Flash tied on strict accuracy and swapped places between two consecutive runs of the same config, so three cases could not separate them; Luna took it on latency (20 s against 56 s) and because Flash once returned only SQL tool calls with no report body at all. Treat this as provisional: the comparison is under-powered, and it should be redone once insights has more than three cases. Coach stays on Opus because it has no eval coverage at all, so moving it would be unmeasured.

The verifier moved from DeepSeek Pro to DeepSeek Flash on 2026-08-09, on accuracy rather than price. Over the seven `verification_judge` cases at five runs each, Flash took 85.7% strict against Pro's 57.1% and Luna's 57.1%, catching every seeded defect while still passing both sound-draft controls — and it largely fixed the unsupported-VO2max-recency case that Pro missed 0/5. Flash is not the cheap option on this workload: it emitted 2.5x Pro's output tokens, costing $0.00341 a call against $0.00315 and running 63 s against 50 s. The verifier is the trust backstop, so accuracy wins over both.

The fallback moved off Opus 5 in the same change: its billing ceiling errored thirteen verifier attempts in a single run, which is the failure mode a fallback exists to prevent. Luna takes it, with a known weakness — it rejected a sound draft four times in five, so while Flash is down the verifier over-suppresses rather than under-catches. That is the safer direction for a backstop, but it is not free: suppressed content reaches nobody and collects no feedback.
