# Commands

Always use `uv run`. Run any command with `--help` for the full flag list.

```bash
uv run python main.py import              # import from Auto Export
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
```

Useful examples:

```bash
uv run python main.py insights --week last --telegram
uv run python main.py nudge --trigger log_update
uv run python main.py llm-log --id 42 --feedback
uv run python main.py events --since 3d --category nudge
uv run python main.py db status
uv run python main.py db schema
uv run python main.py notify reset all
```

LLM evals have their own runner. See [LLM evals](evals.md).

## Data Directory Override

Override the default iCloud data directory with `--data-dir` or the `HEALTH_DATA_DIR` environment variable.
