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
uv run python -m evals.run --record                     # persist a run to evals/leaderboard/runs.jsonl
uv run python -m evals.matrix --feature chat --models deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro --reasoning-efforts high --record
uv run python -m evals.matrix --feature insights --models anthropic/claude-opus-4-8,deepseek/deepseek-v4-pro --reasoning-efforts high --record
uv run python -m evals.matrix --production --record     # current configured smoke suite
uv run python -m evals.leaderboard render               # rebuild evals/leaderboard.md from raw history
uv run python -m evals.leaderboard render-html          # rebuild evals/leaderboard.html with filters and sortable views
```

These evals call the configured real model and may use network/API quota.

Some models reject a `temperature` parameter (for example `claude-opus-4-8`). For those, pass `--no-temperature` to omit it from the request.

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

Recorded leaderboard runs live in `evals/leaderboard/runs.jsonl`. The generated Markdown snapshot lives in `evals/leaderboard.md`.

Comparisons are scope-aware: runs over different case sets are rendered in separate sections rather than ranked together.

Each recorded run includes per-feature pass/fail summaries and per-case route metadata. This keeps all-case smoke runs honest when different features resolve different production model routes.

The interactive HTML report lives in `evals/leaderboard.html` and is generated from the same raw JSONL history.

`evals/leaderboard.html` is published to GitHub Pages by `.github/workflows/evals-pages.yml` at <https://alchemication.github.io/zdrowskit/>. Enable Pages with **Settings -> Pages -> Source: GitHub Actions**; after that, pushes to `main` that update `evals/leaderboard/runs.jsonl` rebuild and deploy the latest leaderboard as the Pages `index.html`. The workflow can also be run manually from the Actions tab.
