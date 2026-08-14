# Multi-Profile Architecture

zdrowskit can serve a small operator-managed roster from one daemon and one
Telegram bot. The shared process does not make the health data shared: each
person has a separate SQLite database, context tree, import cache, generated
output, preferences, and daemon state.

This is family/friend hosting, not a public multi-tenant service. There is no
self-service signup, group-chat support, web admin, or cross-profile query path.
For the operator workflow, see [Family hosting](family-hosting.md).

## The isolation boundary

The profile directory and database file are the boundary:

```text
~/Documents/zdrowskit/
  profiles.toml
  ingest_tokens.json
  profiles/
    adam/
      health.db
      ContextFiles/
      Imports/
        http/
        archive/
        google-drive/
      Reports/
      Nudges/
      notification_prefs.json
      model_prefs.json
      daemon_state.json
    anna/
      ...the same isolated tree...
```

Health rows do not carry a `profile_id`. A query against
`profiles/anna/health.db` can only see Anna's rows; the existing health schema,
primary keys, parser, reports, migrations, and read-only SQL tool remain
unchanged.

That choice also makes backup and deletion profile-scoped. Copying or removing
one directory cannot merge it with another person's data.

## Roster

`profiles.toml` is the small control plane:

```toml
[profiles.adam]
telegram_id = 111111111
operator = true
import_source = "http"

[profiles.anna]
telegram_id = 222222222

[profiles.tomek]
telegram_id = 333333333
enabled = false
```

| Field | Default | Meaning |
| --- | --- | --- |
| `telegram_id` | required | Stable numeric Telegram user ID and private-chat destination. |
| `operator` | `false` | Grants operator-only coding-agent commands. Exactly one roster entry must be the operator. |
| `enabled` | `true` | Disabled profiles keep their files but are not loaded, scheduled, or routed. |
| `import_source` | `http` | `http`, `google-drive`, or operator-only `local`. |
| `drive_metrics_folder_id` | — | Required for a Drive profile. |
| `drive_workouts_folder_id` | — | Required for a Drive profile. |

Profile names are also directory names and CLI selectors. They may contain
lowercase letters, digits, `_`, and `-` only. Configuration is read at daemon
startup; roster changes require a restart.

Shared infrastructure remains outside the roster:

- `.env` holds the Telegram bot token, provider keys, and optional shared Drive
  service-account path;
- `ingest_tokens.json` holds only HTTP bearer-token hashes;
- the Tailscale Funnel URL and loopback receiver are shared by every HTTP
  profile.

## Runtime routing

One process owns all shared resources:

```text
Telegram updates ── one poller ── sender ID ── profile runtime
HTTP upload       ── one receiver ─ bearer token ─ profile runtime
Scheduler         ── enabled profiles ─────────── profile runtime
```

Each enabled profile gets one `ProfileRuntime` and one single-threaded worker.
Work for the same person is serialized so conversation state, pending edits,
imports, and rate limits cannot race. Different profiles can run concurrently,
so one slow LLM call does not block another person's chat.

A missing database, missing required context file, or invalid Drive setup
disables that runtime and logs the reason. Other healthy profiles continue.

The process-wide daemon owns:

- the lock file and rotating log;
- one Telegram update stream;
- the loopback HTTP receiver;
- the shared filesystem observer;
- the scheduler and per-profile worker executors.

The profile runtime owns:

- database and context paths;
- conversation history and pending Telegram actions;
- import locks and source-specific state;
- reports, nudges, notification/model preferences, and rate limits;
- scheduled report and data-health decisions.

## Telegram authorization

Only linked private chats are routed. For a valid private update,
`message.from.id` and `message.chat.id` must match the profile's numeric
`telegram_id` and each other.

Unknown, disabled, group-chat, and mismatched identities are denied before they
can reach health commands, context, SQL, or callbacks. When possible, an
unknown sender's numeric ID is reported to the operator so that person can be
added deliberately. Usernames and display names are never authorization keys.

Pending callback and context-edit tokens belong to one profile runtime. A
callback routed to another profile cannot apply them. `/codex` and `/claude`
are hard-coded operator-only because they grant write access to the repository
workspace.

## Health-data routing

The import source is selected per profile:

| Source | Profile selection | Storage behavior |
| --- | --- | --- |
| HTTP | Bearer token before the body is read | Latest pair, ingest state, and daily raw archive under that profile. |
| Google Drive | Folder IDs from that roster entry | Per-profile checksum manifest and local cache. |
| Local/iCloud | The sole operator profile | Watches the host's configured Auto Export directory. |

HTTP tokens are not profile names in disguise. The random token is looked up in
the hash-only registry and resolves the destination before payload parsing.
Auto Export metadata headers and JSON fields cannot select another profile.

One read-only Google service account may access folders shared by several
people, but the folder IDs and cache paths remain profile-specific. Local import
is operator-only because another person's iPhone cannot write into the host's
iCloud directory.

## CLI resolution and migrations

Health-data commands accept `--profile NAME` and otherwise use the operator:

```bash
uv run python main.py status --profile anna
uv run python main.py insights --profile anna
uv run python main.py llm-log --profile anna --id 42
```

The selected profile determines the database, context, reports, nudges,
preferences, and Telegram destination. An explicit `--db PATH` is reserved for
experiments, overrides `--profile`, and must already exist. Normal command
resolution never creates an accidental empty database.

Every database carries its own `schema_migrations` table and auto-applies
pending migrations when opened. Operators can inspect or migrate the whole
roster eagerly:

```bash
uv run python main.py db status --all
uv run python main.py db migrate --all
```

LLM call, trace, feedback, and event IDs are local to each database. Adam's
call 42 and Anna's call 42 are unrelated; always choose the profile before
debugging an ID.

## Creation and adoption

Create a new profile with `profile add`. It creates the database and directory
tree, seeds context templates, validates the roster, and writes the roster
entry:

```bash
uv run python main.py profile add anna --telegram-id 222222222
```

`profile adopt` is different. It is the one-time migration path for an older
single-profile installation with root-level `health.db` and `ContextFiles/`:

```bash
uv run python main.py profile adopt adam --dry-run
uv run python main.py daemon-stop
uv run python main.py profile adopt adam
uv run python main.py daemon-restart
```

Adoption copies the legacy database, context, generated outputs, preferences,
state, and configured Drive cache; validates the copy; then writes
`profiles.toml` last. The original files remain untouched as a rollback copy.

## Privacy and operational limits

Isolation prevents accidental cross-profile access inside the application. It
does not hide hosted data from the machine operator. The operator can open each
database, and stored LLM messages contain the assembled health and context
prompt as well as the reply. Explain that before hosting someone.

Provider credentials and billing are shared. There is no per-profile spend cap
or timezone: schedules use the host's timezone. Pausing, renaming, or deleting a
profile is an operator-managed roster/filesystem operation followed by a daemon
restart.
