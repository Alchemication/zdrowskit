# Daemon

The daemon imports health data, watches context files, and decides whether a
meaningful change should produce a notification.

Health ingestion is selected per entry in `profiles.toml`:

| Source | Behavior |
|---|---|
| `http` | Receive authenticated Auto Export uploads on loopback, pair Metrics + Workouts, then import; default for new profiles |
| `google-drive` | Poll the Metrics and Workouts folder IDs every five minutes by default |
| `local` | Watch the operator's local Auto Export directory; non-operator profiles cannot use it |

```bash
# Test in foreground
uv run python src/daemon.py --foreground

# macOS: install as a background service that starts automatically at login
uv run python main.py daemon-install
```

The foreground daemon runs on macOS and Linux. `daemon-install`,
`daemon-restart`, and `daemon-stop` manage macOS `launchd`; use systemd or
another process supervisor on Linux.

What it watches and when it acts is covered in [Notifications](notifications.md): triggers, suppression rules, and cross-channel awareness.

At startup, one process loads every enabled roster entry, creates one isolated
runtime per healthy profile, and starts one shared Telegram poller. A missing
database, required context file, or Drive setting disables only that runtime;
other profiles continue. Logs include the profile name for profile-owned work.

## Health Import

For HTTP, the receiver starts with the daemon and listens on
`127.0.0.1:8787`. Valid uploads are staged durably and imported only as a
complete Metrics + Workouts pair. Tailscale Funnel is a separate persistent
host service that proxies public HTTPS to this loopback listener; the daemon
does not terminate TLS or expose a LAN socket. See [HTTP ingest](http-ingest.md).

For Google Drive, the daemon polls immediately on startup. This first pass fully
imports an existing cache even when no Drive checksum changed. Later polls skip
parsing and database access when every Drive file is current. New or changed
files are downloaded atomically and enter the same `new_data` nudge flow as an
iCloud file event.

Configure the cadence in `.env`:

```dotenv
ZDROWSKIT_GOOGLE_DRIVE_POLL_INTERVAL_S=300
```

For local/iCloud imports, watchdog observes the data directory and retains the
three-minute debounce that collapses a burst of filesystem events into one
import.

The checked-in plist under `launchd/` is a placeholder template. `daemon-install` generates the real plist with your checkout path, `uv` path, `HOME`, `PATH`, log location, and resolved Codex / Claude CLI paths if `codex` and/or `claude` are available. It currently installs one daemon instance for the current macOS user.

## Files

Profile state:

```text
~/Documents/zdrowskit/profiles/<name>/daemon_state.json
```

This tracks rate limits, recent nudge history, coach summaries, the deferred nudge queue, and pending Telegram reason prompts for feedback / proposal rejection.

Notification preferences:

```text
~/Documents/zdrowskit/profiles/<name>/notification_prefs.json
```

Set via Telegram `/notify` — see [Notifications](notifications.md). Delete the file to fall back to built-in defaults.

Logs:

```text
~/Library/Logs/zdrowskit.daemon.log
```

Logs rotate for 7 days.

## Operations

On macOS, check if the launchd service is running. Look for a non-dash PID and
exit code 0:

```bash
launchctl list | grep zdrowskit
# 6405    0    com.zdrowskit.daemon  <- good: running, clean exit
# -       78   com.zdrowskit.daemon  <- bad: not running, error
```

Watch live logs:

```bash
tail -f ~/Library/Logs/zdrowskit.daemon.log
```

Check every profile database after a deploy or migration:

```bash
uv run python main.py db status --all
```

Restart rules:

| Scenario | Command |
|---|---|
| Code change in `src/`, such as `daemon.py` or `commands.py` | `uv run python main.py daemon-restart` |
| Change to `.env`, such as a new API key | `uv run python main.py daemon-restart` |
| Add, pause, rename, or edit a profile in `profiles.toml` | `uv run python main.py daemon-restart` |
| PATH or CLI location changes, such as installing Codex or Claude under Homebrew | `uv run python main.py daemon-install` |
| Stop for testing in foreground | `uv run python main.py daemon-stop` |
| Change to the `.plist` itself | See below |
| Context file changes (`*.md`) | No restart needed; read at trigger time |
| State file reset | No restart needed; read on every trigger |
| `notification_prefs.json` edit/reset | No restart needed; read on every trigger |

Updating the plist requires a full reload after editing `launchd/com.zdrowskit.daemon.plist`:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.zdrowskit.daemon.plist
uv run python main.py daemon-install
```
