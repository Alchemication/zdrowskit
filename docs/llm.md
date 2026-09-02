# LLM Setup

zdrowskit uses separate LLM routes for separate jobs. Chat, weekly reports,
coach reviews, nudges, preference parsing, workout cloning, memory, verification,
and verification rewrites can each use a different primary and fallback model.

This separation is deliberate. A model that is good enough to parse a `/notify`
request is not automatically a good choice for an evidence-heavy weekly report,
and an interactive chat route has a different latency budget from a background
verifier.

## Built-in routes

Every feature resolves to a primary model, a fallback, and a reasoning setting.
Two shared tiers exist so routes do not each repeat a provider pair:

- **`pro`** — the high-capability pair, for long evidence-heavy generation.
- **`flash`** — the cheap, fast pair, for the high-volume surfaces.

A feature takes its tier's fallback unless it names one of its own, which
several do where an eval found the tier default was the wrong safety net. So
tier membership does not tell you the effective fallback; only the resolved
route does.

### When the fallback engages

A call that fails is retried on its own route first, with exponential backoff.
What happens after that depends on what failed:

- **The provider refused** — overloaded, or otherwise unwilling. The other
  provider is exactly the right answer, so the call crosses to the fallback and
  runs the same ladder there.
- **The network dropped** — a refused, reset or unassignable connection. The
  fallback is reached over the same socket layer that just failed, so crossing
  to it would only spend a second ladder of backoff to learn the same thing.
  The call stops after one ladder and reports the fault to its caller.

Transport faults used to get no retries at all, only an instant hop to the
other provider — which meant a blip lasting seconds consumed both routes and
failed the call. That is what lost the weekly report on 31 Aug 2026.

`reasoning_effort` is the one reasoning control: Anthropic receives it
directly; DeepSeek translates `high`/`max` into thinking mode and treats the
other values as thinking off. It is on for every judgment surface — reports,
coach, nudges, chat, verification, rewrites, `/add` — and off for the two
extraction jobs, `/notify` parsing and weekly memory, where selecting a
structured answer against a stated rule list is the whole task. Temperature is
omitted throughout.

This document does not list which model each feature currently uses. Those
choices change whenever an eval says they should, and a copy here goes stale
without anything failing. Print the live routes instead:

```bash
uv run python main.py models
uv run python main.py models --profile anna
```

`src/config.py` holds every default alongside the measurement that justified
it — the `DEFAULT_*_MODEL` docstrings are the reasoning behind the current
routing, not just its values. The [LLM evals](evals.md) and their
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
uv run python main.py models set chat PROVIDER/MODEL \
  --fallback PROVIDER/MODEL \
  --reasoning high \
  --temperature omit

uv run python main.py models reset chat
uv run python main.py models reset --all
```

Model identifiers are LiteLLM route strings, `provider/model`. Run
`main.py models` to see the ones in use.

`--fallback auto` removes the feature-specific fallback and uses its `pro` or
`flash` profile fallback. The tier defaults themselves can also be changed:

```bash
uv run python main.py models profile flash \
  --primary PROVIDER/MODEL \
  --fallback PROVIDER/MODEL
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

`uv run python main.py models doctor` reports keys missing for the models a
profile actually routes to, which is the check to run after changing a route or
dropping a provider.

## Environment defaults

Environment variables seed the built-in route configuration at import time. A
saved `model_prefs.json` feature override remains authoritative until it is
reset, so `.env` is the wrong tool for a routing change you want to keep — use
`models set`, which persists per profile.

Both tiers are settable:

```env
ZDROWSKIT_PRIMARY_PRO_MODEL=
ZDROWSKIT_FALLBACK_PRO_MODEL=
ZDROWSKIT_PRIMARY_FLASH_MODEL=
ZDROWSKIT_FALLBACK_FLASH_MODEL=
```

Each feature also has its own `ZDROWSKIT_<FEATURE>_MODEL` variable, and there
are token-budget and verification overrides besides. They are not listed here
with their values: `src/config.py` declares every one of them next to the
measurement that set it, and is the only copy that cannot drift from the code.

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
