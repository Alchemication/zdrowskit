# Eval Leaderboard

Regression scorecard for zdrowskit evals. A case is one frozen input plus checks on the answer; most are recorded from real failures. The first table is what the daemon ships today; the per-feature tables are alternatives measured on the same cases. Not a general benchmark.

## What ships today

The most recent scored run for each feature, on the model it actually runs on, against the 55 cases in `evals/cases` today.

| Feature | Route | Cases | Strict | Attempt | Flaky | Repeat | Tool calls | Avg Latency | Cost/run | Commit | Recorded |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| chat | gpt-5.6-luna (high) | 12/12 | 83.3% | 88.9% | 2 | 3 | 0.9 avg (8 cases, 1-3), 3 varied | 9.31s | $0.0181 | a722d28 | 2026-09-25 |
| checkin | gpt-5.6-luna | 1/1 | 100.0% | 100.0% | 0 | 3 | - | 1.09s | $0.0002 | 51fb250 | 2026-09-25 |
| coach | gpt-5.6-luna (high) | 6/6 | 83.3% | 94.4% | 1 | 3 | 0.8 avg (5 cases, up to 1), 1 varied | 15.15s | $0.0175 | a722d28 | 2026-09-25 |
| insights | gpt-5.6-luna (high) | 7/7 | 71.4% | 85.7% | 2 | 3 | 3.2 avg (7 cases, 1-6), 5 varied | 32.26s | $0.0378 | a722d28 | 2026-09-25 |
| memory | gpt-5.6-luna | 3/3 | 100.0% | 100.0% | 0 | 3 | - | 1.39s | $0.0007 | a722d28 | 2026-09-25 |
| nudge | gpt-5.6-luna (high) | 11/11 | 100.0% | 100.0% | 0 | 3 | none used | 4.91s | $0.0070 | 51fb250* | 2026-09-25 |
| plan_frame | gpt-5.6-luna | 4/4 | 100.0% | 100.0% | 0 | 3 | - | 1.23s | $0.0007 | a722d28 | 2026-09-25 |
| standout | gpt-5.6-luna | 2/2 | 100.0% | 100.0% | 0 | 3 | - | 1.38s | $0.0003 | a722d28 | 2026-09-25 |
| targets | gpt-5.6-luna | 2/2 | 100.0% | 100.0% | 0 | 3 | - | 1.59s | $0.0005 | a722d28 | 2026-09-25 |
| verification_judge | deepseek-v4-flash, deepseek-v4-flash (high) | 7/7 | 100.0% | 100.0% | 0 | 3 | - | 30.98s | $0.0363 | a722d28 | 2026-09-25 |

## chat · 12 cases

Answering your questions in Telegram, including the SQL it writes.

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Tool calls | Avg Latency | Cost/run | Commit | Not passing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| gpt-5.6-luna | high | 3 | 11/12 | 90.9% | 90.9% | 0 | 0.9 avg (7 cases, 1-4), 2 varied | 9.83s | $0.0166 | 01eca8f* | `chat_tempo_short_warmup_negative` 0/3 |
| **gpt-5.6-luna** (ships today) | high | 3 | 12/12 | 83.3% | 88.9% | 2 | 0.9 avg (8 cases, 1-3), 3 varied | 9.31s | $0.0181 | a722d28 | `chat_running_speed_trend_chart_text_independent` 1/3<br>`chat_tempo_short_warmup_negative` 1/3 |
| gpt-5.6-luna | high | 5 | 11/12 | 81.8% | 89.1% | 1 | 0.9 avg (7 cases, 1-3), 3 varied | 6.77s | $0.0146 | cdaa5d2* | `chat_running_speed_trend_chart_text_independent` 4/5<br>`chat_tempo_short_warmup_negative` 0/5 |
| **gpt-5.6-luna** (ships today) | high | 3 | 11/12 | 81.8% | 87.9% | 1 | - | 6.40s | $0.0108 | 760d8b6 | `chat_strategy_change_updates_weekly_plan` 2/3<br>`chat_tempo_short_warmup_negative` 0/3 |
| **gpt-5.6-luna** (ships today) | high | 3 | 11/12 | 81.8% | 87.9% | 1 | - | 7.82s | $0.0132 | adc9c5e* | `chat_running_speed_trend_chart_text_independent` 2/3<br>`chat_tempo_short_warmup_negative` 0/3 |
| **gpt-5.6-luna** (ships today) | high | 3 | 11/12 | 63.6% | 81.8% | 3 | 0.8 avg (7 cases, 1-3), 3 varied | 7.12s | $0.0168 | 2bfda76* | `chat_log_entry_token_format` 2/3<br>`chat_running_speed_trend_chart_text_independent` 2/3<br>`chat_strategy_change_updates_weekly_plan` 2/3<br>`chat_tempo_short_warmup_negative` 0/3 |
| gpt-6-luna | high | 5 | 11/12 | 63.6% | 80.0% | 3 | 0.9 avg (7 cases, 1-3), 3 varied | 9.29s | $0.0068 | cdaa5d2* | `chat_log_entry_token_format` 3/5<br>`chat_running_speed_trend_chart_text_independent` 3/5<br>`chat_tempo_progressive_positive` 3/5<br>`chat_tempo_short_warmup_negative` 0/5 |
| glm-5.3-flash | high | 3 | 11/12 | 63.6% | 66.7% | 1 | 1.2 avg (7 cases, up to 4), 2 varied | 33.14s | $0.0206 | 01eca8f* | `chat_running_speed_trend_chart_text_independent` 0/3<br>`chat_strategy_change_updates_weekly_plan` 0/3<br>`chat_tempo_progressive_positive` 1/3<br>`chat_tempo_short_warmup_negative` 0/3 |

Leading row (`gpt-5.6-luna`, repeat=3) per-case stability:

- `chat_tempo_short_warmup_negative` 0/3 fail — treats_shortened_session_as_not_meeting_the_prescription

## checkin · 1 cases

How the coach asks what happened, on a week when you trained far less than usual.

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Tool calls | Avg Latency | Cost/run | Commit | Not passing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **gpt-5.6-luna** (ships today) | none | 5 | 1/1 | 100.0% | 100.0% | 0 | - | 1.20s | $0.0002 | a2fba5f* | - |
| **gpt-5.6-luna** (ships today) | none | 3 | 1/1 | 100.0% | 100.0% | 0 | - | 1.09s | $0.0002 | 51fb250 | - |
| **gpt-5.6-luna** (ships today) | none | 3 | 1/1 | 0.0% | 66.7% | 1 | - | 1.68s | $0.0002 | a722d28 | `checkin_asks_without_delivering_a_verdict` 2/3 |

Leading row (`gpt-5.6-luna`) passed every case on every attempt.

## coach · 6 cases

The weekly review that proposes changes to your plan, and must speak up when a goal keeps being missed or is due for review.

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Tool calls | Avg Latency | Cost/run | Commit | Not passing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **gpt-5.6-luna** (ships today) | high | 3 | 6/6 | 83.3% | 94.4% | 1 | 0.8 avg (5 cases, up to 1), 1 varied | 15.15s | $0.0175 | a722d28 | `coach_proposes_when_a_target_is_missed_most_weeks` 2/3 |

Leading row (`gpt-5.6-luna`, repeat=3) per-case stability:

- `coach_proposes_when_a_target_is_missed_most_weeks` 2/3 FLAKY — proposes_a_strategy_edit

## insights · 7 cases

The weekly report.

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Tool calls | Avg Latency | Cost/run | Commit | Not passing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **claude-opus-5** (ships today) | high | 3 | 3/7 | 100.0% | 100.0% | 0 | - | 22.90s | $0.5787 | 760d8b6 | - |
| **gpt-5.6-luna** (ships today) | high | 3 | 7/7 | 71.4% | 85.7% | 2 | 3.2 avg (7 cases, 1-6), 5 varied | 32.26s | $0.0378 | a722d28 | `insights_does_not_reissue_last_weeks_priority` 1/3<br>`insights_does_not_restate_the_progress_strip` 2/3 |
| gpt-5.6-luna | high | 3 | 3/7 | 66.7% | 88.9% | 1 | 1.7 avg (3 cases, 1-3) | 24.80s | $0.0141 | 01eca8f* | `insights_does_not_contradict_the_stated_hrv_trend` 2/3 |
| glm-5.3-flash | high | 3 | 3/7 | 66.7% | 88.9% | 1 | 3.9 avg (3 cases, up to 7), 3 varied | 178.04s | $0.0190 | 01eca8f* | `insights_does_not_label_a_post_week_run_with_the_wrong_day` 2/3 |
| gpt-6-luna | high | 5 | 3/7 | 66.7% | 73.3% | 1 | 1.2 avg (3 cases, 1-2), 1 varied | 38.56s | $0.0057 | cdaa5d2* | `insights_does_not_contradict_the_stated_hrv_trend` 1/5 |
| **gpt-5.6-luna** (ships today) | high | 3 | 3/7 | 66.7% | 66.7% | 0 | 2.2 avg (3 cases, 1-6), 1 varied | 23.81s | $0.0139 | 2bfda76* | `insights_does_not_contradict_the_stated_hrv_trend` 0/3 |
| claude-opus-5 | high | 5 | 3/7 | 33.3% | 86.7% | 2 | - | 24.54s | $0.5852 | 760d8b6* | `insights_does_not_contradict_the_stated_hrv_trend` 4/5<br>`insights_fits_a_phone_notification_w31` 4/5 |
| gpt-5.6-luna | high | 5 | 3/7 | 33.3% | 73.3% | 2 | - | 19.58s | $0.0117 | 760d8b6* | `insights_does_not_contradict_the_stated_hrv_trend` 2/5<br>`insights_fits_a_phone_notification_w31` 4/5 |
| gpt-5.6-luna | high | 5 | 3/7 | 33.3% | 73.3% | 2 | 2.2 avg (3 cases, 1-8), 1 varied | 28.22s | $0.0139 | cdaa5d2* | `insights_does_not_contradict_the_stated_hrv_trend` 3/5<br>`insights_fits_a_phone_notification_w31` 3/5 |
| deepseek-v4-flash | high | 5 | 3/7 | 33.3% | 66.7% | 2 | - | 56.22s | $0.0076 | 760d8b6* | `insights_does_not_label_a_post_week_run_with_the_wrong_day` 3/5<br>`insights_fits_a_phone_notification_w31` 2/5 |
| **claude-opus-5** (ships today) | high | 3 | 1/7 | 0.0% | 66.7% | 1 | - | 28.95s | $0.3072 | adc9c5e* | `insights_fits_a_phone_notification_w31` 2/3 |
| deepseek-v4-pro | high | 5 | 3/7 | 0.0% | 53.3% | 2 | - | 56.88s | $0.0204 | 760d8b6* | `insights_does_not_contradict_the_stated_hrv_trend` 4/5<br>`insights_does_not_label_a_post_week_run_with_the_wrong_day` 4/5<br>`insights_fits_a_phone_notification_w31` 0/5 |

Leading row (`claude-opus-5`) passed every case on every attempt.

## memory · 3 cases

What carries over from one week to the next.

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Tool calls | Avg Latency | Cost/run | Commit | Not passing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **gpt-5.6-luna** (ships today) | none | 3 | 3/3 | 100.0% | 100.0% | 0 | - | 1.00s | $0.0007 | 760d8b6 | - |
| **gpt-5.6-luna** (ships today) | none | 3 | 3/3 | 100.0% | 100.0% | 0 | - | 1.39s | $0.0007 | a722d28 | - |
| **gpt-5.6-luna** (ships today) | none | 3 | 3/3 | 100.0% | 100.0% | 0 | - | 1.15s | $0.0007 | 2bfda76* | - |
| **gpt-5.6-luna** (ships today) | none | 3 | 3/3 | 100.0% | 100.0% | 0 | - | 1.27s | $0.0007 | adc9c5e* | - |

Leading row (`gpt-5.6-luna`) passed every case on every attempt.

## nudge · 11 cases

Short, timely messages during the day.

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Tool calls | Avg Latency | Cost/run | Commit | Not passing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **gpt-5.6-luna** (ships today) | high | 3 | 6/11 | 100.0% | 100.0% | 0 | - | 4.51s | $0.0054 | 760d8b6 | - |
| **gpt-5.6-luna** (ships today) | high | 3 | 11/11 | 100.0% | 100.0% | 0 | none used | 4.91s | $0.0070 | 51fb250* | - |
| **gpt-5.6-luna** (ships today) | high | 3 | 6/11 | 100.0% | 100.0% | 0 | 0.1 avg (1 case, up to 1), 1 varied | 5.88s | $0.0084 | 2bfda76* | - |
| **gpt-5.6-luna** (ships today) | high | 3 | 6/11 | 100.0% | 100.0% | 0 | - | 5.19s | $0.0095 | adc9c5e* | - |
| **gpt-5.6-luna** (ships today) | high | 3 | 9/11 | 88.9% | 92.6% | 1 | none used | 6.95s | $0.0127 | a722d28 | `nudge_respects_constraints_the_user_logged` 1/3 |
| gpt-6-luna | high | 5 | 6/11 | 83.3% | 90.0% | 1 | 0.6 avg (5 cases, up to 1), 3 varied | 22.49s | $0.0066 | cdaa5d2* | `nudge_respects_constraints_the_user_logged` 2/5 |
| gpt-5.6-luna | high | 10 | 6/11 | 66.7% | 90.0% | 2 | 0.0 avg (1 case, up to 1), 1 varied | 6.97s | $0.0057 | cdaa5d2 | `nudge_respects_constraints_the_user_logged` 6/10<br>`nudge_writes_when_a_session_lands` 8/10 |
| gpt-5.6-luna | high | 5 | 6/11 | 66.7% | 90.0% | 2 | none used | 6.22s | $0.0069 | cdaa5d2* | `nudge_respects_constraints_the_user_logged` 3/5<br>`nudge_writes_when_a_session_lands` 4/5 |
| gpt-5.6-luna | high | 3 | 6/11 | 66.7% | 83.3% | 2 | none used | 5.49s | $0.0083 | 01eca8f* | `nudge_respects_constraints_the_user_logged` 1/3<br>`nudge_writes_when_a_session_lands` 2/3 |
| glm-5.3-flash | high | 3 | 6/11 | 50.0% | 72.2% | 3 | 0.5 avg (4 cases, up to 2), 3 varied | 81.05s | $0.0207 | 01eca8f* | `nudge_says_a_missing_reading_is_missing` 2/3<br>`nudge_week_totals_match_logged_workouts_w21` 1/3<br>`nudge_writes_when_a_session_lands` 1/3 |

Leading row (`gpt-5.6-luna`) passed every case on every attempt.

## plan_frame · 4 cases

Deciding whether this is a week to be measured at all, or one where a progress bar would be the wrong thing to show.

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Tool calls | Avg Latency | Cost/run | Commit | Not passing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **gpt-5.6-luna** (ships today) | none | 5 | 2/4 | 100.0% | 100.0% | 0 | - | 1.27s | $0.0004 | a2fba5f | - |
| **gpt-5.6-luna** (ships today) | none | 3 | 4/4 | 100.0% | 100.0% | 0 | - | 1.23s | $0.0007 | a722d28 | - |

Leading row (`gpt-5.6-luna`) passed every case on every attempt.

## standout · 2 cases

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Tool calls | Avg Latency | Cost/run | Commit | Not passing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **gpt-5.6-luna** (ships today) | none | 3 | 2/2 | 100.0% | 100.0% | 0 | - | 1.38s | $0.0003 | a722d28 | - |

Leading row (`gpt-5.6-luna`) passed every case on every attempt.

## targets · 2 cases

Turning the goals you wrote in prose into the numbers a progress bar is drawn against.

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Tool calls | Avg Latency | Cost/run | Commit | Not passing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **gpt-5.6-luna** (ships today) | none | 5 | 2/2 | 100.0% | 100.0% | 0 | - | 1.37s | $0.0003 | a2fba5f* | - |
| **gpt-5.6-luna** (ships today) | none | 3 | 2/2 | 100.0% | 100.0% | 0 | - | 1.59s | $0.0005 | a722d28 | - |

Leading row (`gpt-5.6-luna`) passed every case on every attempt.

## verification_judge · 7 cases

The second model that fact-checks a draft before it is sent.

| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky | Tool calls | Avg Latency | Cost/run | Commit | Not passing |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| **deepseek-v4-flash** (ships today) | mixed | 3 | 7/7 | 100.0% | 100.0% | 0 | - | 30.98s | $0.0363 | a722d28 | - |
| deepseek-v4-flash | high | 5 | 7/7 | 85.7% | 97.1% | 1 | - | 62.77s | $0.0239 | c015036* | `verification_judge_insights_unsupported_vo2max_recency_w15` 4/5 |
| **deepseek-v4-flash** (ships today) | high | 3 | 7/7 | 85.7% | 90.5% | 1 | - | 65.96s | $0.0227 | 2bfda76* | `verification_judge_insights_unsupported_vo2max_recency_w15` 1/3 |
| gpt-5.6-luna | high | 5 | 7/7 | 57.1% | 74.3% | 3 | - | 36.02s | $0.0479 | c015036* | `verification_judge_insights_hrv_precedes_workout_w32` 4/5<br>`verification_judge_insights_unsupported_vo2max_recency_w15` 1/5<br>`verification_judge_nudge_passes_accurate_week_totals` 1/5 |
| deepseek-v4-pro | high | 5 | 7/7 | 57.1% | 71.4% | 2 | - | 49.64s | $0.0220 | c015036 | `verification_judge_insights_invented_drought_length_w32` 3/5<br>`verification_judge_insights_unsupported_vo2max_recency_w15` 0/5<br>`verification_judge_nudge_compound_week_totals_w21` 2/5 |
| **deepseek-v4-pro** (ships today) | high | 3 | 7/7 | 42.9% | 71.4% | 3 | - | 50.55s | $0.0229 | adc9c5e* | `verification_judge_insights_hrv_precedes_workout_w32` 2/3<br>`verification_judge_insights_invented_drought_length_w32` 2/3<br>`verification_judge_insights_unsupported_vo2max_recency_w15` 0/3<br>`verification_judge_nudge_compound_week_totals_w21` 2/3 |
| **deepseek-v4-pro** (ships today) | high | 3 | 7/7 | 25.0% | 37.5% | 0 | - | 73.48s | $0.0277 | 760d8b6 | `verification_judge_insights_hrv_precedes_workout_w32` errored<br>`verification_judge_insights_invented_drought_length_w32` errored<br>`verification_judge_insights_unsupported_vo2max_recency_w15` 0/3<br>`verification_judge_nudge_compound_week_totals_w21` 0/1<br>`verification_judge_nudge_passes_accurate_week_totals` errored<br>`verification_judge_nudge_passes_bereavement_soft_prescription` 0/1 |

Leading row (`deepseek-v4-flash`) passed every case on every attempt.

---

Strict = the share of cases that passed every attempt. Attempt = the share of individual attempts that passed, i.e. the score one run would be expected to report. They diverge exactly when cases are flaky, and a flaky case is the one result a single run reports with false confidence.
