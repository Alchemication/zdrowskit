# Apple Health Data Export

Apple's built-in health export dumps everything into a single massive XML file. On any non-trivial data size, this crashes or overheats the iPhone, so it is not a real solution for this project.

The workaround is a third-party iOS app that reads HealthKit directly and
exports structured JSON. zdrowskit uses [Auto Export](https://apps.apple.com/app/myhealth-export-to-icloud/id6737380982).
It can post directly to the zdrowskit HTTP receiver or write to cloud storage.
It works on iOS 26, while some alternatives do not yet. The Basic tier unlocks
Shortcut actions; Premium (a one-time purchase, still cheap) is needed for
scheduled Automations.

One universal constraint: iOS requires the phone to be unlocked for any health data export. Automations silently skip when the phone is locked.

## Choosing a Transport

Three transports carry the same Auto Export JSON to the same parser. They differ
in latency, how many people they can serve, and what happens when something is
switched off.

| | HTTP (Tailscale) | iCloud / local | Google Drive |
|---|---|---|---|
| Profiles | Many | Operator only | Many |
| Delivery | Push, on export | File watch + 3 min debounce | Poll, up to 5 min |
| Host requirement | Tailscale-capable | Mac on the same Apple ID | Any macOS or Linux |
| Survives host downtime | No | Yes | Yes |
| Historical backfill | No | Yes | Yes |
| Credentials to manage | Per-profile token | None | Service-account key |

### HTTP via Tailscale Funnel (default)

**Pros**

- Fastest. The phone pushes on export, with no sync layer or poll interval in
  between.
- The only transport that serves several people from one endpoint, routed by
  per-profile bearer token. Rotation is one command and needs no daemon restart.
- Most reliable in practice: nothing sits between the phone and the host that
  can silently stall.
- Payloads are validated at the door, so a misconfigured automation gets an
  actionable `422` on the phone instead of quietly importing wrong data.
- Metrics and Workouts import as a complete pair, so a nudge reacts to a whole
  day rather than half of one. Neither half can erase the other regardless.
- Leaves you with a working private HTTPS endpoint on the host, which is
  reusable for other home projects.

**Cons**

- **An upload is lost if the host is down.** iCloud and Drive queue the file
  until the daemon catches up; a failed POST has nothing behind it. This is the
  real cost of push delivery, and the reason to keep the daemon on `KeepAlive`.
- Depends on Tailscale Funnel, still a Tailscale beta, plus the macOS user being
  logged in and Tailscale running.
- Bounded by per-metric and per-workout entry caps, so a large historical
  backfill still needs a different transport.
- Both automations must arrive within an hour of each other to pair.
- The most setup steps of the three.

### iCloud / local files

**Pros**

- Simplest to get working. No tokens, no service account, no network exposure.
- Files queue on disk, so daemon downtime costs nothing.
- Handles large historical backfills.

**Cons**

- **Operator profile only.** This is enforced in code, so it cannot host family
  or friends. Reach for it when zdrowskit serves exactly one person.
- Requires the daemon host to be a Mac signed into the same Apple ID with iCloud
  Drive syncing.
- iCloud sync latency is opaque and unreportable, on top of a three-minute
  debounce after the file lands.
- No validation before import.

### Google Drive

**Pros**

- Familiar to most people, and portable: the host does not need to be Apple
  hardware, so a Linux box or Raspberry Pi works.
- Serves several people through per-profile folder IDs.
- Files queue in Drive, so daemon downtime costs nothing.
- Handles historical backfills.

**Cons**

- Slowest of the three: up to a five-minute poll interval on top of Drive's own
  sync delay.
- The least reliable Auto Export path in practice.
- Needs a Google Cloud service account, its JSON key stored on the host, and
  per-profile folder sharing.
- That key is a long-lived credential you have to protect and rotate by hand.

A practical combination: run HTTP for day-to-day delivery, and temporarily
enable local or Drive import when you need a backfill.

## Auto Export Setup

The Automations feature exports health data over HTTP, to iCloud Drive, or to
Google Drive on a schedule, with no taps required once configured.

Setup in the app:

1. Create two automations: one for **Metrics**, one for **Workouts**.
2. For HTTP, use JSON v2, Metrics aggregation **Days**, and Workouts
   aggregation **Minutes**. Set Metrics to **Last 7 Days** and leave
   Workouts on **Default** — see [choosing date ranges](http-ingest.md#choosing-date-ranges).
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
