# Feedback Eval Leaderboard

Feedback-derived regression scorecard for zdrowskit evals. Sections compare only runs over the same recorded case set; this is not a general benchmark.

## 14 cases · feature=all · case set `193f95385106`

Latest recorded: `2026-08-04T21:31:35Z`

Case IDs: `chat_explicit_add_to_log`, `chat_log_life_disruption`, `chat_log_social_rest_day`, `chat_plan_lookup_no_log`, `chat_running_speed_trend_chart_text_independent`, `chat_running_speed_trend_pace_format`, `chat_strategy_change_updates_weekly_plan`, `chat_tempo_end_counts`, `chat_tempo_progressive_positive`, `chat_tempo_short_warmup_negative`, `insights_midweek_memory_current_week_w20`, `verification_judge_insights_midweek_memory_contract_w20`, `verification_judge_insights_unsupported_vo2max_recency_w15`, `verification_judge_nudge_hrv_direction_reversal`

| Model | Reasoning | Accuracy | Passed | Failed | Routes | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| deepseek-v4-pro | none | 78.6% | 11 | 3 | chat: deepseek-v4-pro<br>insights: deepseek-v4-pro<br>verification_judge: deepseek-v4-pro | 26.27s | 83.66s | $0.0582 | $0.0042 | fa89a1b | chat_tempo_short_warmup_negative, insights_midweek_memory_current_week_w20, verification_judge_insights_unsupported_vo2max_recency_w15 |

Latest-row feature breakdown:

| Feature | Cases | Accuracy | Passed | Failed | Routes |
| --- | ---: | ---: | ---: | ---: | --- |
| chat | 10 | 90.0% | 9 | 1 | deepseek-v4-pro |
| insights | 1 | 0.0% | 0 | 1 | deepseek-v4-pro |
| verification_judge | 3 | 66.7% | 2 | 1 | deepseek-v4-pro |

## 12 cases · feature=all · case set `41cbc11e02cb`

Latest recorded: `2026-05-11T21:29:00Z`

Case IDs: `chat_explicit_add_to_log`, `chat_log_life_disruption`, `chat_log_social_rest_day`, `chat_plan_lookup_no_log`, `chat_running_speed_trend_chart_text_independent`, `chat_running_speed_trend_pace_format`, `chat_strategy_change_updates_weekly_plan`, `chat_tempo_end_counts`, `chat_tempo_progressive_positive`, `chat_tempo_short_warmup_negative`, `verification_judge_insights_unsupported_vo2max_recency_w15`, `verification_judge_nudge_hrv_direction_reversal`

| Model | Reasoning | Accuracy | Passed | Failed | Routes | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| deepseek-v4-pro | none | 75.0% | 9 | 3 | chat: deepseek-v4-pro<br>verification_judge: deepseek-v4-pro | 44.71s | 157.75s | $0.0159 | $0.0013 | 4977a16* | chat_tempo_end_counts, chat_tempo_short_warmup_negative, verification_judge_insights_unsupported_vo2max_recency_w15 |

Latest-row feature breakdown:

| Feature | Cases | Accuracy | Passed | Failed | Routes |
| --- | ---: | ---: | ---: | ---: | --- |
| chat | 10 | 80.0% | 8 | 2 | deepseek-v4-pro |
| verification_judge | 2 | 50.0% | 1 | 1 | deepseek-v4-pro |

## 10 cases · feature=chat · case set `e746a0f4838c`

Latest recorded: `2026-05-11T21:19:50Z`

Case IDs: `chat_explicit_add_to_log`, `chat_log_life_disruption`, `chat_log_social_rest_day`, `chat_plan_lookup_no_log`, `chat_running_speed_trend_chart_text_independent`, `chat_running_speed_trend_pace_format`, `chat_strategy_change_updates_weekly_plan`, `chat_tempo_end_counts`, `chat_tempo_progressive_positive`, `chat_tempo_short_warmup_negative`

| Model | Reasoning | Accuracy | Passed | Failed | Routes | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| deepseek-v4-pro | none | 90.0% | 9 | 1 | chat: deepseek-v4-pro | 28.07s | 65.09s | $0.0161 | $0.0016 | 4977a16 | chat_running_speed_trend_chart_text_independent |

## 2 cases · feature=verification_judge · case set `5eb9eb33e561`

Latest recorded: `2026-05-11T21:26:01Z`

Case IDs: `verification_judge_insights_unsupported_vo2max_recency_w15`, `verification_judge_nudge_hrv_direction_reversal`

| Model | Reasoning | Accuracy | Passed | Failed | Routes | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| deepseek-v4-pro | high | 50.0% | 1 | 1 | verification_judge: deepseek-v4-pro (high) | 141.12s | 180.85s | $0.0074 | $0.0037 | 4977a16* | verification_judge_insights_unsupported_vo2max_recency_w15 |

## 1 cases · feature=all · case set `ab7d75487c5e`

Latest recorded: `2026-05-20T09:03:27Z`

Case IDs: `verification_judge_insights_midweek_memory_contract_w20`

| Model | Reasoning | Accuracy | Passed | Failed | Routes | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| deepseek-v4-pro | high | 100.0% | 1 | 0 | verification_judge: deepseek-v4-pro (high) | 135.99s | 135.99s | $0.0046 | $0.0046 | 51471a1* | - |

## 1 cases · feature=all · case set `08ce45cee00a`

Latest recorded: `2026-05-20T09:03:21Z`

Case IDs: `insights_midweek_memory_current_week_w20`

| Model | Reasoning | Accuracy | Passed | Failed | Routes | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| claude-opus-4-7 | high | 0.0% | 0 | 1 | insights: claude-opus-4-7 (high) | 51.57s | 51.57s | $0.2364 | $0.2364 | 51471a1* | insights_midweek_memory_current_week_w20 |
