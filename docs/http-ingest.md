# Auto Export HTTP Ingest

HTTP is the default transport for new profiles. Auto Export sends JSON directly
to a loopback receiver in the zdrowskit daemon; Tailscale Funnel supplies the
stable public HTTPS URL. Google Drive and local/iCloud imports remain available.

## Security Model

- The receiver binds to `127.0.0.1:8787`, never the LAN interface.
- Tailscale Funnel terminates public HTTPS and forwards to that loopback port.
- Each profile has a random bearer token. The token, not the request body or
  Auto Export headers, selects the destination profile.
- `~/Documents/zdrowskit/ingest_tokens.json` stores only SHA-256 token hashes,
  uses mode `0600`, and is added to the state repository's `.gitignore`.
- Invalid authentication is rejected before the request body is read.

The host operator can read every hosted profile database. Explain this to each
person before accepting their health data.

## What Needs Tailscale

Only the host running zdrowskit needs Tailscale. Family and friends do not need
a Tailscale account or app: Funnel makes one narrow HTTPS endpoint publicly
reachable, and each person's bearer token controls which profile may receive
their uploads.

```text
iPhone Auto Export
  -> public HTTPS Funnel URL
  -> Tailscale on the host
  -> http://127.0.0.1:8787
  -> zdrowskit daemon
```

No router port forwarding, public host IP, or firewall rule is required. Do not
configure an exit node or subnet router for zdrowskit.

## Install Tailscale on the Host

These are the recommended macOS steps. Tailscale currently recommends its
[Standalone macOS variant](https://tailscale.com/docs/install/mac); it also
provides the smoothest CLI setup for zdrowskit.

1. Install the Standalone app and complete the macOS VPN/network-extension
   approval prompts.
2. Sign in with the host operator's normal identity provider. A new personal
   tailnet is fine.
3. In Tailscale **Settings**:
   - enable **Allow incoming connections**;
   - enable **Use Tailscale DNS settings**;
   - enable **Launch Tailscale at login**;
   - under **CLI integration**, choose **Show me how**, then **Install Now**.
4. Open a new Terminal and verify the command by name—do not assume the binary
   already exists at `/usr/local/bin/tailscale`:

   ```bash
   command -v tailscale
   tailscale version
   tailscale status
   ```

For the Standalone app on macOS Ventura 13 or later, CLI Integration installs a
launcher at `/usr/local/bin/tailscale`. If the command is still missing, repeat
**Settings -> CLI integration -> Show me how**. See the official
[macOS CLI instructions](https://tailscale.com/docs/reference/tailscale-cli?tab=macos).

If the Mac App Store variant is already installed, its CLI lives inside the
application:

```bash
/Applications/Tailscale.app/Contents/MacOS/Tailscale version
/Applications/Tailscale.app/Contents/MacOS/Tailscale status
```

You can use that full path for manual commands. The integrated `ingest setup
--funnel` helper expects a `tailscale` executable on `PATH`, so the Standalone
variant with CLI Integration is the least surprising setup.

`tailscale status` should show this Mac as connected with a `100.x.y.z` address.
If it reports that preferences cannot be loaded, open the Tailscale app, finish
sign-in, and connect it before continuing.

## First HTTP Setup

Create or switch a profile to HTTP, then initialize ingestion:

```bash
uv run python main.py profile add adam --telegram-id ID --operator
# Existing profile:
uv run python main.py profile source adam http

# Print the upload token once. Keep this Terminal output open.
uv run python main.py ingest setup

# Install/start the daemon and its loopback receiver.
uv run python main.py daemon-install
```

`ingest setup` prints the token once. Put it into Auto Export immediately; only
its hash remains on the host. If it is lost, rotate it:

```bash
uv run python main.py ingest token adam --rotate
```

The receiver reloads the token registry on every request, so rotation does not
require a daemon restart. Update both iPhone automations immediately; the old
token stops working as soon as rotation completes.

Check the local receiver before exposing it:

```bash
curl http://127.0.0.1:8787/healthz
# {"status":"ok"}
```

Now create the Funnel:

```bash
tailscale funnel --bg --https=443 http://127.0.0.1:8787
```

The first Funnel command may open a browser for tailnet approval. Approve it as
the tailnet owner/admin. Tailscale then enables the required MagicDNS, HTTPS
certificate, and Funnel policy settings. Funnel is available on all Tailscale
plans but remains a beta feature; its public HTTPS ports are restricted to 443,
8443, and 10000. zdrowskit uses 443. See the official
[Funnel requirements](https://tailscale.com/docs/features/tailscale-funnel).

The command prints the public URL:

```text
https://<machine>.<tailnet>.ts.net
```

It normally stays unchanged while the Mac's Tailscale machine name and tailnet
name remain unchanged. `--bg` makes the Funnel configuration persistent across
Tailscale restarts and host reboots. You can also run `uv run python main.py
ingest setup --funnel` after the CLI and Funnel approval are known to work, but
the explicit command above is easier to diagnose on a first setup.

Verify both layers:

```bash
tailscale funnel status
curl https://<machine>.<tailnet>.ts.net/healthz
uv run python main.py ingest status
```

Finally, open `https://<machine>.<tailnet>.ts.net/healthz` in Safari on an iPhone
with Wi-Fi disabled. Seeing `{"status":"ok"}` confirms the endpoint works from
the public internet. The iPhone does not need the Tailscale app.

The receiver is part of the existing daemon. The generated macOS LaunchAgent
uses `RunAtLoad` and `KeepAlive`, so it returns after login and is restarted if
it exits. This is a user service: after a full reboot, the macOS user must log
in before the receiver and Tailscale app run. Keep **Launch Tailscale at login**
enabled; the persistent Funnel cannot serve while the Tailscale app is stopped.

## Optional Funnel-Only Smoke Test

To test Tailscale before creating profiles or tokens, run a disposable empty
directory server. Do this only while the zdrowskit daemon is stopped because it
uses the same local port.

Terminal 1:

```bash
mkdir -p /tmp/zdrowskit-funnel-probe
uv run python -m http.server 8787 \
  --bind 127.0.0.1 \
  --directory /tmp/zdrowskit-funnel-probe
```

Terminal 2:

```bash
tailscale funnel --bg --https=443 http://127.0.0.1:8787
tailscale funnel status
```

Open the printed HTTPS URL from another device. A directory listing confirms
the Funnel works. Stop the temporary server with Ctrl+C, then either point the
same Funnel at the real receiver after `daemon-install`, or turn it off:

```bash
tailscale funnel --https=443 off
```

## iPhone Auto Export Setup

Create two REST API automations with the same schedule.

Shared settings:

- URL: `https://<machine>.<tailnet>.ts.net/v1/auto-export`
- Method: `POST`
- Content type: `application/json`
- Authorization: `Bearer <profile-token>`
- JSON format: version 2
- Batch export: off

Metrics:

- Automation name: `Metrics`
- Summarized data: on
- Aggregation: `Days`
- Date Range: `Last 7 Days`

Workouts:

- Automation name: `Workouts`
- Aggregation: `Minutes`
- Include routes and metadata
- Date Range: `Default`

## Choosing Date Ranges

Auto Export resends whatever window you choose on every run, so the window is
how long a sync outage stays recoverable. Anything older than it is lost from
the automatic path and would need a manual file export.

The two halves deserve different windows because they cost wildly different
amounts and fail differently:

| | 2 days | 7 days | why |
|---|---|---|---|
| Metrics | ~12 KB | ~40 KB | Sleep, HRV, resting HR and steps. Their absence is *invisible* — it silently distorts baselines rather than announcing itself. Cheap enough to widen freely; even `Last 30 Days` is only ~170 KB. |
| Workouts | ~1.2 MB | ~3 MB | GPS routes are about two thirds of the payload. At roughly 18 uploads a day a wide window costs tens of MB daily off the phone, and triples the archive. A missing workout is *loud* — you know you ran — so it gets noticed and can be re-exported. |

Widening Metrics alone is safe: an import only replaces workouts for the dates
its Workouts export actually covered, so the extra Metrics days never touch
workout history. Before that rule existed, this combination deleted every
workout in the gap on every import.

The entry caps (`MAX_METRIC_ENTRIES`, `MAX_WORKOUTS`) reject a genuinely
oversized export with an actionable message, so an over-wide window fails
closed rather than silently.

Auto Export supplies `automation-name`, `automation-id`, `automation-period`,
`automation-aggregation`, and `session-id` headers automatically. Do not add a
profile name to the URL, headers, or JSON.

## Validation, Pairing, and Retention

The receiver checks authentication, content type, a 64 MiB size limit, required
Auto Export headers, the JSON envelope, dates, finite numbers, route bounds,
array limits, and compatibility with the production parser. It returns `422`
with an actionable message when the automation settings or payload are wrong.

Metrics and Workouts are staged independently, but imported only after both
arrive within ten minutes. This matters because the existing database import
replaces complete daily snapshots. A missing half therefore never erases the
other half's fields.

Per profile, the ingest cache is bounded to:

- `Imports/http/Metrics/latest.json`
- `Imports/http/Workouts/latest.json`
- a small state file with the latest hashes and at most 100 import receipts
- one transient immutable pair while an import is running

An identical completed pair is acknowledged but not re-imported. A crash after
HTTP `202` leaves the latest pair on disk; daemon startup detects and imports
it. Parser failures keep the latest pair and a short error record for diagnosis.

## Raw Payload Archive

The bounded cache above is overwritten by every upload, so it cannot answer
"what did the phone actually send?" after the fact. One gzipped, unmodified
snapshot per kind per day is therefore kept outside that cache, with the day's
last upload winning:

```text
Imports/archive/metrics/2026-08-04.json.gz
Imports/archive/workouts/2026-08-04.json.gz
```

The archive is what makes a parser fix or a widened metric map replayable
without asking someone to re-export from their phone. Archiving never blocks
ingestion — if the write fails the payload still imports, and the failure is
logged as an error.

One snapshot a day is enough because Auto Export sends a rolling multi-day
window: every date is covered both by its own day's snapshot and by the next
day's, and the next day's is the more complete of the two once Apple Health
has backfilled past midnight. Keeping every upload instead would store around
twenty near-identical copies a day — and could not be deduplicated by content
hash, because Auto Export does not serialize JSON keys in a stable order, so
unchanged content still arrives as different bytes on every send.

Measured against real traffic this costs a few tens of MB per profile per
year, dominated by workout GPS routes. There is no pruning; revisit that only
if the directory ever actually grows enough to matter.

## Operations

```bash
uv run python main.py ingest status
uv run python main.py doctor
tail -f ~/Library/Logs/zdrowskit.daemon.log
curl http://127.0.0.1:8787/healthz
tailscale funnel status
```

`ingest status` never prints bearer tokens. It reports receiver reachability,
the public DNS URL when Tailscale is connected, token presence, arrival times,
the last successful import, and the last error. `doctor` runs the same receiver
probe, so a stopped daemon fails both.

Each profile also reports one `pairing:` line explaining whether the next import
can happen:

| `pairing:` | Meaning |
|---|---|
| `up to date` | Both halves arrived and imported. Steady state. |
| `queued for import` | Both halves are staged; the daemon is importing them now. |
| `waiting for the other half` | Only Metrics or only Workouts has ever arrived, or the other half is missing. Check that both automations exist and use the same URL and token. |
| `halves arrived too far apart` | Both arrived, but more than ten minutes apart, so they were not paired. Give both automations the same schedule and re-run them together. |

The daemon log records every accepted upload (profile, kind, size), every
rejection with its reason and HTTP status, and a warning whenever halves land
outside the pairing window. `tail -f ~/Library/Logs/zdrowskit.daemon.log` while
triggering an automation is the fastest way to diagnose a family member's phone.

### Stop or Recreate the Funnel

```bash
# Stop public access on HTTPS port 443.
tailscale funnel --https=443 off

# Re-enable the same persistent mapping and URL.
tailscale funnel --bg --https=443 http://127.0.0.1:8787

# Remove every Funnel configuration from this Mac (broader than zdrowskit).
tailscale funnel reset
```

Stopping Funnel does not stop the zdrowskit daemon, Telegram, reports, or local
database access. It only prevents new iPhone HTTP uploads from reaching the
receiver.

## Troubleshooting

| Symptom | Check |
|---|---|
| `tailscale: command not found` | Use Standalone **Settings -> CLI integration -> Show me how**, or use the App Store app's full executable path. |
| `Failed to load preferences` | Open Tailscale, complete sign-in/VPN approval, and connect it. |
| First Funnel command opens a browser | Approve Funnel for the tailnet; this provisions its policy and HTTPS support. |
| Public URL does not resolve immediately | Initial public DNS propagation can take up to ten minutes. Avoid repeatedly recreating the Funnel/certificate. |
| Local `/healthz` works, public URL does not | Run `tailscale funnel status`; port 443 must be a Funnel proxy to `http://127.0.0.1:8787`, not a private Tailscale Serve mapping. |
| `/` returns `404` | Expected. Production exposes `GET /healthz` and authenticated `POST /v1/auto-export`, not a directory listing. |
| Auto Export gets `401` | Re-enter the exact `Bearer <token>` authorization value or rotate the profile token. |
| Auto Export gets `422` | Read the returned error; normally aggregation, headers, JSON format, or an oversized export. |
| `pairing: waiting for the other half` | Only one automation reached the receiver. Check both exist and use the same URL and token. |
| `pairing: halves arrived too far apart` | Both automations work but run more than ten minutes apart. Put them on the same schedule. |
| Works until reboot | Enable **Launch Tailscale at login**, confirm the macOS user logged in, then check `tailscale funnel status` and `main.py ingest status`. |

Tailscale documents that public DNS may take up to ten minutes to propagate and
that the most recent `serve`/`funnel` command owns a given public port. It also
warns against repeatedly provisioning certificates because certificate rate
limits can cause long delays.

The receiver accepts any Date Range; the entry caps are what bound an export.
Keep local or Google Drive import available for large historical backfills;
they remain idempotent and can be disabled again after the backfill.
