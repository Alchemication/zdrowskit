# Family Hosting

One zdrowskit daemon and one Telegram bot can serve a small operator-managed
roster. Each person has a separate database, context tree, Drive cache,
generated output, preferences, conversation state, and daemon state under:

```text
~/Documents/zdrowskit/
  profiles.toml
  profiles/
    adam/
      health.db
      ContextFiles/
      Imports/google-drive/
      Reports/
      Nudges/
      notification_prefs.json
      model_prefs.json
      daemon_state.json
```

Provider keys, the Telegram bot token, and the Google service-account JSON
remain shared infrastructure in `.env`.

## Create a New Operator

For a new local/iCloud installation:

```bash
uv run python main.py profile add adam \
  --telegram-id 111111111 \
  --operator \
  --source local
```

Exactly one roster entry must be the operator. Only that profile may use the
host's local Auto Export directory. The operator alone can use `/codex` and
`/claude`, because those commands grant repository write access.

## Adopt an Existing Installation

`profile adopt` is the supported one-time migration command for a legacy
root-level installation. It is not used when adding later people, and it is not
an automatic daemon startup step.

```bash
uv run python main.py profile adopt adam --dry-run
uv run python main.py daemon-stop
uv run python main.py profile adopt adam
uv run python main.py daemon-restart
```

Adoption reports every copy, validates the copied database and required context
files, then writes the roster last. Pass `--telegram-id ID` when the old `.env`
does not define `TELEGRAM_CHAT_ID`.

Legacy root-level files remain untouched as a rollback copy. Seeing the same
root-level `health.db`, `ContextFiles/`, `Reports/`, or Drive cache after
adoption is expected; the daemon now uses the copies under
`profiles/<name>/`. After completing the verification checklist below, archive
or remove the legacy files manually.

## Add a Drive Profile

```bash
uv run python main.py profile add anna \
  --telegram-id 222222222 \
  --google-drive-metrics-folder-id ANNA_METRICS_FOLDER_ID \
  --google-drive-workouts-folder-id ANNA_WORKOUTS_FOLDER_ID
```

This creates the database, context templates, generated-output directories,
Drive cache, and roster entry. The equivalent roster is:

```toml
[profiles.adam]
telegram_id = 111111111
operator = true
import_source = "local"

[profiles.anna]
telegram_id = 222222222
drive_metrics_folder_id = "ANNA_METRICS_FOLDER_ID"
drive_workouts_folder_id = "ANNA_WORKOUTS_FOLDER_ID"
```

Restart after every roster edit; configuration is intentionally not hot
reloaded. `profile add` also changes the roster, so restart the already-running
daemon after adding a person. A Drive profile with missing folder configuration
is logged as disabled at runtime and does not stop other profiles.

Unknown, disabled, group-chat, and mismatched sender/chat identities are
default-denied. Authorization uses Telegram's numeric user ID, never username.

## Operations

- Pause: set `enabled = false`, then restart.
- Rename: stop the daemon, rename `profiles/<old>`, edit the TOML key, restart.
- Back up: copy `profiles.toml` and the relevant `profiles/<name>/` directory.
- Delete: stop the daemon, remove the roster section and profile directory,
  restart.
- Check migrations: `uv run python main.py db status --all`.
- Apply migrations eagerly: `uv run python main.py db migrate --all`.

CLI health commands accept `--profile NAME` and default to the operator:

```bash
uv run python main.py status --profile anna
uv run python main.py insights --profile anna --week last
uv run python main.py llm-log --profile anna --id 42
```

The technical operator can read every hosted database, including stored LLM
prompts and replies. Tell people you host before adding their profile.

## Verification Checklist

After adoption, verify the operator before deleting any legacy files:

```bash
uv run python main.py db status --all
uv run python main.py status --profile adam
tail -f ~/Library/Logs/zdrowskit.daemon.log
```

The log should identify `adam`, attach its Telegram sender, and report one
enabled runtime. `/status` and `/context me` in Telegram should return the
operator's existing data and context. For a Drive profile, the startup poll
should report downloaded/current files for that profile.

The definitive isolation test requires two profiles:

1. Add the second profile, restart, and confirm the log reports two enabled
   runtimes.
2. Run `db status --all`; both database paths must be under different
   `profiles/<name>/` directories.
3. Put a unique harmless marker in each `ContextFiles/me.md`. Each person runs
   `/context me` and sees only their own marker.
4. Each person runs `/status` and sees their own health-data coverage.
5. The non-operator tries `/codex`; it must be rejected as operator-only.
6. An unlinked account messages the bot; it must receive no health access, and
   the operator should receive an unknown-user notice.

That covers routing, database/context isolation, outbound delivery, operator
authorization, and default-deny behavior. Keep the legacy rollback copy until
the one-profile checks pass; full multi-profile confidence comes from the
two-profile test.
