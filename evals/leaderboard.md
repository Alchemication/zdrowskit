# Feedback Eval Leaderboard

Feedback-derived regression scorecard for zdrowskit evals. Sections compare only runs over the same recorded case set; this is not a general benchmark.

## 10 cases · feature=all · case set `3590f468ddb9`

Latest recorded: `2026-05-01T18:49:16Z`

Case IDs: `chat_explicit_add_to_log`, `chat_log_life_disruption`, `chat_log_social_rest_day`, `chat_plan_lookup_no_log`, `chat_running_speed_trend_chart_text_independent`, `chat_running_speed_trend_pace_format`, `chat_strategy_change_updates_weekly_plan`, `chat_tempo_end_counts`, `chat_tempo_short_warmup_negative`, `nudge_verify_hrv_direction_reversal`

| Model | Reasoning | Accuracy | Passed | Failed | Avg Latency | p95 Latency | Total Cost | Avg Cost | Revision | Failed Cases |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| deepseek-v4-pro | none | 90.0% | 9 | 1 | 20.93s | 58.17s | $0.0166 | $0.0017 | 82b6160 | chat_tempo_short_warmup_negative |
| claude-opus-4-6 | none | 90.0% | 9 | 1 | 9.99s | 18.23s | $0.6938 | $0.0694 | d27dc12* | chat_tempo_short_warmup_negative |
| claude-sonnet-4-6 | none | 80.0% | 8 | 2 | 8.25s | 17.96s | $0.4129 | $0.0413 | d27dc12* | chat_tempo_end_counts, chat_tempo_short_warmup_negative |
| deepseek-v4-flash | none | 60.0% | 6 | 4 | 20.53s | 110.26s | $0.0063 | $0.0006 | d27dc12* | chat_log_social_rest_day, chat_running_speed_trend_chart_text_independent, chat_tempo_end_counts, chat_tempo_short_warmup_negative |
| claude-haiku-4-5 | none | 50.0% | 5 | 5 | 4.96s | 14.53s | $0.1405 | $0.0140 | d27dc12* | chat_log_life_disruption, chat_log_social_rest_day, chat_strategy_change_updates_weekly_plan, chat_tempo_end_counts, chat_tempo_short_warmup_negative |
