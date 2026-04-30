# LLM Setup

zdrowskit relies on capable models. The coach writes personalised reports, decides when to stay quiet, generates SQL queries against your data, and produces chart code.

Default: DeepSeek V4 Pro for async judgement surfaces, with Anthropic Opus 4.6 as the cross-provider fallback. Telegram chat defaults to Anthropic Opus 4.7 with reasoning off for lower latency.

Minimum: Claude Sonnet 4.6 or equivalent. Anything below that and the reports get generic, the queries get unreliable, and the charts break.

Any model provider works through [litellm](https://github.com/BerriAI/litellm), so you can swap in OpenAI, Google, or any compatible API.

## Model Defaults and Fallback Policy

Model routing is managed in:

```text
~/Documents/zdrowskit/model_prefs.json
```

You can change routing with:

```bash
uv run python main.py models
```

or through Telegram `/models`.

The Telegram panel groups features as Chat / Reports / Coach / Nudges / Utilities and tags every model button with its capability tier: premium / pro / flash / lite. Chat also exposes Reasoning and Temperature controls; other groups inherit sensible defaults from their primary model.

A `Reset all` button on the main panel and `uv run python main.py models reset --all` restore everything to built-in defaults. Picking the `Auto` fallback, or `--fallback auto` from the CLI, defers to the profile's fallback so future profile changes propagate.

Insights, coach, and nudges default to `deepseek/deepseek-v4-pro` with `anthropic/claude-opus-4-6` fallback. Chat defaults to `anthropic/claude-opus-4-7` with reasoning off and temperature omitted, falling back to DeepSeek Pro.

Lightweight utility surfaces, including `/notify` interpretation, `/log` flow building, and `/add` workout clone selection, default to `deepseek/deepseek-v4-flash` with `anthropic/claude-haiku-4-5` fallback.

Logged LLM calls record the effective model, and fallback calls include `requested_model` and `fallback_used` in params/metadata.

## Cost Projection

Providers bill in USD per million tokens. Prices below were checked on 2026-04-30 against [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/) and [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing).

Current default routes:

| Feature | Primary | Normal cadence |
|---|---|---:|
| Weekly + midweek reports | `deepseek/deepseek-v4-pro` | 2/week |
| Coach review | `deepseek/deepseek-v4-pro` | 1/week |
| Nudges | `deepseek/deepseek-v4-pro` | up to 3/day |
| Verification | `deepseek/deepseek-v4-pro` | reports, coach, nudges |
| Verification rewrites | `deepseek/deepseek-v4-flash` | only when verifier asks |
| Chat | `anthropic/claude-opus-4-7` | on demand |

Using recent logged token sizes from this app, the always-on daemon lands around:

| Workload | Projected cost |
|---|---:|
| Reports, including verification | ~$0.04/week |
| Coach review | ~$0.01/week |
| Nudges at the 3/day cap, including verification | ~$0.23/week |
| **Daemon total at default caps** | **~$0.30/week** |

This assumes DeepSeek Pro succeeds and Anthropic fallback is rare. If every Pro-class call used Anthropic Opus instead, the same daemon workload would be several dollars per week. If DeepSeek Pro returns to list pricing after the current promo, the default daemon projection rises to roughly `$1.10/week`.

Chat is separate because it is user-driven. A typical Opus 4.7 chat turn with tools can cost around `$0.08`; routing chat to DeepSeek Flash is usually under one cent per turn, but quality may drop for harder analysis.

Inspect actual spend from your local DB:

```bash
uv run python main.py llm-log --stats
```

## Environment Overrides

The defaults live in `src/config.py` and can be overridden from `.env`:

```env
ZDROWSKIT_PRIMARY_PRO_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_FALLBACK_PRO_MODEL=anthropic/claude-opus-4-6
ZDROWSKIT_PRIMARY_FLASH_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_FALLBACK_FLASH_MODEL=anthropic/claude-haiku-4-5
ZDROWSKIT_ANTHROPIC_OPUS_4_7_MODEL=anthropic/claude-opus-4-7

ZDROWSKIT_INSIGHTS_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_COACH_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_NUDGE_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_CHAT_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_NOTIFY_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_LOG_FLOW_MODEL=anthropic/claude-haiku-4-5
# /log uses deepseek/deepseek-v4-flash as its feature-level fallback
ZDROWSKIT_ADD_CLONE_MODEL=deepseek/deepseek-v4-flash

# DeepSeek V4 defaults to thinking enabled/high; app calls disable it by default.
ZDROWSKIT_DEEPSEEK_THINKING=disabled

ZDROWSKIT_MAX_TOKENS_DEFAULT=4096
ZDROWSKIT_MAX_TOKENS_INSIGHTS=8192
ZDROWSKIT_MAX_TOKENS_COACH=8192
ZDROWSKIT_MAX_TOKENS_CHAT=4096
ZDROWSKIT_MAX_TOKENS_NUDGE=4096
ZDROWSKIT_MAX_TOKENS_NOTIFY=512
ZDROWSKIT_MAX_TOKENS_LOG_FLOW=4096
ZDROWSKIT_MAX_TOKENS_ADD_CLONE=512
ZDROWSKIT_MAX_TOKENS_VERIFICATION=4096
ZDROWSKIT_MAX_TOKENS_VERIFICATION_REWRITE=4096
```

## API Keys

The default configuration expects DeepSeek and Anthropic keys:

```env
DEEPSEEK_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

Set additional provider keys as needed for your chosen litellm model strings.

## Verification

Post-generation verification runs by default for async LLM outputs: reports, coach reviews, and nudges. This adds a separate verifier call and, when the issue is fixable, one bounded rewrite call before the output is saved or sent.

Chat remains unverified because it is interactive and latency-sensitive.

```env
# Optional overrides:
ZDROWSKIT_ENABLE_LLM_VERIFICATION=0
ZDROWSKIT_VERIFICATION_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_VERIFICATION_REWRITE_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_VERIFY_JSON_MODE=1
# Defaults to ZDROWSKIT_DEEPSEEK_THINKING when unset.
ZDROWSKIT_VERIFY_DEEPSEEK_THINKING=disabled
ZDROWSKIT_MAX_TOKENS_VERIFICATION=4096
ZDROWSKIT_MAX_TOKENS_VERIFICATION_REWRITE=4096
ZDROWSKIT_MAX_VERIFICATION_REVISIONS=1
ZDROWSKIT_VERIFY_INSIGHTS=1
ZDROWSKIT_VERIFY_COACH=1
ZDROWSKIT_VERIFY_NUDGE=1
```

Verification traces are logged as `insights_verify`, `insights_rewrite`, `coach_verify`, `coach_rewrite`, `nudge_verify`, and `nudge_rewrite`.

The original source call metadata also records the verifier verdict, issue counts, issue details, and verifier/rewrite call IDs. Use `uv run python main.py llm-log --id N` on either the source call or a verifier call to see the related verification trace.
