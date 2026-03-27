# CLAUDE.md

## What is zdrowskit

Your 24/7 ultra-personal trainer. Parses Apple Health exports (metrics, workouts, GPX routes, sleep), stores them in SQLite, and uses an LLM to generate personalised weekly reports and short nudges via Telegram/email. A daemon watches for new data, fires nudges automatically, and listens for incoming Telegram messages for interactive two-way coaching chat — including ad-hoc data queries and on-demand charts.

## Commands

Always use `uv run` — never plain `python`. Run any subcommand with `--help` for full flags.

```bash
uv run python main.py import                      # import from Auto Export (default)
uv run python main.py import --source shortcuts    # import from iOS Shortcuts export
uv run python main.py insights        # LLM weekly report (add --week last|current, --telegram, --email, --explain)
uv run python main.py nudge           # short LLM nudge (add --trigger TYPE)
uv run python main.py report          # terminal summary (add --llm, --history, --json)
uv run python main.py status          # DB row counts + date range
uv run python main.py context         # show context files and their status
uv run python main.py llm-log         # query LLM call history (add --stats, --id N, --json)
uv run python main.py daemon-restart  # restart the background launchd daemon
uv run python main.py daemon-stop     # stop and unload the background daemon
uv run python src/daemon.py --foreground  # run filesystem watcher + chat in foreground
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

## Architecture

Two data sources, same pipeline output:

```
autoexport (default, ongoing — Auto Export app iCloud Drive automation):
  Metrics/HealthAutoExport-*.json  ─┐
  Workouts/HealthAutoExport-*.json ─┤─→ src/parsers/ → src/assembler.py → list[DailySnapshot]
  (sleep + routes embedded)        ─┘                                            │

shortcuts (historical backfill — iOS Shortcuts export):                          │
  Metrics/{activity,heart,mobility}.json ─┐                                      │
  Workouts/workouts.json                 ─┤─→ same parsers + sleep/gpx ──────────┤
  Sleep/sleep.json                       ─┤                                      │
  Routes/*.xml                           ─┘                                      │
                                                                                 ▼
                                                                      src/aggregator.py
                                                                                 │
                                                                                 ▼
                                                                          WeeklySummary
```

Schema lives in `src/models.py` — start there when changing fields. `src/commands.py` has all subcommand handlers; `main.py` is just dispatch.

Data source paths are in `src/config.py` (`AUTOEXPORT_DATA_DIR`, `SHORTCUTS_DATA_DIR`). The `--source` flag on the import command selects which parser path to use.

## Context Files

LLM context files live in `~/Documents/zdrowskit/ContextFiles/`:

| File | Ownership | Purpose |
|------|-----------|---------|
| `me.md` | user | Physical profile — age, weight, injuries, pace zones |
| `goals.md` | user | Fitness goals with timelines |
| `plan.md` | user | Weekly training schedule, diet, sleep targets |
| `log.md` | user | Weekly journal — what happened and why (trimmed to last 5 entries in prompts) |
| `baselines.md` | auto | Rolling averages from DB (written by `insights`) |
| `history.md` | auto | LLM memory (appended after each weekly report; same-day runs replace, not duplicate) |

Prompt templates live in `src/prompts/` (version-controlled, single source of truth):

| File | Purpose |
|------|---------|
| `soul.md` | AI coach persona (static — not auto-updated via chat) |
| `prompt.md` | Weekly report prompt template |
| `nudge_prompt.md` | Nudge prompt template |
| `chat_prompt.md` | Conversational chat prompt template |

## Daemon Scheduled Reports

The daemon (`src/daemon.py`) runs a background thread that fires reports on a schedule:

- **Monday 8–9am** — full week review (`--week last`, previous Mon–Sun)
- **Thursday 9–10am** — mid-week progress check (`--week current`, current Mon–Thu)

Both triggers import fresh data before generating the report. Rate-limited to once per day per report type via `~/.daemon_state.json`.

## Telegram Interactive Chat

The daemon runs a Telegram long-polling listener (`src/telegram_bot.py`) for two-way coaching conversations. Key modules:

- `src/telegram_bot.py` — `TelegramPoller` (long polling) + `ConversationBuffer` (thread-safe, 20-message in-memory buffer)
- `src/context_edit.py` — auto-update context files from chat (extract `update_context` tool call from LLM response, confirm via inline keyboard, write file)
- `src/tools.py` — `run_sql` tool for ad-hoc database queries from chat (read-only, SELECT-only, row-limited, timeout-protected)
- `src/prompts/chat_prompt.md` — conversational chat prompt template (includes DB schema reference for query tool)
- Bot commands: `/clear` (reset buffer), `/status` (buffer size, nudge count), `/context` (list files or `/context <name>` for full content), `/help` (command reference)
- Reply-to context: replying to a nudge/report injects the original text so the LLM knows what you're responding to
- Context auto-updates: the LLM can propose edits to me/goals/plan/log.md; user confirms via Accept/Reject buttons (or auto-accept via `ZDROWSKIT_AUTO_ACCEPT_EDITS=1`)

### Interactive Data Queries

The chat supports a tool-calling loop (`_chat_reply` in `src/daemon.py`) that lets the LLM query the database and generate charts on demand. The loop runs up to `MAX_TOOL_ITERATIONS` (5) rounds:

1. LLM decides it needs data → calls `run_sql` with a SELECT query
2. Tool executes against a read-only SQLite connection → returns JSON rows
3. LLM sees results → may query again, or produce a final response
4. If the response includes `<chart>` blocks, they are rendered as Plotly PNGs and sent as Telegram photos

Available tools in chat: `run_sql` (database queries) + `update_context` (context file edits).

Example questions: "What's my avg run pace by week?", "Show me my HRV trend since January", "When did I start collecting data?", "Compare my sleep this month vs last month".

### Charts

All three prompt types (weekly report, nudge, chat) can produce Plotly charts via `<chart title="...">` blocks. Chart code runs in a sandboxed namespace (`src/charts.py`) with a 10-second timeout.

- **Reports/nudges:** chart code uses the `data` dict (pre-loaded health data JSON)
- **Chat:** chart code uses the `rows` variable (accumulated query results from `run_sql` calls in that turn)
- Consistent style across all prompts: `{chart_theme}` template, color-coded markers (red/green/blue), annotations with arrows, baseline hlines, tight margins
