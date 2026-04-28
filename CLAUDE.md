## What is zdrowskit

Your 24/7 ultra-personal trainer. Parses Apple Health exports (metrics, workouts, GPX routes, sleep), stores them in SQLite, derives per-km splits from route-bearing runs, and uses an LLM to generate personalised weekly reports and short nudges via Telegram/email. A daemon watches for new data, fires nudges automatically, and listens for incoming Telegram messages for interactive two-way coaching chat — including ad-hoc data queries and on-demand charts.

## Commands

Always use `uv run` — never plain `python`. Run any subcommand with `--help` for full flags.

```bash
uv run python main.py import                      # import from Auto Export
uv run python main.py insights        # LLM weekly report (add --week last|current, --telegram, --email, --explain)
uv run python main.py nudge           # short LLM nudge (add --trigger TYPE)
uv run python main.py coach           # coaching review with plan/goal proposals (add --week, --telegram, --email)
uv run python main.py report          # terminal summary (add --llm, --history, --json)
uv run python main.py status          # DB row counts + date range
uv run python main.py db status       # migration status for the SQLite DB
uv run python main.py db schema       # print the live SQLite schema
uv run python main.py context         # show context files and their status
uv run python main.py llm-log         # query LLM call history (add --stats, --id N, --json)
uv run python main.py models          # inspect/change feature model routing
uv run python main.py events          # system event log (add --category, --kind, --since 3d)
uv run python main.py telegram-setup  # register bot /commands for Telegram autocomplete + menu
uv run python main.py daemon-restart  # restart the background launchd daemon
uv run python main.py daemon-stop     # stop and unload the background daemon
uv run python src/daemon.py --foreground  # run filesystem watcher + chat in foreground
uv run python -m evals.run            # run feedback-derived LLM evals (real model; add case IDs, --feature, --details)
```

Preferred LLM tracing path for debugging: use `uv run python main.py llm-log --id N` to inspect the full stored trace for one call, including messages, tool use, and final response.

Telegram bot commands now include `/notify` for showing/changing notification preferences. These preferences live in `~/Documents/zdrowskit/notification_prefs.json`, separate from `ContextFiles/`, and daemon notification scheduling should consult them before making LLM notification calls.

Telegram bot commands also include `/models` for button-based model routing. Model preferences live in `~/Documents/zdrowskit/model_prefs.json`. The Telegram panel groups features (Chat/Reports/Coach/Nudges/Utilities), tags every model button with its capability tier, and exposes Reasoning/Temperature pickers under the Chat group. `Reset all` (Telegram) and `uv run python main.py models reset --all` (CLI) restore defaults. Picking `Auto` for the fallback (or `--fallback auto`) persists JSON `null` and defers to the profile fallback at resolve time. Chat defaults to `anthropic/claude-opus-4-7` with reasoning off and temperature omitted, while insights/coach/nudges default to DeepSeek Pro with Anthropic Opus fallback.

DeepSeek V4 models, including Pro and Flash, default to thinking mode enabled with high effort unless explicitly disabled. `src/llm.py` applies the config-backed DeepSeek default `extra_body={"thinking": {"type": "disabled"}}` only to DeepSeek attempts and omits it for Anthropic fallbacks. Verifier calls additionally request `response_format={"type": "json_object"}` via `src/llm_verify.py`; `ZDROWSKIT_VERIFY_DEEPSEEK_THINKING` inherits `ZDROWSKIT_DEEPSEEK_THINKING` unless explicitly overridden.

Database access should go through `store.open_db()` or `store.connect_db(..., migrate=True)` so pending SQLite migrations are applied automatically. Avoid raw `sqlite3.connect(...)` unless you intentionally need a migration-free connection.
Database schema changes must be implemented as new timestamped migration files in `src/db/migrations/`. Do not add ad-hoc runtime `ALTER TABLE`, column-existence checks, or other schema-patching logic in application code.

## Collaboration Style

Challenge my ideas early. If an approach is over-engineered, fragile, or there's a simpler/better alternative I might be missing — say so directly with reasoning. Don't just execute instructions; flag knowledge gaps, hidden trade-offs, or narrowed thinking. Be pragmatic: save me from wasting time on something that could be done better.

**Verification:** After completing a complex or cross-cutting feature (touches multiple modules, changes interfaces, moves files), automatically run a verification pass before reporting done. Grep for stale references, check all callers of changed functions, confirm imports, run lint + tests, and fix any issues found — don't wait to be asked. Always check whether `README.md` or `CLAUDE.md` reference values you changed (defaults, limits, paths, command flags) and update them in the same commit.

## Code Style

- **Linter/formatter:** `uv run ruff check .` and `uv run ruff format .`
- **Type hints:** required on all signatures. Use native types (`list`, `dict`, `str | None`) — never `typing.List` etc.
- **Docstrings:** Google style.
- **File size:** keep source files under ~1000 lines. If a module grows past that, extract a cohesive subset into its own file (see `daemon_*.py` and `cmd_*.py` for the pattern).
- **No backward-compat shims:** when moving code to a new module, update all callers to import from the new location directly. Don't leave re-export stubs behind.

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

## LLM Evals

LLM evals in `evals/` are feedback-derived regressions, not a generated benchmark suite. Do not add broad LLM-created scenarios, stale blueprint/cache machinery, or cases without provenance.

When adding eval coverage:
- Start from a real thumbs-down feedback item. Use `uv run python main.py llm-log --feedback` to find it and `uv run python main.py llm-log --id N` to inspect the trace.
- Add the real failure first as `case_kind: "real_regression"`.
- Add only the minimum synthetic controls needed to isolate the hypothesis or guard a false positive, using `case_kind: "synthetic_positive"` or `case_kind: "synthetic_negative"`.
- Preserve provenance on every case with `source_feedback_id`, `source_llm_call_id`, and `derived_from.hypothesis`.
- Prefer structured fixtures: pinned date, context snippets, conversation turns, and only the health data needed for the behavior under test.
- Prefer deterministic assertions. Add LLM-as-judge only when a real feedback case cannot be checked with tool-call, argument, text, word-count, or forbidden-opening assertions.

`uv run pytest` must stay mocked and must never call a real LLM. Manual evals are opt-in through `uv run python -m evals.run`, which uses the configured model and may spend API quota. For prompt/tool behavior changes, run the relevant mocked tests plus the specific eval cases that represent the affected feedback cluster.

## Key Modules

- `src/models.py` — schema (start here when changing fields)
- `src/commands.py` — all subcommand handlers; `main.py` is just dispatch
- `src/daemon.py` — file watcher, scheduled reports, Telegram chat loop
- `src/llm.py` — `load_context()`, `build_messages()`, `call_llm()`
- `src/milestones.py` — lifetime PRs, streaks, and milestone summaries for prompts
- `src/prompts/` — prompt templates (soul, report, nudge, chat, coach)
- `src/charts.py` — Plotly chart rendering from `<chart>` blocks
- `src/tools.py` — LLM tools (`run_sql`, `update_context`)
- `evals/` — feedback-derived LLM eval cases and deterministic runner

User context files: `~/Documents/zdrowskit/ContextFiles/` (`me.md`, `strategy.md`, `log.md` — user-edited; `baselines.md`, `history.md` — auto-generated). `baselines.md` now includes rolling plus seasonal / YoY baselines. `strategy.md` is the merged goals + weekly plan + diet + sleep file (formerly `goals.md` + `plan.md`).
