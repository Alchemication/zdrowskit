# Commands

Always use `uv run`. Run any command with `--help` for the full flag list.

```bash
uv run python main.py import              # import from local/iCloud or Google Drive
uv run python main.py status              # DB row counts + date range
uv run python main.py report              # current week: summary + daily
uv run python main.py insights            # personalised weekly report via LLM
uv run python main.py coach               # coaching review with plan/goal proposals
uv run python main.py nudge               # short reactive nudge
uv run python main.py context             # show context files and their status
uv run python main.py setup               # create .env + first-run context files
uv run python main.py doctor              # check local setup readiness
uv run python main.py events              # system event log: fires, skips, imports
uv run python main.py llm-log             # inspect stored LLM call traces
uv run python main.py notify              # inspect/reset notification settings
uv run python main.py models              # inspect/change model routing
uv run python main.py telegram-setup      # register bot /commands for Telegram menu
uv run python main.py daemon-install      # generate + load launchd daemon plist
uv run python main.py daemon-restart      # restart the background daemon
uv run python main.py daemon-stop         # stop the background daemon
uv run python src/daemon.py --foreground # run daemon directly on macOS/Linux
```

Useful examples:

```bash
uv run python main.py insights --week last --telegram
uv run python main.py nudge --trigger log_update
uv run python main.py llm-log --id 42 --feedback
uv run python main.py llm-log --trace 7
uv run python main.py events --since 3d --category nudge
uv run python main.py events --usage --since 30d
uv run python main.py db status
uv run python main.py db schema
uv run python main.py notify reset all
```

LLM evals have their own runner. See [LLM evals](evals.md).

```bash
uv run python -m evals.run
uv run python -m evals.matrix --feature chat --models deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro --reasoning-efforts high
uv run python -m evals.matrix --feature insights --models anthropic/claude-opus-4-7,deepseek/deepseek-v4-pro --reasoning-efforts high
uv run python -m evals.leaderboard render
```

## Data Directory Override

Override the default iCloud data directory with `--data-dir` or the `HEALTH_DATA_DIR` environment variable.

## Google Drive Import

Set `ZDROWSKIT_IMPORT_SOURCE=google-drive` and the three
`ZDROWSKIT_GOOGLE_DRIVE_*` values documented in [Google Drive import](google-drive.md),
then use the normal import command:

```bash
uv run python main.py import
```

CLI flags override the environment for a one-off or second profile:

```bash
uv run python main.py import \
  --source google-drive \
  --google-drive-service-account PATH \
  --google-drive-metrics-folder-id ID \
  --google-drive-workouts-folder-id ID \
  --data-dir PATH \
  --db PATH
```

Google Drive imports first update the local `Metrics/` and `Workouts/` cache,
then run the same idempotent database import as a local source.

The daemon uses the same source configuration. For Drive it polls immediately
at startup and every `ZDROWSKIT_GOOGLE_DRIVE_POLL_INTERVAL_S` seconds afterward.
