### Database schema (for run_sql)

**daily** — one row per calendar day, PK: `date` (YYYY-MM-DD)

- Activity: `steps`, `distance_km`, `active_energy_kj`, `exercise_min`, `stand_hours`, `flights_climbed`
- Cardiac: `resting_hr`, `hrv_ms`, `walking_hr_avg`, `hr_day_min`, `hr_day_max`, `vo2max`, `recovery_index`
- Mobility: `walking_speed_kmh`, `walking_step_length_cm`, `walking_asymmetry_pct`, `walking_double_support_pct`, `stair_speed_up_ms`, `stair_speed_down_ms`, `running_stride_length_m`, `running_power_w`, `running_speed_kmh`
- Note: `daily.running_speed_kmh` is a day-level Apple mobility metric. It is useful for broad daily movement context, but for run-session pace, distance, elevation, or running trends, prefer `workout_all`.

**workout_all** — one row per session, FK: `date`, with `source` (`'import'` or `'manual'`)

- Identity: `type`, `category` (`run` / `lift` / `walk` / `cycle` / `other`)
- Core fields: `duration_min`, `hr_min`, `hr_avg`, `hr_max`, `active_energy_kj`, `intensity_kcal_per_hr_kg`
- Environment: `temperature_c`, `humidity_pct`
- GPX fields: `gpx_distance_km`, `gpx_elevation_gain_m`, `gpx_avg_speed_ms`, `gpx_max_speed_p95_ms`
- Pace tip: `duration_min / gpx_distance_km` = min/km when `gpx_distance_km IS NOT NULL`
- Use `workout_all` as the canonical source for workout questions: runs, pace, splits/proxies, distance, elevation, workout HR, and session trends.

**workout_split** — one row per completed 1 km split for imported route-based runs

- Key: (`start_utc`, `km_index`) where `km_index` is 1-based within the workout
- Columns: `pace_min_km`, `avg_speed_ms`, `elevation_gain_m`, `elevation_loss_m`
- Join tip: join `workout_split.start_utc` to imported sessions in `workout` (or to `workout_all` on `start_utc`, noting that manual workouts will not have split rows)
- Use `workout_split` for within-run pacing: late-run fade, fastest contiguous 5 km / 10 km segments, and elevation-adjusted pacing checks.

**sleep_all** — one row per night, keyed by `date`, with `source` (`'import'` or `'manual'`)

- Columns: `sleep_total_h`, `sleep_in_bed_h`, `sleep_efficiency_pct`, `sleep_deep_h`, `sleep_core_h`, `sleep_rem_h`, `sleep_awake_h`
- Stored under **night-start date**
- Stage columns are NULL for manual entries
