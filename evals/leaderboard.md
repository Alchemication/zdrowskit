# Feedback Eval Leaderboard

Feedback-derived regression scorecard for zdrowskit evals. Sections compare only runs over the same recorded case set; this is not a general benchmark.

## 7 cases · feature=all · case set `6f31e6561d6a`

Latest recorded: `2026-04-13T15:55:32Z`

Case IDs: `chat_explicit_add_to_log`, `chat_log_life_disruption`, `chat_log_social_rest_day`, `chat_plan_lookup_no_log`, `chat_running_speed_trend_chart_text_independent`, `chat_running_speed_trend_pace_format`, `chat_strategy_change_updates_weekly_plan`

| Model | Reasoning | Accuracy | Passed | Failed | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| claude-opus-4-6 | none | 100.0% | 7 | 0 | 13.75s | 21.27s | $0.5697 | $0.0814 | c1295a6* | - |
| claude-sonnet-4-6 | none | 85.7% | 6 | 1 | 10.75s | 19.78s | $0.3808 | $0.0544 | c1295a6* | chat_log_social_rest_day |
| claude-haiku-4-5 | none | 57.1% | 4 | 3 | 5.32s | 10.55s | $0.1100 | $0.0157 | c1295a6* | chat_log_life_disruption, chat_log_social_rest_day, chat_strategy_change_updates_weekly_plan |
