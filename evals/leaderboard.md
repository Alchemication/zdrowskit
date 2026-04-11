# Feedback Eval Leaderboard

Feedback-derived regression scorecard for zdrowskit evals. Sections compare only runs over the same recorded case set; this is not a general benchmark.

## 6 cases · feature=all · case set `63288c9bf71d`

Latest recorded: `2026-04-11T16:02:00Z`

Case IDs: `chat_explicit_add_to_log`, `chat_log_life_disruption`, `chat_plan_lookup_no_log`, `chat_running_speed_trend_chart_text_independent`, `chat_running_speed_trend_pace_format`, `chat_strategy_change_updates_weekly_plan`

| Model | Reasoning | Accuracy | Passed | Failed | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| claude-sonnet-4-6 | none | 100.0% | 6 | 0 | 9.62s | 16.35s | $0.3225 | $0.0537 | afa64f5* | - |
| claude-opus-4-6 | none | 100.0% | 6 | 0 | 12.01s | 18.56s | $0.4717 | $0.0786 | afa64f5* | - |
| claude-haiku-4-5 | none | 66.7% | 4 | 2 | 5.26s | 9.18s | $0.0989 | $0.0165 | afa64f5* | chat_log_life_disruption, chat_strategy_change_updates_weekly_plan |
