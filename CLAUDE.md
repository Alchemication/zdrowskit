# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Always use `uv run` — never plain `python`. The three subcommands are `import`, `report`, and `status`. Run any with `--help` for the full flag list. Key defaults and overrides:

- **Data dir:** `~/Documents/adamskit/MyHealth/` — override with `--data-dir PATH` or `HEALTH_DATA_DIR` env var.
- **Database:** `~/Documents/adamskit/health.db` — override with `--db PATH` or `ADAMSKIT_DB` env var.

```bash
uv run python main.py import                        # parse default data dir, upsert into DB
uv run python main.py report                        # current week: summary + daily breakdown
uv run python main.py report --history              # all weeks: one summary per ISO week
uv run python main.py report --llm                  # JSON for LLM: current week + 3mo history
uv run python main.py report --llm --months 6       # same, 6 months of history
uv run python main.py report --json                 # current week as raw JSON
uv run python main.py report --since DATE           # scope any mode to a date range
uv run python main.py status                        # DB row counts + date range
```

## Logging

Use the stdlib `logging` module — never `print()` for diagnostic output. Every module that emits operational messages should declare a module-level logger:

```python
import logging
logger = logging.getLogger(__name__)
```

`src/log.py` provides `setup_logging()`, which wires up a colored stderr handler. It is called once in `main()` before the pipeline runs.

**Use `print()` only for intentional user-facing report output** (i.e. the formatted weekly summary and daily breakdown in `main.py`). Everything else — status messages, warnings, errors — goes through the logger.

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
- `src/store.py` — SQLite persistence layer. `open_db()` creates/migrates the DB; `store_snapshots()` upserts; `load_snapshots()` re-hydrates `DailySnapshot` objects with nested workouts. Default DB: `~/Documents/adamskit/health.db`.
- `main.py` — CLI entry point. Adds `src/` to `sys.path` so modules import without a package prefix. Dispatches `import` / `report` / `status` subcommands. The `report` subcommand has three modes: default (current week + daily), `--history` (one summary per ISO week), and `--llm` (combined JSON for LLM consumption).

**Data directory layout** (configurable via `--data-dir` or `HEALTH_DATA_DIR` env var):
```
MyHealth/
  Metrics/activity.json   — steps, distance, energy, exercise/stand time
  Metrics/heart.json      — HR, HRV, resting HR, VO2max (sparse on run days)
  Metrics/mobility.json   — walking/running gait metrics, stair speed
  Workouts/workouts.json  — workout sessions with per-minute energy, HR, temp, humidity
  Routes/*.xml            — GPX tracks for outdoor workouts (~1 point/sec)
```
