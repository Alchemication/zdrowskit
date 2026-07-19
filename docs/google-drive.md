# Google Drive Import

Google Drive is the portable Auto Export transport. The import command fetches
JSON through the Drive API into a local cache, validates the payload type, and
then runs the normal zdrowskit parser. It works anywhere Python and `uv` run,
including a Raspberry Pi.

Google Drive is the recommended transport for new installations. The runtime
default remains `local` so existing iCloud installations do not break; selecting
Drive explicitly in `.env` enables API polling in both `import` and the daemon.

## Auto Export Setup

Create separate Google Drive automations for Metrics and Workouts. Use:

- Export Format: JSON
- Date Range: Week
- Aggregation: Day
- Folder Name: `Adam/Metrics` and `Adam/Workouts`

Google Drive stores those as two literal folder names directly under
`Health Auto Export`; `/` does not create a nested directory. For another person,
use names such as `Anna/Metrics` and `Anna/Workouts`.

Leave the advanced root and backup folder-ID fields alone. Auto Export manages
those IDs for its own recovery and migration state.

## Service Account

In Google Cloud:

1. Create or select a project and enable the Google Drive API.
2. Create a service account. It does not need a project IAM role; Google Cloud
   Storage roles are unrelated to Google Drive.
3. Create a JSON key and keep it outside the repository.
4. In Google Drive, share the generated `Health Auto Export` root folder with
   the service-account email as **Viewer**. Do not enable public link access.

Sharing the root grants inherited read access to the automation folders. Auto
Export continues writing as the Google account connected on the iPhone; the
service account only reads the exported files.

## Folder IDs

Open each automation folder in the normal Google Drive web app. Its ID is the
value after `/folders/` in the URL:

```text
https://drive.google.com/drive/folders/FOLDER_ID
```

Use IDs, not folder names, in zdrowskit configuration. Drive permits duplicate
and mutable names, while an ID remains stable when a folder is renamed or moved.

## Configuration

Add one profile's Drive source to `.env`:

```dotenv
ZDROWSKIT_IMPORT_SOURCE=google-drive
ZDROWSKIT_GOOGLE_DRIVE_SERVICE_ACCOUNT=~/Documents/zdrowskit/secrets/service-account.json
ZDROWSKIT_GOOGLE_DRIVE_METRICS_FOLDER_ID=METRICS_FOLDER_ID
ZDROWSKIT_GOOGLE_DRIVE_WORKOUTS_FOLDER_ID=WORKOUTS_FOLDER_ID
ZDROWSKIT_GOOGLE_DRIVE_POLL_INTERVAL_S=300
HEALTH_DATA_DIR=~/Documents/zdrowskit/Imports/adam
```

Then fetch and import in one command:

```bash
uv run python main.py import
```

The cache is materialized as:

```text
~/Documents/zdrowskit/Imports/adam/
  Metrics/*.json
  Workouts/*.json
  .drive-fetch-manifest.json
```

The manifest makes polling idempotent: files whose Drive checksums are already
current are skipped. Downloaded JSON is rejected if the configured Metrics
folder does not contain `data.metrics` or the Workouts folder does not contain
`data.workouts`.

## Daemon Polling

With `ZDROWSKIT_IMPORT_SOURCE=google-drive`, the daemon uses Drive API polling
instead of filesystem events for health data:

1. Poll immediately at startup and perform a full cache import.
2. Poll every `ZDROWSKIT_GOOGLE_DRIVE_POLL_INTERVAL_S` seconds; the default is
   five minutes.
3. Compare Drive checksums with the local manifest.
4. When files changed, download atomically, import, and run the existing
   `new_data` notification flow.
5. When nothing changed, skip parsing and database work.

The iCloud three-minute filesystem debounce does not apply to Drive. Failed
polls leave the existing cache and database intact and retry on the next
interval. A persistent failure (bad folder ID, revoked share) is recorded in
the event log once per streak, not once per poll; the next successful poll
re-arms the failure event.

Run the daemon directly on macOS or Linux:

```bash
uv run python src/daemon.py --foreground
```

`uv run python main.py daemon-install` installs a macOS `launchd` service. On a
Raspberry Pi or other Linux host, run the same daemon command under your normal
service manager, such as systemd.

## Standalone Fetch

Use the standalone command to inspect or populate a cache without touching the
database:

```bash
uv run python scripts/drive_fetch.py \
  --service-account ~/Documents/zdrowskit/secrets/service-account.json \
  --metrics-folder-id METRICS_FOLDER_ID \
  --workouts-folder-id WORKOUTS_FOLDER_ID \
  --data-dir ~/Documents/zdrowskit/Imports/adam \
  --dry-run --verbose
```

Remove `--dry-run` to download. The resulting directory can also be imported
explicitly with `uv run python main.py import --source local --data-dir PATH`.

## Multiple People

One service account can read folders shared by multiple people. Keep each
person's folder IDs, local cache, and SQLite database separate. Do not import two
people into one database; the schema is currently single-profile and has no
user identifier.

The `.env` defaults represent one profile. A second profile can be run with CLI
overrides and a separate database:

```bash
uv run python main.py import \
  --source google-drive \
  --google-drive-service-account ~/Documents/zdrowskit/secrets/service-account.json \
  --google-drive-metrics-folder-id ANNA_METRICS_FOLDER_ID \
  --google-drive-workouts-folder-id ANNA_WORKOUTS_FOLDER_ID \
  --data-dir ~/Documents/zdrowskit/Imports/anna \
  --db ~/Documents/zdrowskit/anna.db
```

The daemon currently owns one profile per process and app-home directory. A
first-class multi-profile supervisor is separate future work.
