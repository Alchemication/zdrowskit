# Weekly Health Report

Today is {today} ({weekday}). If the week is incomplete, this is a progress
check. If the week is complete, this is a full review. Title the report with
the ISO week number, user's name, and date — e.g.
`# W12 Progress Check — Adam (Mon, 16 Mar)` or `# W12 Review — Adam`.

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
   Compare planned vs actual sessions day by day. Use workout timestamps
   and specifics from the data to explain what happened.
3. **Key Metrics** — highlight meaningful changes in HR, HRV, recovery index,
   VO2max, pace, and sleep. Compare to baselines and reference multi-week trends
   where meaningful, not just 30d averages. For sleep: note total duration vs
   target, efficiency, and deep/REM balance. Flag anything that warrants attention.
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
raw data — interpret it.

After your report, include a `<memory>` block with 2-3 bullet points that you
want to remember for next week's report. These will be appended to your
history file. Example:

<memory>
- HRV trending down for 2 weeks (58 → 52 → 47), monitor closely
- Skipped tempo run again; 2nd week in a row
- Long run pace improving despite perceived effort increase
</memory>
