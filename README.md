# zdrowskit

> An AI coach that actually knows you. Powered by your Apple Health data.

Your watch collects thousands of data points a week. Apple shows you rings. zdrowskit gives you a coach.

- **Personalised weekly reports** — not generic summaries, but analysis that knows your goals, your plan, your injuries, and what you wrote in your journal last Tuesday
- **Coaching proposals** — every Monday after the weekly report, the coach reviews the completed week and proposes concrete changes to your training plan or goals, with diff-first Approve/Reject buttons in Telegram
- **Reactive nudges** — skipped a session? New data synced? The coach notices and says something useful (or stays quiet if there's nothing to say)
- **Adjust notifications from Telegram** — use `/notify` to change report days/times, mute nudges temporarily, or reset everything to defaults with an Approve/Reject confirmation
- **Ask anything about your data** — "What's my fastest 1km pace?", "How's my HRV trending since January?", "Do I sleep worse after evening runs?" — if the data exists, it'll find the answer and chart it
- **Two-way conversation** — reply to a report, update your goals mid-chat, get a chart on demand. It's a Telegram conversation, not a dashboard

Your raw data lives in a SQLite database on your machine — no third-party sync, no analytics, no telemetry. Be aware though: every coaching call sends the relevant slice of that data (metrics, workouts, journal excerpts) to the LLM provider and the responses go through Telegram. Storage is local; the intelligence is not.

Built by Adam Napora (adamsky). *Zdrowie* is Polish for health. *Kit* is the tool.

---

## How it works

```
Auto Export iOS app (iCloud Drive, on a schedule)
    Metrics/HealthAutoExport-*.json  — steps, energy, HR, HRV, VO2max, mobility, sleep
    Workouts/HealthAutoExport-*.json — sessions with HR, energy, temp, embedded routes
            ↓
        zdrowskit import          → SQLite database
            ↓
        zdrowskit report          → weekly summary + daily breakdown
        zdrowskit report --llm    → structured JSON for LLM consumption
            ↓
        zdrowskit insights        → personalised weekly report (~600 words)
        zdrowskit coach           → plan/goal proposals with Approve/Reject
        zdrowskit nudge           → short reactive notification (≤80 words)
            + context files: your profile, goals, plan, journal
            ↓
        Telegram                  → delivered to your phone
            ↑
        zdrowskit daemon          → watches for new data and context changes,
                                    triggers reports and nudges automatically
                                    + listens for Telegram messages (interactive chat)
                                    + answers data questions via SQL tool-calling loop
                                    + generates on-demand Plotly charts
```

zdrowskit's storage is local — SQLite on your machine, no third-party sync. The processing isn't: every coaching call sends the relevant slice of your data (metrics, workouts, journal excerpts) to the LLM provider, and the responses are delivered through Telegram. If your health data leaving the machine for an LLM API is a dealbreaker, this isn't the tool for you.

## Getting your data out of Apple Health

Apple's built-in health export dumps everything into a single massive XML file. On any non-trivial data size, this crashes or overheats the iPhone — it's not a real solution.

The workaround is a third-party iOS app that reads HealthKit directly and writes structured JSON to iCloud Drive. zdrowskit uses [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982) for this. It works on iOS 26 (some alternatives don't yet). The Basic tier unlocks Shortcut actions; **Premium** (still cheap, one-time purchase) is needed for scheduled Automations.

**One universal constraint:** iOS requires the phone to be **unlocked** for any health data export — automations silently skip when the phone is locked.

### Auto Export automations (ongoing, `--source autoexport`)

The Automations feature syncs health data to iCloud Drive on a schedule — no taps required once configured.

**Setup in the app:**
1. Create two automations: one for **Metrics**, one for **Workouts**
2. Set both to: **Date Range = Week**, **Aggregation = Day**, **Destination = iCloud Drive**
3. Select all metrics you care about (steps, energy, HR, HRV, VO2max, mobility, resting heart rate, sleep analysis, etc.)
4. Set the schedule — **every 5 minutes recommended** (shorter intervals catch more unlock windows)

**Limitations:**
- Automations only export the **current week** — you can't pull historical data at daily granularity this way ("Year" aggregation has no daily option; "Month" has daily but you can't choose which month)
- Sleep data is pre-aggregated nightly totals (no per-segment breakdown)
- Workout routes are embedded as `route` arrays (latitude, longitude, altitude, speed, timestamp)
- The app writes weekly JSON files: `Metrics/HealthAutoExport-YYYY-WW.json` and `Workouts/HealthAutoExport-YYYY-WW.json`

**Data path:** `~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/`

### Auto Export shortcuts actions (one-time backfill, `--source shortcuts`)

Auto Export also provides iOS Shortcut actions with flexible date ranges — useful for backfilling historical data that automations can't reach.

**Limitations:**
- Each action supports **max 10 metrics** — you need a chain of actions to cover everything (metrics, workouts, routes)
- Every action in the chain requires a **manual confirmation tap** (iOS limitation on health data access), and the whole shortcut must be kicked off manually on an unlocked phone
- Separate output files for metrics, workouts, sleep, and GPX routes

Tedious, but it's a one-time chore. Once historical data is imported, automations handle everything going forward.

**Data path:** `~/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/MyHealth/`

### Recommended workflow

1. **Backfill** historical data using Shortcuts actions: `uv run python main.py import --source shortcuts`
2. **Set up Auto Export** automations (Week + Day aggregation, see setup above)
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

# See DB migration status / inspect the live schema
uv run python main.py db status
uv run python main.py db schema

# Get a weekly report
uv run python main.py report
```

Normal CLI usage auto-applies pending SQLite migrations when the database is opened. Use `uv run python main.py db status` when you want to inspect schema state explicitly.

### Setting up insights (LLM reports)

1. Copy the example user context files:
   ```bash
   mkdir -p ~/Documents/zdrowskit/ContextFiles
   cp examples/context/*.md ~/Documents/zdrowskit/ContextFiles/
   ```
2. Edit them with your real data — at minimum `me.md` and `strategy.md`
3. Add your API key to `.env` (plus Telegram credentials — see [Notifications](#notifications)):
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
uv run python main.py insights --telegram      # send report via Telegram

uv run python main.py nudge                             # short nudge via Telegram (default)
uv run python main.py nudge --trigger log_update        # respond to a log.md change
uv run python main.py nudge --trigger missed_session    # missed training day reminder
uv run python main.py nudge --trigger goal_updated      # acknowledge a goals change

uv run python main.py coach                              # coaching review: propose plan/goal updates for last week
uv run python main.py coach --week current               # provisional review of the current week so far
uv run python main.py coach --telegram                   # send proposals with Approve/Reject buttons

uv run python main.py llm-log                           # last 10 LLM calls
uv run python main.py llm-log --stats                   # usage summary by type and model
uv run python main.py llm-log --id 42                   # full stored trace for one LLM call (messages, tool use, response)
uv run python main.py llm-log --feedback                # recent thumbs-down feedback
uv run python main.py llm-log --json                    # output as JSON

uv run python main.py telegram-setup                    # register bot /commands for autocomplete + menu
uv run python main.py daemon-stop                       # stop the background daemon
uv run python main.py daemon-restart                    # restart (or re-load) the daemon

uv run python -m evals.run                              # run all feedback-derived LLM evals (real model)
uv run python -m evals.run --feature chat               # run only chat eval cases
uv run python -m evals.run chat_log_life_disruption     # run one eval case
uv run python -m evals.run --details                    # include failed-case text and captured tool calls
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

What it watches and when it acts is covered in the [Notifications](#notifications) section — triggers, suppression rules, and cross-channel awareness.

**State file:** `~/Documents/zdrowskit/.daemon_state.json` tracks rate limits, recent nudge history, coach summaries, the deferred nudge queue, and pending Telegram reason prompts for feedback / proposal rejection.

**Notification prefs:** `~/Documents/zdrowskit/notification_prefs.json` stores notification overrides and temporary mutes set via Telegram `/notify`. Delete it to fall back to built-in defaults.

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
| `notification_prefs.json` edit/reset | **No restart needed** — read on every trigger |

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

The `insights`, `coach`, `nudge`, and `chat` commands use markdown files from `~/Documents/zdrowskit/ContextFiles/` to give the LLM real context about *you* — not just your numbers:

| File | Who edits | Purpose |
|------|-----------|---------|
| `me.md` | you (or chat) | Your profile — age, weight, injuries, pace zones |
| `strategy.md` | you (or chat or coach) | Goals + weekly training schedule + diet + sleep targets, all in one file |
| `log.md` | you (or chat) | Freeform weekly journal — *why* things happened (travel, illness, life) |
| `baselines.md` | auto | Rolling averages computed from DB (updated on each `insights` run) |
| `history.md` | auto | LLM's own memory — appended after each weekly report |
| `coach_feedback.md` | auto | Accept/reject history for coach and chat suggestions, including optional rejection reasons |

Example user context files are in `examples/context/`.

The journal (`log.md`) is what makes this different from a dashboard. Numbers say *what* happened. The journal says *why*. The LLM connects both.

## Notifications

Each notification type is a distinct LLM call with its own prompt, context, tools, and purpose. They complement each other — not repeat each other.

| Channel | Purpose | Trigger | Frequency | Length | Tools | Special output |
|---------|---------|---------|-----------|--------|-------|----------------|
| **Insights** | Full weekly report | Scheduled (default: Mon 8am) or manual `/review` | 1×/week | ~600 words | `run_sql` | `<chart>` (0+), `<memory>` (always 1, appended to `history.md`) |
| **Coach** | Weekly strategy review, only when proposals exist | After insights (silent on no-change weeks) | 1×/week | ~300 words | `run_sql`, `update_context` (`strategy` only) | `SKIP` if no changes warranted; bundled message with inline Accept/Reject buttons per edit |
| **Nudge** | Short reactive next-action nudge | Data sync, file edit, missed session | Up to 3/day by default | 80 words | `run_sql` | `SKIP` if nothing changes; `<chart>` (0–1) |
| **Chat** | Interactive conversation — answer the current message, ask anything, get charts | Your Telegram message | On demand | 150 words | `run_sql` (up to 5/turn), `update_context` (any file) | `<chart>` (optional), `update_context` (at most 1) |

### Notification preferences via Telegram

Use `/notify` in Telegram to inspect and change notification behavior without editing files by hand.

- `/notify` shows current effective settings, active temporary mutes, and examples
- `/notify no nudges before 11am`
- `/notify send weekly insights on Tuesday at 8`
- `/notify turn off midweek report`
- `/notify mute nudges today`
- `/notify bring weekly insights back to default`
- `/notify set all as default`

How it works:

- A small LLM interprets the request into a strict structured proposal
- The bot shows the interpreted change back to you with `Accept` / `Reject`
- Nothing is saved until you tap `Accept`
- If the request is ambiguous, the bot asks a short clarification question
- Preferences live in `~/Documents/zdrowskit/notification_prefs.json`

What can be changed:

- nudges on/off
- nudge earliest send time
- weekly insights on/off, weekday, and time
- midweek report on/off, weekday, and time
- temporary mutes for all notifications or one notification type
- reset one setting or everything back to built-in defaults

### What triggers nudges

| Event | Debounce | What it does |
|-------|----------|-------------|
| Health data synced via iCloud | 3 min | One data observation + suggestion for today/tomorrow |
| `log.md` / `strategy.md` / `me.md` edited | 60 sec | Responds to the change — acknowledges, flags tension, or confirms |
| 8–9 PM, no workout on a training day | — | Factual note + one suggestion (skip, shift, lighter alternative) |
| Monday 8–9 AM | scheduled | Full weekly report, then coaching review |
| Thursday 9–10 AM | scheduled | Mid-week progress report |

### Interactive chat

The daemon runs a Telegram long-polling listener alongside the file watcher. Send a message and get a coaching response backed by your full health context.

- Ask analytical questions — the LLM queries your database with SQL and charts the results
- Reply to a nudge or report — the bot knows which message you're responding to
- Share updates naturally ("my weight is 76kg now") — the LLM proposes context file edits with Accept/Reject buttons
- Thumbs down a bad output, pick a category, optionally reply with more detail, and undo it if you tapped it during testing or a demo
- Commands: `/review [current|last]`, `/coach [current|last]`, `/add`, `/notify`, `/clear`, `/status`, `/context [name]`, `/tutorial`, `/help`
- `/tutorial` opens a 9-step guided tour of the system (what it does, key metrics, features, honest trade-offs) with Next/Back/Exit buttons
- `/status` shows bot state, data coverage, recent activity, and notification state
- Conversation buffer: last 20 messages in memory, resets on daemon restart

### Cross-message awareness

Each channel sees what the others recently said so the LLM avoids redundancy:

- **Coach** sees recent nudges sent
- **Nudge** sees last 3 nudges + last coach review summary
- **Chat** sees last 3 nudges + last coach review summary
- **Insights** is independent (has its own `history.md` memory)

### Suppression and rate limiting

- **Earliest nudge time:** nudges are deferred until the configured earliest send time. Triggers queue and drain as one consolidated nudge once the window opens
- **Temporary mute / disable:** when a notification type is muted or disabled, the daemon skips the notification LLM call entirely
- **Report suppression:** nudges suppressed ±1 hour around scheduled reports — the report already covers the big picture
- **Rate limits:** max 3 nudges/day by default, min 90 minutes apart
- **LLM SKIP:** the nudge LLM can respond `SKIP` if there's nothing genuinely new to say
- **Coach:** runs at most once per calendar day
- **No replay after mute:** skipped nudges/reports are not replayed after a temporary mute expires

### Telegram configuration

**Telegram** (for nudges, chat, and daemon-triggered reports):
```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHI...
TELEGRAM_CHAT_ID=123456789
```

## Testing

```bash
uv run pytest                                    # run all tests
uv run pytest -v                                 # verbose output
uv run pytest --cov=src --cov-report=term-missing # with coverage
uv run pytest tests/test_parsers_metrics.py      # single file
```

Tests live in `tests/` with fixture data in `tests/fixtures/`. The suite covers parsers (metrics, workouts, GPX), aggregation logic, the SQLite store round-trip, report formatting, LLM utility functions, and the `run_sql` tool (SQL validation, read-only safety, row limits, query execution). Shared fixtures (sample snapshots, in-memory DB) are in `tests/conftest.py`.

## LLM evals

LLM evals live in `evals/` and are feedback-derived regressions, not a broad generated benchmark. They are meant to preserve real product judgement: start with an actual thumbs-down Telegram feedback item, inspect the stored trace with `uv run python main.py llm-log --feedback` and `uv run python main.py llm-log --id N`, then encode the smallest case that would have caught the issue.

The current philosophy:

- Start every eval cluster with a `real_regression` case from one real failure.
- Add only the minimum synthetic cases needed to broaden the surface around that failure, such as an explicit positive control or a false-positive guard.
- Keep synthetic cases tied to the original feedback using `source_feedback_id`, `source_llm_call_id`, and `derived_from.hypothesis`.
- Prefer structured fixtures over pasted raw transcripts: pinned date, context snippets, conversation turns, and only the health data needed for the case.
- Use deterministic assertions first: tool called/not called, argument matching, text contains/does-not-contain, max word count, and forbidden openings.
- Avoid LLM-as-judge unless a future real feedback case genuinely cannot be evaluated deterministically.

```bash
uv run python -m evals.run                              # all feedback-derived eval cases
uv run python -m evals.run chat_log_life_disruption     # one case
uv run python -m evals.run --feature chat               # feature filter
uv run python -m evals.run --details                    # debug failed cases
```

These evals call the configured real model and may use network/API quota. Normal `uv run pytest` uses mocks and must never call a real LLM.

## Requirements

- **Apple Watch + iPhone** — zdrowskit reads Apple Health data. That's the only supported source right now. You need the [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982) iOS app to get data out of HealthKit into iCloud Drive as JSON.
- **Mac** — the daemon watches your iCloud Drive folder, so it needs to run on a Mac where iCloud syncs. The rest of the stack (Python, SQLite) runs anywhere, but the data pipeline assumes macOS paths.
- **A capable LLM** — this isn't a simple summariser. The coach writes personalised reports, decides when to stay quiet, generates SQL queries against your data, and produces chart code. That requires real intelligence. **Recommended: Claude Opus 4.6** (or equivalent). **Minimum: Claude Sonnet 4.6** — anything below that and the reports get generic, the queries get unreliable, and the charts break. Any model provider works — zdrowskit uses [litellm](https://github.com/BerriAI/litellm) so you can swap in OpenAI, Google, or any compatible API.
- **Python 3.11+** and [uv](https://github.com/astral-sh/uv)
- **Telegram bot** (for notifications and chat)

## Stack

- Python + [uv](https://github.com/astral-sh/uv)
- SQLite (local storage; LLM API calls still send data slices off-machine)
- [litellm](https://github.com/BerriAI/litellm) for LLM calls (provider-agnostic)
- [Plotly](https://plotly.com/python/) for chart rendering (PNG via Kaleido)
- [watchdog](https://github.com/gorakhargosh/watchdog) for filesystem monitoring
- Telegram Bot API for notifications and interactive chat
