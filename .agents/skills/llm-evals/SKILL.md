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

## Running evals

Use `evals.run` for one selected run:

```bash
uv run python -m evals.run
uv run python -m evals.run --feature chat --record
uv run python -m evals.run chat_log_life_disruption --details
```

Use `evals.matrix` for model or route comparisons:

```bash
uv run python -m evals.matrix --production --record
uv run python -m evals.matrix --feature chat --models deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro --reasoning-efforts high --no-temperature --record
```

If `evals/.cache.sqlite` was wiped, the first run is fresh. Otherwise use `--refresh-cache` for real model comparisons.

## Model and route comparisons

- `chat` evals take the requested CLI model directly.
- `nudge_verify` and `insights_verify` resolve the production `verification` and `verification_rewrite` routes through `model_prefs`; changing `--models` is not the way to A/B verifier behavior.
- To compare verifier models, change routes with `uv run python main.py models`, then run `uv run python -m evals.matrix --feature nudge_verify --record` or `--feature insights_verify --record`.
- Mixed `--production` runs are smoke/regression checks. Make model decisions from feature-scoped runs.

Leaderboard records store run-level `requested_model` plus per-case actual `route` metadata. Actual model identity belongs to the case route, not a top-level compatibility field.
