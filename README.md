# zdrowskit

> An AI coach that actually knows you. Powered by your Apple Health data.

Your watch collects thousands of data points a week. Apple shows you rings. zdrowskit gives you a coach.

- **Personalised weekly reports** — not generic summaries, but analysis that knows your goals, your plan, your injuries, what you wrote in your journal last Tuesday, and how this season compares to prior years
- **Coaching proposals** — every Monday after the weekly report, the coach reviews the completed week and proposes concrete changes to your training plan or goals, with diff-first Approve/Reject buttons in Telegram
- **Reactive nudges** — new data synced or context changed? The coach notices and says something useful (or stays quiet if there's nothing to say)
- **Remembers you week to week** — a freeform journal captures *why* things happened (travel, illness, life), and the coach appends its own memory after each report. No cold starts.
- **Ask anything about your data** — "What's my fastest 1km pace?", "How's my HRV trending since January?", "Do I sleep worse after evening runs?" — if the data exists, it'll find the answer and chart it

It's a Telegram conversation, not a dashboard — reply to a report, update your goals mid-chat, get a chart on demand.

Your raw data stays local — SQLite on your machine, no third-party sync. The LLM calls don't: every coaching call sends the relevant slice to the provider. See [How it works](#how-it-works) for the full picture.

Built by Adam Napora (adamsky). *Zdrowie* is Polish for health. *Kit* is the tool.

---

## How it works

Three loops run continuously:

- **Data in** — The Auto Export iOS app writes weekly JSON files to iCloud Drive on a schedule. A daemon on your Mac imports new files into SQLite as they arrive.
- **Coach out** — The daemon decides when to send something: a Monday weekly report, coaching proposals that change your plan or goals, a midweek check-in, or reactive nudges when new data lands or you edit a context file. Each notification is a distinct LLM call with its own prompt, tools, and purpose — and the LLM can stay silent if there's nothing useful to say.
- **Two-way chat** — Reply in Telegram and the chat LLM reads your full health history via SQL, renders charts on demand, and proposes edits to your context files (profile, goals, journal) with Approve/Reject buttons.

Storage is local — SQLite on your machine, no third-party sync. The processing isn't: every coaching call sends the relevant slice of your data (metrics, workouts, journal excerpts) to the LLM provider, and responses come back through Telegram. If your health data leaving the machine for an LLM API is a dealbreaker, this isn't the tool for you.

## Requirements

- **Apple Watch + iPhone** — zdrowskit reads Apple Health data. That's the only supported source right now. You need the [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982) iOS app to get data out of HealthKit into iCloud Drive as JSON.
- **Mac** — the daemon watches your iCloud Drive folder, so it needs to run on a Mac where iCloud syncs. The rest of the stack (Python, SQLite) runs anywhere, but the data pipeline assumes macOS paths.
- **A capable LLM** — this isn't a simple summariser. The coach writes personalised reports, decides when to stay quiet, generates SQL queries against your data, and produces chart code. That requires real intelligence. **Default: DeepSeek V4 Pro** for async judgement surfaces, with Anthropic Opus 4.6 as the cross-provider fallback. Telegram chat defaults to Anthropic Opus 4.7 with reasoning off for lower latency. **Minimum: Claude Sonnet 4.6 or equivalent** — anything below that and the reports get generic, the queries get unreliable, and the charts break. Any model provider works — zdrowskit uses [litellm](https://github.com/BerriAI/litellm) so you can swap in OpenAI, Google, or any compatible API.
- **Python 3.11+** and [uv](https://github.com/astral-sh/uv)
- **Telegram bot** (for notifications and chat)

Under the hood: SQLite for storage, [litellm](https://github.com/BerriAI/litellm) for provider-agnostic LLM calls, [Plotly](https://plotly.com/python/) + Kaleido for charts, [watchdog](https://github.com/gorakhargosh/watchdog) for filesystem events, Telegram Bot API for delivery.

**Model defaults and fallback policy:** model routing is managed in
`~/Documents/zdrowskit/model_prefs.json` and can be changed with
`uv run python main.py models` or Telegram `/models`. The Telegram panel
groups features (Chat / Reports / Coach / Nudges / Utilities) and tags every
model button with its capability tier (premium / pro / flash / lite). Chat
also exposes Reasoning and Temperature controls; other groups inherit
sensible defaults from their primary model. A `Reset all` button on the main
panel and `uv run python main.py models reset --all` restore everything to
built-in defaults. Picking the `Auto` fallback (or `--fallback auto` from the
CLI) defers to the profile's fallback so future profile changes propagate.

Insights, coach, and nudges default to `deepseek/deepseek-v4-pro` with
`anthropic/claude-opus-4-6` fallback. Chat defaults to
`anthropic/claude-opus-4-7` with reasoning off and temperature omitted,
falling back to DeepSeek Pro. Lightweight utility surfaces — `/notify`
interpretation, `/log` flow building, and `/add` workout clone selection —
default to `deepseek/deepseek-v4-flash` with `anthropic/claude-haiku-4-5`
fallback. Logged LLM calls record the effective model, and fallback calls
include `requested_model` and `fallback_used` in params/metadata.

The defaults live in `src/config.py` and can be overridden from `.env`:

```env
ZDROWSKIT_PRIMARY_PRO_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_FALLBACK_PRO_MODEL=anthropic/claude-opus-4-6
ZDROWSKIT_PRIMARY_FLASH_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_FALLBACK_FLASH_MODEL=anthropic/claude-haiku-4-5
ZDROWSKIT_ANTHROPIC_OPUS_4_7_MODEL=anthropic/claude-opus-4-7

ZDROWSKIT_INSIGHTS_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_COACH_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_NUDGE_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_CHAT_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_NOTIFY_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_LOG_FLOW_MODEL=anthropic/claude-haiku-4-5
# /log uses deepseek/deepseek-v4-flash as its feature-level fallback
ZDROWSKIT_ADD_CLONE_MODEL=deepseek/deepseek-v4-flash
```

## Getting your data out of Apple Health

Apple's built-in health export dumps everything into a single massive XML file. On any non-trivial data size, this crashes or overheats the iPhone — it's not a real solution.

The workaround is a third-party iOS app that reads HealthKit directly and writes structured JSON to iCloud Drive. zdrowskit uses [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982) for this. It works on iOS 26 (some alternatives don't yet). The Basic tier unlocks Shortcut actions; **Premium** (still cheap, one-time purchase) is needed for scheduled Automations.

**One universal constraint:** iOS requires the phone to be **unlocked** for any health data export — automations silently skip when the phone is locked.

### Auto Export setup

The Automations feature syncs health data to iCloud Drive on a schedule — no taps required once configured.

**Setup in the app:**
1. Create two automations: one for **Metrics**, one for **Workouts**
2. Set both to: **Date Range = Week**, **Aggregation = Day**, **Destination = iCloud Drive**
3. Select all metrics you care about (steps, energy, HR, HRV, VO2max, mobility, resting heart rate, sleep analysis, etc.)
4. Set the schedule — **every 5 minutes recommended** (shorter intervals catch more unlock windows)

The app writes weekly JSON files: `Metrics/HealthAutoExport-YYYY-WW.json` and `Workouts/HealthAutoExport-YYYY-WW.json`

**Data path:** `~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/`

**Notes:**
- Sleep data is pre-aggregated nightly totals (no per-segment breakdown)
- Workout routes are embedded as `route` arrays (latitude, longitude, altitude, speed, timestamp); zdrowskit derives per-km splits from these when present

### Historical backfill

Each automation has a **Manual Export** button (at the bottom of the automation screen) that supports custom date ranges. Use this to backfill historical data — the output uses the exact same format as the scheduled exports, so no separate import path is needed.

**How to backfill:**
1. Open an existing automation in Auto Export
2. Scroll to the bottom and tap **Manual Export**
3. Set a custom date range (e.g. the whole of 2024) — the app splits it into weekly files automatically
4. Wait for the files to sync via iCloud
5. Run `uv run python main.py import` — the same command handles both current and historical data

Do this once per automation (Metrics and Workouts). The import is idempotent — re-running it won't duplicate data.

### Recommended workflow

1. **Set up Auto Export** automations (Week + Day aggregation, see setup above)
2. **Backfill** historical data using Manual Export from each automation
3. **Import everything:** `uv run python main.py import`
4. **Run the daemon** — it watches the Auto Export iCloud folder and imports new data automatically
5. Never think about exporting again (until Apple changes something)

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
3. Add your API keys to `.env` (plus Telegram credentials — see [Notifications](#notifications)). The defaults call DeepSeek with Anthropic as the cross-provider fallback, so set both keys to enable fallback:
   ```
   DEEPSEEK_API_KEY=sk-...
   ANTHROPIC_API_KEY=sk-ant-...
   ```
4. Generate your first report:
   ```bash
   uv run python main.py insights
   ```

The LLM reads your profile, goals, training plan, and weekly journal alongside your health data. After each run it appends a brief memory to `history.md` so it can track your progress across weeks.
Reports and coach reviews also include auto-computed seasonal baselines, lifetime milestones, and split-derived run pacing when route data is available.

## Commands

```bash
uv run python main.py import              # import from Auto Export
uv run python main.py status              # DB row counts + date range
uv run python main.py report              # current week: summary + daily
uv run python main.py insights            # personalised weekly report via LLM
uv run python main.py coach               # coaching review with plan/goal proposals
uv run python main.py nudge               # short reactive nudge
uv run python main.py context             # show context files and their status
uv run python main.py events              # system event log (fires, skips, imports)
uv run python main.py llm-log             # inspect stored LLM call traces
uv run python main.py models              # inspect/change model routing
uv run python main.py telegram-setup      # register bot /commands for Telegram menu
uv run python main.py daemon-restart      # restart the background daemon
uv run python main.py daemon-stop         # stop the background daemon
```

Run any command with `--help` for the full flag list — e.g. `insights --week last --telegram`, `nudge --trigger log_update`, `llm-log --id 42 --feedback`, `events --since 3d --category nudge`. LLM evals have their own runner, see [LLM evals](#llm-evals).

Override the default iCloud data directory with `--data-dir` or the `HEALTH_DATA_DIR` env var.

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
| `baselines.md` | auto | Rolling + seasonal baselines computed from DB (updated on each `insights` run) |
| `history.md` | auto | LLM's own memory — appended after each weekly report |
| `coach_feedback.md` | auto | Accept/reject history for coach and chat suggestions, including optional rejection reasons |

Example user context files are in `examples/context/`.

The journal (`log.md`) is what makes this different from a dashboard. Numbers say *what* happened. The journal says *why*. The LLM connects both.

## Notifications

Each notification type is a distinct LLM call with its own prompt, context, tools, and purpose. They complement each other — not repeat each other.

| Channel | Purpose | Trigger | Frequency | Length | Tools | Special output |
|---------|---------|---------|-----------|--------|-------|----------------|
| **Insights** | Full weekly report | Scheduled (default: Mon 10am) or manual `/review` | 1×/week | ~450 words | `run_sql` | `<chart>` (0+), `<memory>` (always 1, appended to `history.md`) |
| **Coach** | Weekly strategy review, only when proposals exist | After insights (silent on no-change weeks) | 1×/week | ~300 words | `run_sql`, `update_context` (`strategy` only) | `SKIP` if no changes warranted; bundled message with inline Accept/Reject buttons per edit |
| **Nudge** | Short reactive next-action nudge | Data sync, file edit | Up to 3/day by default | 80 words | `run_sql` | `SKIP` if nothing changes; `<chart>` (0–1) |
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
| Monday 8–9 AM | scheduled | Full weekly report, then coaching review |
| Thursday 9–10 AM | scheduled | Mid-week progress report |

### Interactive chat

The daemon runs a Telegram long-polling listener alongside the file watcher. Send a message and get a coaching response backed by your full health context.

- Ask analytical questions — the LLM queries your database with SQL and charts the results
- Reply to a nudge or report — the bot knows which message you're responding to
- Share updates naturally ("my weight is 76kg now") — the LLM proposes context file edits with Accept/Reject buttons
- Thumbs down a bad output, pick a category, optionally reply with more detail, and undo it if you tapped it during testing or a demo
- Commands: `/review [current|last]`, `/coach [current|last]`, `/add`, `/log`, `/notify`, `/clear`, `/status`, `/events [N] [category]`, `/context [name]`, `/tutorial`, `/help`
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

### Optional LLM verification

Post-generation verification can be enabled for reports, coach reviews, and
nudges. This adds a separate verifier call and, when the issue is fixable, one
bounded rewrite call before the output is saved or sent. It is off by default
for controlled eval rollouts.

```env
ZDROWSKIT_ENABLE_LLM_VERIFICATION=1
ZDROWSKIT_VERIFICATION_MODEL=deepseek/deepseek-v4-pro
ZDROWSKIT_VERIFICATION_REWRITE_MODEL=deepseek/deepseek-v4-flash
ZDROWSKIT_MAX_VERIFICATION_REVISIONS=1
ZDROWSKIT_VERIFY_INSIGHTS=1
ZDROWSKIT_VERIFY_COACH=1
ZDROWSKIT_VERIFY_NUDGE=1
```

Verification traces are logged as `insights_verify`, `insights_rewrite`,
`coach_verify`, `coach_rewrite`, `nudge_verify`, and `nudge_rewrite`. The
original source call metadata also records the verifier verdict, issue counts,
issue details, and verifier/rewrite call IDs. Use
`uv run python main.py llm-log --id N` on either the source call or a verifier
call to see the related verification trace.

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
uv run python -m evals.run --record                     # persist a run to evals/leaderboard/runs.jsonl
uv run python -m evals.run --no-temperature             # omit temperature (required by claude-opus-4-7)
uv run python -m evals.leaderboard render               # rebuild evals/leaderboard.md from raw history
uv run python -m evals.leaderboard render-html          # rebuild evals/leaderboard.html with filters and sortable views
```

These evals call the configured real model and may use network/API quota. Normal `uv run pytest` uses mocks and must never call a real LLM.

Recorded leaderboard runs live in `evals/leaderboard/runs.jsonl`. The generated Markdown snapshot lives in `evals/leaderboard.md`. Comparisons are scope-aware: runs over different case sets are rendered in separate sections rather than ranked together.
The interactive HTML report lives in `evals/leaderboard.html` and is generated from the same raw JSONL history.

`evals/leaderboard.html` is published to GitHub Pages by `.github/workflows/evals-pages.yml` at <https://alchemication.github.io/zdrowskit/>. Enable Pages with **Settings -> Pages -> Source: GitHub Actions**; after that, pushes to `main` that update `evals/leaderboard/runs.jsonl` rebuild and deploy the latest leaderboard as the Pages `index.html`. The workflow can also be run manually from the Actions tab.
