# Feedback Eval Leaderboard

Feedback-derived regression scorecard for zdrowskit evals. Sections compare only runs over the same recorded case set; this is not a general benchmark.

## 12 cases · feature=all · case set `41cbc11e02cb`

Latest recorded: `2026-05-11T20:23:00Z`

Case IDs: `chat_explicit_add_to_log`, `chat_log_life_disruption`, `chat_log_social_rest_day`, `chat_plan_lookup_no_log`, `chat_running_speed_trend_chart_text_independent`, `chat_running_speed_trend_pace_format`, `chat_strategy_change_updates_weekly_plan`, `chat_tempo_end_counts`, `chat_tempo_progressive_positive`, `chat_tempo_short_warmup_negative`, `verification_judge_insights_unsupported_vo2max_recency_w15`, `verification_judge_nudge_hrv_direction_reversal`

| Model | Reasoning | Accuracy | Passed | Failed | Routes | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| deepseek-v4-pro | none | 83.3% | 10 | 2 | chat: deepseek-v4-pro<br>verification_judge: deepseek-v4-pro | 41.41s | 131.78s | $0.0359 | $0.0030 | 7544528* | chat_running_speed_trend_chart_text_independent, verification_judge_insights_unsupported_vo2max_recency_w15 |

Latest-row feature breakdown:

| Feature | Cases | Accuracy | Passed | Failed | Routes |
| --- | ---: | ---: | ---: | ---: | --- |
| chat | 10 | 90.0% | 9 | 1 | deepseek-v4-pro |
| verification_judge | 2 | 50.0% | 1 | 1 | deepseek-v4-pro |
