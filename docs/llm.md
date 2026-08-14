# LLM Setup

zdrowskit uses separate LLM routes for separate jobs. Chat, weekly reports,
coach reviews, nudges, preference parsing, workout cloning, memory, verification,
and verification rewrites can each use a different primary and fallback model.

This separation is deliberate. A model that is good enough to parse a `/notify`
request is not automatically a good choice for an evidence-heavy weekly report,
and an interactive chat route has a different latency budget from a background
verifier.

## Built-in routes

These are the defaults returned by `model_prefs.default_model_prefs()`:

| Feature | Primary | Fallback | Reasoning |
| --- | --- | --- | --- |
| Reports (`insights`) | `openai/gpt-5.6-luna` | `deepseek/deepseek-v4-flash` | high |
| Coach | `openai/gpt-5.6-luna` | `deepseek/deepseek-v4-flash` | high |
| Nudges | `openai/gpt-5.6-luna` | `deepseek/deepseek-v4-flash` | high |
| Chat | `openai/gpt-5.6-luna` | `anthropic/claude-haiku-4-5` | high |
| Verification | `deepseek/deepseek-v4-flash` | `openai/gpt-5.6-luna` | high |
| `/notify` parser | `deepseek/deepseek-v4-flash` | `anthropic/claude-haiku-4-5` | off |
| `/add` workout clone | `deepseek/deepseek-v4-flash` | `anthropic/claude-haiku-4-5` | high |
| Weekly memory | `openai/gpt-5.6-luna` | `anthropic/claude-haiku-4-5` | off |
| Verification rewrite | `deepseek/deepseek-v4-flash` | `anthropic/claude-haiku-4-5` | high |

Temperature is omitted for the built-in routes. `reasoning_effort` is the one
reasoning control: Anthropic receives it directly; DeepSeek translates
`high`/`max` into thinking mode and treats the other values as thinking off.

The defaults are current routing decisions, not permanent recommendations.
Model quality, latency, and pricing move. The [LLM evals](evals.md) and their
[published leaderboard](https://alchemication.github.io/zdrowskit/evals/) are
the evidence used to compare routes.

## Configure a profile

Effective routes are stored per health profile:

```text
~/Documents/zdrowskit/profiles/<name>/model_prefs.json
```

Inspect them with:

```bash
uv run python main.py models
uv run python main.py models --profile anna
uv run python main.py models --profile anna doctor
```

Set or reset one route:

```bash
uv run python main.py models set chat openai/gpt-5.6-luna \
  --fallback anthropic/claude-haiku-4-5 \
  --reasoning high \
  --temperature omit

uv run python main.py models reset chat
uv run python main.py models reset --all
```

`--fallback auto` removes the feature-specific fallback and uses its `pro` or
`flash` profile fallback. The profile defaults themselves can also be changed:

```bash
uv run python main.py models profile flash \
  --primary deepseek/deepseek-v4-flash \
  --fallback anthropic/claude-haiku-4-5
```

Add `--profile NAME` before the models subcommand when changing another
profile. Telegram `/models` exposes the same feature routes as buttons and
always changes the sender's own profile.

## API keys

The built-in routes need all three keys in `.env`:

```env
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

Routes are sent through
[LiteLLM](https://github.com/BerriAI/litellm). If you choose another provider,
add the credential that provider's LiteLLM integration expects.

## Environment defaults

Environment variables seed the built-in route configuration. A saved
`model_prefs.json` feature override remains authoritative until it is reset.
The most useful model variables are:

```env
ZDROWSKIT_PRIMARY_PRO_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_FALLBACK_PRO_MODEL=anthropic/claude-opus-5
ZDROWSKIT_PRIMARY_FLASH_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_FALLBACK_FLASH_MODEL=anthropic/claude-haiku-4-5

ZDROWSKIT_INSIGHTS_MODEL=openai/gpt-5.6-luna
ZDROWSKIT_COACH_MODEL=openai/gpt-5.6-luna
ZDROWSKIT_NUDGE_MODEL=openai/gpt-5.6-luna
ZDROWSKIT_CHAT_MODEL=openai/gpt-5.6-luna
ZDROWSKIT_NOTIFY_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_ADD_CLONE_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_MEMORY_MODEL=openai/gpt-5.6-luna
ZDROWSKIT_VERIFICATION_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_VERIFICATION_REWRITE_MODEL=deepseek/deepseek-v4-flash
```

Token-budget and verification overrides are documented in the repository's
`.env_example`; `src/config.py` is the executable source of truth.

## Verification

Reports, coach reviews, and nudges are verified before they are saved or sent.
The verifier returns one of three outcomes:

- `pass` — deliver the draft;
- `revise` — run one bounded rewrite by default, then apply deterministic
  output guards;
- `fail` — suppress the output.

Chat is not verified because it is interactive and latency-sensitive. Weekly
memory is a separate call after report generation, not a hidden section written
by the report model. Sync alerts do not use an LLM at all.

Verification can be disabled globally or per async surface:

```env
ZDROWSKIT_ENABLE_LLM_VERIFICATION=0
ZDROWSKIT_VERIFY_INSIGHTS=0
ZDROWSKIT_VERIFY_COACH=0
ZDROWSKIT_VERIFY_NUDGE=0
ZDROWSKIT_MAX_VERIFICATION_REVISIONS=1
```

The rewrite budget is derived from the writer budgets so a full draft still
fits while corrections are applied.

## Traces, failures, and cost

One product operation creates one LLM trace. Writer tool calls, forced final
synthesis, verification, and rewrites are separate call rows linked by that
trace.

```bash
uv run python main.py llm-log --id 42
uv run python main.py llm-log --trace 7
uv run python main.py llm-log --feedback
uv run python main.py llm-log --stats
uv run python main.py llm-log --profile anna --stats
```

Call IDs and trace IDs are local to a profile database. Always select the
profile before investigating another person's ID.

There is intentionally no fixed cost forecast here. Spend depends on chat
volume, how many nudge attempts return `SKIP`, verifier rewrites, fallback use,
and the routes saved for each profile. `llm-log --stats` reports the usage and
cost actually recorded in that database; the eval leaderboard reports measured
quality, latency, and cost for controlled comparisons.
