# zdrowskit

> An AI coach that actually knows you. Powered by your Apple Health data.

Your watch collects thousands of data points a week. Apple shows you rings. zdrowskit gives you a coach.

- **Personalised weekly reports** - not generic summaries, but analysis that knows your goals, your plan, your injuries, your journal, and how this season compares to prior years
- **Coaching proposals** - every Monday after the weekly report, the coach reviews the completed week and proposes concrete changes to your training plan or goals, with diff-first Approve/Reject buttons in Telegram
- **Reactive nudges** - new data synced or context changed? The coach notices and says something useful, or stays quiet if there is nothing to say
- **Remembers you week to week** - a freeform journal captures why things happened, and the coach appends its own memory after each report
- **Ask anything about your data** - "What's my fastest 1km pace?", "How's my HRV trending since January?", "Do I sleep worse after evening runs?" If the data exists, it will find the answer and chart it
- **Host a small family roster** - one daemon and bot route each private chat to an isolated database, context directory, preferences, and runtime state, for roughly 1-10 people you personally know
- **Ask a coding agent about the repo** - the operator can route `/codex` or `/claude` questions to the local Codex / Claude Code CLI in workspace-edit mode

It is a Telegram conversation, not a dashboard: reply to a report, update your goals mid-chat, get a chart on demand.

Your raw data stays local in SQLite on your machine. LLM calls do send the relevant slice of your data to the configured provider. If your health data leaving the machine for an LLM API is a dealbreaker, this is not the tool for you. See [LLM setup](docs/llm.md) for model and API details.

Built by Adam Napora (adamsky). *Zdrowie* is Polish for health. *Kit* is the tool.

Under the hood: SQLite for storage, [litellm](https://github.com/BerriAI/litellm) for provider-agnostic LLM calls, [Plotly](https://plotly.com/python/) + Kaleido for charts, [watchdog](https://github.com/gorakhargosh/watchdog) for filesystem events, and Telegram Bot API for delivery.

## How It Works

Three loops run continuously:

- **Data in** - Auto Export posts Apple Health JSON through HTTPS (or writes to iCloud/Google Drive). zdrowskit imports metrics, workouts, routes, and sleep into SQLite.
- **Coach out** - scheduled reports, weekly coaching reviews, midweek check-ins, and reactive nudges each use their own prompt, tools, and LLM call.
- **Two-way chat** - Telegram messages can query your full health history through SQL, render charts, and propose context-file edits with Approve/Reject buttons.

## Requirements

- Apple Watch + iPhone
- [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982) for scheduled Apple Health JSON export
- A macOS or Linux machine that runs Python
- [Tailscale](https://tailscale.com/docs/install/mac) on the host; HTTP + Funnel is the default transport
- Python 3.12+ and [uv](https://github.com/astral-sh/uv)
- A capable LLM provider API key
- Telegram bot for notifications and chat

## Scale It Is Built For

zdrowskit is a family-and-friends tool: one machine, one daemon, one bot,
serving roughly 1-10 people the operator knows personally. That assumption is
deliberate and shows up everywhere — profiles are added by hand-editing a TOML
roster and restarting, there is no self-service signup or web admin, and the
host operator can read every hosted person's database. Trust between operator
and users replaces the access controls a real multi-tenant service would need.

If you want the mental model in one line: it is a household appliance, not a
service. Scale it past a handful of people and the missing pieces stop being
conveniences and start being real problems.

## Current Caveats

zdrowskit is personal and Apple-first. See [Limitations](docs/limitations.md) for the full list, including the local-storage / LLM-API boundary.

## Quick Start

Follow [HTTP ingest](docs/http-ingest.md) while running this quick start; it
shows where the generated token and Funnel URL go in Auto Export. Local/iCloud
and Google Drive remain available — see
[choosing a transport](docs/apple-health.md#choosing-a-transport) for the
trade-offs, including the one case where HTTP is the wrong pick.

```bash
git clone <repo-url> && cd zdrowskit
uv sync

uv run python main.py setup
uv run python main.py profile add me --telegram-id YOUR_NUMERIC_ID --operator
uv run python main.py ingest setup
uv run python main.py doctor
uv run python main.py daemon-install
curl http://127.0.0.1:8787/healthz
tailscale funnel --bg --https=443 http://127.0.0.1:8787
# Send the Metrics + Workouts automations from Auto Export
uv run python main.py ingest status
uv run python main.py status
uv run python main.py insights --week last
```

For the full first-run flow, see [Setup](docs/setup.md).

## Common Commands

```bash
uv run python main.py import              # import a local/iCloud or Drive backfill
uv run python main.py ingest status       # inspect direct HTTP uploads and imports
uv run python main.py status              # DB row counts + date range
uv run python main.py report              # current week: summary + daily
uv run python main.py insights            # personalised weekly report via LLM
uv run python main.py coach               # coaching review with plan/goal proposals
uv run python main.py nudge               # short reactive nudge
uv run python main.py context             # show context files and their status
uv run python main.py models              # inspect/change model routing
uv run python main.py telegram-setup      # register Telegram bot commands
uv run python main.py daemon-install      # install the launchd daemon
uv run python main.py daemon-restart      # restart the background daemon
uv run python main.py profile add anna --telegram-id ID # add an isolated profile
uv run python main.py ingest status      # receiver and per-profile upload state
uv run python main.py db status --all     # check every profile database
```

Run any command with `--help` for the full flag list. See [Commands](docs/commands.md) for the complete command reference.

## Documentation

| Topic | Details |
|---|---|
| [Setup](docs/setup.md) | Installation, `.env`, first-run context files, first LLM report |
| [Apple Health data export](docs/apple-health.md) | Transport pros and cons, Auto Export setup, iCloud paths, historical backfill |
| [HTTP ingest](docs/http-ingest.md) | Direct Auto Export uploads, Tailscale Funnel, tokens, validation, retention |
| [Google Drive import](docs/google-drive.md) | Portable API fetch, service-account setup, multiple profiles |
| [Family hosting](docs/family-hosting.md) | Profile roster, account linking, adoption, isolation, backup |
| [Commands](docs/commands.md) | CLI commands, useful flags, data directory override |
| [Daemon](docs/daemon.md) | HTTP receiving, Drive polling, iCloud watching, service operation and logs |
| [Telegram](docs/telegram.md) | Bot configuration, chat, commands, `/models`, `/notify` |
| [Context files](docs/context-files.md) | `me.md`, `strategy.md`, `log.md`, generated memory files |
| [Notifications](docs/notifications.md) | Notification types, preferences, triggers, suppression, rate limits |
| [LLM setup](docs/llm.md) | Model defaults, fallbacks, environment variables, verification, tracing |
| [Limitations](docs/limitations.md) | Platform assumptions, export constraints, local/LLM privacy boundary |
| [Testing](docs/testing.md) | pytest, ruff, fixtures, coverage |
| [LLM evals](docs/evals.md) | Feedback-derived eval workflow and leaderboard |

## Development

Tests live in `tests/`; evals live in `evals/`; prompts live in `src/prompts/`. See [Testing](docs/testing.md) and [LLM evals](docs/evals.md) for normal project workflows.

`AGENTS.md` and `CLAUDE.md` contain agent-specific coding instructions and are intentionally separate from the user-facing docs.
