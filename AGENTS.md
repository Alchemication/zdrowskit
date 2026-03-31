# AGENTS.md

## What is zdrowskit

Your 24/7 ultra-personal trainer. Parses Apple Health exports (metrics, workouts, GPX routes, sleep), stores them in SQLite, and uses an LLM to generate personalised weekly reports and short nudges via Telegram/email. A daemon watches for new data, fires nudges automatically, and listens for incoming Telegram messages for interactive two-way coaching chat — including ad-hoc data queries and on-demand charts.

## Commands

Always use `uv run` — never plain `python`. Run any subcommand with `--help` for full flags.

```bash
uv run python main.py import                      # import from Auto Export (default)
uv run python main.py import --source shortcuts    # import from iOS Shortcuts export
uv run python main.py insights        # LLM weekly report (add --week last|current, --telegram, --email, --explain)
uv run python main.py nudge           # short LLM nudge (add --trigger TYPE)
uv run python main.py coach           # coaching review with plan/goal proposals (add --week, --telegram, --email)
uv run python main.py report          # terminal summary (add --llm, --history, --json)
uv run python main.py status          # DB row counts + date range
uv run python main.py context         # show context files and their status
uv run python main.py llm-log         # query LLM call history (add --stats, --id N, --json)
uv run python main.py telegram-setup  # register bot /commands for Telegram autocomplete + menu
uv run python main.py daemon-restart  # restart the background launchd daemon
uv run python main.py daemon-stop     # stop and unload the background daemon
uv run python src/daemon.py --foreground  # run filesystem watcher + chat in foreground
uv run python -m evals.run               # run all AI evals (add --model, --scenario, --reasoning-effort)
uv run python -m evals.data.extract      # refresh eval blueprints from live data
```

## Collaboration Style

Challenge my ideas early. If an approach is over-engineered, fragile, or there's a simpler/better alternative I might be missing — say so directly with reasoning. Don't just execute instructions; flag knowledge gaps, hidden trade-offs, or narrowed thinking. Be pragmatic: save me from wasting time on something that could be done better.

**Verification:** After completing a complex or cross-cutting feature (touches multiple modules, changes interfaces, moves files), automatically run a verification pass before reporting done. Grep for stale references, check all callers of changed functions, confirm imports, run lint + tests, and fix any issues found — don't wait to be asked.

## Code Style

- **Linter/formatter:** `uv run ruff check .` and `uv run ruff format .`
- **Type hints:** required on all signatures. Use native types (`list`, `dict`, `str | None`) — never `typing.List` etc.
- **Docstrings:** Google style.

## Output Rules

- `print()` for user-facing content (reports, JSON) → stdout.
- `logger` (stdlib `logging`) for diagnostics, progress, errors → stderr.
- `rich` (import lazily) for structured terminal display (tables, panels).
- Error messages should tell the user what to do, not just what went wrong.

## Testing

Run with `uv run pytest`. Fixtures in `tests/fixtures/` and `tests/conftest.py`.

**Must have tests:** parsers (`src/parsers/`), aggregator, store (round-trips), report utilities (date arithmetic), and pure LLM utility functions (`extract_memory`, `_recent_history`, etc.).

**Style:**
- Group in classes (`class TestParseMetricsFile`).
- Use `tmp_path` for files, `in_memory_db` fixture for DB.
- Test edge cases that would silently break (None, missing fields, empty inputs).
- Run `uv run ruff check tests/ && uv run ruff format tests/` before committing.

## Key Modules

- `src/models.py` — schema (start here when changing fields)
- `src/commands.py` — all subcommand handlers; `main.py` is just dispatch
- `src/daemon.py` — file watcher, scheduled reports, Telegram chat loop
- `src/llm.py` — `load_context()`, `build_messages()`, `call_llm()`
- `src/prompts/` — prompt templates (soul, report, nudge, chat, coach)
- `src/charts.py` — Plotly chart rendering from `<chart>` blocks
- `src/tools.py` — LLM tools (`run_sql`, `update_context`)
- `evals/` — AI eval harness (pinned blueprints, scenario perturbations, structural assertions)

User context files: `~/Documents/zdrowskit/ContextFiles/` (`me.md`, `goals.md`, `plan.md`, `log.md` — user-edited; `baselines.md`, `history.md` — auto-generated).
