# zdrowskit

> Your 24/7 ultra-personal trainer. Powered by your Apple Health data.

Apple sends you a nudge when you close your rings. zdrowskit reads your actual data — runs, lifts, heart rate variability, recovery — and tells you something worth knowing.

Had a rough Monday and skipped your workout? No panic. zdrowskit knows your plan, your goals, and your week so far. It tells you what matters — not what a streak counter thinks matters.

Want to talk back? Send a message to your Telegram bot — ask about your data, reply to a nudge, or tell it to update your training log. It's a two-way coaching conversation, not a dashboard.

Built by Adam Napora (adamsky). *Zdrowie* is Polish for health. *Kit* is the tool.

---

## How it works

```
Auto Export iOS app (iCloud Drive, every 30 min)
    Metrics/HealthAutoExport-*.json  — steps, energy, HR, HRV, VO2max, mobility, sleep
    Workouts/HealthAutoExport-*.json — sessions with HR, energy, temp, embedded routes
            ↓
        zdrowskit import          → SQLite database
            ↓
        zdrowskit report          → weekly summary + daily breakdown
        zdrowskit report --llm    → structured JSON for LLM consumption
            ↓
        zdrowskit insights        → personalised weekly report (~600 words)
        zdrowskit nudge           → short reactive notification (≤80 words)
            + context files: your profile, goals, plan, journal
            ↓
        Telegram / Email          → delivered to your phone or inbox
            ↑
        zdrowskit daemon          → watches for new data and context changes,
                                    triggers reports and nudges automatically
                                    + listens for Telegram messages (interactive chat)
```

zdrowskit is a local pipeline. Your data stays on your machine in a SQLite database. The only external calls are the LLM API and your chosen notification channel.

## Getting your data out of Apple Health

Apple Health doesn't offer a usable export API. Getting daily data onto your Mac requires a third-party iOS app and some patience. zdrowskit supports two export methods via the `--source` flag.

### Auto Export app (recommended, `--source autoexport`)

[Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982) (Premium, one-time purchase) can sync health data to iCloud Drive on a schedule — no taps required once configured.

**Setup in the app:**
1. Create two automations: one for **Metrics**, one for **Workouts**
2. Set both to: **Date Range = Week**, **Aggregation = Day**, **Destination = iCloud Drive**
3. Select all metrics you care about (steps, energy, HR, HRV, VO2max, mobility, resting heart rate, sleep analysis, etc.)
4. Set the schedule to **every 30 minutes**

**Important limitations:**
- iOS requires the phone to be **unlocked** for health data access — automations silently skip if the phone is locked
- 30-minute intervals work well in practice: your phone is unlocked often enough during the day to catch most windows
- The app writes weekly JSON files to iCloud: `Metrics/HealthAutoExport-YYYY-WW.json` and `Workouts/HealthAutoExport-YYYY-WW.json`
- **Date range gotcha:** "Year" only offers Week/Month/Year aggregation (no daily!). "Month" offers Day but you can't select which month. **"Week" with "Day" aggregation** is the sweet spot — gives daily granularity for the current week
- Sleep data is embedded as a `sleep_analysis` metric with pre-aggregated nightly totals (no per-segment breakdown)
- Workout routes are embedded as `route` arrays in the workout JSON (latitude, longitude, altitude, speed, timestamp)

**Data path:** `~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/`

### iOS Shortcuts export (one-time backfill, `--source shortcuts`)

The original method — an iOS Shortcut that reads health data and writes JSON/GPX files to iCloud Drive. Useful for backfilling historical data before switching to Auto Export.

**Limitations:**
- Requires **5 manual "Done" taps** per export run (one per data category)
- Must be triggered manually or via a scheduled automation (still needs taps to confirm)
- Separate files for metrics, workouts, sleep, and GPX routes

**Data path:** `~/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/MyHealth/`

### Recommended workflow

1. **Backfill** your historical data with a Shortcuts export: `uv run python main.py import --source shortcuts`
2. **Set up Auto Export** automations (30-min schedule, Week + Day)
3. **Run the daemon** — it watches the Auto Export iCloud folder and imports new data automatically
4. Never think about exporting again (until Apple changes something)

## Quick start

**Prerequisites:** Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
# Clone and install
git clone <repo-url> && cd zdrowskit
uv sync

# Import your Apple Health data (see "Getting your data out" above)
uv run python main.py import

# See what's in the database
uv run python main.py status

# Get a weekly report
uv run python main.py report
```

### Setting up insights (LLM reports)

1. Copy the example user context files:
   ```bash
   mkdir -p ~/Documents/zdrowskit/ContextFiles
   cp examples/context/*.md ~/Documents/zdrowskit/ContextFiles/
   ```
2. Edit them with your real data — at minimum `me.md`, `goals.md`, and `plan.md`
3. Add your API key to `.env` (plus notification credentials — see [Notifications](#notifications)):
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
4. Generate your first report:
   ```bash
   uv run python main.py insights
   ```

The LLM reads your profile, goals, training plan, and weekly journal alongside your health data. After each run it appends a brief memory to `history.md` so it can track your progress across weeks.

## Commands

```bash
uv run python main.py import                   # import from Auto Export (default)
uv run python main.py import --source shortcuts # one-time backfill from Shortcuts export
uv run python main.py report                   # current week: summary + daily
uv run python main.py report --history         # all weeks, one block each
uv run python main.py report --llm             # JSON for LLM: current + 3mo history
uv run python main.py report --llm --months 6  # same, 6 months
uv run python main.py status                   # DB row counts + date range
uv run python main.py context                  # show context files and their status

uv run python main.py insights                 # personalised weekly report via LLM
uv run python main.py insights --week last     # full review of previous week
uv run python main.py insights --explain       # show diagnostics (tokens, cost, context)
uv run python main.py insights --email         # send report via email
uv run python main.py insights --telegram      # send report via Telegram

uv run python main.py nudge                             # short nudge via Telegram (default)
uv run python main.py nudge --trigger log_update        # respond to a log.md change
uv run python main.py nudge --trigger missed_session    # missed training day reminder
uv run python main.py nudge --trigger goal_updated      # acknowledge a goals change
uv run python main.py nudge --email                     # send nudge via email instead

uv run python main.py llm-log                           # last 10 LLM calls
uv run python main.py llm-log --stats                   # usage summary by type and model
uv run python main.py llm-log --id 42                   # full detail for a specific call
uv run python main.py llm-log --json                    # output as JSON

uv run python main.py daemon-stop                       # stop the background daemon
uv run python main.py daemon-restart                    # restart (or re-load) the daemon
```

Each source has its own default iCloud data directory. Override with `--data-dir` or the `HEALTH_DATA_DIR` env var. Run any command with `--help` for the full flag list.

## The daemon — always-on trainer mode

The daemon watches your iCloud health data folder and context files. When something meaningful happens, it decides whether to send a notification.

```bash
# Test in foreground
uv run python src/daemon.py --foreground

# Install as a background service (starts automatically at login)
cp launchd/com.zdrowskit.daemon.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.zdrowskit.daemon.plist
```

**What triggers a notification:**

| Event | Delay | Action |
|---|---|---|
| Monday 8–9 AM | scheduled | Full weekly report (previous week) |
| New health data synced | 3 min | Short nudge |
| `me.md` updated | 60 sec | Nudge noting the profile change |
| `log.md` updated | 60 sec | Nudge responding to your note |
| `goals.md` updated | 60 sec | Nudge acknowledging the change |
| `plan.md` updated | 60 sec | Nudge reviewing the new plan |
| 8–9 PM, no session logged | — | Missed-session reminder |

**Smart suppression:** Before sending, the LLM sees the last 3 notifications and can choose to `SKIP` if there's nothing genuinely new to say. Nudges are also rate-limited to 3 per day with a 90-minute minimum gap.

**State file:** `~/Documents/zdrowskit/.daemon_state.json` tracks rate limits and recent nudge history. Delete or reset it to force a notification.

**Logs:** `~/Library/Logs/zdrowskit.daemon.log` (rotating, 7 days).

### Daemon operations

Check if it's running (look for a non-dash PID and exit code 0):
```bash
launchctl list | grep zdrowskit
# 6405    0    com.zdrowskit.daemon  ← good: running, clean exit
# -       78   com.zdrowskit.daemon  ← bad: not running, error
```

Watch live logs:
```bash
tail -f ~/Library/Logs/zdrowskit.daemon.log
```

**When you need to restart:**

| Scenario | Command |
|---|---|
| Code change in `src/` (e.g. `daemon.py`, `commands.py`) | `uv run python main.py daemon-restart` |
| Change to `.env` (new API key, etc.) | `uv run python main.py daemon-restart` |
| Stop for testing in foreground | `uv run python main.py daemon-stop` |
| Change to the `.plist` itself | See below |
| Context file changes (`*.md`) | **No restart needed** — read at trigger time |
| State file reset | **No restart needed** — read on every trigger |

**Updating the plist** (full reload required after editing `launchd/com.zdrowskit.daemon.plist`):
```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.zdrowskit.daemon.plist
cp launchd/com.zdrowskit.daemon.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.zdrowskit.daemon.plist
```

**Install from scratch** (first time or after a reset):
```bash
cp launchd/com.zdrowskit.daemon.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.zdrowskit.daemon.plist
```

## Interactive chat — talk to your coach

The daemon also runs a Telegram long-polling listener. Send a message to your bot from Telegram and get a coaching response backed by your full health context.

**What you can do:**
- Send any message — ask about your data, how your week is going, or what to do next
- Reply to a nudge or weekly report — the bot knows which message you're responding to
- Share updates naturally ("my weight is 76kg now", "dropping strength to 1x/week") — the LLM proposes edits to your context files with Accept/Reject buttons
- `/clear` — reset the conversation buffer
- `/status` — see buffer size and nudge count
- `/context` — list all context files with line counts
- `/context <name>` — show full content of a file (e.g. `/context me`)
- `/help` — list all available commands

The chat listener starts automatically when you run the daemon (see [above](#the-daemon--always-on-trainer-mode)). The conversation buffer holds the last 20 messages in memory. It resets when the daemon restarts, but the LLM still has your context files and history for continuity.

## Context files

The `insights`, `nudge`, and `chat` commands use markdown files from `~/Documents/zdrowskit/ContextFiles/` to give the LLM real context about *you* — not just your numbers:

| File | Who edits | Purpose |
|------|-----------|---------|
| `me.md` | you (or chat) | Your profile — age, weight, injuries, pace zones |
| `goals.md` | you (or chat) | Health and fitness goals with timelines |
| `plan.md` | you (or chat) | Weekly training schedule, diet approach, sleep targets |
| `log.md` | you (or chat) | Freeform weekly journal — *why* things happened (travel, illness, life) |
| `baselines.md` | auto | Rolling averages computed from DB (updated on each `insights` run) |
| `history.md` | auto | LLM's own memory — appended after each weekly report |

Example user context files are in `examples/context/`.

The journal (`log.md`) is what makes this different from a dashboard. Numbers say *what* happened. The journal says *why*. The LLM connects both.

## Notifications

Reports and nudges can be delivered to your phone or inbox.

**Telegram** (default for nudges and daemon-triggered reports):
```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHI...
TELEGRAM_CHAT_ID=123456789
```

**Email** via [Resend](https://resend.com) (good for the full weekly report):
```env
RESEND_API_KEY=re_xxxxx
EMAIL_TO=you@example.com
```

## Testing

```bash
uv run pytest                                    # run all tests
uv run pytest -v                                 # verbose output
uv run pytest --cov=src --cov-report=term-missing # with coverage
uv run pytest tests/test_parsers_metrics.py      # single file
```

Tests live in `tests/` with fixture data in `tests/fixtures/`. The suite covers parsers (metrics, workouts, GPX), aggregation logic, the SQLite store round-trip, report formatting, and LLM utility functions. Shared fixtures (sample snapshots, in-memory DB) are in `tests/conftest.py`.

## Stack

- Python + [uv](https://github.com/astral-sh/uv)
- SQLite (local, no cloud)
- Apple Health export format ([Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982) iOS app)
- [litellm](https://github.com/BerriAI/litellm) for LLM calls (Claude Opus by default)
- [watchdog](https://github.com/gorakhargosh/watchdog) for filesystem monitoring
- [Resend](https://resend.com) for email delivery (optional)
- Telegram Bot API for mobile notifications (default)
