# zdrowskit

> What Apple Health notifications should be.

Apple sends you a nudge when you close your rings. zdrowskit reads your actual data — runs, lifts, heart rate variability, recovery — and tells you something worth knowing.

Built by Adam Napora (adamsky). *Zdrowie* is Polish for health. *Kit* is the tool. You do the math.

---

## What it does today

A local Python pipeline that:

1. **Parses** weekly Apple Health exports from iCloud Drive — activity rings, workouts, heart metrics, GPS routes, mobility data
2. **Stores** everything in a local SQLite database, upserted on each import
3. **Reports** weekly summaries and per-day breakdowns: run distance, pace, HR, HRV, elevation, lift time, recovery index, and more
4. **Exports LLM-ready JSON** — current week in detail plus months of weekly history, structured for feeding to a language model

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
```

## Usage

```bash
uv run python main.py import                   # parse export, upsert into DB
uv run python main.py report                   # current week: summary + daily
uv run python main.py report --history         # all weeks, one block each
uv run python main.py report --llm             # JSON for LLM: current + 3mo history
uv run python main.py report --llm --months 6  # same, 6 months
uv run python main.py status                   # DB row counts + date range
```

Data dir defaults to `~/Documents/adamskit/MyHealth/`. Override with `--data-dir` or `HEALTH_DATA_DIR`.

---

## The plan

zdrowskit is the data layer for something larger: a personal health intelligence system that replaces Apple's dumb rule-based nudges with reports that actually understand your life.

The full picture:

- **`soul.md`** - AI assistant's core persona, values, and long-term memory
- **`me.md`** — age, weight, medical history, baseline context
- **`goals.md`** — current health and fitness goals with timelines
- **`plan.md`** — active workout plan, diet approach, sleep targets

These three files, combined with the structured data zdrowskit produces, get passed to an LLM that generates a personalised weekly report and smart, context-aware notifications — including knowing when *not* to push you. Sick? Sleep-deprived? Life disrupted? It should know.

Apple tells you that you closed your rings. zdrowskit will tell you whether that actually mattered.

---

## Stack

- Python + [uv](https://github.com/astral-sh/uv)
- SQLite (local, no cloud)
- Apple Health export format (MyHealth app)
