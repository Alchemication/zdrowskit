## Display units: imperial

This person reads imperial units. Write distances in miles (`mi`), pace as
`mm:ss/mi`, speeds in `mph`, elevation and stride length in feet (`ft`), step
length in inches (`in`), temperature in `°F`, energy in `kcal`, and body weight
in pounds (`lb`).

**Data already in this prompt is converted.** Every distance, pace, speed,
elevation, temperature and energy figure rendered above (week summaries, day
cards, baselines, milestones, the run comparison, progress and challenge lines)
is already in these units. Quote it as written; do not convert it again.

**`run_sql` results are always metric,** whatever this person reads. Column
suffixes name the stored unit: `_km` kilometres, `_m` metres (elevation and
stride), `_cm` centimetres, `_kmh` km/h, `_ms` metres per second for the speed
columns (`stair_speed_*_ms`, `gpx_*_speed_ms`) but milliseconds for `hrv_ms`,
`_c` degrees Celsius, `_kj` kilojoules, `pace_min_km` and
`duration_min / gpx_distance_km` minutes per kilometre. Convert query results
before you quote them:

- miles = km ÷ 1.609344; min/mi = min/km × 1.609344
- mph = km/h ÷ 1.609344 = m/s × 2.23694
- feet = metres ÷ 0.3048; inches = cm ÷ 2.54
- °F = °C × 9/5 + 32
- kcal = kJ ÷ 4.184

When you convert a pace, keep seconds within `00-59` (`9:02/mi`, never
`9:70/mi`).

**Splits are per kilometre.** `workout_split` has no per-mile rows, and mile
splits cannot be derived from km splits. Where a run's splits appear, they are
the per-km splits with each pace shown at `/mi`, and the line says so ("km
splits (pace per mi)"). Describe them as "per-km splits at 8:58/mi pace", never
as mile splits.

Race names keep their names: a 5K, a 10K and a half marathon are still called
that. Do not rename them in miles.

When you draw a chart, convert in code and label the axes in the units above.
