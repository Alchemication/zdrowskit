# Weekly Health Report

Today is {today} ({weekday}). If the week is incomplete, frame this as a
progress check with what's ahead. If the week is complete, frame it as a
full weekly review.

You are generating a personalized weekly health and fitness report.

## About the User
{me}

## Their Goals
{goals}

## Current Training Plan
{plan}

## Their Baselines (auto-computed from DB)
{baselines}

## Their Notes This Week
{log}

## Your Previous Notes
{history}

## Health Data (JSON)
```json
{health_data}
```

---

## Instructions

Analyze the health data above in context of the user's profile, goals, plan,
and their own notes. Produce a report with these sections:

1. **Week at a Glance** — 2-3 sentence executive summary of the week.
2. **Training Review** — did they hit the plan? What deviated and why?
   Compare planned vs actual sessions day by day.
3. **Key Metrics** — highlight meaningful changes in HR, HRV, recovery index,
   VO2max, pace. Compare to their baselines in the profile. Flag anything
   that warrants attention.
4. **Recovery Status** — based on HRV trend, resting HR, recovery index.
   Simple verdict: ready to push / maintain / back off.
5. **Next Week** — 2-3 specific, actionable suggestions based on the data
   and their goals.

Keep the report under 500 words. Be specific with numbers. Do not repeat
raw data — interpret it.

After your report, include a `<memory>` block with 2-3 bullet points that you
want to remember for next week's report. These will be appended to your
history file. Example:

<memory>
- HRV trending down for 2 weeks (58 → 52 → 47), monitor closely
- Skipped tempo run again; 2nd week in a row
- Long run pace improving despite perceived effort increase
</memory>
