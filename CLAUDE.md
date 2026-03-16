# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Zdrowskit - why this project exists

> What Apple Health notifications should be.

Apple sends you a nudge when you close your rings. zdrowskit reads your actual data — runs, lifts, heart rate variability, recovery — and tells you something worth knowing.

## Commands

Always use `uv run` — never plain `python`. The five subcommands are `import`, `report`, `status`, `context`, and `insights`. Run any with `--help` for the full flag list. Key defaults and overrides:

- **Data dir:** `~/Documents/zdrowskit/MyHealth/` — override with `--data-dir PATH` or `HEALTH_DATA_DIR` env var.
- **Database:** `~/Documents/zdrowskit/health.db` — override with `--db PATH` or `zdrowskit_DB` env var.

```bash
uv run python main.py import                        # parse default data dir, upsert into DB
uv run python main.py report                        # current week: summary + daily breakdown
uv run python main.py report --history              # all weeks: one summary per ISO week
uv run python main.py report --llm                  # JSON for LLM: current week + 3mo history
uv run python main.py report --llm --months 6       # same, 6 months of history
uv run python main.py report --json                 # current week as raw JSON
uv run python main.py report --since DATE           # scope any mode to a date range
uv run python main.py status                        # DB row counts + date range
uv run python main.py context                       # show context files and their status
uv run python main.py insights                      # LLM-driven personalised weekly report
uv run python main.py insights --months 6           # same, with 6 months of history
uv run python main.py insights --week last          # report on previous ISO week (Monday morning flow)
uv run python main.py insights --no-update-history  # skip appending memory to history.md
uv run python main.py insights --no-update-baselines # skip auto-computed baselines
uv run python main.py insights --explain            # show context, prompt, token usage diagnostics
uv run python main.py insights --email              # send report via email (Resend)
uv run python main.py insights --telegram           # send report via Telegram bot
uv run python main.py insights --model MODEL        # use a different litellm model
```

## Logging

Use the stdlib `logging` module — never `print()` for diagnostic output. Every module that emits operational messages should declare a module-level logger:

```python
import logging
logger = logging.getLogger(__name__)
```

`src/log.py` provides `setup_logging()`, which wires up a colored stderr handler. It is called once in `main()` before the pipeline runs.

**Use `print()` only for intentional user-facing report output** (i.e. the formatted weekly summary and daily breakdown in `src/report.py`). Everything else — status messages, warnings, errors — goes through the logger.

## CLI Output & UX

The CLI is the primary user interface. **Great UX matters** — every command should produce output that is scannable, informative, and visually clean. Pick the right output tool for the job:

- **`rich` for structured output:** Use `rich` tables, panels, and styled text for any diagnostic, status, or informational display (e.g. `--explain`, `context` command). Import `rich` lazily (inside the function) to keep startup fast. When the output is meant for the terminal and benefits from visual hierarchy, reach for `rich`.
- **`print()` for primary content:** Report text, JSON output, and other "pipe-friendly" content goes to stdout via `print()`. Keep it clean — no ANSI codes, no rich markup. Use this when the output might be redirected or piped.
- **`logger` for operational messages:** Progress updates, warnings, errors go to stderr via the logger. These should be concise and actionable (e.g. "Report saved to /path/file.md", not "The report has been successfully saved"). The logger is for what's happening behind the scenes, not the result itself.
- **Stderr vs stdout separation:** Diagnostics (`--explain`, logger) go to stderr. Content (reports, JSON) goes to stdout. This lets users redirect output cleanly: `insights > report.md` captures only the report.
- **Error messages:** Always tell the user what to do, not just what went wrong. "RESEND_API_KEY not set. Add it to your .env file." not "Missing API key."
- **General principle:** If you're unsure which to use, ask: "Is this the result the user asked for?" → `print()`. "Is this a table/panel the user reads in the terminal?" → `rich`. "Is this a progress/status note?" → `logger`.

## Code Style

- **Linter/formatter:** ruff. Run with `uv run ruff check .` and `uv run ruff format .`.
- **Type hints:** required on all function signatures. Use native types (`list`, `dict`, `tuple`, `str | None`) — never `typing.List`, `typing.Dict`, `Optional`, etc.
- **Docstrings:** Google style on all functions and classes.
- **Module headers:** Every module with a public API must have a top-of-file docstring listing its public symbols and a minimal usage example.

```python
def parse_metrics_file(path: Path) -> dict[str, dict[str, float]]:
    """Parse a single Apple Health metrics JSON file.

    Args:
        path: Path to the JSON file to parse.

    Returns:
        A dict mapping ISO date strings to a flat dict of field name → value.
    """
```

## Architecture

This is a personal Apple Health data pipeline. It parses weekly exports from the iOS Health app and assembles them into structured summaries, intended eventually to be fed to an LLM for contextual health reporting.

**Data flow:**

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

**Key modules:**

- `src/models.py` — the schema. All three dataclasses (`WorkoutSnapshot`, `DailySnapshot`, `WeeklySummary`) live here. Every other module imports from it; start here when changing field names or adding metrics.
- `src/parsers/metrics.py` — generic parser for all three Metrics JSONs (activity, heart, mobility). The `METRIC_MAP` dict translates Apple Health metric names to internal field names.
- `src/parsers/workouts.py` — parses `workouts.json`. Note the schema uses nested `{"qty": x, "units": y}` dicts, not flat values.
- `src/parsers/gpx.py` — parses GPX route files, derives distance (haversine), elevation gain (3-point rolling-min smoothed to suppress GPS noise), and speed (uses `<extensions><speed>` field, 95th percentile for max). Matches GPX files to workouts by comparing `<metadata><time>` to `workout.start` within a 60-second tolerance.
- `src/assembler.py` — joins all parser outputs by date into `list[DailySnapshot]`. This is the only module that knows about inter-source relationships (GPX↔workout matching, date alignment).
- `src/aggregator.py` — computes `WeeklySummary` from the daily snapshots. Contains `WEEKLY_RUN_TARGET` and `WEEKLY_LIFT_TARGET` constants used for consistency scoring.
- `src/log.py` — configures a colored stderr logger via `setup_logging()`. Call once at startup in `main()`; all other modules just `getLogger(__name__)`.
- `src/store.py` — SQLite persistence layer. `open_db()` creates/migrates the DB; `store_snapshots()` upserts; `load_snapshots()` re-hydrates `DailySnapshot` objects with nested workouts. Default DB: `~/Documents/zdrowskit/health.db`.
- `src/llm.py` — LLM integration. Loads markdown context files (`soul.md`, `me.md`, `goals.md`, `plan.md`, `log.md`, `history.md`, `prompt.md`) from the context directory, assembles a prompt, calls an LLM via litellm, and manages the memory/history feedback loop. Also builds the combined current-week + history JSON structure via `build_llm_data()`. Default model: `anthropic/claude-haiku-4-5-20251001`.
- `src/notify.py` — notification delivery. `send_email()` sends HTML reports via Resend API. `send_telegram()` sends plain-text reports via Telegram Bot API. Both read credentials from env vars.
- `src/config.py` — shared path constants (`DEFAULT_DATA_DIR`, `CONTEXT_DIR`, `REPORTS_DIR`) and `resolve_data_dir()`. Single source of truth for all `~/Documents/zdrowskit/` paths.
- `src/report.py` — report formatting and display. `print_summary()`, `print_daily()` for terminal output. `to_dict()`, `fmt()`, `current_week_bounds()`, `group_by_week()` for data conversion and week arithmetic.
- `src/baselines.py` — `compute_baselines()` runs rolling 30/90-day SQL averages and weekly training volume queries, returns formatted markdown.
- `src/commands.py` — subcommand handlers (`cmd_import`, `cmd_report`, `cmd_status`, `cmd_context`, `cmd_insights`). All business logic lives here; `main.py` only dispatches to these.
- `main.py` — CLI entry point. Thin layer: `sys.path` setup, `.env` loading, argparse definition, and dict-based dispatch to `src/commands.py`. **Keep this file slim** — new business logic belongs in `src/`.

**Data directory layout** (configurable via `--data-dir` or `HEALTH_DATA_DIR` env var):
```
MyHealth/
  Metrics/activity.json   — steps, distance, energy, exercise/stand time
  Metrics/heart.json      — HR, HRV, resting HR, VO2max (sparse on run days)
  Metrics/mobility.json   — walking/running gait metrics, stair speed
  Workouts/workouts.json  — workout sessions with per-minute energy, HR, temp, humidity
  Routes/*.xml            — GPX tracks for outdoor workouts (~1 point/sec)
```

**Context files** for `insights` (live in `~/Documents/zdrowskit/ContextFiles/`):
```
soul.md      — AI coach persona and tone
me.md        — user profile, baselines (resting HR, HRV, pace)
goals.md     — fitness goals with timelines
plan.md      — weekly training schedule, diet, sleep targets
log.md       — freeform weekly journal (why things happened)
history.md   — LLM's own memory (auto-appended after each run)
prompt.md    — prompt template with {placeholders} for context + data
```

Example versions of all context files are in `examples/context/`.
