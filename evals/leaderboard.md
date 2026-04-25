# Feedback Eval Leaderboard

Feedback-derived regression scorecard for zdrowskit evals. Sections compare only runs over the same recorded case set; this is not a general benchmark.

## 7 cases · feature=all · case set `6f31e6561d6a`

Latest recorded: `2026-04-25T20:25:46Z`

Case IDs: `chat_explicit_add_to_log`, `chat_log_life_disruption`, `chat_log_social_rest_day`, `chat_plan_lookup_no_log`, `chat_running_speed_trend_chart_text_independent`, `chat_running_speed_trend_pace_format`, `chat_strategy_change_updates_weekly_plan`

| Model | Reasoning | Accuracy | Passed | Failed | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| deepseek-v4-pro | none | 100.0% | 7 | 0 | 28.12s | 68.26s | $0.0199 | $0.0028 | d9fcdb4* | - |
| claude-opus-4-7 | none | 100.0% | 7 | 0 | 8.88s | 14.21s | $0.7681 | $0.1097 | 7a6424f* | - |
| deepseek-v4-flash | none | 85.7% | 6 | 1 | 12.76s | 27.65s | $0.0077 | $0.0011 | d9fcdb4* | chat_log_life_disruption |
| claude-sonnet-4-6 | none | 85.7% | 6 | 1 | 9.02s | 16.33s | $0.3254 | $0.0465 | 7a6424f* | chat_log_life_disruption |
| claude-haiku-4-5 | none | 42.9% | 3 | 4 | 4.69s | 6.61s | $0.1120 | $0.0160 | 7a6424f* | chat_log_life_disruption, chat_log_social_rest_day, chat_running_speed_trend_chart_text_independent, chat_strategy_change_updates_weekly_plan |
