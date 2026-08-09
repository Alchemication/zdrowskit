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

Insights and coach default to `anthropic/claude-opus-5` with `reasoning_effort=high`, temperature omitted, and `deepseek/deepseek-v4-pro` fallback. Chat and nudges default to `openai/gpt-5.6-luna` with `reasoning_effort=high` and temperature omitted — chat falling back to `anthropic/claude-haiku-4-5`, nudges to `deepseek/deepseek-v4-flash`.

Lightweight utility surfaces, including `/notify` interpretation and `/add` workout clone selection, default to `deepseek/deepseek-v4-flash` with `anthropic/claude-haiku-4-5` fallback. `/add` and verifier rewrites use `reasoning_effort=high` with temperature omitted; `/notify` stays plain Flash. Weekly memory routes to `openai/gpt-5.6-luna` with reasoning off.

Weekly memory is a call of its own rather than a `<memory>` section of the report. Splitting it shortened the insights prompt, made the block scorable as an eval feature on its own, and moved it off the premium model — deciding which two lines to carry forward from a finished 1024-character report is a much smaller job than writing the report. It runs on Luna with reasoning off, in about 200 tokens against a 1024 budget. DeepSeek Flash cannot do this job at all: it emits a reasoning trace whether or not thinking is requested, and exhausted the whole budget returning empty text at 1024, at 4096, and once at 8192 — and an empty block is indistinguishable from a week worth carrying nothing.

Logged LLM calls record the effective model, and fallback calls include `requested_model` and `fallback_used` in params/metadata.

## Cost Projection

Providers bill in USD per million tokens. Logged call costs use LiteLLM's pricing data, with provider-reported cost as a fallback. Consult [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/) and [Anthropic pricing](https://www.anthropic.com/pricing) for current rates; the projections below use recent logged token sizes from this app.

Current default routes:

| Feature | Primary | Normal cadence |
|---|---|---:|
| Weekly report | `anthropic/claude-opus-5` | 1/week |
| Coach review | `anthropic/claude-opus-5` | 1/week |
| Nudges | `openai/gpt-5.6-luna` | up to 2/day |
| Verification | `deepseek/deepseek-v4-pro` | reports, coach, nudges; Opus 5 fallback |
| Verification rewrites | `deepseek/deepseek-v4-flash` | only when verifier asks |
| Weekly memory | `openai/gpt-5.6-luna` | 1/week, after the report is sent |
| Chat | `openai/gpt-5.6-luna` | on demand |

Using recent logged token sizes from this app, the always-on daemon lands around:

| Workload | Projected cost |
|---|---:|
| Report, including DeepSeek verification | ~$0.10/week |
| Coach review | ~$0.10/week |
| Nudges at the 2/day cap, including DeepSeek verification | ~$0.21/week |
| **Daemon total at default caps** | **~$0.51/week** |

This assumes verification normally succeeds on DeepSeek Pro with thinking engaged (via `reasoning_effort=high`) and rewrite calls remain rare. Verification falls back to Opus 5 with the same `reasoning_effort=high` (sent natively) and omitted temperature.

Nudges moved off Opus 5 on 2026-08-08. Measured across the nudge eval cases at five runs per model, Luna took 80% while DeepSeek Flash, DeepSeek Pro and Opus 5 all sat at 40%; Luna also swept the week-totals case 5/5 and answered in 4.6 s against 13-28 s. Opus 5 cost $0.098 a nudge — 80x Luna — for no measured gain, which is why the fallback is not the profile's premium default. It moved from DeepSeek Pro to DeepSeek Flash on 2026-08-09: all three scored the same 40%, and Luna's observed failure is `max_output_tokens` — it spends the output budget on reasoning and emits nothing — so a reasoning model inheriting the same effort and ceiling is the tier most likely to repeat it. Note that no model passes the invented-metric case reliably (best 3/5): that is a prompt defect, not a routing one.

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
