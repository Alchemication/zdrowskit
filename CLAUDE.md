# CLAUDE.md

## What is zdrowskit

Your 24/7 ultra-personal trainer. Parses Apple Health exports (metrics, workouts, GPX routes, sleep), stores them in SQLite, and uses an LLM to generate personalised weekly reports and short nudges via Telegram/email. A daemon watches for new data, fires nudges automatically, and listens for incoming Telegram messages for interactive two-way coaching chat.

## Commands

Always use `uv run` — never plain `python`. Run any subcommand with `--help` for full flags.

```bash
uv run python main.py import          # parse health data, upsert into DB
uv run python main.py insights        # LLM weekly report (add --telegram, --email, --explain)
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
MyHealth/Workouts/*.json  ─┤
MyHealth/Routes/*.xml     ─┤─→ src/parsers/ → src/assembler.py → list[DailySnapshot]
MyHealth/Sleep/sleep.json ─┘                                            │
                                                                         ▼
                                                              src/aggregator.py
                                                                         │
                                                                         ▼
                                                                  WeeklySummary
```

Schema lives in `src/models.py` — start there when changing fields. `src/commands.py` has all subcommand handlers; `main.py` is just dispatch.

## Context Files

LLM context files live in `~/Documents/zdrowskit/ContextFiles/`:

| File | Ownership | Purpose |
|------|-----------|---------|
| `me.md` | user | Physical profile — age, weight, injuries, pace zones |
| `goals.md` | user | Fitness goals with timelines |
| `plan.md` | user | Weekly training schedule, diet, sleep targets |
| `log.md` | user | Weekly journal — what happened and why (trimmed to last 5 entries in prompts) |
| `soul.md` | user | AI coach persona (static — not auto-updated via chat) |
| `baselines.md` | auto | Rolling averages from DB (written by `insights`) |
| `history.md` | auto | LLM memory (appended after each weekly report; same-day runs replace, not duplicate) |
| `prompt.md`, `nudge_prompt.md`, `chat_prompt.md` | user | Prompt templates (examples in `examples/context/`) |

## Telegram Interactive Chat

The daemon runs a Telegram long-polling listener (`src/telegram_bot.py`) for two-way coaching conversations. Key modules:

- `src/telegram_bot.py` — `TelegramPoller` (long polling) + `ConversationBuffer` (thread-safe, 20-message in-memory buffer)
- `src/context_edit.py` — auto-update context files from chat (extract `update_context` tool call from LLM response, confirm via inline keyboard, write file)
- `examples/context/chat_prompt.md` — conversational prompt template (must be copied to ContextFiles)
- Bot commands: `/clear` (reset buffer), `/status` (buffer size, nudge count), `/context` (list files or `/context <name>` for full content), `/help` (command reference)
- Reply-to context: replying to a nudge/report injects the original text so the LLM knows what you're responding to
- Context auto-updates: the LLM can propose edits to me/goals/plan/log.md; user confirms via Accept/Reject buttons (or auto-accept via `ZDROWSKIT_AUTO_ACCEPT_EDITS=1`)
