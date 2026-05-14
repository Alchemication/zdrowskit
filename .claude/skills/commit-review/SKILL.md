---
name: commit-review
description: Use when the user asks to review the latest commit (or last N commits), assess a recent change, or check work done by another LLM that has already landed. Phrasings include "review the latest commit and suggest critical improvements", "any critical issues in the last commit", "another LLM did this, can you review". Produces a triaged list of findings against project rules in CLAUDE.md, runs the verification the user expects, then waits for the user to pick fixes.
---

# Commit Review (zdrowskit)

User lands one or more commits — often from another LLM — and wants a critical pass before pushing. Expectation: triaged findings, not a unilateral cleanup. User picks what to apply.

## Scope

- "Latest commit" → `HEAD`. "Last N" → `git log --oneline -n N`. Branch review → `git diff main...HEAD`.
- Read the diff (`git show <sha>` / `git diff <range>`). Messages describe intent; the diff is what shipped.
- Flag any change outside the stated scope as its own finding.

## Project rules to check (from CLAUDE.md)

- `uv run` everywhere; no bare `python`.
- Schema changes via new timestamped migration under `src/db/migrations/`. No ad-hoc `ALTER TABLE` or column-existence checks in app code.
- DB opens via `store.open_db()` / `store.connect_db(..., migrate=True)`.
- Native type hints; no `typing.List`. File budget ~1000 lines.
- No backward-compat shims / re-export stubs ([[feedback_backward_compat]]).
- `print()` user output, `logger` diagnostics, lazy `rich` for structured display.
- LLM prompts in `src/prompts/`; tool schemas beside tool code.
- `reasoning_effort` is the only reasoning knob in `src/llm.py`. Flag new env-var toggles.
- Required tests: parsers, aggregator, store round-trips, date utilities, pure LLM utility functions.

## Extra skepticism when the diff came from another LLM

External LLMs reliably introduce: over-engineering (new abstractions for one-shot ops), backward-compat shims, defensive try/except on internal calls, half-finished feature-flag stubs, narrative comments ("added for X"), stale docs ([[feedback_docs_check]]).

## Verification to run before reporting

Run, don't promise:

1. `uv run ruff check .` and `uv run ruff format --check .` on changed paths.
2. `uv run pytest` (or a targeted subset).
3. Grep stale refs and callers of any renamed/moved symbol.
4. Grep `README.md`, `CLAUDE.md`, `docs/` for values the commit changed (defaults, paths, flags, model IDs).

## Report format

Three tiers — **Critical / Quality / Nit**. Each finding: clickable file link (`[src/foo.py:42-51](src/foo.py#L42-L51)`), one or two sentences on the problem, concrete fix. No essays.

## Hand off

- If a finding shifts LLM-surface behavior, flag whether [[feedback-triage]] or [[llm-evals]] applies.
- Schema work → point at `src/db/migrations/`, don't propose inline SQL.

## After the user picks

- Apply only the selected items. No "while I'm here" cleanup unless invited.
- New commit per logical group; don't amend the commit under review.
- Re-run ruff + pytest. Docs updates ride in the same commit as the code change.

## Pitfalls

- Don't review the commit message instead of the diff.
- Passing ruff/tests isn't correctness — read the test bodies for any changed prompt/parser/store path.
- Don't propose reverts without explicit user go-ahead.
