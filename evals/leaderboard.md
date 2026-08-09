# Feedback Eval Leaderboard

Feedback-derived regression scorecard for zdrowskit evals. The production table is what the daemon ships today; the per-feature tables are alternatives measured against it. Not a general benchmark.

## Production

Latest run on production routes per feature, against the 28 cases in `evals/cases` today.

| Feature | Route | Cases | Strict | Attempt | Flaky | Repeat | Avg Latency | Cost/run | Revision | Recorded |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| chat | gpt-5.6-luna (high) | 11/11 | 81.8% | 87.9% | 1 | 3 | 7.82s | $0.0132 | adc9c5e* | 2026-08-08 |
| insights | claude-opus-5 (high) | 1/1 | 0.0% | 66.7% | 1 | 3 | 28.95s | $0.3072 | adc9c5e* | 2026-08-08 |
| memory | gpt-5.6-luna | 3/3 | 100.0% | 100.0% | 0 | 3 | 1.27s | $0.0007 | adc9c5e* | 2026-08-08 |
| nudge | gpt-5.6-luna (high) | 6/6 | 100.0% | 100.0% | 0 | 3 | 5.19s | $0.0095 | adc9c5e* | 2026-08-08 |
| verification_judge | deepseek-v4-pro (high) | 7/7 | 42.9% | 71.4% | 3 | 3 | 50.55s | $0.0229 | adc9c5e* | 2026-08-08 |

## chat · 11 cases

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Avg Latency | Cost/run | Revision | Failing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **production routes** | production | 3 | 11/11 | 81.8% | 87.9% | 1 | 7.82s | $0.0132 | adc9c5e* | `chat_running_speed_trend_chart_text_independent` 2/3<br>`chat_tempo_short_warmup_negative` 0/3 |

Leading row (`production routes`, repeat=3) per-case stability:

- `chat_running_speed_trend_chart_text_independent` 2/3 FLAKY — includes_chart_block
- `chat_tempo_short_warmup_negative` 0/3 fail — treats_shortened_session_as_not_meeting_the_prescription

## insights · 1 cases

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Avg Latency | Cost/run | Revision | Failing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **production routes** | production | 3 | 1/1 | 0.0% | 66.7% | 1 | 28.95s | $0.3072 | adc9c5e* | `insights_fits_a_phone_notification_w31` 2/3 |

Leading row (`production routes`, repeat=3) per-case stability:

- `insights_fits_a_phone_notification_w31` 2/3 FLAKY — does_not_count_the_days_since_the_week_closed

## memory · 3 cases

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Avg Latency | Cost/run | Revision | Failing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **production routes** | production | 3 | 3/3 | 100.0% | 100.0% | 0 | 1.27s | $0.0007 | adc9c5e* | - |

Leading row (`production routes`) passed every case on every attempt.

## nudge · 6 cases

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Avg Latency | Cost/run | Revision | Failing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **production routes** | production | 3 | 6/6 | 100.0% | 100.0% | 0 | 5.19s | $0.0095 | adc9c5e* | - |

Leading row (`production routes`) passed every case on every attempt.

## verification_judge · 7 cases

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Avg Latency | Cost/run | Revision | Failing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **production routes** | production | 3 | 7/7 | 42.9% | 71.4% | 3 | 50.55s | $0.0229 | adc9c5e* | `verification_judge_insights_hrv_precedes_workout_w32` 2/3<br>`verification_judge_insights_invented_drought_length_w32` 2/3<br>`verification_judge_insights_unsupported_vo2max_recency_w15` 0/3<br>`verification_judge_nudge_compound_week_totals_w21` 2/3 |

Leading row (`production routes`, repeat=3) per-case stability:

- `verification_judge_insights_hrv_precedes_workout_w32` 2/3 FLAKY — verifier_flagged_the_hrv_timing_claim
- `verification_judge_insights_invented_drought_length_w32` 2/3 FLAKY — identifies_unsupported_week_count
- `verification_judge_insights_unsupported_vo2max_recency_w15` 0/3 fail — verifier_did_not_pass_unsupported_claim, verifier_quoted_or_flagged_w15_recency
- `verification_judge_nudge_compound_week_totals_w21` 2/3 FLAKY — verifier_examined_the_week_totals

---

Strict = cases passing every attempt. Attempt = attempt-weighted, the score one run would be expected to report. They diverge exactly when cases are flaky, and a flaky case is the one result a single run reports with false confidence.
