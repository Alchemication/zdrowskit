# zdrowskit

> An AI coach that actually knows you. Powered by your Apple Health data.

Your watch collects thousands of data points a week. Apple shows you rings. zdrowskit gives you a coach.

- **Personalised weekly reports** - not generic summaries, but analysis that knows your goals, your plan, your injuries, your journal, and how this season compares to prior years
- **Coaching proposals** - every Monday after the weekly report, the coach reviews the completed week and proposes concrete changes to your training plan or goals, with diff-first Approve/Reject buttons in Telegram
- **Reactive nudges** - new data synced or context changed? The coach notices and says something useful, or stays quiet if there is nothing to say
- **Remembers you week to week** - a freeform journal captures why things happened, and the coach appends its own memory after each report
- **Ask anything about your data** - "What's my fastest 1km pace?", "How's my HRV trending since January?", "Do I sleep worse after evening runs?" If the data exists, it will find the answer and chart it

It is a Telegram conversation, not a dashboard: reply to a report, update your goals mid-chat, get a chart on demand.

Your raw data stays local in SQLite on your machine. LLM calls do send the relevant slice of your data to the configured provider. If your health data leaving the machine for an LLM API is a dealbreaker, this is not the tool for you. See [LLM setup](docs/llm.md) for model and API details.

Built by Adam Napora (adamsky). *Zdrowie* is Polish for health. *Kit* is the tool.

Under the hood: SQLite for storage, [litellm](https://github.com/BerriAI/litellm) for provider-agnostic LLM calls, [Plotly](https://plotly.com/python/) + Kaleido for charts, [watchdog](https://github.com/gorakhargosh/watchdog) for filesystem events, and Telegram Bot API for delivery.

## How It Works

Three loops run continuously:

- **Data in** - Auto Export writes Apple Health JSON files to iCloud Drive. zdrowskit imports metrics, workouts, routes, and sleep into SQLite.
- **Coach out** - scheduled reports, weekly coaching reviews, midweek check-ins, and reactive nudges each use their own prompt, tools, and LLM call.
- **Two-way chat** - Telegram messages can query your full health history through SQL, render charts, and propose context-file edits with Approve/Reject buttons.

Storage is local. Processing is not fully local: coaching calls send selected metrics, workouts, and context snippets to your configured LLM provider.

## Requirements

- Apple Watch + iPhone
- [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982) for scheduled Apple Health JSON export
- Mac with iCloud Drive sync for the always-on daemon
- Python 3.12+ and [uv](https://github.com/astral-sh/uv)
- A capable LLM provider API key
- Telegram bot for notifications and chat

## Current Caveats

zdrowskit is personal, Apple-first, and not fully local because LLM calls send selected health context to your provider. See [Limitations](docs/limitations.md) for the full list.

## Quick Start

Before you start: `import` does nothing until Auto Export has synced Apple Health data to iCloud Drive. If you have not done that yet, set it up first — see [Apple Health data export](docs/apple-health.md).

```bash
git clone <repo-url> && cd zdrowskit
uv sync

uv run python main.py setup
uv run python main.py doctor
uv run python main.py import
uv run python main.py status
uv run python main.py insights --week last
```

For the full first-run flow, see [Setup](docs/setup.md).

## Common Commands

```bash
uv run python main.py import              # import from Auto Export
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
```

Run any command with `--help` for the full flag list. See [Commands](docs/commands.md) for the complete command reference.

## Documentation

| Topic | Details |
|---|---|
| [Setup](docs/setup.md) | Installation, `.env`, first-run context files, first LLM report |
| [Apple Health data export](docs/apple-health.md) | Auto Export setup, iCloud paths, historical backfill |
| [Commands](docs/commands.md) | CLI commands, useful flags, data directory override |
| [Daemon](docs/daemon.md) | Always-on trainer mode, launchd install, state, logs, restart rules |
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
