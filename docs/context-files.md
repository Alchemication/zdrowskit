# Context Files

The `insights`, `coach`, `nudge`, and `chat` commands use markdown files from
`~/Documents/zdrowskit/profiles/<name>/ContextFiles/`. Every profile has an
independent copy; `--profile NAME` selects it in the CLI.

| File | Who edits | Purpose |
|------|-----------|---------|
| `soul.md` | you | The coach's persona: who it is and how it talks to you |
| `me.md` | you or chat | Your profile: age, body, injuries, what you already do |
| `strategy.md` | you, chat, or coach | Goals + weekly training schedule + diet + sleep targets, all in one file |
| `log.md` | you or chat | Freeform weekly journal: why things happened, such as travel, illness, or life |
| `baselines.md` | auto | Rolling + seasonal baselines computed from DB, updated on each `insights` run |
| `history.md` | auto | LLM's own memory, appended after each weekly report |
| `coach_feedback.md` | auto | Accept/reject history for coach and chat suggestions, including optional rejection reasons |

A worked example of filled-in context is in `examples/context/`.

## Persona vs Conduct

The system message is built from two layers, in this order:

1. **`ContextFiles/soul.md`** — per profile, yours to rewrite. Who the coach
   is and how it speaks. The right voice for someone chasing a 5K PR is the
   wrong voice for someone who has not started yet, so this cannot be one
   shared file.
2. **`src/prompts/conduct.md`** — in the repo, identical for everyone. What
   the coach may do: output format, tool-turn protocol, never inventing facts
   about the user, word limits.

Conduct comes second so a persona can never read as permission to ignore it.
Rewrite your soul however you like; you cannot loosen a conduct rule by doing
so, and a fix to one of those rules lands for every profile at once.

Tone belongs in `soul.md`, not in the task prompts. Hard content limits —
"never discuss calories", "don't mention my weight" — belong in `me.md`,
which is about you rather than about the coach.

A profile whose `soul.md` is still an unfilled template falls back to the
shipped default persona in `src/templates/context/soul.md`.

## Unfilled Context

`profile add` seeds these files from `src/templates/context/` — headings and
guidance comments, describing nobody. A new profile therefore starts with no
identity rather than someone else's, which matters because the prompts treat
context as fact about the user.

A file holding only headings and comments is loaded as
`(not filled in yet — the user has not told you this)` rather than passed
through as terse-looking content. Prompts act on that marker: they must not
invent an age, injury, sport, or goal to fill the gap, and the nudge's
scheduled-session rule does not fire without a real weekly plan, so a new
profile is never prescribed training it did not ask for. Anything written
outside a heading or comment counts as filled in.

The journal (`log.md`) is what makes this different from a dashboard. Numbers say what happened. The journal says why. The LLM connects both.

## Data Maturity

Health data reads the same whether it covers eight years or eight days, so
`insights`, `coach`, `nudge`, and `chat` are all given an explicit
`## Data Maturity` block stating what is actually knowable: how much history
exists, how many workouts are recorded, which metrics have enough readings to
define a personal normal, and whether there is any coaching history at all.

The block states facts only. How to behave on a thin profile is a coaching
decision and lives in `src/prompts/conduct.md`, which forbids presenting a
sparse metric as a norm, adherence-checking against a plan that does not
exist, referring back to conversations that never happened, and using training
jargon the user has not shown they speak.

Baselines apply the same rule to themselves. A rolling average needs
`BASELINE_MIN_SAMPLES` readings before it is reported, per-week training
volume needs `BASELINE_MIN_WINDOW_COVERAGE` of its window present before the
window is divided, and sections with nothing to say are omitted rather than
rendered as a grid of dashes. A mature profile is unaffected; a two-day-old
one no longer gets three days of data printed under a "90-day avg" heading.

`coach_feedback.md` is retained as a full audit log on disk. Prompt context is
filtered to recent strategy/coach-relevant entries so ordinary chat log appends
do not pollute future coaching reviews. Approved strategy edits are included as
positive signal; rejected edits are included only when they have a meaningful
reason.
