# Feedback Eval Leaderboard

Feedback-derived regression scorecard for zdrowskit evals. The production table is what the daemon ships today; the per-feature tables are alternatives measured against it. Not a general benchmark.

## Production

Latest run on production routes per feature, against the 30 cases in `evals/cases` today.

| Feature | Route | Cases | Strict | Attempt | Flaky | Repeat | Avg Latency | Cost/run | Revision | Recorded |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| chat | gpt-5.6-luna (high) | 11/11 | 81.8% | 87.9% | 1 | 3 | 6.40s | $0.0108 | 760d8b6 (stale) | 2026-08-09 |
| insights | claude-opus-5 (high) | 3/3 | 100.0% | 100.0% | 0 | 3 | 22.90s | $0.5787 | 760d8b6 (stale) | 2026-08-09 |
| memory | gpt-5.6-luna | 3/3 | 100.0% | 100.0% | 0 | 3 | 1.00s | $0.0007 | 760d8b6 (stale) | 2026-08-09 |
| nudge | gpt-5.6-luna (high) | 6/6 | 100.0% | 100.0% | 0 | 3 | 4.51s | $0.0054 | 760d8b6 (stale) | 2026-08-09 |
| verification_judge | deepseek-v4-pro (high) | 7/7 | 25.0% | 37.5% | 0 | 3 | 73.48s | $0.0277 | 760d8b6 (stale) | 2026-08-09 |

**Measured code that has since changed:** `chat`, `insights`, `memory`, `nudge`, `verification_judge`. Re-record to score the code as it stands.

**Answered by a fallback model:** `chat`. The route's primary failed on at least one case, so those scores belong to the fallback, not the model named above.

## chat · 11 cases

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Avg Latency | Cost/run | Revision | Failing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **production routes** | production | 3 | 11/11 | 81.8% | 87.9% | 1 | 6.40s | $0.0108 | 760d8b6 (stale) | `chat_strategy_change_updates_weekly_plan` 2/3<br>`chat_tempo_short_warmup_negative` 0/3 |
| **production routes** | production | 3 | 11/11 | 81.8% | 87.9% | 1 | 7.82s | $0.0132 | adc9c5e* (stale) | `chat_running_speed_trend_chart_text_independent` 2/3<br>`chat_tempo_short_warmup_negative` 0/3 |

Leading row (`production routes`, repeat=3) per-case stability:

- `chat_strategy_change_updates_weekly_plan` 2/3 FLAKY — weekly_plan_mentions_four_runs
- `chat_tempo_short_warmup_negative` 0/3 fail — treats_shortened_session_as_not_meeting_the_prescription

## insights · 3 cases

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Avg Latency | Cost/run | Revision | Failing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **production routes** | production | 3 | 3/3 | 100.0% | 100.0% | 0 | 22.90s | $0.5787 | 760d8b6 (stale) | - |
| claude-opus-5 | high | 5 | 3/3 | 33.3% | 86.7% | 2 | 24.54s | $0.5852 | 760d8b6* (stale) | `insights_does_not_contradict_the_stated_hrv_trend` 4/5<br>`insights_fits_a_phone_notification_w31` 4/5 |
| gpt-5.6-luna | high | 5 | 3/3 | 33.3% | 73.3% | 2 | 19.58s | $0.0117 | 760d8b6* (stale) | `insights_does_not_contradict_the_stated_hrv_trend` 2/5<br>`insights_fits_a_phone_notification_w31` 4/5 |
| deepseek-v4-flash | high | 5 | 3/3 | 33.3% | 66.7% | 2 | 56.22s | $0.0076 | 760d8b6* (stale) | `insights_does_not_label_a_post_week_run_with_the_wrong_day` 3/5<br>`insights_fits_a_phone_notification_w31` 2/5 |
| **production routes** | production | 3 | 1/3 | 0.0% | 66.7% | 1 | 28.95s | $0.3072 | adc9c5e* (stale) | `insights_fits_a_phone_notification_w31` 2/3 |
| deepseek-v4-pro | high | 5 | 3/3 | 0.0% | 53.3% | 2 | 56.88s | $0.0204 | 760d8b6* (stale) | `insights_does_not_contradict_the_stated_hrv_trend` 4/5<br>`insights_does_not_label_a_post_week_run_with_the_wrong_day` 4/5<br>`insights_fits_a_phone_notification_w31` 0/5 |

Leading row (`production routes`) passed every case on every attempt.

## memory · 3 cases

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Avg Latency | Cost/run | Revision | Failing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **production routes** | production | 3 | 3/3 | 100.0% | 100.0% | 0 | 1.00s | $0.0007 | 760d8b6 (stale) | - |
| **production routes** | production | 3 | 3/3 | 100.0% | 100.0% | 0 | 1.27s | $0.0007 | adc9c5e* (stale) | - |

Leading row (`production routes`) passed every case on every attempt.

## nudge · 6 cases

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Avg Latency | Cost/run | Revision | Failing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **production routes** | production | 3 | 6/6 | 100.0% | 100.0% | 0 | 4.51s | $0.0054 | 760d8b6 (stale) | - |
| **production routes** | production | 3 | 6/6 | 100.0% | 100.0% | 0 | 5.19s | $0.0095 | adc9c5e* (stale) | - |

Leading row (`production routes`) passed every case on every attempt.

## verification_judge · 7 cases

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Avg Latency | Cost/run | Revision | Failing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| deepseek-v4-flash | high | 5 | 7/7 | 85.7% | 97.1% | 1 | 62.77s | $0.0239 | c015036* | `verification_judge_insights_unsupported_vo2max_recency_w15` 4/5 |
| gpt-5.6-luna | high | 5 | 7/7 | 57.1% | 74.3% | 3 | 36.02s | $0.0479 | c015036* | `verification_judge_insights_hrv_precedes_workout_w32` 4/5<br>`verification_judge_insights_unsupported_vo2max_recency_w15` 1/5<br>`verification_judge_nudge_passes_accurate_week_totals` 1/5 |
| deepseek-v4-pro | high | 5 | 7/7 | 57.1% | 71.4% | 2 | 49.64s | $0.0220 | c015036 | `verification_judge_insights_invented_drought_length_w32` 3/5<br>`verification_judge_insights_unsupported_vo2max_recency_w15` 0/5<br>`verification_judge_nudge_compound_week_totals_w21` 2/5 |
| **production routes** | production | 3 | 7/7 | 42.9% | 71.4% | 3 | 50.55s | $0.0229 | adc9c5e* (stale) | `verification_judge_insights_hrv_precedes_workout_w32` 2/3<br>`verification_judge_insights_invented_drought_length_w32` 2/3<br>`verification_judge_insights_unsupported_vo2max_recency_w15` 0/3<br>`verification_judge_nudge_compound_week_totals_w21` 2/3 |
| **production routes** | production | 3 | 7/7 | 25.0% | 37.5% | 0 | 73.48s | $0.0277 | 760d8b6 (stale) | `verification_judge_insights_hrv_precedes_workout_w32` errored<br>`verification_judge_insights_invented_drought_length_w32` errored<br>`verification_judge_insights_unsupported_vo2max_recency_w15` 0/3<br>`verification_judge_nudge_compound_week_totals_w21` 0/1<br>`verification_judge_nudge_passes_accurate_week_totals` errored<br>`verification_judge_nudge_passes_bereavement_soft_prescription` 0/1 |

Leading row (`deepseek-v4-flash`, repeat=5) per-case stability:

- `verification_judge_insights_unsupported_vo2max_recency_w15` 4/5 FLAKY — verifier_did_not_pass_unsupported_claim, verifier_quoted_or_flagged_w15_recency

---

Strict = cases passing every attempt. Attempt = attempt-weighted, the score one run would be expected to report. They diverge exactly when cases are flaky, and a flaky case is the one result a single run reports with false confidence.
