# Notifications

Insights, memory, coach, nudges, and chat are separate LLM calls with their own
prompts, context, tools, and purposes. Sync alerts are deterministic checks and
do not call an LLM.

| Channel | Purpose | Trigger | Frequency | Length | Tools | Special output |
|---------|---------|---------|-----------|--------|-------|----------------|
| **Insights** | Full weekly report | Scheduled, default Monday 10:00, or manual `/review` | Weekly when scheduled; manual on demand | ≤ 1024 chars | `run_sql` | Exactly 1 `<chart>`, skipped when it would mislead |
| **Memory** | Decides what carries forward from an insights report | After every scheduled or manual insights report | Same as insights | ≤ 2 bullets | none | Extracted bullets stored under the week in `history.md`; never sent to the user |
| **Coach** | Strategy review, only when proposals exist | After scheduled insights, or manual `/coach` | Weekly when scheduled; manual on demand | ≤ 300 words | `run_sql`, `update_context` for `strategy` only | `SKIP` if no changes warranted; bundled message with inline Accept/Reject buttons per edit |
| **Nudge** | Short reactive next-action nudge | Data sync, file edit | Up to 2/day by default | 80 words | `run_sql` | `SKIP` if nothing changes; text only, no chart |
| **Sync alerts** | Warns when an HTTP profile's metrics stop updating or its imports stall | Checked every 30 minutes | At most 1/day per unchanged condition | Short deterministic message | none | Pipe-fault all-clear; unresolved dates named after stale recovery |
| **Chat** | Interactive conversation: answer the current message, ask anything, get charts | Your Telegram message | On demand | < 150 words unless you ask for detail | `run_sql`, `update_context` for `me`, `strategy`, or `log` | Optional `<chart>`; context edits require confirmation by default |

## Notification Preferences Via Telegram

Use `/notify` in Telegram to inspect and change notification behavior without editing files by hand.

CLI equivalents:

```bash
uv run python main.py notify
uv run python main.py notify --profile anna
uv run python main.py notify reset all
uv run python main.py notify reset nudges
```

Examples:

- `/notify`
- `/notify no nudges before 11am`
- `/notify send weekly insights on Tuesday at 8`
- `/notify mute sync alerts for a week`
- `/notify only warn me about sync after two days`
- `/notify mute nudges today`
- `/notify bring weekly insights back to default`
- `/notify set all as default`

How it works:

- A small LLM interprets the request into a strict structured proposal.
- The bot shows the interpreted change back to you with `Accept` / `Reject`.
- Telegram changes are not saved until you tap `Accept`. CLI resets write
  immediately.
- If the request is ambiguous, the bot asks a short clarification question.
- By default, preferences live in
  `~/Documents/zdrowskit/profiles/<name>/notification_prefs.json`. Setting
  `ZDROWSKIT_HOME` changes that root.
- CLI commands default to the operator profile; use `--profile NAME` for
  another person.

What can be changed:

- nudges on/off
- nudge earliest send time
- maximum nudges per day (1–6)
- weekly insights on/off, weekday, and time
- sync alerts on/off, missing-metric days, and stalled-import hours
- temporary mutes for all notifications or one notification type
- reset one setting or everything back to built-in defaults

## What Triggers Nudge Attempts

Each event below can start a nudge LLM call. Delivery preferences, report
suppression, rate limits, and the LLM's own `SKIP` decision can still prevent a
message from being sent.

| Event | Debounce | What it does |
|-------|----------|-------------|
| Metrics + Workouts received over HTTP | Wait for the matching pair | Reacts to the imported data |
| Health data fetched from Google Drive | Poll interval (5 min default) | Reacts when changed files are parsed |
| Health data synced via iCloud | 3 min debounce | Imports the settled files, then reacts |
| `log.md` / `strategy.md` / `me.md` edited | 60 sec | Acknowledges the change, flags tension, or confirms it |

## Cross-Message Awareness

The coaching and content LLMs share enough recent output to avoid redundancy:

- **Insights, Coach, Nudge, and Chat** read the rolling `history.md` memory.
- **Memory** reads the existing history so it does not store the same thread
  twice.
- **Coach** sees recent nudges sent.
- **Nudge** sees last 3 nudges + last coach review summary.
- **Chat** sees last 3 nudges + last coach review summary.
- **Insights** does not see the transient nudge or coach-summary state; it uses
  `history.md` for continuity.

## Suppression and Rate Limiting

- **Earliest nudge time:** nudges are deferred until the configured earliest send time. Triggers queue and drain as one consolidated nudge once the window opens.
- **Temporary mute / disable:** for LLM notifications, the daemon skips the LLM
  call entirely. A suppressed sync condition is still assessed and logged but
  is not delivered.
- **Report suppression:** nudges are suppressed +/- 1 hour around scheduled reports because the report already covers the big picture.
- **Rate limits:** max 2 nudges/day by default, min 3 hours apart.
- **LLM SKIP:** the nudge LLM can respond `SKIP` if there is nothing genuinely new to say.
- **Coach:** the scheduled path runs at most once per calendar day. Manual
  `/coach` calls can rerun it on demand.
- **No replay after mute:** skipped nudges/reports are not replayed after a temporary mute expires.

## Sync Alerts

Sync alerts currently monitor profiles whose import source is HTTP. A phone that
stops feeding an HTTP profile is otherwise completely silent: uploads just stop,
or stop pairing, and the only trace is a line in the daemon log. For the operator
that is survivable. For a hosted profile it is fatal — their data quietly goes
stale and they conclude the product is dead. Local iCloud and Google Drive
profiles do not currently run these health checks.

Three conditions are reported, checked in order of how specific they are about
the cause:

| Condition | Default | Meaning |
|-----------|---------|---------|
| `error` | 6h stalled; immediate for unreadable state | The last import failed and none has succeeded while the pipe is stalled, or the ingest state file cannot be read. |
| `split` | 6h | Uploads *are* arriving but nothing imports. A strong signal — the phone is demonstrably reachable. The message distinguishes the two causes: halves further apart than the one-hour pairing window means two automations whose schedules have drifted, and anything closer than that means the phone is fine and the import on this end is stuck. |
| `stale` | 2 days | The pipe looks fine but two completed days of daily metrics are missing. The catch-all: phone off, token revoked, Funnel down, app deleted, parser rejecting every payload. |

A profile that has never uploaded is never alerted: it is mid-setup, not broken.
Once a pair has imported, a profile for which no daily metrics have ever been
stored alerts on the next scheduler check instead of waiting two days, because
that points to a parser or payload problem rather than ordinary delivery lag.

### Why staleness is counted in days of data, not hours of silence

Auto Export uploads in unpredictable bursts. In the retained sample from one
real HTTP profile, 70 complete pairs had a 1.7h median gap, a 10.1h p90 and a
34.9h maximum — so an hours-based silence threshold low enough to catch a dead
phone also fires on the tail of ordinary operation. A long gap can still land as
a complete backfill, while a short gap can import successfully but omit a day.

So the check asks the database, not just the upload log: what is the newest
completed date that actually holds a daily metric? A `daily` row is created as
soon as an import sees the date, including for a workout-only day, so rows alone
prove nothing — the query ignores any row whose daily metrics are all null.
Today is excluded even if it already has a partial metric, because an incomplete
today must not hide a missing yesterday.

The default is two missing days. In the retained eight-day arrival sample, the
first Metrics upload arrived after 10:00 twice — at 12:40 and 20:03 — and both
gaps backfilled normally. A one-day default would have reported those routine
delays. The observed Metrics payloads carried a rolling 7–8 days, so two days of
grace still leaves several days in which a delayed upload can refill the gap.
Workouts carried a shorter window, but workout absence is not inferable from a
day with no workout; stalled or unpaired Workouts uploads are covered by the
`split` and `error` checks instead.

Only the newest completed edge is used for the ongoing alert. Old interior holes
do not keep producing notifications, but recovery checks every date in the range
named by the original alert rather than assuming that a newer maximum filled the
whole gap.

Today never counts as missing, since it is still in progress. At 10:00 local
(`DATA_HEALTH_STALE_CUTOFF_HOUR`) yesterday joins the completed-day count. The
two-day threshold absorbs the observed same-day arrival variance. This cutoff is
independent from `SLEEP_SYNC_CUTOFF_HOUR`: full Metrics payloads and individual
sleep nights have different arrival behavior and can evolve separately.

### Recovery notices

Each condition is reported once and then not again for 24 hours while it
persists. What is sent when it clears depends on what the outage cost:

- **A stalled pipe** (`split`, `error`) always gets its all-clear. You were told
  to go fix something; the confirmation answers an action you took.
- **A stale gap that backfilled** sends nothing. Auto Export catching up on the
  days it missed is the normal ending, and announcing it only doubles the
  message count for the fault.
- **A stale gap that stayed a gap** names the alerted dates whose daily metrics
  are still absent. Reports covering them may be incomplete; a later rolling
  Metrics upload can still backfill them.

Sync alerts are held during quiet hours (22:00–08:00 local) and released at
08:00, rather than waking anyone at 2am. A stalled import is never fixable while
you are asleep — the fault persists until you act either way — so an overnight
alert is pure noise. The alert is *held*, not dropped: the daemon keeps
re-checking and delivers the first one after the window closes. Recovery
notices are held the same way, so a fault you heard about in the evening that
clears at 3am still gets its all-clear in the morning. A fault that both begins
and resolves overnight is never sent at all.

Alerts go to the affected profile, not to the operator, because the person who
can fix it is the one holding the phone. They obey the same mute and disable
machinery as everything else (`data_health`), and when suppressed the condition
is still written to the daemon log at WARNING.

For bot setup and interactive chat commands, see [Telegram](telegram.md).
