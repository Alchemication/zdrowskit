# Notifications

Insights, memory, coach, nudges, and chat are separate LLM calls with their own
prompts, context, tools, and purposes. Sync alerts are deterministic checks and
do not call an LLM.

| Channel | Purpose | Trigger | Frequency | Length | Tools | Special output |
|---------|---------|---------|-----------|--------|-------|----------------|
| **Insights** | Full weekly report | Scheduled, default Monday 10:00, or manual `/review` | Weekly when scheduled; manual on demand | A single Telegram message | `run_sql` | Exactly 1 `<chart>`, skipped when it would mislead |
| **Memory** | Decides what carries forward from an insights report | After every scheduled or manual insights report | Same as insights | A couple of bullets | none | Extracted bullets stored under the week in `history.md`; never sent to the user |
| **Coach** | Strategy review, only when proposals exist | After scheduled insights, or manual `/coach` | Weekly when scheduled; manual on demand | A few paragraphs | `run_sql`, `update_context` for `strategy` only | `SKIP` if no changes warranted; bundled message with inline Accept/Reject buttons per edit |
| **Nudge** | Short reactive next-action nudge | Data sync, file edit | Up to 2/day by default | One or two sentences | `run_sql` | `SKIP` if nothing changes; text only, no chart |
| **Sync alerts** | Warns when an HTTP profile's metrics stop updating or its imports stall | Checked on the scheduler tick | At most 1/day per unchanged condition | Short deterministic message | none | Pipe-fault all-clear; unresolved dates named after stale recovery |
| **Chat** | Interactive conversation: answer the current message, ask anything, get charts | Your Telegram message | On demand | Brief unless you ask for detail | `run_sql`, `update_context` for `me`, `strategy`, or `log` | Optional `<chart>`; context edits require confirmation by default |

Exact length ceilings live with the thing that enforces them: the visible-report
limit in `src/config.py`, the per-channel word counts in the prompts under
`src/prompts/`.

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

Five conditions are reported. `node` and `funnel` are settled first whenever
uploads have gone quiet, because silence invalidates the premise the
upload-timing conditions rest on — that the phone is demonstrably reaching us.
`node` precedes `funnel` because a disconnected Mac produces an identical
missing DNS record from a cause that is repairable in seconds. The rest follow
in order of how specific they are about the cause:

| Condition | Default | Meaning |
|-----------|---------|---------|
| `node` | 3h of silence | This Mac lost its Tailscale connection, so Tailscale stopped publishing the address phones upload to. **Operator only.** Repairable here: the daemon restarts Tailscale once per outage and reports whether the node actually came back. See [HTTP ingest](http-ingest.md#the-address-stops-resolving). |
| `funnel` | 3h of silence | Tailscale stopped publishing that address for a Mac that is still connected — checked first, so this condition means the local side is verified healthy. **Operator only** — one Funnel serves every profile and nobody else can act on it. Past outages cleared themselves in 26-35 hours. See [HTTP ingest](http-ingest.md#the-address-stops-resolving). |
| `error` | 6h stalled; immediate for unreadable state | The last import failed and none has succeeded while the pipe is stalled, or the ingest state file cannot be read. |
| `split` | 6h | Uploads arrived and nothing imports. A strong signal while the phone is demonstrably reachable. Because a half now imports on its own once the pairing window lapses, a stall this long is a fault on this end whatever the arrival gap was; the message names the gap but never sends you after the automation schedules. Counted from when the un-imported upload landed, and never reported before the pair's import deadline — an overnight gap is hours with nothing to import, not hours of failing to import. |
| `stale` | 2 days | The pipe looks fine but two completed days of daily metrics are missing. The catch-all: phone off, token revoked, app deleted, parser rejecting every payload. |

This ordering is what keeps one fault from hiding another. A stalled pair cannot
clear itself while nothing can arrive, so before it an outage that began during
a stall stayed permanently mislabelled as `split`, and the 48-hour escalation
that hangs off `funnel` never ran. The same logic separates `node` from
`funnel`: on 2026-08-29 a disconnected Mac was reported as an outage to wait
out for 55 hours, when restarting Tailscale fixed it in twelve seconds.

### What a repair message may claim

A repair is only ever described by what the daemon watched happen. After
restarting Tailscale it polls the node's own connection state and says either
that it came back or that it did not — it never infers success from a command's
exit code, and never from whatever recovered afterwards. The recovery notice
records what was attempted and whether it was verified, and says outright when
a recovery is not attributable to it.

That rule exists because the previous version wrote "Funnel DNS resolves again;
1.0h after the re-assert" for a recovery its own re-assert had nothing to do
with — the fix was a hand-run app restart the daemon never saw. Crediting
whichever command ran last is how a repair that has never worked stayed in the
documentation for weeks.

A profile that has never uploaded is never alerted: it is mid-setup, not broken.
Once a pair has imported, a profile for which no daily metrics have ever been
stored alerts on the next scheduler check instead of waiting two days, because
that points to a parser or payload problem rather than ordinary delivery lag.

### Why staleness is counted in days of data, not hours of silence

Auto Export uploads in unpredictable bursts, so silence proves nothing: a long
gap can land as a complete backfill, while a short gap can import successfully
and still omit a day. Measured upload gaps on a real profile ran from under two
hours to over a day, which leaves no hours-based threshold that catches a dead
phone without also firing on ordinary operation.

So the check asks the database, not just the upload log: what is the newest
completed date that actually holds a daily metric? A `daily` row is created as
soon as an import sees the date, including for a workout-only day, so rows alone
prove nothing — the query ignores any row whose daily metrics are all null.
Today is excluded even if it already has a partial metric, because an incomplete
today must not hide a missing yesterday.

The default is two missing days rather than one, because a healthy Metrics
export was observed arriving late in the day often enough that a one-day
threshold would report routine delay. Metrics payloads carry a rolling window of
several days, so two days of grace still leaves room for a late upload to refill
the gap. Workouts carry a shorter window, but workout absence is not inferable
from a day with no workout; stalled or unpaired Workouts uploads are covered by
the `split` and `error` checks instead.

Only the newest completed edge is used for the ongoing alert. Old interior holes
do not keep producing notifications, but recovery checks every date in the range
named by the original alert rather than assuming that a newer maximum filled the
whole gap.

Today never counts as missing, since it is still in progress; a morning cutoff
decides when yesterday joins the completed-day count. That cutoff is deliberately
independent from the one that decides when an absent night stops being a pending
sync, since full Metrics payloads and individual sleep nights arrive differently.

The arrival measurements behind both thresholds, with the sample they came from,
are in the `DATA_HEALTH_*` docstrings in `src/config.py`.

### Recovery notices

Each condition is reported once and then not again for 24 hours while it
persists. What is sent when it clears depends on what the outage cost:

- **A stalled pipe** (`split`, `error`, `node`) always gets its all-clear. You
  were told to go fix something, or told the daemon tried; the confirmation
  answers an action that was taken.
- **A `funnel` outage** also gets its all-clear, for the opposite reason: you
  were told there was nothing to do, so the message that ends the waiting is
  the only one that closes it. The backlog re-sends on its own, since both
  automations carry rolling windows.

- **A stale gap that backfilled** sends nothing. Auto Export catching up on the
  days it missed is the normal ending, and announcing it only doubles the
  message count for the fault.
- **A stale gap that stayed a gap** names the alerted dates whose daily metrics
  are still absent. Reports covering them may be incomplete; a later rolling
  Metrics upload can still backfill them.

`funnel` is the one condition exempt from the 24-hour repeat. It is a wait, not
a task, and a daily reminder would only teach you to swipe away the channel that
also carries `split` and `error`. It gets one further message if it outlives
`FUNNEL_OUTAGE_ESCALATE_AFTER_H`, where it stops being the known behaviour.

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
