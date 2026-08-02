# Apple Health Data Export

Apple's built-in health export dumps everything into a single massive XML file. On any non-trivial data size, this crashes or overheats the iPhone, so it is not a real solution for this project.

The workaround is a third-party iOS app that reads HealthKit directly and
exports structured JSON. zdrowskit uses [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982).
It can post directly to the zdrowskit HTTP receiver or write to cloud storage.
It works on iOS 26, while some alternatives do not yet. The Basic tier unlocks
Shortcut actions; Premium (a one-time purchase, still cheap) is needed for
scheduled Automations.

One universal constraint: iOS requires the phone to be unlocked for any health data export. Automations silently skip when the phone is locked.

## Auto Export Setup

The Automations feature exports health data over HTTP, to iCloud Drive, or to
Google Drive on a schedule, with no taps required once configured.

Setup in the app:

1. Create two automations: one for **Metrics**, one for **Workouts**.
2. For HTTP, use **Date Range = Default**, JSON v2, Metrics aggregation
   **Days**, and Workouts aggregation **Minutes**.
3. Select all metrics you care about, such as steps, energy, HR, HRV, VO2max, mobility, resting heart rate, and sleep analysis.
4. Set the schedule. Every 5 minutes is recommended because shorter intervals catch more unlock windows.

For direct delivery, follow [HTTP ingest](http-ingest.md). For iCloud, use
`Metrics` and `Workouts` as folder names. Google Drive setup is documented in
[Google Drive import](google-drive.md).

The app writes weekly JSON files:

```text
Metrics/HealthAutoExport-YYYY-WW.json
Workouts/HealthAutoExport-YYYY-WW.json
```

Default iCloud data path:

```text
~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/
```

Notes:

- Sleep data is pre-aggregated nightly totals, with no per-segment breakdown.
- Workout routes are embedded as `route` arrays with latitude, longitude, altitude, speed, and timestamp. zdrowskit derives per-km splits from these when present.
- Route workouts are also reverse-geocoded during import into locality-level locations (for example Crosshaven, Warsaw, Malaga). Full route coordinates are not stored in SQLite. Set `ZDROWSKIT_LOCATION_GEOCODER=off` to disable the external lookup; the default provider is Nominatim and results are looked up/cached by coarse start coordinate.

## Historical Backfill

Each automation has a **Manual Export** button at the bottom of the automation
screen that supports custom date ranges. Use this to backfill historical data.
The output uses the same format as scheduled exports.

How to backfill:

1. Open an existing automation in Auto Export.
2. Scroll to the bottom and tap **Manual Export**.
3. Set a custom date range, such as the whole of 2024. The app splits it into weekly files automatically.
4. For HTTP, send both manual exports and check `uv run python main.py ingest
   status`; the receiver imports the matching pair. For iCloud/Drive, wait for
   the files and run `uv run python main.py import`.

Do this once per automation: Metrics and Workouts. The import is idempotent, so re-running it will not duplicate data.

## Recommended Workflow

1. Set up the two Default-range HTTP automations described above.
2. Backfill historical data using Manual Export from each automation.
3. With HTTP, send both exports and confirm the completed pair:

   ```bash
   uv run python main.py ingest status
   ```

   With an iCloud/Drive backfill, use `uv run python main.py import` instead.

4. Run the daemon. It receives HTTP by default, watches a local/iCloud source,
   or polls Drive according to each profile's `import_source`.
