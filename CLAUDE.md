# CLAUDE.md

## What is zdrowskit

Your 24/7 ultra-personal trainer. Parses Apple Health exports (metrics, workouts, GPX routes), stores them in SQLite, and uses an LLM to generate personalised weekly reports and short nudges via Telegram/email. A daemon watches for new data and fires nudges automatically.

## Commands

Always use `uv run` — never plain `python`. Run any subcommand with `--help` for full flags.

```bash
uv run python main.py import       # parse health data, upsert into DB
uv run python main.py insights     # LLM weekly report (add --telegram, --email, --explain)
uv run python main.py nudge        # short LLM nudge (add --trigger TYPE)
uv run python main.py report       # terminal summary (add --llm, --history, --json)
uv run python main.py status       # DB row counts + date range
uv run python main.py context      # show context files and their status
uv run python src/daemon.py --foreground  # run filesystem watcher
```

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

## Architecture

```
MyHealth/Metrics/*.json   ─┐
MyHealth/Workouts/*.json  ─┤─→ src/parsers/ → src/assembler.py → list[DailySnapshot]
MyHealth/Routes/*.xml     ─┘                                            │
                                                                         ▼
                                                              src/aggregator.py
                                                                         │
                                                                         ▼
                                                                  WeeklySummary
```

Schema lives in `src/models.py` — start there when changing fields. `src/commands.py` has all subcommand handlers; `main.py` is just dispatch.

## Context Files

LLM context files live in `~/Documents/zdrowskit/ContextFiles/`:

`soul.md`, `me.md`, `goals.md`, `plan.md`, `log.md`, `history.md`, `prompt.md`, `nudge_prompt.md`

Examples in `examples/context/`. `me.md` is also auto-updated by `src/baselines.py`.
