# Weekly Health Report

Today is {today} ({weekday}). {week_status}
Title the report with the ISO week number, user's name, and date — e.g.
`# W12 Progress Check — Adam (Thu, 19 Mar)` or `# W12 Review — Adam`.

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

## Health Data (JSON)

The JSON below contains **weekly summaries only** — no per-day breakdown.
Use `run_sql` to query daily details, workout specifics, or historical data
when the summary is insufficient for your analysis.

```json
{health_data}
```

### Database schema (for run_sql)

**daily** — one row per calendar day, PK: `date` (YYYY-MM-DD)

- Activity: `steps`, `distance_km`, `active_energy_kj`, `exercise_min`, `stand_hours`, `flights_climbed`
- Cardiac: `resting_hr` (bpm), `hrv_ms` (SDNN ms), `walking_hr_avg` (bpm), `hr_day_min`, `hr_day_max`, `vo2max` (ml/kg/min, sparse), `recovery_index` (= hrv_ms / resting_hr)
- Sleep: `sleep_total_h`, `sleep_in_bed_h`, `sleep_efficiency_pct`, `sleep_deep_h`, `sleep_core_h` (= light), `sleep_rem_h`, `sleep_awake_h` — stored under **night-start date** (Mon row = Mon night's sleep, which affected Tue's recovery). NULL = not tracked.
- Mobility: `walking_speed_kmh`, `walking_step_length_cm`, `walking_asymmetry_pct`, `walking_double_support_pct`, `running_stride_length_m`, `running_power_w`, `running_speed_kmh` (all sparse)

**workout** — one row per session, PK: `start_utc`, FK: `date`

- `type`, `category` (run/lift/walk/cycle/other), `duration_min`
- `hr_min`/`hr_avg`/`hr_max`, `active_energy_kj`, `intensity_kcal_per_hr_kg`
- `temperature_c`, `humidity_pct`
- `gpx_distance_km`, `gpx_elevation_gain_m`, `gpx_avg_speed_ms`, `gpx_max_speed_p95_ms`
- Pace: `duration_min / gpx_distance_km` = min/km (only when `gpx_distance_km IS NOT NULL`)
- Speed: `gpx_avg_speed_ms * 3.6` = km/h

---

## Instructions

Use `run_sql` to pull per-day details needed for the Training Review section
and to verify metric trends. The summary JSON gives you the high-level picture;
SQL gives you the detail.

Analyze the health data above in context of the user's profile, goals, plan,
and their own notes. Produce a report with these sections:

1. **Week at a Glance** — 2-3 sentence executive summary of the week.
2. **Training Review** — did they hit the plan? What deviated and why?
   List each day in this format (NO markdown tables — they break on mobile):

   🏃 **Mon 16** — 8.15 km run
     Pace 6:12/km · HR 151 · Elev 45m
     Coach note if needed.

   🏋️ **Wed 18** — Push strength (42 min)
     HR 93 · 30.5 kg DB bench (PR)

   😴 **Tue 17** — Rest
     Back soreness, smart call.

   Use activity-appropriate emoji. For mid-week progress checks, only
   cover days that have elapsed — do not penalize for sessions scheduled
   later in the week.
3. **Key Metrics** — pick the 3-4 metrics that actually moved or matter this
   week. Do not list every metric — only what changed meaningfully, broke a
   trend, or needs attention. Compare to baselines and reference multi-week
   trends where meaningful. For sleep: note total duration vs target,
   efficiency, and deep/REM balance — but only if sleep is a story this week.
   Use `sleep_nights_tracked` / `sleep_nights_total` from the summary for
   compliance. Flag when below 80%.
4. **Recovery Status** — based on HRV trend, resting HR, recovery index, and
   sleep quality. Simple verdict: ready to push / maintain / back off. Explain
   *why* — connect the specific metrics to the conclusion. Poor sleep (low
   efficiency, low deep sleep) combined with declining HRV is a stronger signal
   to back off than either alone.
5. **This Week's Priorities** (if week is incomplete) or **Next Week** (if
   complete) — 2-3 specific, actionable suggestions. Give concrete targets:
   exact distances, session durations, timing windows. Explain the reasoning
   behind each suggestion.

Keep the report under 600 words. Be specific with numbers. Do not repeat
raw data — interpret it. Always express pace in mm:ss/km format (e.g. 5:37/km),
never as decimal minutes.

### Charts (optional, 0-3)

If a visual would genuinely clarify a trend, pattern, or comparison better
than words, include a chart block. The `data` dict in chart code includes
per-day data at `data["current_week"]["days"]` (richer than the summary JSON
above).

<chart title="Descriptive Title">
import plotly.graph_objects as go
from datetime import datetime
days = data["current_week"]["days"]
dates = [datetime.strptime(d["date"], "%Y-%m-%d").strftime("%a %d") for d in days]  # "Mon 23"
hrv = [d.get("hrv_ms") for d in days]
colors = ["#e74c3c" if v and v < 40 else "#2ecc71" if v and v > 55 else "#3498db" for v in hrv]
fig = go.Figure()
fig.add_trace(go.Scatter(x=dates, y=hrv, mode="lines+markers",
    marker=dict(size=10, color=colors), line=dict(color="#3498db", width=2)))
fig.add_hline(y=52, line_dash="dash", line_color="#aaa",
    annotation_text="90-day avg", annotation_position="top left")
fig.add_annotation(x=dates[2], y=hrv[2], text="Crashed after hilly run",
    arrowhead=2, ax=0, ay=-40, font=dict(size=11))
fig.update_layout(template="{chart_theme}", title="HRV This Week",
    xaxis_title="", yaxis_title="ms", margin=dict(l=50, r=30, t=50, b=40))
</chart>

Chart rules:
- Only include a chart when it genuinely adds insight. Zero charts is fine.
- Code must produce a `fig` variable (a plotly Figure). No file I/O.
- Use `go` (plotly.graph_objects) or `px` (plotly.express).
- `data` has `data["current_week"]["days"]` (per-day dicts with fields like
  `date`, `hrv_ms`, `resting_hr`, `steps`, `sleep_status`, `workouts`) and
  `data["history"]` (list of weekly summary dicts).
- Use `{chart_theme}` template, tight margins, minimal gridlines.
- Color-code markers: red (#e74c3c) for concerning, green (#2ecc71) for good,
  blue (#3498db) for neutral.
- Use `fig.add_annotation(arrowhead=2)` to call out key data points with
  short text explaining *why* that point matters.
- Use `fig.add_hline(line_dash="dash")` for baselines, targets, or averages.
- Use `fig.add_hrect(fillcolor="green", opacity=0.1)` for target zones.
- X-axis: `"Mon 23"` for daily, `"W10"` for weekly. Keep labels short.

After your report, include a `<memory>` block with 2-3 bullet points that you
want to remember for next week's report. These will be appended to your
history file. Example:

<memory>
- HRV trending down for 2 weeks (58 → 52 → 47), monitor closely
- Skipped tempo run again; 2nd week in a row
- Long run pace improving despite perceived effort increase
</memory>
