# zdrowskit

> What Apple Health notifications *should* be.

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
        zdrowskit insights        → personalised weekly report
            + context files: your profile, goals, plan, journal
            ↓
        Email / Telegram          → delivered to your inbox or phone
```

zdrowskit is a local pipeline. Your data stays on your machine in a SQLite database. The only external call is the LLM API when you run `insights`.

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
3. Add your API key to `.env`:
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
```

Data dir defaults to `~/Documents/zdrowskit/MyHealth/`. Override with `--data-dir` or the `HEALTH_DATA_DIR` env var. Run any command with `--help` for the full flag list.

## Context files

The `insights` command uses markdown files from `~/Documents/zdrowskit/ContextFiles/` to give the LLM real context about *you* — not just your numbers:

| File | Who edits | Purpose |
|------|-----------|---------|
| `me.md` | you + auto | Your profile — age, weight, injuries, personal baselines |
| `goals.md` | you | Health and fitness goals with timelines |
| `plan.md` | you | Weekly training schedule, diet approach, sleep targets |
| `log.md` | you | Freeform weekly journal — *why* things happened (travel, illness, life) |
| `soul.md` | you | AI coach persona — tone, style, coaching philosophy |
| `prompt.md` | you | Prompt template — controls what the report looks like |
| `history.md` | auto | LLM's own memory — appended after each run for week-over-week continuity |

Example versions of all files are in `examples/context/`.

The journal (`log.md`) is what makes this different from a dashboard. Numbers say *what* happened. The journal says *why*. The LLM connects both.

## Notifications

Reports can be delivered straight to your inbox or phone.

**Email** via [Resend](https://resend.com):
```env
RESEND_API_KEY=re_xxxxx
EMAIL_TO=you@example.com
```

**Telegram** via Bot API:
```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHI...
TELEGRAM_CHAT_ID=123456789
```

Then: `uv run python main.py insights --email --telegram`

## Stack

- Python + [uv](https://github.com/astral-sh/uv)
- SQLite (local, no cloud)
- Apple Health export format ([MyHealth](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982) app)
- [litellm](https://github.com/BerriAI/litellm) for LLM calls (Claude Haiku by default)
- [Resend](https://resend.com) for email delivery (optional)
- Telegram Bot API for mobile notifications (optional)
