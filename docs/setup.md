# Setup

This guide covers a new installation, the operator profile, and the first LLM
report. For Apple Health export setup, see
[Apple Health data export](apple-health.md). For model defaults and provider
routing, see [LLM setup](llm.md). To migrate an existing installation, use
[Adopt an Existing Installation](#adopt-an-existing-installation) instead.

## Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)
- [Tailscale](https://tailscale.com/docs/install/mac) on the host when using the default HTTP transport
- Apple Health data exported by [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982)
- LLM API keys for the routes you want to use
- A Telegram bot token if you want notifications and chat

Direct HTTP delivery through Tailscale is recommended for new installations.
The local/iCloud and Google Drive sources remain available, particularly for
historical backfills. See [Auto Export HTTP ingest](http-ingest.md), and
[choosing a transport](apple-health.md#choosing-a-transport) for how the three
compare on latency, profile count, and behaviour when the host is offline.

Before the repository setup, install/sign in to Tailscale and confirm
`tailscale status` works in Terminal. The HTTP guide covers the macOS app
variants, CLI Integration, first Funnel approval, persistence, and testing from
another device.

## Create the Telegram Bot

Each Telegram-enabled installation needs exclusive control of one bot token.
The bot does not have to be newly created, but no other application or
zdrowskit daemon may poll the same token. Telegram does not distribute updates
between competing consumers: concurrent `getUpdates` pollers conflict, and
inbound messages and callback buttons become unreliable.

Create a bot with [BotFather](https://t.me/BotFather), or dedicate an existing
unused bot to this installation. One bot serves the operator and every hosted
profile; family members and friends do not create separate bots. A second
independent operator running their own zdrowskit installation needs a different
bot token.

After `uv run python main.py setup` creates `.env`, add the token as
`TELEGRAM_BOT_TOKEN`. Do not give the token to hosted users. Before starting
the daemon, message the bot once and obtain the operator's numeric Telegram ID
as described in [Telegram configuration](telegram.md#configuration).

## First Run

```bash
# Clone and install
git clone <repo-url> && cd zdrowskit
uv sync

# Create .env
uv run python main.py setup

# Create the operator profile and isolated context/database tree
uv run python main.py profile add me --telegram-id YOUR_NUMERIC_ID --operator

# Fill in the profile and plan before asking the coach to reason about them
# ~/Documents/zdrowskit/profiles/me/ContextFiles/me.md
# ~/Documents/zdrowskit/profiles/me/ContextFiles/strategy.md

# Create the upload token and show the iPhone settings; keep this output open
uv run python main.py ingest setup

# Install the daemon/receiver
uv run python main.py daemon-install

# Verify locally, then expose only this loopback service through HTTPS
curl http://127.0.0.1:8787/healthz
tailscale funnel --bg --https=443 http://127.0.0.1:8787
tailscale funnel status

# Check the complete local setup, running receiver, and public DNS
uv run python main.py doctor

# Configure and send Metrics + Workouts from Auto Export, then verify the pair
uv run python main.py ingest status

# See what's in the database
uv run python main.py status

# See DB migration status / inspect the live schema
uv run python main.py db status
uv run python main.py db schema

# Get a weekly report (no LLM)
uv run python main.py report

# Run HTTP receiving / Drive polling / iCloud watching in the foreground
uv run python src/daemon.py --foreground
```

Normal CLI usage auto-applies pending SQLite migrations when the database is
opened. Use `uv run python main.py db status` when you want to inspect schema
state explicitly.

`setup` creates the shared app home and copies `.env_example` to `.env` when
`.env` does not already exist. It does not create a database or context files;
`profile add` creates the isolated profile tree.

The first profile must use `--operator`; HTTP is the default source. Use
`--source local` for the host's iCloud directory. For Google Drive, pass
`--source google-drive`, both folder IDs, and configure the shared service
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

2. Add the OpenAI, DeepSeek, and Anthropic keys used by the built-in primary and
   fallback routes. See [LLM setup](llm.md#api-keys) before omitting a provider
   or changing those routes.

3. Generate your first report:

   ```bash
   uv run python main.py insights
   ```

The LLM reads your profile, goals, training plan, and weekly journal alongside your health data. Once a report is written, a separate short call decides what is worth carrying forward and appends it to `history.md`, so later runs build on earlier weeks.

Reports and coach reviews also include auto-computed seasonal baselines, lifetime milestones, and split-derived run pacing and heart rate when route data is available.
