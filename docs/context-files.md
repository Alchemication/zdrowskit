# Context Files

The `insights`, `coach`, `nudge`, and `chat` commands use markdown files from `~/Documents/zdrowskit/ContextFiles/` to give the LLM real context about you, not just your numbers.

| File | Who edits | Purpose |
|------|-----------|---------|
| `me.md` | you or chat | Your profile: age, weight, injuries, pace zones |
| `strategy.md` | you, chat, or coach | Goals + weekly training schedule + diet + sleep targets, all in one file |
| `log.md` | you or chat | Freeform weekly journal: why things happened, such as travel, illness, or life |
| `baselines.md` | auto | Rolling + seasonal baselines computed from DB, updated on each `insights` run |
| `history.md` | auto | LLM's own memory, appended after each weekly report |
| `coach_feedback.md` | auto | Accept/reject history for coach and chat suggestions, including optional rejection reasons |

Example user context files are in `examples/context/`.

The journal (`log.md`) is what makes this different from a dashboard. Numbers say what happened. The journal says why. The LLM connects both.
