# zdrowskit

> Your 24/7 ultra-personal trainer. Powered by your Apple Health data.

Apple sends you a nudge when you close your rings. zdrowskit reads your actual data — runs, lifts, heart rate variability, recovery — and tells you something worth knowing.

Had a rough Monday and skipped your workout? No panic. zdrowskit knows your plan, your goals, and your week so far. It tells you what matters — not what a streak counter thinks matters.

Built by Adam Napora (adamsky). *Zdrowie* is Polish for health. *Kit* is the tool.

---

## How it works

```
Apple Health export (iCloud Drive)
    MyHealth/Metrics/     — steps, energy, HR, HRV, VO2max, mobility
    MyHealth/Workouts/    — sessions with per-minute HR, energy, temp
    MyHealth/Routes/      — GPX tracks matched to workouts by timestamp
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
```

zdrowskit is a local pipeline. Your data stays on your machine in a SQLite database. The only external calls are the LLM API and your chosen notification channel.

## Quick start

**Prerequisites:** Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
# Clone and install
git clone <repo-url> && cd zdrowskit
uv sync

# Import your Apple Health data
uv run python main.py import --data-dir ~/Documents/zdrowskit/MyHealth

# See what's in the database
uv run python main.py status

# Get a weekly report
uv run python main.py report
```

### Setting up insights (LLM reports)

1. Copy the example context files:
   ```bash
   mkdir -p ~/Documents/zdrowskit/ContextFiles
   cp examples/context/*.md ~/Documents/zdrowskit/ContextFiles/
   ```
2. Edit them with your real data — at minimum `me.md`, `goals.md`, and `plan.md`
3. Add your API key and notification credentials to `.env`:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   TELEGRAM_BOT_TOKEN=123456789:ABCdefGHI...
   TELEGRAM_CHAT_ID=123456789
   ```
4. Generate your first report:
   ```bash
   uv run python main.py insights
   ```

The LLM reads your profile, goals, training plan, and weekly journal alongside your health data. After each run it appends a brief memory to `history.md` so it can track your progress across weeks.

## Commands

```bash
uv run python main.py import                   # parse export, upsert into DB
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
```

Data dir defaults to `~/Documents/zdrowskit/MyHealth/`. Override with `--data-dir` or the `HEALTH_DATA_DIR` env var. Run any command with `--help` for the full flag list.

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
| Code change in `src/` (e.g. `daemon.py`, `commands.py`) | `launchctl kickstart -k gui/$(id -u)/com.zdrowskit.daemon` |
| Change to `.env` (new API key, etc.) | `launchctl kickstart -k gui/$(id -u)/com.zdrowskit.daemon` |
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

## Context files

The `insights` and `nudge` commands use markdown files from `~/Documents/zdrowskit/ContextFiles/` to give the LLM real context about *you* — not just your numbers:

| File | Who edits | Purpose |
|------|-----------|---------|
| `me.md` | you + auto | Your profile — age, weight, injuries, personal baselines |
| `goals.md` | you | Health and fitness goals with timelines |
| `plan.md` | you | Weekly training schedule, diet approach, sleep targets |
| `log.md` | you | Freeform weekly journal — *why* things happened (travel, illness, life) |
| `soul.md` | you | AI coach persona — tone, style, coaching philosophy |
| `prompt.md` | you | Weekly report prompt template |
| `nudge_prompt.md` | you | Nudge prompt template — controls short notification style |
| `history.md` | auto | LLM's own memory — appended after each weekly report |

Example versions of all files are in `examples/context/`.

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
