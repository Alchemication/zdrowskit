---
name: feedback-triage
description: Use when the user mentions a thumbs-down, asks "what went wrong with this nudge/chat/coach/insight", references a specific feedback id, or asks to look at recent feedback. Covers the triage flow (how to walk an LLM call trace), how to localize a bug across the source/verify/rewrite chain, how to cross-check user-reported contradictions against data resyncs, and when to escalate to an eval (hand off to the `llm-evals` skill).
---

# Feedback Triage (zdrowskit)

Upstream sibling of `llm-evals`: this skill covers analysis of an existing failure. Once a reproducible bug is localized, hand off to `llm-evals` for capturing it as a regression case.

## Standard sweep

1. List recent feedback: `uv run python main.py llm-log --feedback` (or `--feedback --json` for full reasons).
2. Pick the item — note `feedback_id`, `llm_call_id`, `category`, `message_type`.
3. Inspect the cited call: `uv run python main.py llm-log --id <llm_call_id>`. The output includes the system+user prompt, the final response, and a metadata block.
4. If the metadata contains a `*_verification` block (e.g. `nudge_verification`), the call went through the verify/rewrite pipeline — walk the chain (next section).

## Localize the bug across the verify/rewrite chain

For nudge/coach/insights surfaces, an LLM trace is **three calls**: source draft, verifier, optional rewriter. The text the user actually saw is the rewriter's output (or the source draft if verdict was `pass`). Bugs can live in any of the three. The metadata block on the source call exposes `verifier_call_id` and `rewrite_call_id` — inspect each:

- **Source call**: did the draft itself have the issue the user complained about?
- **Verifier call**: did the verifier flag a real problem (correct), miss a real problem (under-active), or invent one (the call-601 pattern — verifier introduced an arithmetic-reversal "correction" on a numerically correct draft)?
- **Rewriter call**: did the rewriter faithfully apply the verifier's correction (so the bug is upstream in the verifier), or did it mangle a correct correction?

Reading the **Final Response** panel for each call is what tells you which stage owns the bug.

## Cross-check the user's claim against data resync

The user's complaint may not be the actual verifier/model bug — it may be data drift the model has no awareness of. The canonical case: HRV (and other Apple Health metrics) resync repeatedly through the day. A morning nudge may quote HRV 35 ms; by evening the same date reads 44.9 ms after later sync. The user perceives this as the model contradicting itself, but the model is faithfully reading the current snapshot.

Before blaming the LLM, check whether the user's "you said X, now you say Y" complaint is actually about a value that drifted between the historical nudge (in `recent_nudges_text`) and the current `health_data_text`. If so, the fix is either prompt-side (acknowledge resync drift) or product-side (don't emit nudges off freshly imported, still-noisy data).

## Cross-check rendered prompt data against canonical DB data

The value in the LLM prompt may be wrong even when the canonical table has the user's expected fact. Compare:

- the rendered `## Health Data` / `Today` block in the source call
- current DB rows via `store.open_db(store.default_db_path())` or `store.connect_db(..., migrate=True)`
- canonical all-source views such as `sleep_all` / `workout_all`, especially when the user manually logged data

Manual sleep is stored in `manual_sleep` and exposed through `sleep_all` using the **night-start date**. If both imported and manual sleep rows exist for the same date, product context should prefer manual sleep. If the prompt still shows the imported value, localize the bug to data assembly (for example `store.load_snapshots()` / `llm_health.build_llm_data()`), not to the writer/verifier model.

## Decide what to do next

- **Verifier introduced the bug** → almost always a model-quality issue. A/B by flipping the verifier route's `reasoning_effort` via `main.py models` or Telegram `/models` (on DeepSeek, `high` engages thinking; `medium` leaves it off). Capture as a `nudge_verify` real_regression if reproducible.
- **Source draft already had it** → prompt or context issue. Capture as a `chat` or other surface real_regression if there's a chat trace; otherwise iterate on the prompt.
- **Rewriter mangled a correct correction** → rewriter prompt issue.
- **User-perception bug from data resync** → not an LLM eval target. Open a product/prompt issue instead.
- **Prompt context was wrong** → fix the data assembly path and add a deterministic regression at the loader/rendering boundary; this is usually not an LLM eval target.

## Clean up bad delivered nudges

If a delivered nudge is factually wrong and would contaminate future prompts, remove it from daemon state after the root cause is fixed:

1. Inspect `~/Documents/zdrowskit/.daemon_state.json`.
2. Remove only the bad entry from `recent_nudges`.
3. Recompute `last_nudge_ts` from the first remaining recent nudge, or set it to `null` if none remain.
4. Recompute `nudge_count_today` from remaining `recent_nudges` whose `ts` starts with `nudge_date`.
5. Verify the bad phrase/timestamp no longer appears in the state JSON.

Archived markdown files under `~/Documents/zdrowskit/Nudges/` are historical records. Future LLM context comes from daemon state, so do not delete archives unless the user explicitly asks.

## Reproducibility threshold

A "real" regression should reproduce ≥ ~20% under the same config. Below that, capture only if the failure is high-impact or you have a structural reason to believe a fix would lock it down. LLM output is non-deterministic even at temperature 0; running 5x is the cheap way to gauge.

## Hand off to `llm-evals`

When you know which surface and stage to capture, switch to the `llm-evals` skill for fixture authoring, case_kind taxonomy, provenance fields, and deterministic-assertion rules. Supported eval features today: `chat` (full tool loop, `--model`-driven) and `nudge_verify` (verifier-only, env-driven).

## Pitfalls to remember

- **Multi-model pipelines**: the nudge pipeline uses three different model picks (draft / verify / rewrite) resolved from `src/config.py` and `src/model_prefs.py`. `--model` on the eval runner only flows to the chat path. For verifier evals, change the verifier model via `ZDROWSKIT_VERIFICATION_MODEL` and the reasoning posture via `main.py models` / Telegram `/models`.
- **Empty-verifier-response false-pass**: if the verifier hits its output token cap, the failure path emits a "verifier returned empty" critical issue. `text_absent` assertions trivially pass against this — always include an explicit assertion rejecting that failure mode in `nudge_verify` cases.
- **Verifier writes to the source call's metadata**: if you query the source call's metadata, the verification verdict is already there — no need to look it up separately.
