---
name: llm-evals
description: Use when adding, modifying, or running LLM evaluation cases in evals/ (zdrowskit project). Covers the feedback-derived regression philosophy, case-kind taxonomy (real_regression / synthetic_positive / synthetic_negative), provenance fields, fixture preferences, deterministic-assertion rules, and the boundary between mocked pytest and opt-in real-LLM evals.
---

# LLM Evals (zdrowskit)

LLM evals in `evals/` are feedback-derived regressions, not a generated benchmark suite. Do not add broad LLM-created scenarios, stale blueprint/cache machinery, or cases without provenance.

## When adding eval coverage

- Start from a real thumbs-down feedback item. Use `uv run python main.py llm-log --feedback` to find it and `uv run python main.py llm-log --id N` to inspect the trace.
- Add the real failure first as `case_kind: "real_regression"`.
- Add only the minimum synthetic controls needed to isolate the hypothesis or guard a false positive, using `case_kind: "synthetic_positive"` or `case_kind: "synthetic_negative"`.
- Preserve provenance on every case with `source_feedback_id`, `source_llm_call_id`, and `derived_from.hypothesis`.
- Prefer structured fixtures: pinned date, context snippets, conversation turns, and only the health data needed for the behavior under test.
- Prefer deterministic assertions. Add LLM-as-judge only when a real feedback case cannot be checked with tool-call, argument, text, word-count, or forbidden-opening assertions.

## Mocked vs. real LLM boundary

`uv run pytest` must stay mocked and must never call a real LLM. Manual evals are opt-in through `uv run python -m evals.run`, which uses the configured model and may spend API quota.

For prompt/tool behavior changes, run the relevant mocked tests plus the specific eval cases that represent the affected feedback cluster.
