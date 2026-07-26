# Setup

This guide covers a new installation, the operator profile, and the first LLM
report. For Apple Health export setup, see
[Apple Health data export](apple-health.md). For model defaults and provider
routing, see [LLM setup](llm.md). To migrate an existing installation, use
[Adopt an Existing Installation](#adopt-an-existing-installation) instead.

## Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)
- Apple Health data exported by [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982)
- LLM API keys for the providers you want to use
- Telegram bot credentials if you want notifications and chat

Google Drive is recommended for new installations because the daemon can poll
it on macOS or Linux. The local/iCloud source remains available for existing
macOS setups. See [Google Drive import](google-drive.md).

## First Run

```bash
# Clone and install
git clone <repo-url> && cd zdrowskit
uv sync

# Create .env
uv run python main.py setup

# Create the operator profile and isolated context/database tree
uv run python main.py profile add me --telegram-id YOUR_NUMERIC_ID --operator --source local

# Check paths, credentials, roster, and profile files
uv run python main.py doctor

# Import your Apple Health data
uv run python main.py import

# See what's in the database
uv run python main.py status

# See DB migration status / inspect the live schema
uv run python main.py db status
uv run python main.py db schema

# Get a weekly report (no LLM)
uv run python main.py report

# Run Drive polling / iCloud watching in the foreground
uv run python src/daemon.py --foreground
```

Normal CLI usage auto-applies pending SQLite migrations when the database is opened. Use `uv run python main.py db status` when you want to inspect schema state explicitly.

`setup` still creates root-level context templates for legacy compatibility.
Once `profiles.toml` exists, active files live only under
`profiles/<name>/`; `profile add` creates that profile tree.

The first profile must use `--operator`. Use `--source local` for the host's
iCloud/Auto Export directory. To use Google Drive instead, omit `--source
local`, pass both Drive folder-ID options, and configure the shared service
account as described in [Google Drive import](google-drive.md).

The Telegram ID is the stable numeric user ID, not a username. For the first
operator, send the bot a message before starting the daemon and read
`message.from.id` from the official Bot API `getUpdates` response. Later, an
unlinked person can message the running bot once; the operator receives a notice
containing that numeric ID. Unknown users remain denied until added.

## Adopt an Existing Installation

Do not run `profile add` for an installation that already has root-level
`health.db` and `ContextFiles/`. Adoption is a one-time, explicit migration:

```bash
uv run python main.py profile adopt adam --dry-run
uv run python main.py daemon-stop
uv run python main.py profile adopt adam
uv run python main.py daemon-restart
```

Pass `--telegram-id ID` if the legacy `.env` has no `TELEGRAM_CHAT_ID`.
Adoption copies the database, context, generated outputs, preferences, daemon
state, and configured Drive cache into `profiles/adam/`, then writes
`profiles.toml` last. The old root-level files intentionally remain unchanged
as a rollback copy. See [Family hosting](family-hosting.md#adopt-an-existing-installation)
for validation and cleanup guidance.

## Enabling LLM Reports

After the first run above, to enable personalised LLM-generated reports:

1. Edit `me.md` and `strategy.md` under
   `~/Documents/zdrowskit/profiles/<name>/ContextFiles/`.

2. Add your API keys to `.env`. Defaults call DeepSeek with Anthropic as the cross-provider fallback — see [LLM setup](llm.md#api-keys) for keys and overrides.

3. Generate your first report:

   ```bash
   uv run python main.py insights
   ```

The LLM reads your profile, goals, training plan, and weekly journal alongside your health data. After each run it appends a brief memory to `history.md` so it can track your progress across weeks.

Reports and coach reviews also include auto-computed seasonal baselines, lifetime milestones, and split-derived run pacing when route data is available.
