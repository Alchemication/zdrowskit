---
name: feedback-triage
description: Use when the user mentions a thumbs-down, asks "what went wrong with this nudge/chat/coach/insight", references a specific feedback id, or asks to look at recent feedback. Covers walking an LLM trace, assigning the bug to source/verify/rewrite/tool/data assembly, checking data drift, and deciding when to hand off to `llm-evals`.
---

# Feedback Triage (zdrowskit)

Analyze an existing thumbs-down or bad LLM output. Once the failing stage is clear and reproducible enough, hand off to `llm-evals`.

## Fast Path

1. List feedback: `uv run python main.py llm-log --feedback` (or `--feedback --json`).
2. Inspect the cited call: `uv run python main.py llm-log --id <llm_call_id>`.
3. Use the trace table as the map. For compact view: `uv run python main.py llm-log --trace <trace_id>`.
4. Identify the delivered stage, then inspect only the relevant prompt, tool result, and final response.

## Read The Trace

All related provider calls share `trace_id`: tool-loop iterations, synthesis retries, verifier, and rewriter.

- `iteration: 0`, `1`, ...: tool loop / draft calls.
- `iteration: final_synthesis`, `truncation_retry`, `empty_retry`: recovery or answer synthesis.
- `stage: verify`: verifier call.
- `stage: rewrite`: bounded rewriter call.

`response_text` / Final Response is the delivered text. If metadata has `postprocessed_response_text: true`, inspect `metadata.raw_response_text` too.

For tool failures, compare the tool request and result inside the same trace. Repeated or near-repeated SQL/results usually means the model failed to synthesize from evidence it already had.

## Assign Ownership

For nudge/coach/insights, bugs can live in source draft, verifier, or rewriter:

- **Source draft wrong**: prompt/context/data assembly issue, or source model quality.
- **Verifier missed real issue**: verifier under-active.
- **Verifier invented issue**: verifier over-active / model-quality problem.
- **Rewriter mangled valid correction**: rewriter prompt/model issue.

Read each stage's Final Response before assigning blame. The delivered text is the rewrite output when a rewrite exists; otherwise it is the source draft.

## Check Data First

The user's complaint can be true while the LLM faithfully followed bad or stale prompt data.

- **Resync drift**: HRV and other Apple Health metrics can change later in the day. Compare historical text in `recent_nudges_text` with current `health_data_text` before calling it a contradiction bug.
- **Prompt assembly bug**: compare rendered prompt data with canonical DB rows via `store.open_db(store.default_db_path())` or `store.connect_db(..., migrate=True)`.
- **Manual data precedence**: manual sleep lives in `manual_sleep` and `sleep_all` by night-start date. If manual and imported sleep both exist, prompt context should prefer manual.

If prompt data was wrong, fix the data assembly path and add deterministic coverage there. Usually not an LLM eval.

## Decide

- **Verifier introduced the bug**: model-quality issue. A/B verifier route reasoning via `main.py models` or Telegram `/models` (DeepSeek `high` engages thinking; `medium` leaves it off). Capture as `nudge_verify` or `insights_verify` real_regression if reproducible (surface set by `fixture.kind`).
- **Source draft already had it**: prompt/context/model issue. Capture as `chat` or the relevant source surface if reproducible.
- **Rewriter mangled a correct correction**: rewriter prompt/model issue.
- **Data resync caused user confusion**: product/prompt issue, not an eval.
- **Prompt context was wrong**: data assembly fix plus deterministic loader/rendering regression.

## Bad Nudge Cleanup

If a delivered nudge is factually wrong and would contaminate future prompts, remove it from daemon state after the root cause is fixed:

1. Inspect `~/Documents/zdrowskit/.daemon_state.json`.
2. Remove only the bad entry from `recent_nudges`.
3. Recompute `last_nudge_ts` from the first remaining nudge, or set it to `null`.
4. Recompute `nudge_count_today` from remaining `recent_nudges` whose `ts` starts with `nudge_date`.
5. Verify the bad phrase/timestamp no longer appears in state JSON.

Archives under `~/Documents/zdrowskit/Nudges/` are historical records. Do not delete them unless the user explicitly asks.

## Eval Handoff

A real regression should reproduce at least roughly 20% under the same config. Below that, capture only if high-impact or structurally likely to recur. Run 5x as the cheap check.

When stage and surface are clear, switch to `llm-evals`. Supported eval features today: `chat` (full tool loop, `--model`-driven), `nudge_verify`, and `insights_verify` (both verifier-only, env-driven, sharing the same `run_verify.py` runner).

## Pitfalls

- **Multi-model pipelines**: nudge uses draft / verify / rewrite routes from `src/config.py` and `src/model_prefs.py`. `--model` on eval runner only flows to chat. For verifier evals, change verifier model via `ZDROWSKIT_VERIFICATION_MODEL` and reasoning via `main.py models` / Telegram `/models`.
- **Empty-verifier-response false-pass**: if verifier hits output cap, failure emits "verifier returned empty" critical issue. `text_absent` assertions can trivially pass; add an assertion rejecting that failure mode.
- **Verifier writes source metadata**: source-call metadata already contains verifier verdict and call IDs.
