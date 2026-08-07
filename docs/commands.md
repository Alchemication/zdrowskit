# Commands

Always use `uv run`. Run any command with `--help` for the full flag list.

```bash
uv run python main.py import              # import a local/iCloud or Drive source
uv run python main.py status              # DB row counts + date range
uv run python main.py report              # current week: summary + daily
uv run python main.py insights            # personalised weekly report via LLM
uv run python main.py coach               # coaching review with plan/goal proposals
uv run python main.py nudge               # short reactive nudge
uv run python main.py context             # show context files and their status
uv run python main.py setup               # create .env bootstrap files
uv run python main.py doctor              # check local setup readiness
uv run python main.py events              # system event log: fires, skips, imports
uv run python main.py llm-log             # inspect stored LLM call traces
uv run python main.py notify              # inspect/reset notification settings
uv run python main.py models              # inspect/change model routing
uv run python main.py telegram-setup      # register bot /commands for Telegram menu
uv run python main.py daemon-install      # generate + load launchd daemon plist
uv run python main.py daemon-restart      # restart the background daemon
uv run python main.py daemon-stop         # stop the background daemon
uv run python main.py profile add NAME --telegram-id ID # create a profile
uv run python main.py profile adopt NAME --dry-run       # preview legacy migration
uv run python main.py profile source NAME http           # change import transport
uv run python main.py ingest setup        # tokens + Auto Export configuration
uv run python main.py ingest setup --funnel # same, plus start Funnel when CLI is ready
uv run python main.py ingest status       # receiver and upload state
uv run python main.py ingest token NAME --rotate         # replace a lost token
uv run python src/daemon.py --foreground # run daemon directly on macOS/Linux
```

Useful examples:

```bash
uv run python main.py insights --telegram
uv run python main.py nudge --trigger log_update
uv run python main.py llm-log --id 42 --feedback
uv run python main.py llm-log --trace 7
uv run python main.py events --since 3d --category nudge
uv run python main.py events --usage --since 30d
uv run python main.py db status
uv run python main.py db status --all
uv run python main.py db migrate --all
uv run python main.py db schema
uv run python main.py notify reset all
```

LLM evals have their own runner. See [LLM evals](evals.md).

```bash
uv run python -m evals.run
uv run python -m evals.matrix --feature chat --models deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro --reasoning-efforts high
uv run python -m evals.matrix --feature insights --models anthropic/claude-opus-5,deepseek/deepseek-v4-pro --reasoning-efforts high
uv run python -m evals.leaderboard render
```

## Profile Selection

`import`, `report`, `status`, `db`, `insights`, `nudge`, `coach`, `llm-log`,
`events`, `context`, `notify`, and `models` accept `--profile NAME` and default
to the operator profile. The database, context, reports, nudges, notification
preferences, model preferences, and Telegram destination are derived from
`profiles.toml`.

```bash
uv run python main.py status --profile anna
uv run python main.py llm-log --profile anna --id 42
uv run python main.py models --profile anna
```

For database-backed commands, an explicit `--db PATH` wins over `--profile` for
experiments. It must already exist; normal commands never create an accidental
empty database.

Create a fresh roster/profile:

```bash
uv run python main.py profile add adam --telegram-id 111111111 --operator
uv run python main.py profile add anna --telegram-id 222222222
uv run python main.py ingest setup
```

For an existing single-profile install, preview and then run adoption:

```bash
uv run python main.py profile adopt adam --dry-run
uv run python main.py profile adopt adam
```

Adoption copies legacy data, validates the copy, writes `profiles.toml` last,
and leaves the legacy files intact for rollback. Stop the daemon before the
non-dry-run adoption, then restart it afterwards. This command is only for the
one-time migration of a root-level installation; use `profile add` for every
later person.

## Data Directory Override

Override the default iCloud data directory with `--data-dir` or the `HEALTH_DATA_DIR` environment variable.

## Google Drive Import

Put the shared service-account path in `.env`; put each profile's source and
folder IDs in `profiles.toml`. Then use the normal profile-aware command:

```bash
uv run python main.py import
```

CLI flags still support one-off experiments:

```bash
uv run python main.py import \
  --source google-drive \
  --google-drive-service-account PATH \
  --google-drive-metrics-folder-id ID \
  --google-drive-workouts-folder-id ID \
  --data-dir PATH \
  --db EXISTING_PATH
```

Google Drive imports first update the local `Metrics/` and `Workouts/` cache,
then run the same idempotent database import as a local source.

The daemon loads all enabled profiles, polls each Drive source independently,
and uses one shared Telegram update stream.
