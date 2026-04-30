# Setup

This guide covers local installation, first-run files, and the first LLM report. For Apple Health export setup, see [Apple Health data export](apple-health.md). For model defaults and provider routing, see [LLM setup](llm.md).

## Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv)
- Apple Health data exported by [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982)
- LLM API keys for the providers you want to use
- Telegram bot credentials if you want notifications and chat

## First Run

```bash
# Clone and install
git clone <repo-url> && cd zdrowskit
uv sync

# Create .env and first-run context files under ~/Documents/zdrowskit
uv run python main.py setup

# Check local setup without calling external APIs
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
```

Normal CLI usage auto-applies pending SQLite migrations when the database is opened. Use `uv run python main.py db status` when you want to inspect schema state explicitly.

## Enabling LLM Reports

After the first run above, to enable personalised LLM-generated reports:

1. Edit the files created by `setup` with your real data. At minimum, fill in `me.md` and `strategy.md` under `~/Documents/zdrowskit/ContextFiles/`.

2. Add your API keys to `.env`. The defaults call DeepSeek with Anthropic as the cross-provider fallback, so set both keys to enable fallback:

   ```env
   DEEPSEEK_API_KEY=sk-...
   ANTHROPIC_API_KEY=sk-ant-...
   ```

3. Generate your first report:

   ```bash
   uv run python main.py insights
   ```

The LLM reads your profile, goals, training plan, and weekly journal alongside your health data. After each run it appends a brief memory to `history.md` so it can track your progress across weeks.

Reports and coach reviews also include auto-computed seasonal baselines, lifetime milestones, and split-derived run pacing when route data is available.

## Data and Privacy

Your raw data is stored locally in SQLite. LLM calls send selected context, metrics, workouts, and journal excerpts to your configured provider. If health data leaving your machine for an LLM API is a dealbreaker, this is not the right setup.
