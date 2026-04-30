## Rules

Always use `uv run`, never plain `python`. Full command list: `docs/commands.md` (or `--help`).

Debug LLM behavior with `uv run python main.py llm-log --id N` — full stored trace (messages, tool use, response) for one call.

`src/llm.py`: the DeepSeek thinking-disabled `extra_body` default applies only to DeepSeek calls, never to Anthropic fallbacks. Verifier calls in `src/llm_verify.py` inherit `ZDROWSKIT_DEEPSEEK_THINKING` unless `ZDROWSKIT_VERIFY_DEEPSEEK_THINKING` overrides it.

Open DBs via `store.open_db()` or `store.connect_db(..., migrate=True)` — these auto-apply pending migrations. Use raw `sqlite3.connect()` only when you specifically need to skip migration.

Schema changes go in new timestamped migration files under `src/db/migrations/`. No ad-hoc `ALTER TABLE`, column-existence checks, or schema-patching in application code.

Natural-language LLM prompts live in `src/prompts/`; keep tool schemas beside tool code.

## Collaboration Style

Challenge my ideas early. If an approach is over-engineered, fragile, or has a simpler alternative — say so directly with reasoning. Flag knowledge gaps, hidden trade-offs, or narrowed thinking. Be pragmatic; save me from wasting time on something that could be done better.

**Prose:** terse, no pleasantries / hedging / filler. Fragments fine. Full sentences only for warnings, destructive-op confirmations, and commit/PR messages.

**Verification:** After cross-cutting changes (multiple modules, interface changes, file moves), verify before reporting done. Grep for stale references, check callers of changed functions, confirm imports, run lint + tests, and fix what you find — don't wait to be asked. Check whether `README.md`, `CLAUDE.md`, or `docs/` reference values you changed (defaults, limits, paths, flags) and update them in the same commit.

## Code Style

- **Linter/formatter:** `uv run ruff check .` and `uv run ruff format .`
- **Type hints:** required on all signatures. Native types only (`list`, `dict`, `str | None`) — never `typing.List` etc.
- **Docstrings:** Google style.
- **File size:** keep source files under ~1000 lines. If a module grows past that, extract a cohesive subset into its own file (see `daemon_*.py`, `cmd_*.py`).
- **No backward-compat shims:** when moving code, update all callers to import from the new location. No re-export stubs.

## Output Rules

- `print()` for user-facing content (reports, JSON) → stdout.
- `logger` (stdlib `logging`) for diagnostics, progress, errors → stderr.
- `rich` (import lazily) for structured terminal display (tables, panels).
- Error messages should tell the user what to do, not just what went wrong.

## Testing

`uv run pytest`. Fixtures in `tests/fixtures/` and `tests/conftest.py`.

**Must have tests:** parsers (`src/parsers/`), aggregator, store round-trips, report utilities (date arithmetic), pure LLM utility functions (`extract_memory`, `_recent_history`).

**Style:** group in classes (`class TestParseMetricsFile`); use `tmp_path` for files and `in_memory_db` for DB; cover edge cases that would silently break (None, missing fields, empty inputs); run ruff on `tests/` before committing.

## Code map

`main.py` is dispatch only — subcommand handlers live in `src/commands.py` plus `src/cmd_*.py` (split as `commands.py` grew past ~1000 lines). The always-on daemon is similarly factored across `src/daemon.py` and `src/daemon_*.py` flows.
