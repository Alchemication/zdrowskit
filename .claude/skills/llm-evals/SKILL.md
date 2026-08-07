---
name: llm-evals
description: Use when adding, modifying, or running LLM evaluation cases in evals/ (zdrowskit project). Covers feedback-derived regression philosophy, case-kind taxonomy, provenance fields, fixture preferences, deterministic and judge assertions, route-aware leaderboard records, matrix runs, and the boundary between mocked pytest and opt-in real-LLM evals.
---

# LLM Evals (zdrowskit)

LLM evals in `evals/` are feedback-derived regressions, not a generated benchmark suite. Do not add broad LLM-created scenarios, stale blueprint/cache machinery, or cases without provenance.

Recorded eval history has no backward-compatibility contract. Keep the schema clean and update renderers/tests/callers together when the record shape changes.

## When adding eval coverage

- Start from a real thumbs-down feedback item. Use `uv run python main.py llm-log --feedback` to find it and `uv run python main.py llm-log --id N` to inspect the trace.
- Add the real failure first as `case_kind: "real_regression"`.
- Add only the minimum synthetic controls needed to isolate the hypothesis or guard a false positive, using `case_kind: "synthetic_positive"` or `case_kind: "synthetic_negative"`.
- Preserve provenance on every case with `source_feedback_id`, `source_llm_call_id`, and `derived_from.hypothesis`.
- Prefer structured fixtures: pinned date, context snippets, conversation turns, and only the health data needed for the behavior under test.
- Prefer deterministic assertions. Add LLM-as-judge only for narrow semantic invariants where tool-call, argument, text, word-count, or forbidden-opening assertions would be brittle or fake-precise.

## Silent failures (no thumbs-down to anchor on)

Thumbs-down feedback remains the preferred seed — start from `--feedback` whenever possible. But some LLM outputs are *never user-visible*, so no thumbs-down can ever land. The feedback queue is blind to these by construction.

Recurring blind-spot classes in this project:

- **Verifier suppression** — a nudge / insights / coach response is generated, the verifier rejects it, nothing ships to Telegram. Lives in `llm_calls` as `nudge_verify`, `insights_verify`, etc.
- **Stripped-from-output blocks** — the LLM emits structured side-channel content that is removed before the user sees the message. The `<memory>` block in insights is the live example: extracted server-side, appended to `history.md`, but stripped from the Telegram-visible text. A wrong or missing memory extraction never produces a thumbs-down.
- **Verifier-rewritten text** — the user only sees the rewrite, so the original draft's flaws never collect feedback.
- **Tool-use omissions** — the model produces a plausible-looking answer without calling the tool it should have. The user has nothing concrete to flag.

For all of these, the LLM call log replaces the feedback queue as the seed:

- Browse with `uv run python main.py llm-log --last N` (optionally `--json` and grep by request type) and inspect candidates with `--id N`. The prompt shows the candidate output + context, the response shows what was produced (verdict, memory block, rewrite, tool calls or lack thereof).
- A case is worth adding when *you* judge the silent output was wrong on review: a nudge was suppressed that you'd have wanted, memory dropped a signal that should have carried, the rewrite mangled the draft, the model skipped a needed tool call. Reviewer judgment replaces the user's thumbs-down as the signal.

Provenance for silent-failure cases:

- `source_llm_call_id`: the call that produced the silent output.
- `source_feedback_id`: `null` — state it explicitly rather than omitting it, so a reader can tell "there is no feedback row" from "provenance was never filled in". Never use `0`: it names a feedback row that was never written.
- `derived_from.hypothesis`: state the reviewer judgment plainly, e.g. "verifier suppressed a reasonable post-run nudge because it read tempo phrasing as a contradiction" or "insights memory dropped the recurring evening-headache pattern that should carry across weeks."
- `case_kind` is still `real_regression` — the trace is real; only the seed differs.

Pick the feature to match what the case exercises: `verification_judge` for suppression/rewrite verdicts (`fixture.kind` picks `nudge` / `insights` / `coach`), and the relevant generation feature (e.g. `insights`) for memory-extraction or tool-use cases. Don't route silent-failure cases through full multi-step pipelines just to recreate the symptom.

## LLM-as-judge assertions

Use optional `judge_assertions` for semantic reasoning quality, not broad taste judgements. Keep each statement concrete and independently checkable. Good examples: “The response says or clearly implies that 3 km easy followed by 2 km tempo counts as the prescribed tempo block.” Bad examples: “The response gives good coaching advice.”

Execution rules:

- The runner evaluates deterministic `assertions` first.
- If any deterministic assertion fails, the judge is skipped.
- If deterministic assertions pass and `judge_assertions` exists, the runner makes one structured judge call for the case.
- Missing `judge_assertions` means no judge call.
- Judge output uses a Pydantic response schema; invalid structured output fails the judge path.
- All deterministic and judge assertions must pass for the case to pass.
- Default judge model is `anthropic/claude-sonnet-4-6`; override with `ZDROWSKIT_EVAL_JUDGE_MODEL`.

Template:

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

## Mocked vs. real LLM boundary

`uv run pytest` must stay mocked and must never call a real LLM. Manual evals are opt-in through `uv run python -m evals.run` or `uv run python -m evals.matrix`, which use configured real models and may spend API quota.

For prompt/tool behavior changes, run the relevant mocked tests plus the specific eval cases that represent the affected feedback cluster.

## Run-to-run variability

A single run is one sample, not a verdict. Identical inputs produce different
output between runs, and for graded outputs the variation is not only wording:
the verifier returns a *subset* of the issues it finds, so a draft with four
defects can be flagged for two of them on one run and a different two on the
next. A case asserting on one specific defect then passes or fails on a coin
flip while looking entirely deterministic.

- **Use `--repeat N` whenever a result will inform a decision** — adding a
  case, judging a prompt change, comparing models. The summary reports a
  per-case pass rate and marks anything strictly between 0 and N as `FLAKY`.
  A case at 2/3 has told you almost nothing yet.
- **Caching is off by default, and `--repeat` refuses to run with it.** A
  cached response is one frozen sample replayed, so a cached suite reports
  perfect stability however variable the model actually is. `--cache` exists
  for iterating on assertions against a fixed response; never use it to judge
  a model.
- **A consistent 0/N is a healthy result**, not a broken case: it documents a
  real gap. Flakiness is the dangerous state, because one run reports it as a
  clean pass or a clean failure with equal confidence.

When a case is flaky, suspect the fixture before the model. A fixture carrying
several independent defects measures which one the model chose to report.
Reduce it to a single defect — while keeping the artifact structurally
complete, since a verifier rejects an incomplete draft on structure alone and
never reaches its claims. Only once the fixture isolates one behavior is
residual flakiness a finding about the model. Never loosen an assertion merely
to turn a case green.

## Running evals

Use `evals.run` for one selected run:

```bash
uv run python -m evals.run
uv run python -m evals.run --feature chat --record
uv run python -m evals.run chat_log_life_disruption --details
uv run python -m evals.run --feature verification_judge --repeat 3
```

Use `evals.matrix` for model or route comparisons:

```bash
uv run python -m evals.matrix --production --record
uv run python -m evals.matrix --feature chat --models deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro --reasoning-efforts high --no-temperature --record
uv run python -m evals.matrix --feature verification_judge --models deepseek/deepseek-v4-pro,anthropic/claude-sonnet-4-6 --reasoning-efforts high --no-temperature --record
```

If `evals/.cache.sqlite` was wiped, the first run is fresh. Otherwise use `--refresh-cache` for real model comparisons.

## Model and route comparisons

- `chat` evals take the requested CLI model directly.
- `verification_judge` evals take the requested CLI model directly and exercise only the verifier prompt/schema. The surface (`nudge`, `insights`, `coach`) comes from `fixture.kind`.
- Do not use the full `verify_and_rewrite` pipeline for model selection. If rewrite behavior needs coverage, add a separate rewrite-step feature from real rewrite failures.
- Mixed `--production` runs are smoke/regression checks. Make model decisions from feature-scoped matrix runs.

Leaderboard records store run-level `requested_model` plus per-case actual `route` metadata. Actual model identity belongs to the case route, not a top-level compatibility field.
