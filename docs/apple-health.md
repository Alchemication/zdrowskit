# Apple Health Data Export

Apple's built-in health export dumps everything into a single massive XML file. On any non-trivial data size, this crashes or overheats the iPhone, so it is not a real solution for this project.

The workaround is a third-party iOS app that reads HealthKit directly and writes structured JSON to iCloud Drive. zdrowskit uses [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982). It works on iOS 26, while some alternatives do not yet. The Basic tier unlocks Shortcut actions; Premium is needed for scheduled Automations.

One universal constraint: iOS requires the phone to be unlocked for any health data export. Automations silently skip when the phone is locked.

## Auto Export Setup

The Automations feature syncs health data to iCloud Drive on a schedule, with no taps required once configured.

Setup in the app:

1. Create two automations: one for **Metrics**, one for **Workouts**.
2. Set both to: **Date Range = Week**, **Aggregation = Day**, **Destination = iCloud Drive**.
3. Select all metrics you care about, such as steps, energy, HR, HRV, VO2max, mobility, resting heart rate, and sleep analysis.
4. Set the schedule. Every 5 minutes is recommended because shorter intervals catch more unlock windows.

The app writes weekly JSON files:

```text
Metrics/HealthAutoExport-YYYY-WW.json
Workouts/HealthAutoExport-YYYY-WW.json
```

Data path:

```text
~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/
```

Notes:

- Sleep data is pre-aggregated nightly totals, with no per-segment breakdown.
- Workout routes are embedded as `route` arrays with latitude, longitude, altitude, speed, and timestamp. zdrowskit derives per-km splits from these when present.

## Historical Backfill

Each automation has a **Manual Export** button at the bottom of the automation screen that supports custom date ranges. Use this to backfill historical data. The output uses the exact same format as scheduled exports, so no separate import path is needed.

How to backfill:

1. Open an existing automation in Auto Export.
2. Scroll to the bottom and tap **Manual Export**.
3. Set a custom date range, such as the whole of 2024. The app splits it into weekly files automatically.
4. Wait for the files to sync via iCloud.
5. Run `uv run python main.py import`. The same command handles both current and historical data.

Do this once per automation: Metrics and Workouts. The import is idempotent, so re-running it will not duplicate data.

## Recommended Workflow

1. Set up Auto Export automations with Week date range and Day aggregation.
2. Backfill historical data using Manual Export from each automation.
3. Import everything:

   ```bash
   uv run python main.py import
   ```

4. Run the daemon. It watches the Auto Export iCloud folder and imports new data automatically.
5. Stop thinking about exporting until Apple changes something.
