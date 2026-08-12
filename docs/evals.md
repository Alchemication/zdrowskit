# LLM Evals

## What an eval is here

Every LLM feature is scored against a fixed set of **cases**. A case is one
frozen input — a pinned date, the context files, the health data, the
conversation so far — plus assertions about what the answer must or must not
contain. Running a case sends that input to a real model and checks the reply.

One case in plain English (`chat_explicit_add_to_log`):

> **Given** it is 9 April 2026, the log already holds yesterday's tempo run, and
> the user says *"Add to log: small one is sick today and did not go to creche,
> so strength may not happen."*
>
> **Assert** chat calls `update_context` exactly once, appends to `log`, and
> dates the entry `2026-04-09` — today, not the date of the entry above it.

That is the whole idea. Everything below is detail.

These are regressions, not a benchmark. They exist to stop specific defects
coming back, so the score only means something next to the case list.

## What gets measured

Each feature is one prompt and one call path, isolated so a failure names a
stage rather than a pipeline.

| Feature | Exercises | Deliberately skips |
| --- | --- | --- |
| `chat` | The full chat tool loop, end to end. | — |
| `insights` | The weekly-insights writer prompt and its `run_sql` loop. | Verification, rewrite, charts, saved reports, Telegram. |
| `nudge` | The nudge writer prompt and its `run_sql` loop. | Verification, rewrite, saved nudges, Telegram. |
| `memory` | The `<memory>` block written from a finished report. No tool loop — the report is already written. | Everything downstream of the block. |
| `verification_judge` | The verifier prompt and its structured response schema. `fixture.kind` picks the surface: `nudge`, `insights` or `coach`. | `verify_and_rewrite`, `model_prefs`, DB writes, rewrites. |

Case files live in `evals/cases/*.json`, one case per file, named for the
behaviour they pin.

## Where cases come from

**Start from a real failure.** That is the recommendation and the default
shape: a thumbs-down in Telegram, or — for output the user never sees — a bad
call you found in the log. Inspect the trace, then encode the smallest case
that would have caught it.

```bash
uv run python main.py llm-log --feedback   # thumbs-down items
uv run python main.py llm-log --id N       # one call: messages, tools, response
```

Real failures are preferred because they are proof the defect ships. They
already carry the exact fixture that broke, and they cannot be argued with.

**Good synthetic cases are welcome too.** A synthetic case earns its place when
it pins a behaviour you would ship a fix for and would catch a plausible
regression. The useful shapes:

- a **positive control** — the model must *keep* doing the right thing
  (`verification_judge_nudge_passes_accurate_week_totals` exists so a
  trigger-happy verifier cannot score well by suppressing everything);
- a **false-positive guard** — the model must not fire on the neighbouring case
  that only looks similar;
- a **stated rule with no failure yet** — a hard constraint the prompt makes,
  like "a nudge fits a phone notification".

What does not earn its place: broad taste tests ("is this good coaching"),
scenarios generated in bulk by an LLM, and anything with no stated hypothesis.

Whatever the seed, keep the provenance honest:

| Field | Meaning |
| --- | --- |
| `case_kind` | `real_regression`, `synthetic_positive`, or `synthetic_negative`. |
| `source_feedback_id` | The thumbs-down row, or `null` when there is none. Never `0` — that names a row that was never written. |
| `source_llm_call_id` | The real call the fixture was lifted from. Required, including for synthetic cases: copy the shape of a real call rather than inventing one. |
| `derived_from.hypothesis` | One sentence on what the case pins and why. |

Prefer structured fixtures — pinned date, context snippets, conversation turns,
and only the health data the case needs — over pasted raw transcripts.

## Assertions

Deterministic checks run first. Available types:

`tool_called`, `tool_not_called`, `tool_arg_matches`, `text_contains`,
`text_absent`, `text_without_chart_absent`, `memory_present`, `memory_contains`,
`memory_absent`, `memory_bullet_max`, `word_count_max`,
`visible_char_count_max`, `forbidden_opening`.

Multi-pattern assertions are not all alike: `text_contains` and
`memory_contains` pass only when **every** pattern matches, while `text_absent`
and `memory_absent` fail as soon as **any** pattern matches. To say "any one of
these is acceptable", write one regex with `|`.

`judge_assertions` are for narrow semantic invariants a regex would make
brittle. Each statement must be concrete enough that two readers would agree:

```json
{
  "judge_assertions": [
    {
      "name": "accepts_valid_tempo_structure",
      "statement": "The response says or clearly implies that 3 km easy followed by the last 2 km at tempo counts as the prescribed 2 km tempo block."
    }
  ]
}
```

The judge runs **only when every deterministic assertion passes**, which keeps
its cost off the failure path. Side effect worth expecting: fixing a
deterministic failure can newly expose a judge failure on the same response, so
the failure surface migrates from mechanical to semantic as you converge.

Omit `judge_assertions` and no judge call is made. The judge answers into a
Pydantic schema; invalid structured output fails the case. Default judge model
is `anthropic/claude-sonnet-4-6`, overridable with `ZDROWSKIT_EVAL_JUDGE_MODEL`.

## Running them

These call real models and spend API quota. `uv run pytest` never does.

```bash
uv run python -m evals.run                              # every case
uv run python -m evals.run chat_log_life_disruption     # one case
uv run python -m evals.run --feature insights           # one feature
uv run python -m evals.run --details                    # show why a case failed
uv run python -m evals.run --repeat 3 --concurrency 12  # 3 samples per case, in parallel
uv run python -m evals.run --repeat 3 --concurrency 12 --record   # …and publish it
```

Some models reject `temperature` (`claude-opus-5`, for one). Pass
`--no-temperature` to omit it.

### One run is one sample

The same input produces different output each time, so a single run is not a
verdict. `--repeat N` runs every case N times and reports a per-case pass rate,
marking anything strictly between 0 and N as `FLAKY`. Use it whenever the result
will inform a decision — adding a case, judging a prompt change, comparing
models.

A consistent 0/N is a healthy result: it documents a real gap. Flakiness is the
dangerous state, because a single run reports it as a clean pass or a clean
failure with equal confidence.

`--concurrency N` fans out across all cases and repeats at once. It defaults to
`--repeat` and is capped at `EVAL_MAX_CONCURRENCY` (12) in
`evals/framework.py` — a burst big enough to trip a provider rate limit costs
more in retries than the parallelism saves. Results are returned in submission
order, so the table does not shuffle between runs.

Response caching is **off by default** and `--repeat` refuses to run with
`--cache`: a cached response is one frozen sample replayed, which reports
perfect stability no matter how variable the model is. `--cache` is for
iterating on assertions against a fixed response, never for judging a model.

## Comparing models

`evals.run` scores what ships. `evals.matrix` compares alternatives, one
feature at a time:

```bash
uv run python -m evals.matrix \
  --feature verification_judge \
  --models deepseek/deepseek-v4-pro,anthropic/claude-sonnet-4-6 \
  --reasoning-efforts high \
  --no-temperature \
  --record

uv run python -m evals.matrix --production --record   # smoke-check the shipped routes
```

Recorded runs store both the requested model and the actual per-case route.
Mixed all-case runs are smoke checks; make model decisions from feature-scoped
sections.

## Two scores, and why

Every recorded row carries both:

- **Strict** — the share of cases that passed *every* attempt. A flaky case
  counts as the unreliable result it is.
- **Attempt** — attempt-weighted: the score a single run would be expected to
  report.

They are equal at `repeat=1` and diverge exactly when cases are flaky. Rows rank
on strict. Errored attempts (a provider 500) stay out of both denominators, so
an outage cannot read as a quality regression. Cost is reported per covered run
(`total_cost / repeat`) so a 5-sample row is not ranked against a 1-sample row
at five times the price.

## Trajectory

Pass rate says whether a case landed. It says nothing about how, and "how" is
where the cost, the latency and much of the instability live. Every attempt
therefore records the tool calls it made, in order:

```
chat_running_speed_trend_pace_format   2 paths   3/3 passing
  ×2   run_sql → reply
  ×1   run_sql → run_sql → update_context → reply
```

Three passes, two different routes. The score is clean and the behaviour is
not settled — a distinction the accuracy columns cannot make.

Recorded per case: average, minimum and maximum tool calls, every distinct
path with the number of attempts that took it, and whether any attempt
exhausted `max_tool_iterations` and was forced to answer with what it had. The
leaderboard shows the average per row and expands to the full paths on click.

Features with no tool loop — `memory` and `verification_judge` are single calls
by design — record this as unknown rather than zero, so "was never given tools"
cannot be read as "chose not to use them".

## Leaderboard

Recorded runs append to `evals/leaderboard/runs.jsonl`. Both views are generated
from that file:

```bash
uv run python -m evals.leaderboard render        # evals/leaderboard.md
uv run python -m evals.leaderboard render-html   # evals/leaderboard.html
```

It answers two questions, in this order:

1. **Production** — one card per feature, from the newest run that resolved
   production routes, scored against the cases on disk today. It says out loud
   what the numbers do not cover: features never recorded, cases added since the
   run, rows where a fallback model answered, and `repeat=1` rows whose
   stability is unmeasured.
2. **Model comparisons** — one table per feature, comparing models, reasoning
   efforts, routes and repeat counts. Grouping is by feature because a feature
   maps to one production route in `model_prefs` and a model decision only means
   something inside a feature. Repeat count is part of a row's identity, so a
   5-sample and a 1-sample run of the same route stay separate rows.

Each record stores run-level metadata (`requested_model`, `is_production`,
`reasoning_effort`, `repeat`, `git_sha`) plus one aggregated entry per case with
its pass rate, flaky flag, actual route, trajectory, and raw per-attempt
latency, cost, tool path and failures. There is no backward-compatibility contract on this file: when the
shape changes, update the renderers and tests with it.

The leaderboard is published at
<https://alchemication.github.io/zdrowskit/evals/> by `marketing/build.py`,
which re-renders it from `runs.jsonl` rather than copying the committed
snapshot, so the published page is never the stale one. Pushes to `main` that
touch `evals/leaderboard/runs.jsonl`, `docs/` or `marketing/` redeploy it;
enable it once with **Settings → Pages → Source: GitHub Actions**.

The landing page cites the current case count and the date of the latest
recorded run, both substituted at build time from the same `runs.jsonl` — see
`landing_placeholders()` in `marketing/build.py`.
