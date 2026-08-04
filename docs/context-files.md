# Context Files

The `insights`, `coach`, `nudge`, and `chat` commands use markdown files from
`~/Documents/zdrowskit/profiles/<name>/ContextFiles/`. Every profile has an
independent copy; `--profile NAME` selects it in the CLI.

| File | Who edits | Purpose |
|------|-----------|---------|
| `me.md` | you or chat | Your profile: age, body, injuries, what you already do |
| `strategy.md` | you, chat, or coach | Goals + weekly training schedule + diet + sleep targets, all in one file |
| `log.md` | you or chat | Freeform weekly journal: why things happened, such as travel, illness, or life |
| `baselines.md` | auto | Rolling + seasonal baselines computed from DB, updated on each `insights` run |
| `history.md` | auto | LLM's own memory, appended after each weekly report |
| `coach_feedback.md` | auto | Accept/reject history for coach and chat suggestions, including optional rejection reasons |

A worked example of filled-in context is in `examples/context/`.

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

`coach_feedback.md` is retained as a full audit log on disk. Prompt context is
filtered to recent strategy/coach-relevant entries so ordinary chat log appends
do not pollute future coaching reviews. Approved strategy edits are included as
positive signal; rejected edits are included only when they have a meaningful
reason.
