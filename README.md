# zdrowskit

> What Apple Health notifications should be.

Apple sends you a nudge when you close your rings. zdrowskit reads your actual data — runs, lifts, heart rate variability, recovery — and tells you something worth knowing.

And it will allow you to manage the week the way you want. Off Monday and Tuesday? No problem. You can catch up over next days - if this is what YOU want!

Built by Adam Napora (adamsky). *Zdrowie* is Polish for health. *Kit* is the tool. You do the math.

---

## What it does today

A local Python pipeline that:

1. **Parses** weekly Apple Health exports from iCloud Drive — activity rings, workouts, heart metrics, GPS routes, mobility data
2. **Stores** everything in a local SQLite database, upserted on each import
3. **Reports** weekly summaries and per-day breakdowns: run distance, pace, HR, HRV, elevation, lift time, recovery index, and more
4. **Exports LLM-ready JSON** — current week in detail plus months of weekly history, structured for feeding to a language model
5. **Generates personalised insights** — calls an LLM with your profile, goals, training plan, weekly journal, and health data to produce an actionable weekly report

```
MyHealth/Metrics/     — steps, energy, heart rate, HRV, VO2max, mobility
MyHealth/Workouts/    — workout sessions with per-minute HR, energy, temp
MyHealth/Routes/      — GPX tracks matched to workouts by timestamp
        ↓
    zdrowskit import
        ↓
    SQLite database
        ↓
    zdrowskit report [--llm]
        ↓
    zdrowskit insights
        + soul.md, me.md, goals.md, plan.md, log.md
        ↓
    LLM → personalised weekly report
```

## Usage

```bash
uv run python main.py import                   # parse export, upsert into DB
uv run python main.py report                   # current week: summary + daily
uv run python main.py report --history         # all weeks, one block each
uv run python main.py report --llm             # JSON for LLM: current + 3mo history
uv run python main.py report --llm --months 6  # same, 6 months
uv run python main.py status                   # DB row counts + date range
uv run python main.py insights                 # LLM-driven personalised weekly report
uv run python main.py insights --no-history    # same, without appending to history.md
```

Data dir defaults to `~/Documents/zdrowskit/MyHealth/`. Override with `--data-dir` or `HEALTH_DATA_DIR`.

### Setting up insights

1. Copy example context files: `cp examples/context/*.md ~/Documents/zdrowskit/ContextFiles/`
2. Edit them with your real data (at minimum: `me.md`, `goals.md`, `plan.md`)
3. Add your API key to `.env`: `ANTHROPIC_API_KEY=sk-...`
4. Run: `uv run python main.py insights`

The LLM reads your profile, goals, training plan, and weekly journal alongside your health data to generate a personalised report. After each run it appends a brief memory to `history.md` for continuity across weeks.

---

## Context files

The `insights` command reads markdown files from `~/Documents/zdrowskit/ContextFiles/` that give the LLM the context it needs to generate truly personalised reports:

| File | Purpose |
|------|---------|
| **`soul.md`** | AI coach persona — tone, style, coaching philosophy |
| **`me.md`** | Your profile — age, weight, injuries, personal baselines (resting HR, HRV, pace) |
| **`goals.md`** | Health and fitness goals with timelines |
| **`plan.md`** | Weekly training schedule, diet approach, sleep targets |
| **`log.md`** | Freeform weekly journal — *why* things happened (travel, illness, life) |
| **`history.md`** | LLM's own memory — auto-appended after each run for week-over-week continuity |
| **`prompt.md`** | Prompt template — controls report structure and instructions to the LLM |

These files, combined with the structured health data zdrowskit produces, get passed to an LLM that generates a personalised weekly report — including knowing when *not* to push you. Sick? Sleep-deprived? Life disrupted? It should know.

Apple tells you that you closed your rings. zdrowskit will tell you whether that actually mattered.

And it will allow you to manage the week the way you want. Off Monday and Tuesday? No problem. You can catch up over next days!

---

## Stack

- Python + [uv](https://github.com/astral-sh/uv)
- SQLite (local, no cloud)
- Apple Health export format (MyHealth app)
- [litellm](https://github.com/BerriAI/litellm) for LLM calls
