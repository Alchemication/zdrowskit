# Feedback Eval Leaderboard

Feedback-derived regression scorecard for zdrowskit evals. Sections compare only runs over the same recorded case set; this is not a general benchmark.

## 7 cases · feature=all · case set `6f31e6561d6a`

Latest recorded: `2026-04-25T20:02:45Z`

Case IDs: `chat_explicit_add_to_log`, `chat_log_life_disruption`, `chat_log_social_rest_day`, `chat_plan_lookup_no_log`, `chat_running_speed_trend_chart_text_independent`, `chat_running_speed_trend_pace_format`, `chat_strategy_change_updates_weekly_plan`

| Model | Reasoning | Accuracy | Passed | Failed | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| deepseek-v4-pro | none | 100.0% | 7 | 0 | 24.15s | 48.48s | - | - | 31991f6* | - |
| claude-sonnet-4-6 | none | 85.7% | 6 | 1 | 9.02s | 16.33s | $0.3254 | $0.0465 | 31991f6* | chat_log_life_disruption |
| claude-opus-4-6 | none | 85.7% | 6 | 1 | 11.75s | 18.03s | $0.5991 | $0.0856 | 31991f6* | chat_running_speed_trend_chart_text_independent |
| deepseek-v4-flash | none | 85.7% | 6 | 1 | 11.88s | 23.38s | - | - | 31991f6* | chat_running_speed_trend_chart_text_independent |
| claude-haiku-4-5 | none | 42.9% | 3 | 4 | 4.69s | 6.61s | $0.1120 | $0.0160 | 31991f6* | chat_log_life_disruption, chat_log_social_rest_day, chat_running_speed_trend_chart_text_independent, chat_strategy_change_updates_weekly_plan |
| claude-opus-4-7 | none | 0.0% | 0 | 7 | - | - | - | - | 31991f6* | chat_explicit_add_to_log, chat_log_life_disruption, chat_log_social_rest_day, chat_plan_lookup_no_log, chat_running_speed_trend_chart_text_independent, chat_running_speed_trend_pace_format, chat_strategy_change_updates_weekly_plan |
