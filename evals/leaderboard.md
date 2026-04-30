# Feedback Eval Leaderboard

Feedback-derived regression scorecard for zdrowskit evals. Sections compare only runs over the same recorded case set; this is not a general benchmark.

## 7 cases · feature=all · case set `6f31e6561d6a`

Latest recorded: `2026-04-30T21:30:29Z`

Case IDs: `chat_explicit_add_to_log`, `chat_log_life_disruption`, `chat_log_social_rest_day`, `chat_plan_lookup_no_log`, `chat_running_speed_trend_chart_text_independent`, `chat_running_speed_trend_pace_format`, `chat_strategy_change_updates_weekly_plan`

| Model | Reasoning | Accuracy | Passed | Failed | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| claude-opus-4-7 | none | 100.0% | 7 | 0 | 8.88s | 14.21s | $0.7681 | $0.1097 | 7a6424f* | - |
| deepseek-v4-pro | none | 85.7% | 6 | 1 | 14.56s | 29.96s | $0.0279 | $0.0040 | a0d6a9f* | chat_running_speed_trend_chart_text_independent |
| claude-sonnet-4-6 | none | 85.7% | 6 | 1 | 9.02s | 16.33s | $0.3254 | $0.0465 | 7a6424f* | chat_log_life_disruption |
| deepseek-v4-flash | none | 57.1% | 4 | 3 | 6.29s | 11.74s | $0.0071 | $0.0010 | a0d6a9f* | chat_log_life_disruption, chat_log_social_rest_day, chat_running_speed_trend_chart_text_independent |
| claude-haiku-4-5 | none | 57.1% | 4 | 3 | 4.86s | 10.07s | $0.1172 | $0.0167 | a0d6a9f | chat_log_life_disruption, chat_log_social_rest_day, chat_strategy_change_updates_weekly_plan |
