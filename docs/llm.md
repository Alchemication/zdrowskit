# LLM Setup

zdrowskit relies on capable models. The coach writes personalised reports, decides when to stay quiet, generates SQL queries against your data, and produces chart code.

Default: Anthropic Opus 4.7 for async judgement surfaces, with high reasoning and temperature omitted. Telegram chat defaults to DeepSeek V4 Flash with DeepSeek thinking enabled for lower latency and cost.

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

The Telegram panel groups features as Chat / Reports / Coach / Nudges / Utilities and tags every model button with its capability tier: premium / pro / flash / lite. Chat exposes Reasoning and Temperature controls. `reasoning_effort` is the single reasoning knob: Anthropic gets it natively, and on DeepSeek, `high`/`max` translate into thinking mode (`extra_body={"thinking": {"type": "enabled"}}`) while `low`/`medium`/`none` leave thinking off.

A `Reset all` button on the main panel and `uv run python main.py models reset --all` restore everything to built-in defaults. Picking the `Auto` fallback, or `--fallback auto` from the CLI, defers to the profile's fallback so future profile changes propagate.

Insights, coach, and nudges default to `anthropic/claude-opus-4-7` with `reasoning_effort=high`, temperature omitted, and `deepseek/deepseek-v4-pro` fallback. Chat defaults to `deepseek/deepseek-v4-flash` with `reasoning_effort=high` (engages DeepSeek thinking), temperature omitted, and `anthropic/claude-haiku-4-5` fallback.

Lightweight utility surfaces, including `/notify` interpretation, `/log` flow building, and `/add` workout clone selection, default to `deepseek/deepseek-v4-flash` with `anthropic/claude-haiku-4-5` fallback. `/log`, `/add`, and verifier rewrites use `reasoning_effort=high` with temperature omitted; `/notify` stays plain Flash. On the DeepSeek primary, `high` engages thinking via translated `extra_body`; on the Anthropic fallback, the same effort is sent natively.

Logged LLM calls record the effective model, and fallback calls include `requested_model` and `fallback_used` in params/metadata.

## Cost Projection

Providers bill in USD per million tokens. Prices below were checked on 2026-05-02 against [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/) and [Anthropic's Opus 4.7 page](https://www.anthropic.com/claude/opus). Anthropic lists Opus 4.7 at $5/MTok input and $25/MTok output.

Current default routes:

| Feature | Primary | Normal cadence |
|---|---|---:|
| Weekly + midweek reports | `anthropic/claude-opus-4-7` | 2/week |
| Coach review | `anthropic/claude-opus-4-7` | 1/week |
| Nudges | `anthropic/claude-opus-4-7` | up to 2/day |
| Verification | `deepseek/deepseek-v4-pro` | reports, coach, nudges; Opus 4.7 fallback |
| Verification rewrites | `deepseek/deepseek-v4-flash` | only when verifier asks |
| Chat | `deepseek/deepseek-v4-flash` | on demand |

Using recent logged token sizes from this app, the always-on daemon lands around:

| Workload | Projected cost |
|---|---:|
| Reports, including DeepSeek verification | ~$0.20/week |
| Coach review | ~$0.10/week |
| Nudges at the 2/day cap, including DeepSeek verification | ~$0.75/week |
| **Daemon total at default caps** | **~$1.05/week** |

This assumes verification normally succeeds on DeepSeek Pro with thinking engaged (via `reasoning_effort=high`) and rewrite calls remain rare. Verification falls back to Opus 4.7 with the same `reasoning_effort=high` (sent natively) and omitted temperature.

Chat is separate because it is user-driven. Routing chat to DeepSeek Flash is usually under one cent per turn, but quality may drop for harder analysis.

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

ZDROWSKIT_INSIGHTS_MODEL=anthropic/claude-opus-4-7
ZDROWSKIT_COACH_MODEL=anthropic/claude-opus-4-7
ZDROWSKIT_NUDGE_MODEL=anthropic/claude-opus-4-7
ZDROWSKIT_CHAT_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_NOTIFY_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_LOG_FLOW_MODEL=deepseek/deepseek-v4-flash
# /log uses anthropic/claude-haiku-4-5 as its flash profile fallback
ZDROWSKIT_ADD_CLONE_MODEL=deepseek/deepseek-v4-flash

ZDROWSKIT_MAX_TOKENS_DEFAULT=4096
ZDROWSKIT_MAX_TOKENS_INSIGHTS=8192
ZDROWSKIT_MAX_TOKENS_COACH=8192
ZDROWSKIT_MAX_TOKENS_CHAT=4096
ZDROWSKIT_MAX_TOKENS_NUDGE=4096
ZDROWSKIT_MAX_TOKENS_NOTIFY=512
ZDROWSKIT_MAX_TOKENS_LOG_FLOW=4096
ZDROWSKIT_MAX_TOKENS_ADD_CLONE=512
ZDROWSKIT_MAX_TOKENS_VERIFICATION=8192
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

The default verifier uses `deepseek/deepseek-v4-pro` with `reasoning_effort=high` (engages DeepSeek thinking) and falls back to Opus 4.7 with the same effort sent natively, no temperature. Bounded rewrites stay on Flash by default, also with `reasoning_effort=high` and no temperature — DeepSeek translates `high` into thinking mode via `extra_body` while Anthropic uses it natively, so the same per-feature setting works across both providers.

```env
# Optional overrides:
ZDROWSKIT_ENABLE_LLM_VERIFICATION=0
ZDROWSKIT_VERIFICATION_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_VERIFICATION_REWRITE_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_MAX_TOKENS_VERIFICATION=8192
ZDROWSKIT_MAX_TOKENS_VERIFICATION_REWRITE=4096
ZDROWSKIT_MAX_VERIFICATION_REVISIONS=1
ZDROWSKIT_VERIFY_INSIGHTS=1
ZDROWSKIT_VERIFY_COACH=1
ZDROWSKIT_VERIFY_NUDGE=1
```

Verification traces are logged as `insights_verify`, `insights_rewrite`, `coach_verify`, `coach_rewrite`, `nudge_verify`, and `nudge_rewrite`.

The original source call metadata also records the verifier verdict, issue counts, issue details, and verifier/rewrite call IDs. Use `uv run python main.py llm-log --id N` on either the source call or a verifier call to see the related verification trace.
