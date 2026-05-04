# Daemon

The daemon watches your iCloud health data folder and context files. When something meaningful happens, it decides whether to send a notification.

```bash
# Test in foreground
uv run python src/daemon.py --foreground

# Install as a background service that starts automatically at login
uv run python main.py daemon-install
```

What it watches and when it acts is covered in [Notifications](notifications.md): triggers, suppression rules, and cross-channel awareness.

The checked-in plist under `launchd/` is a placeholder template. `daemon-install` generates the real plist with your checkout path, `uv` path, `HOME`, `PATH`, log location, and resolved Codex CLI path if `codex` is available. It currently installs one daemon instance for the current macOS user.

## Files

State file:

```text
~/Documents/zdrowskit/.daemon_state.json
```

This tracks rate limits, recent nudge history, coach summaries, the deferred nudge queue, and pending Telegram reason prompts for feedback / proposal rejection.

Notification preferences:

```text
~/Documents/zdrowskit/notification_prefs.json
```

Set via Telegram `/notify` — see [Notifications](notifications.md). Delete the file to fall back to built-in defaults.

Logs:

```text
~/Library/Logs/zdrowskit.daemon.log
```

Logs rotate for 7 days.

## Operations

Check if it is running. Look for a non-dash PID and exit code 0:

```bash
launchctl list | grep zdrowskit
# 6405    0    com.zdrowskit.daemon  <- good: running, clean exit
# -       78   com.zdrowskit.daemon  <- bad: not running, error
```

Watch live logs:

```bash
tail -f ~/Library/Logs/zdrowskit.daemon.log
```

Restart rules:

| Scenario | Command |
|---|---|
| Code change in `src/`, such as `daemon.py` or `commands.py` | `uv run python main.py daemon-restart` |
| Change to `.env`, such as a new API key | `uv run python main.py daemon-restart` |
| PATH or CLI location changes, such as installing Codex under Homebrew | `uv run python main.py daemon-install` |
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
