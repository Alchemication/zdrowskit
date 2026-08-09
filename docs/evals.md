# LLM Evals

LLM evals live in `evals/` and are feedback-derived regressions, not a broad generated benchmark. They preserve real product judgement: start with an actual thumbs-down Telegram feedback item, inspect the stored trace, then encode the smallest case that would have caught the issue.

Useful commands:

```bash
uv run python main.py llm-log --feedback
uv run python main.py llm-log --id N
```

## Philosophy

- Start every eval cluster with a `real_regression` case from one real failure. This is the default and the preferred shape; reach for the hidden-artifact exception below only when a thumbs-down is structurally impossible.
- Add only the minimum synthetic cases needed to broaden the surface around that failure, such as an explicit positive control or a false-positive guard.
- Keep synthetic cases tied to the original feedback using `source_feedback_id`, `source_llm_call_id`, and `derived_from.hypothesis`.
- Hidden-artifact exception: when the artifact under test is structurally invisible to the user (for example the `<memory>` block is stripped by `cmd_insights` before delivery, so a thumbs-down cannot reach it), a cluster may anchor on a stored LLM call / trace instead of a feedback row. Set `source_feedback_id: 0`, point `source_llm_call_id` and `derived_from.trace_id` at the real call, and explain why direct feedback is unavailable in `notes`. The anchor case is still treated as the "real" regression for the cluster even though `case_kind` is `synthetic_negative`.
- Prefer structured fixtures over pasted raw transcripts: pinned date, context snippets, conversation turns, and only the health data needed for the case.
- Use deterministic assertions first: tool called/not called, argument matching, text contains/does-not-contain, memory block checks, max word count, and forbidden openings. For `memory_contains` and `text_contains`, the assertion passes only when **all** patterns match (AND); for `memory_absent` and `text_absent`, the assertion fails as soon as **any** pattern matches (OR). To express "any of these is fine" in a single check, combine them into one regex with `|`.
- Use `judge_assertions` only for narrow semantic invariants that deterministic checks would make brittle. The runner evaluates deterministic assertions first, then makes one structured judge call only when those pass.

## LLM-as-Judge

Cases may define optional `judge_assertions`:

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

If `judge_assertions` is absent, no judge call is made. Judge output uses a Pydantic response schema; invalid structured output fails the judge assertion path. The default judge model is `anthropic/claude-sonnet-4-6`; override with `ZDROWSKIT_EVAL_JUDGE_MODEL`.

The judge runs **only when every deterministic assertion passes**. This keeps judge cost off the failure path, but it has a side effect: if a case currently fails on a deterministic assertion, fixing that failure can newly expose a previously-hidden judge failure on the same response. When iterating on a regression, expect the failure surface to migrate from deterministic to semantic as you converge.

## Running Evals

```bash
uv run python -m evals.run                              # all feedback-derived eval cases
uv run python -m evals.run chat_log_life_disruption     # one case
uv run python -m evals.run --feature chat               # feature filter
uv run python -m evals.run --feature insights           # insights writer cases
uv run python -m evals.run --details                    # debug failed cases
uv run python -m evals.run --repeat 3                   # sample each case 3x and report per-case pass rates
uv run python -m evals.run --repeat 3 --concurrency 12  # same, fanned out across workers
uv run python -m evals.run --record                     # persist a run to evals/leaderboard/runs.jsonl
uv run python -m evals.run --repeat 3 --concurrency 12 --record   # a leaderboard-quality production run
uv run python -m evals.matrix --feature chat --models deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro --reasoning-efforts high --record
uv run python -m evals.matrix --feature insights --models anthropic/claude-opus-5,deepseek/deepseek-v4-pro --reasoning-efforts high --record
uv run python -m evals.matrix --production --record     # current configured smoke suite
uv run python -m evals.leaderboard render               # rebuild evals/leaderboard.md from raw history
uv run python -m evals.leaderboard render-html          # rebuild evals/leaderboard.html with filters and sortable views
```

These evals call the configured real model and may use network/API quota.

Some models reject a `temperature` parameter (for example `claude-opus-5`). For those, pass `--no-temperature` to omit it from the request.

### Repeats and caching

Identical inputs produce different output between runs, so a single run is one sample, not a verdict. `--repeat N` runs every case N times and reports a per-case pass rate, marking anything strictly between 0 and N as `FLAKY`. Use it whenever a result will inform a decision — adding a case, judging a prompt change, comparing models.

Response caching is **off by default**, and `--repeat` refuses to run with `--cache`: a cached response is one frozen sample replayed, so a cached suite reports perfect stability however variable the model actually is. `--cache` exists for iterating on assertions against a fixed response, never for judging a model.

A consistent 0/N is a healthy result — it documents a real gap. Flakiness is the dangerous state, because one run reports it as a clean pass or a clean failure with equal confidence.

### Concurrency

Eval calls are network-bound, so repeats do not have to run back to back. `--concurrency N` runs N executions at once, across *all* cases and repeats — not just the repeats of one case. It defaults to `--repeat` and is capped at `EVAL_MAX_CONCURRENCY` (12) in `evals/framework.py`; the cap is there for the providers, since a burst large enough to trip a rate limit turns into retries and errored cases that cost more time than the parallelism saved.

Results are returned in submission order regardless of completion order, so the printed table and the recorded aggregation do not shuffle between runs. `--concurrency` above 1 refuses to run with `--cache`, whose SQLite connection is not safe to share across threads.

## Supported features

- `chat` — exercises the full chat tool loop end-to-end, taking the model from `--model`.
- `insights` — exercises the insights writer prompt and `run_sql` tool loop, taking the model from `--model`. It does not call verification/rewrite, render charts, save reports, update history, or send Telegram messages.
- `verification_judge` — exercises only the verifier prompt and structured response schema. The surface (`nudge`, `insights`, or `coach`) comes from `fixture.kind`. It does not call `verify_and_rewrite`, resolve `model_prefs`, write DB rows, or invoke rewrites.

## Model Matrices

Use `evals.run` for one smoke run and `evals.matrix` for comparisons.

Direct LLM features (`chat`, `insights`) take `--models` from the matrix runner:

```bash
uv run python -m evals.matrix \
  --feature insights \
  --models deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro,anthropic/claude-sonnet-4-6 \
  --reasoning-efforts high \
  --no-temperature \
  --record
```

Verifier judgement uses the same model-matrix shape:

```bash
uv run python -m evals.matrix \
  --feature verification_judge \
  --models deepseek/deepseek-v4-pro,anthropic/claude-sonnet-4-6 \
  --reasoning-efforts high \
  --no-temperature \
  --record
```

Recorded runs store both the requested CLI model and the actual per-case route. Mixed all-case runs are useful as smoke checks, but model decisions should be made from feature-scoped sections.

## Leaderboard

Recorded runs live in `evals/leaderboard/runs.jsonl`. The Markdown snapshot is `evals/leaderboard.md` and the interactive report is `evals/leaderboard.html`, both generated from that history.

The leaderboard answers two questions, in this order.

**Production** — one row per feature, taken from the newest run that resolved production routes (`evals.run` or `evals.matrix --production` with no `--model`), scored against the cases that exist in `evals/cases` today. This is the "how does what we ship perform" view, and it is the landing section. It calls out what the numbers do not cover:

- features with cases but no recorded production run at all,
- cases added since the run that produced the row,
- rows recorded before the current commit,
- rows recorded at `repeat=1`, where stability is unmeasured.

**Variations** — one table per feature, comparing models, reasoning efforts, routes and repeat settings within that feature. Grouping is by feature, not by case set: a feature maps to one production route in `model_prefs`, matrix runs are already feature-scoped, and a model decision only means something inside a feature. Keying sections on the case-set hash instead made every newly added eval case start a fresh section with no history to compare against.

Each row carries two accuracies, because they answer different questions:

- **Strict** — the share of cases that passed *every* attempt. A flaky case scores as the unreliable result it is.
- **Attempt** — attempt-weighted, the score a single run would be expected to report.

They are equal at `repeat=1` and diverge exactly when cases are flaky. Rows rank on strict accuracy first. Errored attempts (a provider 500, say) stay out of both denominators, so an outage cannot read as a quality regression. Cost is reported per covered run (`total_cost / repeat`) so a `repeat=5` row is not ranked against a `repeat=1` row at five times the price.

Repeat count is part of a row's identity, so a 5-sample run and a 1-sample run of the same commit and route stay as separate rows and never merge.

Each record stores run-level metadata (`requested_model`, `is_production`, `reasoning_effort`, `repeat`, `git_sha`) plus one aggregated entry per case holding its pass rate, flaky flag, actual route, and the raw per-attempt latency, cost and failures.

The leaderboard is published to GitHub Pages at <https://alchemication.github.io/zdrowskit/evals/>, as one section of the public site built by `.github/workflows/pages.yml`. That workflow runs `marketing/build.py`, which renders the landing page, these docs, and the leaderboard into `_site/`; the leaderboard is re-rendered from `runs.jsonl` rather than copied from the committed snapshot, so the published page is never the stale one. Enable Pages with **Settings -> Pages -> Source: GitHub Actions**; after that, pushes to `main` that touch `evals/leaderboard/runs.jsonl`, `docs/`, or `marketing/` rebuild and deploy. The workflow can also be run manually from the Actions tab.

The landing page cites the current eval case count and the date of the latest recorded run. Both are substituted at build time from `runs.jsonl`, so neither can drift from the history it summarises — see `landing_placeholders()` in `marketing/build.py`.
