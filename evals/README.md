# Evals

Data-driven eval harness that tests LLM behaviour across 25 cases and 14 scenarios. Uses pinned blueprint data (committed snapshots of real context files and health data) for reproducibility. Each case applies a named scenario perturbation and asserts on the response using pattern matching, tool-call validation, or SQL execution.

## Running

```bash
uv run python -m evals.run                                          # all cases, default model (opus)
uv run python -m evals.run --suite core                             # product-gating core only
uv run python -m evals.run --no-cache                               # bypass eval cache for a fresh run
uv run python -m evals.run sleep_last_night_chat                    # single case by ID
uv run python -m evals.run --category sleep_markers                 # all cases in a category
uv run python -m evals.run --model anthropic/claude-sonnet-4-6      # specific model
uv run python -m evals.run --model anthropic/claude-sonnet-4-6,anthropic/claude-haiku-4-5-20251001  # compare
uv run python -m evals.run --reasoning-effort low                   # pass reasoning effort hint
uv run python -m evals.data.extract                                 # refresh baseline blueprint from live data
uv run python -m evals.data.extract --name sparse_week              # snapshot a named blueprint
```

## Suites

- **`core`** — smaller product-gating cases that should stay green before prompt/model changes ship
- **`benchmark`** — broader comparison cases for regression review and model selection

## Blueprints

3 pinned blueprints: `baseline`, `sparse_week`, `completed_week`. Responses are cached by default in `evals/.cache.sqlite` using a strong key built from the rendered messages, tool schema, model, case identity, and key runtime settings. Use `--no-cache` when you explicitly want a fresh run.

## Categories (25 cases)

| Category | Cases | What it tests |
|----------|-------|---------------|
| `nudge_contract` | 5 | Nudge `SKIP` vs fire behavior, short-message limits, and trigger-specific usefulness |
| `sleep_markers` | 5 | Sleep date convention ("last night" resolves to yesterday's row), sync_pending not flagged, not_tracked threshold (1 day OK, 3 consecutive flagged) |
| `weekly_report` | 2 | Mid-week progress checks avoid premature judgment; full weekly reviews include required sections and `<memory>` |
| `recovery_verdict` | 3 | Correct coaching signal for crashed / green / mixed recovery markers |
| `chat_data` | 4 | Current-week answers avoid unnecessary SQL; historical and unknown-data questions use tools honestly |
| `context_update` | 4 | Context file updates on durable changes, with at least one case checking `action` + `section`, and no update on casual questions |
| `chart_generation` | 2 | Trend questions produce renderable charts; single-value questions do not |

## Scenarios

Scenarios perturb the blueprint data to test edge cases:

- `baseline` — unmodified real data (control)
- `rest_day` / `training_day_missed` / `boring_new_data` — nudge skip/fire logic
- `sleep_last_night_query` — yesterday has sleep data, today has none (tests date resolution)
- `sleep_not_tracked_3_consecutive` — 3-day gap triggers a flag
- `recovery_crashed` / `recovery_green` / `recovery_mixed` — coaching verdict accuracy
- `midweek_wednesday` — truncated week tests premature judgment

## Architecture

The chat eval path mirrors the daemon's tool loop, so multi-turn chat cases can use `run_sql`, accumulate rows for chart rendering, and then return a final answer for assertion checks. Results include a rich comparison table, separate suite labeling, and bar charts for pass rate, latency, cost, and token usage across models.
