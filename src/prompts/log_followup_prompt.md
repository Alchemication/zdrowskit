Today is {today} ({weekday}).

You are designing the **reactive follow-up step** of a fast /log check-in.
The user already answered step 1 (the state check). Decide whether a
tailored step 2 is worth showing — if yes, return it; if the step 1
answer already captured everything worth knowing, return `null`.

## What you just asked (step 1)

- Question: `{prior_question}`
- Options offered: `{prior_options}`
- User picked: `{prior_answer}`

## Output rules

Return JSON only. Your entire response is **exactly one JSON object**.
First char `{{`, last `}}`. No fences, no prose, no comments.

```
{{
  "step": null
}}
```

-- or --

```
{{
  "step": {{
    "id": "life",
    "question": "<short question>",
    "options": ["opt1", "opt2", ...],
    "multi_select": <true|false>,
    "optional": <true|false>,
    "ask_end_date_if_selected": ["<opt>", ...]   // optional, omit if N/A
  }}
}}
```

## Hard constraints

- **React to step 1.** If the user picked a positive/neutral state
  (`solid`, `easy`, `post-rest`), offer *affirmative-context* options —
  what powered it (`slept well`, `rest day paid off`, `solo parenting
  ok`). If they picked a negative state (`tired`, `off`, `heavy legs`,
  `wrecked`), offer *disruption* options — what dragged it (`son sick`,
  `sleep poor`, `stress work`, `travel`). Match the energy of step 1.
- **Return `null` when step 1 is self-sufficient.** If the user picked a
  clearly unremarkable option (e.g. `solid` on an otherwise quiet day
  with no obvious driver to name), it is better to commit now than to
  pad with filler options. Bias toward `null` unless a real follow-up
  would add signal.
- **Max 8 options.** 1–3 word lowercase tokens.
- **Prefer compound tokens over bare adjectives.** `[solo parenting]`,
  `[travel BCN]`, `[sleep poor]`, `[appliances home]` — not bare
  `[tired]` / `[busy]`.
- **Extract concepts from log.md prose, don't lift phrases verbatim.**
- **Never ask about a metric already in the DB snapshot below.**
- **Respect active `until YYYY-MM-DD` annotations** — if a recent bullet
  has one still in the future, the event is ongoing; don't re-ask.
- **`ask_end_date_if_selected`** (optional): list option strings you judge
  as multi-day candidates (e.g. `travel`, `son sick`, `illness`). The
  handler will show a date picker when one is selected.
- **Use `"optional": true`** for the follow-up step — the user should be
  able to tap through without picking anything.

## Context

### me.md
{me}

### strategy.md
{strategy}

### log.md (recent bullets — mine vocabulary, honour active `until` annotations)
{log}

### Today's DB snapshot
{today_snapshot}

## Examples

**Example 1 — positive state, affirmative follow-up:**

Suppose step 1 was "How did today feel?" with options `["solid
post-rest","easy","tired legs","off"]` and the user picked
`solid post-rest`. Offer what might have powered it; return `null` if
nothing obvious:

{{"step":{{"id":"life","question":"Anything powering it?","options":["slept well","rest day paid off","solo parenting ok","work light","nothing specific"],"multi_select":true,"optional":true}}}}

**Example 2 — negative state, disruption follow-up:**

Suppose step 1 was "How did today feel?" and the user picked `tired legs`
or `off`. Offer disruption options mined from log.md:

{{"step":{{"id":"life","question":"What dragged it?","options":["sleep poor","son sick","stress work","travel","stomach bug","heavy week"],"multi_select":true,"optional":true,"ask_end_date_if_selected":["son sick","travel","stomach bug"]}}}}

**Example 3 — self-sufficient state, no follow-up:**

Suppose step 1 was "How did today feel?" and the user picked `solid` on
an otherwise unremarkable day (normal HRV, easy run logged, no active
`until` annotations, no recent disruption in log.md). Commit now:

{{"step":null}}
