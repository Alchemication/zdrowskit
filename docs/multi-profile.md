# Multi-Profile Family Hosting

Status: implemented

Implementation note: adoption is an explicit `profile adopt NAME` operation
rather than an automatic daemon-start side effect. The profile name cannot be
inferred safely, and health data is not copied until the operator has reviewed
the required dry-run output.

## Summary

Convert the current single-profile system into one that can host a small number
of independently isolated health profiles under one technical operator. One
zdrowskit daemon and one Telegram bot serve all profiles, while every person's
health data, context, preferences, generated outputs, and runtime state live in
separate directories and SQLite databases.

The target is family/friend hosting for 1–10 people, usually 1–2. It is not a
public multi-tenant service. Every design choice below is sized for a roster
that one person edits in a text editor.

## Scope

### Goals

- Run one daemon process and one Telegram bot for multiple people.
- Identify the profile for every inbound Telegram message and callback.
- Route every outbound message and notification to the correct Telegram chat.
- Keep one health SQLite database per profile.
- Keep context files, Drive cache, reports, nudges, preferences, and daemon
  state isolated per profile.
- Read each person's own Google Drive folders through one shared service
  account.
- Default-deny unknown Telegram identities; the operator links accounts.
- Preserve the current health schema and LLM SQL tooling.
- Allow profiles to fail or be paused independently.
- Adopt the existing single-profile installation without data loss.

### Non-Goals

These are active constraints on the implementation, not a wishlist.

- **No control database.** Configuration is a file.
- **No `profile_id` columns** in the health schema, and no per-person tables
  such as `daily_adam`.
- **No cross-profile queries** or context access, in SQL or anywhere else.
- **No group-chat support.** Private chats only.
- **No self-service.** Profiles are created by the operator.
- **No hot configuration reload.** Edit the file, restart the daemon.
- **No web administration interface.**

## Core Decisions

### One health database per profile

Each profile keeps the existing schema in its own SQLite file:

```text
profiles/adam/health.db
profiles/anna/health.db
```

Separate database files:

- prevent accidental cross-profile queries;
- avoid changing primary keys such as `daily.date` and `workout.start_utc`;
- preserve the existing parser, store, report, and migration paths;
- ensure the LLM `run_sql` tool can only see the selected profile
  (`execute_tool` already takes an explicit `db_path`);
- make backup, recovery, and deletion profile-scoped.

### Configuration is one file, not a control database

The roster is at most ten rows, edited by one person, never written
concurrently. A SQLite control database for that would require its own migration
set, its own repositories, and its own schema evolution — all to store what fits
in twenty lines of TOML.

Use `profiles.toml`. Python is already pinned to `>=3.12`, so `tomllib` is
stdlib and this adds no dependency.

Shared infrastructure stays in `.env` as it is today — bot token, provider keys,
service-account path, global limits. **Do not put per-profile settings in
environment variables.** Five profiles times five fields is twenty-five flat
variables with no structure; that is worse than either alternative.

### One Telegram poller, per-profile senders

Only one component consumes the bot token's update stream. Telegram exposes one
update stream per bot, so competing consumers can consume and discard each
other's updates.

The existing `TelegramPoller` class is misnamed: it polls *and* sends, and is
constructed with a single `chat_id`. Split it:

- **`TelegramPoller`** — keeps the name, loses `chat_id` and the send methods.
  Process-wide. Owns `get_updates`, the update offset, the long-poll loop, and
  the handler thread pool.
- **`TelegramSender`** — new, one instance per profile, constructed with that
  profile's chat ID. Takes over the existing chat-bound send/edit/photo/typing
  API unchanged.

This preserves every call site in `daemon_telegram_chat.py`, which already goes
through a bot instance. Threading an explicit `chat_id` argument through every
send call would achieve the same isolation with a far larger diff.

The pre-change footgun was `notify.py` reading `TELEGRAM_CHAT_ID` from the
environment inside `send_telegram` and `_get_telegram_creds`. The implemented
send path now receives the routed destination explicitly.

### Reuse the daemon class as the profile runtime

`ZdrowskitDaemon` is already a profile runtime. It takes `db`, `context_dir`,
health dir, and Drive configuration as constructor arguments, and owns
`_import_lock`, `_state`, `_notification_prefs_path`, pending rejection and
feedback reason maps, `_self_originated_writes`, and every flow handler
(`AddFlowHandler(self)`, `TelegramChatHandler(self)`, …).

Rename it to `ProfileRuntime`, instantiate one per enabled profile, and extract
the process-wide concerns into a new, small `Daemon` class. Do not build a
parallel abstraction and migrate state into it.

`Daemon` (process-wide) owns:

- lock file acquisition and logging setup;
- one shared watchdog `Observer`, with `schedule()` called per profile;
- the `TelegramPoller` and update dispatch;
- the scheduled-check loop, iterating profiles;
- one single-threaded worker per profile.

`ProfileRuntime` (per profile) keeps everything else: health DB path, context
dir, Drive source, conversation buffer, pending context-edit proposals, `/add`
`/notify` `/models` flows, feedback and rejection prompts, agent session
pointers, nudge counters and suppression, preferences, Drive poll state, and
schedules.

The one real refactor here: `_load_state` and `_save_state` are module-level
functions closing over the global `STATE_FILE`. They become instance methods
reading `profiles/<name>/daemon_state.json`.

Flow handlers need no signature changes — they already reach through `self._d`.

### One identifier per profile

The profile name is the `profiles.toml` table key, the directory name, and the
identifier used in CLI arguments and logs. There is no separate immutable ID and
mutable slug.

That pattern exists for systems where users rename themselves and referential
integrity must survive it. Here, renaming is `mv profiles/anna profiles/ania`
plus one line in a text file. Validate the name against `^[a-z0-9_-]+$` so it is
always filesystem-safe.

### One Telegram number per profile

In private chats Telegram sets `chat.id` equal to the sender's `from.id`. Since
group chats are rejected, storing both is storing the same number twice and
inviting "which one do I use here" bugs.

Store one `telegram_id`. Continue to authorize on `from.id` and send to
`chat.id`, and assert they match when routing; a mismatch means the update is
not the private message it claims to be.

### Operator-linked identities, default deny

Today's entire authorization is one comparison inside `_poll_once`:
`msg_chat_id != self._chat_id` for messages, and the equivalent for callbacks.
That filter is what routing replaces — it cannot be removed ahead of routing
landing, or the bot becomes open to anyone who finds it.

Afterwards, an unknown Telegram user reaches nothing. The router logs the
numeric ID and notifies the operator: *unknown user 12345 (@name) messaged*. The
operator adds a section to `profiles.toml` and restarts the daemon.

Authorization always uses stable numeric Telegram IDs, never usernames.

### Drive-only for non-operator profiles

`import_source: local` watches the operator's iCloud Auto Export directory. No
other person's phone writes there. Non-operator profiles are Google Drive only;
the local watchdog path remains operator-only. This keeps the shared `Observer`
simple and removes a class of unusable configuration.

## Architecture

```text
                    Adam's Drive        Anna's Drive
                    (his folders)       (her folders)
                          └──────┬──────────┘
                    one read-only service account
                                 │
┌────────────────────────────────▼─────────────────────────────────────┐
│                              Daemon                                  │
│   lock file · logging · shared Observer · scheduler · per-profile workers │
│                                                                      │
│   TelegramPoller ──► resolve_profile() ◄── profiles.toml             │
│                            │                                         │
│              ┌─────────────┴─────────────┐                           │
│      Adam ProfileRuntime          Anna ProfileRuntime                │
│      + TelegramSender(chat)       + TelegramSender(chat)             │
│      - db path, context dir       - db path, context dir             │
│      - conversation, pending      - conversation, pending            │
│      - Drive poll state           - Drive poll state                 │
│      - prefs, schedules           - prefs, schedules                 │
└──────────────┼────────────────────────────┼──────────────────────────┘
               │                            │
       profiles/adam/               profiles/anna/
```

## Filesystem Layout

```text
~/Documents/zdrowskit/
  profiles.toml                     # the roster
  .daemon.lock                      # process-wide
  profiles/
    <name>/
      health.db
      ContextFiles/
        me.md
        strategy.md
        log.md
        history.md
        coach_feedback.md
        baselines.md
      Imports/
        google-drive/
          Metrics/
          Workouts/
          .drive-fetch-manifest.json
      Reports/
      Nudges/
      notification_prefs.json
      model_prefs.json
      daemon_state.json
```

Shared secrets stay outside profile directories and outside `profiles.toml`:
the Telegram bot token, LLM provider API keys, and the Google Drive
service-account JSON all remain in `.env`.

## Profile Configuration File

```toml
# ~/Documents/zdrowskit/profiles.toml

[profiles.adam]
telegram_id = 111111111
operator = true
import_source = "local"          # operator only; everyone else is Drive

[profiles.anna]
telegram_id = 222222222
drive_metrics_folder_id = "1AbC..."
drive_workouts_folder_id = "1XyZ..."

[profiles.tomek]
telegram_id = 333333333
enabled = false                  # keeps data, stops all activity
```

Field notes:

- `enabled` defaults to `true`. A disabled profile is not instantiated, is not
  polled, and is not routed to. This replaces a status enum; there is no
  persisted `error` state, because runtime degradation belongs in logs, not in
  configuration that would go stale across restarts.
- `operator` defaults to `false`. Exactly one profile may set it; loading fails
  if zero or more than one does.
- `import_source` defaults to `google-drive` and may only be `local` for the
  operator profile.
- No timestamps. Filesystem mtime and the file's git history cover it.

Everything runs in the host's system timezone. Per-profile timezones are not
supported.

## Google Drive Access Model

Each person's Auto Export writes to **their own** Google Drive, under their own
Google account. One shared read-only service account reads all of them.

This works because a service-account email address (ending in
`.iam.gserviceaccount.com`) is a valid Drive sharing principal. It requires no
Workspace, no domain-wide delegation, and no impersonation. The existing
`_access_token` builds plain service-account credentials scoped to
`drive.readonly`, which is exactly the right shape already.

Per-person setup:

1. The person configures Auto Export as documented in `docs/google-drive.md`,
   writing to folders in their own Drive.
2. They open their `Health Auto Export` root folder in Drive, choose **Share**,
   paste the service-account email, and grant **Viewer**.
3. They **uncheck "Notify people"**. Service accounts have no mailbox and the
   notification will fail or bounce.
4. Google may warn that the address is not a Google account, or fail to
   autocomplete it. Share anyway; it resolves correctly.
5. Sharing the root grants inherited read access to the `Metrics` and `Workouts`
   subfolders, so this is one share, not two.
6. They send the operator the URL of the shared root folder. The operator opens
   the two subfolders and copies each ID from the address bar into
   `profiles.toml`.

Properties worth knowing:

- **Files stay owned by the person** and count against *their* Drive quota. The
  service account owns nothing and needs no quota of its own.
- **Folder IDs are globally unique** across all of Drive, so Anna's `Metrics`
  and Adam's `Metrics` never collide. The existing fetch code takes folder IDs
  directly and needs no change to support multiple people.
- **Revocation belongs to the person.** Anna un-shares the folder and access
  stops immediately, without involving the operator. That is a genuinely good
  property for hosting family: consent stays with the data owner.
- One service account can hold an unbounded number of such shares.

The companion [Google Drive guide](google-drive.md) uses this sharing model:
each person shares their own root with the shared service account.

## Migrations

There is one migration set and it is unchanged. `src/db/migrations/` continues
to serve `profiles/*/health.db`.

Each database file already carries its own `schema_migrations` table, so applied
state is correctly per-file. `connect_db(path, migrate=True)` auto-applies on
open, so every profile database migrates lazily the first time it is opened
after a deploy.

The only change required: add `db migrate --all` and `db status --all` to fan
out across every profile. Lazy migration alone means a broken migration surfaces
one profile at a time, whenever that person next chats — the operator wants it
eagerly at deploy. A disabled profile's database is never opened, so its
migrations lag until re-enabled; `db status --all` should make that visible
rather than surprising.

Schema changes continue to go in new timestamped migration files, with no
ad-hoc `ALTER TABLE` or schema-patching in application code.

## Runtime Objects

Keep the object count low. The whole feature needs four new or renamed types
plus two functions.

```python
@dataclass(frozen=True)
class Profile:
    name: str
    telegram_id: int
    root: Path
    operator: bool = False
    enabled: bool = True
    import_source: str = "google-drive"
    drive_metrics_folder_id: str | None = None
    drive_workouts_folder_id: str | None = None

    @property
    def db(self) -> Path:
        return self.root / "health.db"

    @property
    def context(self) -> Path:
        return self.root / "ContextFiles"

    # reports, nudges, drive_cache, state, notification_prefs, model_prefs
    # follow the same one-line pattern.
```

Paths are derived from `root` by fixed convention, so they are properties rather
than nine stored fields and a separate `ProfilePaths` class. Drive folder IDs
are plain optional fields rather than a nested `DriveSource` object — they carry
no behaviour.

| Type or function | Origin |
| --- | --- |
| `Profile` | new dataclass, above |
| `ProfileRuntime` | renamed `ZdrowskitDaemon` |
| `Daemon` | new, small, process-wide |
| `TelegramSender` | extracted from `TelegramPoller` |
| `load_profiles() -> dict[str, Profile]` | function, not a registry class |
| `resolve_profile(update, profiles) -> Profile \| None` | function, not a router class |

A registry and a router that each expose one lookup are ceremony; module-level
functions over a plain dict are clearer and just as testable.

## CLI Profile Resolution

This was load-bearing, not cleanup. Before this feature,
`default_db_path()` returned `APP_HOME / "health.db"`, and `main.py` baked it
into the `--db` default for `import`, `report`, `status`, `db`, `insights`,
`nudge`, `coach`, `llm-log`, and `events`. Adoption copies that file into
`profiles/<name>/health.db`; the profile copy becomes authoritative while the
old path remains only for rollback.

`connect_db` calls `path.parent.mkdir(parents=True, exist_ok=True)` and then
connects, which **creates and migrates a fresh empty database** at any missing
path. Keeping the old default would continue reading and writing the stale
rollback database after adoption. If it were later removed, commands would
silently create a new empty database. Both failures produce a wrong answer
rather than an actionable error.

Requirements:

- Every health-data command takes `--profile NAME`, defaulting to the operator
  profile, and resolves its database through the roster.
- An explicit `--db` still wins, for local experiments and tests.
- The CLI must refuse to open a health database that does not already exist,
  rather than creating one. Creation happens only in `profile add` and adoption.
- Resolution failure is an actionable error naming the roster file, never a
  fallback to an arbitrary profile.

## LLM Logging and Traces

`llm_call`, `llm_trace`, `llm_feedback`, and `events` all live in the health
database. Nothing about them changes.

**No profile identity is added to any row.** The database file is the
attribution: a row in `profiles/anna/health.db` is Anna's by construction. Adding
a profile column would be redundant and would contradict the no-`profile_id`
constraint that keeps the health schema untouched.

Consequences for the existing debugging workflow:

- `llm-log --id N`, `events`, and the feedback-triage flow become
  profile-scoped, and their autoincrement IDs collide across profiles — Anna's
  call 42 and Adam's call 42 are different calls. The `--profile` flag from CLI
  Profile Resolution covers all of them.
- Cross-profile analytics, if ever wanted, means opening each database in turn
  and aggregating in Python. Over at most ten files that is a loop, not a schema
  change. V0 needs none of it.

Privacy note, stated because it is easy to miss: `llm_call.messages_json` holds
the fully assembled prompt — health data plus the contents of that person's
context files — and `response_text` holds the reply. Each profile's database
therefore contains their most sensitive material, and the operator can read any
of it with `llm-log --profile anna`. That is inherent to hosting someone else's
instance, but it should be a known property rather than a surprise, and it is
worth telling people whose profiles you host.

No per-profile spend or call cap in V0. The operator pays for everyone from one
shared set of provider keys, and nothing bounds inbound chat volume, so a
runaway profile is a real if unlikely bill. Mitigation stays manual: the cost
data is already in each `llm_call` table, so a cap is a single `COUNT` query
against existing storage whenever it becomes worth adding.

## Telegram Identity and Routing

For private messages:

- `message.chat.type` must be `private`;
- `message.from.id` is the account identity used for authorization;
- `message.chat.id` is the outbound destination, and must equal `from.id`;
- usernames and display names are informational only.

For callback queries, resolve the profile from the callback message's chat and
validate that the pending callback token belongs to that same profile.

```python
def handle_update(update: dict) -> None:
    profile = resolve_profile(update, profiles)

    if profile is None:
        reject_unknown(update)   # log + notify operator, no data access
        return

    runtimes[profile.name].handle(update)
```

Unknown users receive either a minimal access-denied message or no response.
They must never reach health commands, LLM tools, agent commands, or profile
management. A notification for one profile must never fall back to a global or
default chat.

Operator-only commands, hardcoded rather than configurable:

- profile management and any cross-profile view;
- `/codex` and `/claude`, which grant repository write access.

`/codex` and `/claude` currently have **no authorization whatsoever** — they are
dispatched straight from message text, safe today only because the chat filter
admits exactly one person. Gating them is therefore not deferrable work: it must
land in the same increment that admits a second identity.

## Scheduling and Drive Polling

One `Daemon`-owned scheduler manages all enabled profiles:

- keep report cadence, nudge limits, and suppression state per profile;
- poll Drive independently per profile, with isolated retry/backoff and error
  streaks;
- prevent one slow Drive or LLM request from blocking other profiles;
- serialize chat turns per profile so two rapid messages cannot race
  conversation or proposal state, while allowing different profiles to run
  concurrently.

Give each profile its own single-threaded worker, not an unbounded thread per
operation and not a shared pool guarded by per-profile locks. A shared pool
serializes the whole roster as soon as one person's turns saturate it: workers
block on that profile's lock and everyone else's messages queue behind them.
One worker per profile serializes each profile's own mutable work without a
lock and keeps profiles genuinely independent. Cap the queued chat turns per
profile so a single person cannot outrun their own replies; shed the excess
with a visible notice.

Failure in one profile must not terminate polling or scheduled work for any
other; surface degraded status through logs and `status`.

Each person's `daemon status` over Telegram is served by their own runtime and
therefore already shows their own profile. Every profile-owned log event carries
the profile name. Do not add health or message content to logs merely to ease
multi-profile debugging.

## CLI and Operations

Existing health-data commands gain `--profile NAME` as described in CLI Profile
Resolution. Profile creation supports local or Drive-backed imports:

```text
profile add NAME --telegram-id ID [--operator] [--source local|google-drive]
  [--google-drive-metrics-folder-id ID]
  [--google-drive-workouts-folder-id ID]
```

It appends a section to `profiles.toml`, creates the directory tree, initialises
`health.db`, and copies `me.md` / `strategy.md` templates. The operator fills
them in with the person; refinement afterwards goes through the existing
context-edit proposal flow.

Everything else is file manipulation and needs no command:

- **Pause a profile:** set `enabled = false`, restart.
- **Rename:** `mv` the directory, edit the roster key, restart.
- **Back up:** copy `profiles/<name>/` and `profiles.toml`.
- **Delete:** remove the directory and the roster section, restart.

## Adoption from the Existing Installation

Adoption is an explicit one-time operator action, never a daemon startup side
effect:

1. Preview with `profile adopt NAME --dry-run`.
2. Stop the old daemon.
3. Run `profile adopt NAME`, optionally passing `--telegram-id ID`.
4. Copy legacy state into `profiles/<name>/` and validate database, context,
   and cache paths.
5. Write `profiles.toml` last. Legacy sources remain as a rollback copy.
6. Restart the multi-profile daemon and verify the profile-specific paths.

Adoption must:

- preserve the original after validation as an explicit rollback copy;
- refuse to overwrite an existing profile directory;
- verify the health database opens and migrations apply;
- verify required context files exist;
- report every source and destination path;
- provide a dry-run mode;
- document rollback.

No compatibility re-export shims. Once profile paths exist, update all callers
to receive the correct configuration explicitly.

## Security and Privacy

- Default-deny unknown Telegram identities; the operator adds them explicitly.
- Private chats only; reject any update where `chat.id != from.id`.
- Authorize on stable numeric Telegram IDs, never usernames.
- Validate callback tokens against the routed profile.
- Never accept a profile name or database path from Telegram input; resolve
  paths only from `profiles.toml` entries.
- Validate profile names against `^[a-z0-9_-]+$` before using them in paths.
- Keep provider keys and the service-account JSON out of `profiles.toml` and
  out of profile directories.
- Restrict profile directory filesystem permissions.
- Do not expose other profiles' names or status to non-operator users.
- No cross-profile SQL or context access.
- Validate Drive folder-ID format before storing.
- Drive access is revocable by the data owner, not only by the operator.

## Increments

Two groups: behaviour-preserving groundwork that can land any time, and one
atomic core that cannot be subdivided.

### Groundwork — ships any time, in any order

Each is separately reviewable, separately revertable, and changes no observable
behaviour. Their only purpose is to shrink the core commit.

| Increment | Independent because |
| --- | --- |
| Remove ambient `TELEGRAM_CHAT_ID` from `notify.py` | Pure cleanup — pass the destination explicitly from the one existing caller. |
| Split `TelegramPoller` into poller + `TelegramSender` | One poller, one sender bound to the same chat. Identical behaviour, and the chat filter stays put. |
| `db migrate --all` / `db status --all` | Degenerates to the single existing database until there are more. |

### Core — atomic

The smallest set that makes a second person work. None of it ships alone,
because each piece is load-bearing for the others.

1. `profiles.toml`, the `Profile` dataclass, `load_profiles()`, and name
   validation.
2. Adoption of the existing installation into `profiles/<name>/`, with a
   dry-run mode.
3. `ProfileRuntime` (renamed `ZdrowskitDaemon`) instantiated per profile, with
   the new `Daemon` owning lock, logging, `Observer`, poller, and scheduler.
   Imports, context watching, schedules, preferences, and LLM commands become
   profile-bound, receiving paths explicitly. Bounded concurrency, per-profile
   serialization, and failure isolation land here.
4. Per-profile `daemon_state.json` — `_load_state` / `_save_state` become
   instance methods.
5. CLI profile resolution and the no-implicit-create guard.
6. `resolve_profile()` replacing the poller's chat filter **in the same commit**
   as its removal. Pending callback and context-edit proposal tokens become
   profile-scoped.
7. Operator gating on `/codex`, `/claude`, and profile management.
8. `profile add`.
9. Documentation: family hosting setup, `CLAUDE.md`, `README.md`,
   `docs/commands.md`, the multi-person sharing model in `docs/google-drive.md`,
   and the now-stale single-profile notes in `docs/limitations.md`.

Why these cannot be separated:

- **Routing and filter removal** are one change. Removing the filter first opens
  the bot to the world.
- **Operator gating** must land with routing. Admitting a second identity
  without it hands that person repository write access.
- **Routing needs somewhere to dispatch to**, so runtimes must already be
  roster-instantiated; runtimes need profile-shaped paths, so adoption must
  precede them.
- **CLI resolution must land with adoption.** The moment the profile copy
  becomes authoritative, the old default path becomes unsafe.

Adoption (item 2) carries the real blast radius — it copies live health data
and changes which paths the application treats as authoritative. Keep it
behind a dry-run, and validate before deleting the original.

## Testing Strategy

Unit tests:

- `profiles.toml` parsing: defaults, missing fields, zero or multiple operators,
  malformed names, `local` import source on a non-operator profile.
- Profile path derivation and traversal prevention.
- Telegram identity extraction, private-chat enforcement, and
  `chat.id == from.id` assertion.
- Default-deny routing for unknown and disabled profiles.
- Profile-scoped callback token validation.
- CLI profile resolution, including the refusal to create a missing database.

Integration tests:

- Two profiles with different databases return different SQL results.
- An Adam message cannot read Anna's database or context.
- Notifications for each profile use the correct chat ID.
- One update stream routes interleaved messages correctly.
- Simultaneous profile work does not mix conversation buffers.
- Drive imports use the correct folder IDs, cache, and database.
- One profile's Drive or LLM failure does not stop the other.
- Daemon restart restores both profiles' persistent state.
- `db migrate --all` migrates every profile database.
- Existing single-profile data adopts without loss.

Security regression tests:

- Unknown users cannot invoke commands.
- Usernames cannot impersonate a linked identity.
- Group messages are rejected.
- Cross-profile callback tokens fail.
- Non-operators cannot invoke profile management or coding agents.
- No runtime path can be selected from untrusted Telegram text.

Run full lint and tests after each increment:

```bash
uv run ruff check .
uv run ruff format .
uv run pytest
```

## Acceptance Criteria

The first release is ready when:

- Adam and Anna use the same bot in separate private chats.
- One daemon process polls and routes all Telegram updates.
- Each profile has an independent health database and profile directory.
- Each profile imports only its own Drive folders, shared from its own Google
  account.
- Chat and `run_sql` operate only on the routed profile's database.
- Scheduled reports and nudges reach only the correct chat.
- Conversation, preferences, pending actions, and rate limits stay isolated.
- CLI commands resolve to one profile and never create an empty database.
- The operator can add a profile with one command.
- Unknown users and cross-profile callback attempts are rejected.
- One degraded profile does not stop the other.
- The original single-profile installation adopts without data loss.

## Resolved Decisions

- **No control database.** `profiles.toml` parsed with stdlib `tomllib`.
- **No per-profile environment variables.** Shared infrastructure stays in
  `.env`; per-profile settings live in the roster file.
- **One identifier**, not an ID/slug pair.
- **One `telegram_id`**, not a user/chat pair, because private chats make them
  equal.
- **Operator identity** is `operator = true` in the roster.
- **Drive folder IDs** are operator-collected; each person shares their own
  Drive root with the service account.
- **Preferences stay as per-profile JSON files.** Their loaders already take a
  `path` argument, so this costs nothing.
- **Provider keys:** one shared set, with no per-profile spend cap in V0.
- **LLM logs carry no profile identity;** the database file is the attribution.
- **Coding-agent commands:** operator-only, hardcoded, not configurable.
- **Host system timezone** for everyone.
- **Lifecycle is file manipulation:** pause, rename, back up, and delete are all
  edits to the roster and the profile directory.
