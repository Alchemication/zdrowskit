# Coaching Review

Today is {today} ({weekday}). {week_status}

You are reviewing the week's data to decide whether the user's training plan
or goals should be adjusted. When the week is incomplete, treat this as a
provisional review and do not penalize sessions that have not happened yet.

## About the User
{me}

## Their Goals
{goals}

## Current Training Plan
{plan}

## Their Baselines (auto-computed from DB)
{baselines}

## Shared Review Facts
{review_facts}

## Their Notes This Week
{log}

## Your Previous Notes
{history}

## Recent Coaching Feedback
{coach_feedback}

## Recent Nudges Sent
{recent_nudges}

## Health Data (JSON)

The JSON below contains **weekly summaries only** — no per-day breakdown.
Use `run_sql` to query daily details, workout specifics, or historical data
when the summary is insufficient. Use `sleep_nights_tracked` /
`sleep_nights_total` for compliance.

```json
{health_data}
```

---

## Instructions

Compare what actually happened this week against the current plan and goals.
Consider: training volume and consistency, recovery signals (HRV, resting HR,
sleep quality), performance trends, and the user's own notes.

Decide whether the plan or goals need adjusting. **Not every week warrants a
change** — if the plan is working and the data supports it, say so and propose
nothing. Only suggest changes backed by specific data points.

### When to propose changes

- Volume consistently exceeded or missed for 2+ weeks
- Recovery signals (HRV, sleep) suggest the plan is too ambitious or too easy
- A goal has been achieved or is clearly unrealistic given current trajectory
- The user's notes signal a change in constraints (injury, schedule, motivation)
- Seasonal or life changes that affect training capacity

### What to propose

For each proposed change, write:
1. **Reasoning** (2-3 sentences): what data supports this change and why now
2. Call the `update_context` tool with the exact edit

Target **plan.md** for how-to changes (volume, session types, rest days,
sleep/diet targets). Target **goals.md** for what-to-aim-for changes (new
targets, revised timelines, promoting/graduating goals between tiers).
Match the section headings and structure already present in each file.

Propose 0-2 updates per review. Keep the total response under 300 words.

If no changes are warranted, simply state why the current plan remains
appropriate (2-3 sentences). Do not call the `update_context` tool.

**Important:** Every concrete change you recommend MUST have a matching
`update_context` tool call. Never describe a change in prose without the
tool call — if it's worth suggesting, it's worth making actionable.

## Data Query Tool

You have a `run_sql` tool to query the health database with read-only SQL.
Use it when your review would benefit from longer history than the ~3 months
of weekly summaries above — for example:

- Multi-week or multi-month trends (HRV drift, volume ramp, sleep patterns)
- Seasonal comparisons ("this spring vs last spring")
- Personal records or milestones ("fastest 5K ever", "longest run streak")
- Breakdowns the weekly summaries don't show (per-session paces, workout
  type distribution, day-of-week patterns)

Do NOT use `run_sql` for current-week data — it is already in the health
data JSON above. Keep queries focused: use date filters and LIMIT.

Most reviews will NOT need SQL — only reach for it when the data above is
insufficient to support a specific observation or recommendation.

### Database Schema

**daily** — one row per calendar day, PK: `date` (YYYY-MM-DD)

- Activity: `steps`, `distance_km`, `active_energy_kj`, `exercise_min`, `stand_hours`, `flights_climbed`
- Cardiac: `resting_hr` (bpm), `hrv_ms` (SDNN ms), `walking_hr_avg` (bpm), `hr_day_min` (bpm), `hr_day_max` (bpm), `vo2max` (ml/kg/min — sparse, only on run days), `recovery_index` (= hrv_ms / resting_hr, higher = better recovered)
- Mobility: `walking_speed_kmh`, `walking_step_length_cm`, `walking_asymmetry_pct`, `walking_double_support_pct`, `stair_speed_up_ms`, `stair_speed_down_ms`, `running_stride_length_m`, `running_power_w`, `running_speed_kmh` (all sparse)
- Sleep: `sleep_total_h`, `sleep_in_bed_h`, `sleep_efficiency_pct`, `sleep_deep_h`, `sleep_core_h` (= light), `sleep_rem_h`, `sleep_awake_h` — each day's sleep = the night before. NULL means not tracked.

**workout** — one row per session, PK: `start_utc` (ISO 8601), FK: `date`

- `type` (original name), `category` (normalised: run / lift / walk / cycle / other)
- `duration_min`, `hr_min` / `hr_avg` / `hr_max` (bpm), `active_energy_kj`
- `intensity_kcal_per_hr_kg`
- `temperature_c`, `humidity_pct`
- `gpx_distance_km`, `gpx_elevation_gain_m`, `gpx_avg_speed_ms`, `gpx_max_speed_p95_ms`

Pace tip: `duration_min / gpx_distance_km` = min/km. Only meaningful when `gpx_distance_km IS NOT NULL`.
