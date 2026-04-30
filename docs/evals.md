# LLM Evals

LLM evals live in `evals/` and are feedback-derived regressions, not a broad generated benchmark. They preserve real product judgement: start with an actual thumbs-down Telegram feedback item, inspect the stored trace, then encode the smallest case that would have caught the issue.

Useful commands:

```bash
uv run python main.py llm-log --feedback
uv run python main.py llm-log --id N
```

## Philosophy

- Start every eval cluster with a `real_regression` case from one real failure.
- Add only the minimum synthetic cases needed to broaden the surface around that failure, such as an explicit positive control or a false-positive guard.
- Keep synthetic cases tied to the original feedback using `source_feedback_id`, `source_llm_call_id`, and `derived_from.hypothesis`.
- Prefer structured fixtures over pasted raw transcripts: pinned date, context snippets, conversation turns, and only the health data needed for the case.
- Use deterministic assertions first: tool called/not called, argument matching, text contains/does-not-contain, max word count, and forbidden openings.
- Avoid LLM-as-judge unless a future real feedback case genuinely cannot be evaluated deterministically.

## Running Evals

```bash
uv run python -m evals.run                              # all feedback-derived eval cases
uv run python -m evals.run chat_log_life_disruption     # one case
uv run python -m evals.run --feature chat               # feature filter
uv run python -m evals.run --details                    # debug failed cases
uv run python -m evals.run --record                     # persist a run to evals/leaderboard/runs.jsonl
uv run python -m evals.run --no-temperature             # omit temperature, required by claude-opus-4-7
uv run python -m evals.leaderboard render               # rebuild evals/leaderboard.md from raw history
uv run python -m evals.leaderboard render-html          # rebuild evals/leaderboard.html with filters and sortable views
```

These evals call the configured real model and may use network/API quota.

## Leaderboard

Recorded leaderboard runs live in `evals/leaderboard/runs.jsonl`. The generated Markdown snapshot lives in `evals/leaderboard.md`.

Comparisons are scope-aware: runs over different case sets are rendered in separate sections rather than ranked together.

The interactive HTML report lives in `evals/leaderboard.html` and is generated from the same raw JSONL history.

`evals/leaderboard.html` is published to GitHub Pages by `.github/workflows/evals-pages.yml` at <https://alchemication.github.io/zdrowskit/>. Enable Pages with **Settings -> Pages -> Source: GitHub Actions**; after that, pushes to `main` that update `evals/leaderboard/runs.jsonl` rebuild and deploy the latest leaderboard as the Pages `index.html`. The workflow can also be run manually from the Actions tab.
