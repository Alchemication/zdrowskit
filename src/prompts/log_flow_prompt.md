Today is {today} ({weekday}).

You are building a **fast tap-through log check-in** for the user. The goal
is to capture only the signal the app cannot already see in its database —
subjective state, life events, plan decisions — in as few taps as possible.

## Output rules

Return JSON only. Your entire response is **exactly one JSON object**. The
first character you emit is `{{` and the last is `}}`. No backticks, no
` ```json ` fences, no prose before or after, no comments inside the JSON.

The user will tap through the flow you design. A deterministic writer turns
the taps into one `- YYYY-MM-DD [tag1] [tag2] …` bullet appended to log.md.
You are not writing the bullet — only the interview.

## Hard constraints

- **Max 3 steps.** One step is ideal on an unremarkable day.
- **Max 8 options per step.** Keep option strings terse (1–3 words, lowercase).
- **Linear only.** No conditional branching between steps in v1.
- **Never ask about a metric already available in the DB snapshot below.**
  If sleep data is present, don't ask "how did you sleep". If a workout is
  already logged today, don't ask "did you train".
- **Extract concepts, don't copy phrases.** The user's log.md mixes older
  multi-bullet prose entries with new single-line `- YYYY-MM-DD [tag]`
  bullets — both are valid history. Mine the prose for *recurring
  concepts* (e.g. `son sick`, `solo parenting`, `travel BCN`, `stomach bug`,
  `post-illness`, `appliances home`) and render them as compact tokens.
  Do NOT lift prose phrases verbatim (e.g. `full week on track`,
  `tempo this week`) as options — they read as noise when replayed.
- **Prefer compound tokens over bare adjectives.** Fuse state with its
  driver: `[tired jetlag]`, `[solid post-rest]`, `[heavy post-tempo]`,
  `[rest son sick]`. Bare `[solid]` / `[tired]` / `[off]` carry almost no
  signal when a future LLM reads the log — use them only when nothing
  meaningful qualifies the state.
- **Acknowledge ongoing multi-day events.** If a recent log.md bullet carries
  an `until YYYY-MM-DD` annotation that is still in the future relative to
  today, assume the event is still active — do NOT re-ask about it.
- **`ask_end_date_if_selected`** (optional per step): a list of option
  strings you judge as multi-day candidates (e.g. `travel`, `son sick`,
  `holiday`, `illness`). If the user selects any of them, the handler will
  append a small date keyboard as the next step and annotate the bullet
  with ` until YYYY-MM-DD`. Include this field only when it applies.
- **Optional steps.** Use `"optional": true` when the step has an obvious
  "nothing unusual" fallthrough (e.g. "Anything going on?"). The handler
  lets the user skip optional steps without picking anything.
- **Implicit `+ note`.** The handler always appends a `+ note` button to
  every step for free-text input. Do not list `note` as an option.
- **Always offer a life-events step** when there is no active `until`
  annotation, even on unremarkable days — the human-only signal
  (family, travel, stress, sleep quality, social/logistical disruption)
  is exactly what the DB cannot see. Mark it `"optional": true` so the
  user can skip with one tap when truly nothing happened.
- Don't pad with filler steps: one `state` step plus an optional `life`
  step is the ceiling for an unremarkable day.

## JSON schema

```
{{
  "steps": [
    {{
      "id": "<short slug, lowercase, e.g. 'state' or 'life'>",
      "question": "<short question shown above the keyboard>",
      "options": ["<opt1>", "<opt2>", ...],
      "multi_select": <true|false>,
      "optional": <true|false>,                     // optional, default false
      "ask_end_date_if_selected": ["<opt>", ...]    // optional, omit if N/A
    }}
  ]
}}
```

## Context

### me.md
{me}

### strategy.md
{strategy}

### log.md (recent bullets — look for vocabulary patterns and active `until` annotations)
{log}

### Today's DB snapshot
{today_snapshot}

## Examples

**Example 1 — unremarkable day, state + optional life step:**

Suppose today's snapshot shows normal HRV, a logged easy run, decent sleep,
no active multi-day events. Prefer compound state tokens over bare ones,
and still offer the optional life-events step:

{{"steps":[{{"id":"state","question":"How did today feel?","options":["solid post-rest","easy","tired legs","off"],"multi_select":false,"optional":false}},{{"id":"life","question":"Anything going on?","options":["son sick","solo parenting","travel","out late","work heavy"],"multi_select":true,"optional":true,"ask_end_date_if_selected":["son sick","travel"]}}]}}

**Example 2 — rest day, concepts mined from prose entries:**

Suppose the user has no workout today, HRV is a bit low, and recent log.md
prose mentions "stomach bug", "sore throat", "appliances at home", "solo
parenting", and a Malaga trip. Render those as compact tokens; do not
quote the prose verbatim:

{{"steps":[{{"id":"state","question":"How did today feel?","options":["rest recovery","tired post-illness","wrecked","felt good"],"multi_select":false,"optional":false}},{{"id":"life","question":"Anything going on?","options":["stomach bug","sore throat","solo parenting","travel","appliances home","work heavy"],"multi_select":true,"optional":true,"ask_end_date_if_selected":["stomach bug","travel","sore throat"]}}]}}

**Example 3 — ongoing `until` event, skip re-asking:**

Suppose a recent bullet is `- 2026-04-18 [travel] until 2026-04-21` and today
is 2026-04-20. Do not ask about travel again. A single state-check step is
enough — and still prefer a compound state token over a bare one:

{{"steps":[{{"id":"state","question":"How did today feel?","options":["solid","tired jetlag","heavy legs","rest day"],"multi_select":false,"optional":false}}]}}
