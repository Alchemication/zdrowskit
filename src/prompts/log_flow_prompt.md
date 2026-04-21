Today is {today} ({weekday}).

You are building the **first step** of a fast tap-through log check-in
for the user. The goal is to capture only the signal the app cannot
already see in its database — subjective state — in as few taps as
possible. A **separate reactive call** will design the optional
follow-up step after seeing this step's answer, so you do **not** need
to plan a life-events or disruption step here.

## Output rules

Return JSON only. Your entire response is **exactly one JSON object**. The
first character you emit is `{{` and the last is `}}`. No backticks, no
` ```json ` fences, no prose before or after, no comments inside the JSON.

The user will tap through the flow you design. A deterministic writer turns
the taps into one `- YYYY-MM-DD [tag1] [tag2] …` bullet appended to log.md.
You are not writing the bullet — only the interview.

## Hard constraints

- **Exactly 1 step.** This is the initial state-check. A second reactive
  step may be added by a separate LLM call after the user answers.
- **Max 8 options per step.** Keep option strings terse (1–3 words, lowercase).
- **Never ask about a metric already available in the DB snapshot below.**
  If sleep data is present, don't ask "how did you sleep". If a workout is
  already logged today, don't ask "did you train".
- **Extract concepts, don't copy phrases.** The user's log.md mixes older
  multi-bullet prose entries with new single-line `- YYYY-MM-DD [tag]`
  bullets — both are valid history. Mine the prose for *recurring
  concepts* (e.g. `solid post-rest`, `tired jetlag`, `heavy post-tempo`)
  and render them as compact tokens. Do NOT lift prose phrases verbatim
  (e.g. `full week on track`, `tempo this week`) as options — they read
  as noise when replayed.
- **Prefer compound tokens over bare adjectives.** Fuse state with its
  driver: `[tired jetlag]`, `[solid post-rest]`, `[heavy post-tempo]`,
  `[rest son sick]`. Bare `[solid]` / `[tired]` / `[off]` carry almost no
  signal when a future LLM reads the log — use them only when nothing
  meaningful qualifies the state.
- **Acknowledge ongoing multi-day events.** If a recent log.md bullet carries
  an `until YYYY-MM-DD` annotation that is still in the future relative to
  today, assume the event is still active — do NOT re-ask about it.
- **`ask_end_date_if_selected`** (optional): a list of option strings you
  judge as multi-day candidates (e.g. `sick`, `rest illness`). If the user
  selects any of them, the handler will append a small date keyboard as
  the next step and annotate the bullet with ` until YYYY-MM-DD`. Include
  this field only when it applies.
- **Implicit `+ note`.** The handler always appends a `+ note` button for
  free-text input. Do not list `note` as an option.
- **`multi_select`** should usually be `false` for a state check — the user
  picks one word that best describes today. Only use `true` if two
  dimensions need to coexist (e.g. `solid` + `sore`).
- **`optional`** should be `false` — a bare bullet with no state tag
  carries no signal.

## JSON schema

```
{{
  "steps": [
    {{
      "id": "state",
      "question": "<short question shown above the keyboard>",
      "options": ["<opt1>", "<opt2>", ...],
      "multi_select": <true|false>,
      "optional": <true|false>,
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

**Example 1 — unremarkable day, compound state tokens:**

Suppose today's snapshot shows normal HRV, a logged easy run, decent sleep,
no active multi-day events. Prefer compound state tokens over bare ones:

{{"steps":[{{"id":"state","question":"How did today feel?","options":["solid post-rest","easy","tired legs","off"],"multi_select":false,"optional":false}}]}}

**Example 2 — rest day with recent illness history:**

Suppose the user has no workout today, HRV is a bit low, and recent log.md
prose mentions "stomach bug", "sore throat", "appliances at home". Offer
state options that reflect the shape of the day — recovery, lingering
illness, or feeling restored — and flag illness-like states as multi-day:

{{"steps":[{{"id":"state","question":"How did today feel?","options":["rest recovery","tired post-illness","still wrecked","felt good"],"multi_select":false,"optional":false,"ask_end_date_if_selected":["tired post-illness","still wrecked"]}}]}}

**Example 3 — ongoing `until` event, skip re-asking:**

Suppose a recent bullet is `- 2026-04-18 [travel] until 2026-04-21` and today
is 2026-04-20. Do not ask about travel again in the state options — offer
compound state tokens that naturally carry the travel driver:

{{"steps":[{{"id":"state","question":"How did today feel?","options":["solid","tired jetlag","heavy legs","rest day"],"multi_select":false,"optional":false}}]}}
