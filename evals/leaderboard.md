# Feedback Eval Leaderboard

Feedback-derived regression scorecard for zdrowskit evals. Sections compare only runs over the same recorded case set; this is not a general benchmark.

## 7 cases · feature=all · case set `6f31e6561d6a`

Latest recorded: `2026-04-23T20:59:00Z`

Case IDs: `chat_explicit_add_to_log`, `chat_log_life_disruption`, `chat_log_social_rest_day`, `chat_plan_lookup_no_log`, `chat_running_speed_trend_chart_text_independent`, `chat_running_speed_trend_pace_format`, `chat_strategy_change_updates_weekly_plan`

| Model | Reasoning | Accuracy | Passed | Failed | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| claude-sonnet-4-6 | none | 100.0% | 7 | 0 | 9.38s | 16.07s | $0.3620 | $0.0517 | 56f2cda* | - |
| claude-opus-4-6 | none | 100.0% | 7 | 0 | 12.01s | 18.16s | $0.5953 | $0.0850 | 56f2cda* | - |
| claude-opus-4-7 | none | 100.0% | 7 | 0 | 9.02s | 12.77s | $0.7595 | $0.1085 | 56f2cda* | - |
| claude-haiku-4-5 | none | 57.1% | 4 | 3 | 5.26s | 10.88s | $0.1175 | $0.0168 | 56f2cda* | chat_log_life_disruption, chat_log_social_rest_day, chat_strategy_change_updates_weekly_plan |
