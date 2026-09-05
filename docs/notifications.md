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
| **Targets** | Turns the prose goals in `strategy.md` into countable weekly targets | First notification of a new week, or an edit to the goal sections | Cached per week and goal text, including empty results | None — stored, not sent | none | Strict JSON against a closed metric vocabulary; drives the progress strip |
| **Plan frame** | Decides how much of the progress strip this person's current life warrants | First notification after the journal changes, or when the last decision ages out | Cached until context changes or the decision expires | None — stored, not sent | none | Never shown the measurements, so it cannot hide an unflattering bar |
| **Quiet-week check-in** | Asks what is going on when a week runs far below this person's own normal | Friday, deterministic detection | At most 1/week, and stops after two silences | One question plus four buttons | none | Answer is written to `log.md`, where the plan frame reads it |
| **Sync alerts** | Warns when an HTTP profile's metrics stop updating or its imports stall | Checked on the scheduler tick | At most 1/day per unchanged condition | Short deterministic message | none | Pipe-fault all-clear; unresolved dates named after stale recovery |
| **Chat** | Interactive conversation: answer the current message, ask anything, get charts | Your Telegram message | On demand | Brief unless you ask for detail | `run_sql`, `update_context` for `me`, `strategy`, or `log` | Optional `<chart>`; context edits require confirmation by default |

Exact length ceilings live with the thing that enforces them: the visible-report
limit in `src/config.py`, the per-channel word counts in the prompts under
`src/prompts/`.

## Weekly Progress Strip

Nudges and weekly reports open with progress against this week's goals, so the
first thing you see is where the week stands rather than only what triggered
the message.

A report carries the full strip:

```
Week 2026-W36 · day 4 of 7
Run        ███████░░░  21.4/30 km        8.6 left
Lifts      ██████████      2/2 sessions  done
Sleep ≥7h  ████░░░░░░      2/5 nights    3 left
```

A nudge is headed by one ring instead — the one the arriving data moved:

```
Lifts ●●
Runs ●●○○
Sleep ≥7h ●●●●●○○
Run km 21.4/30 · 8.6 left
```

The report's strip sits in a monospace block, where the columns line up and a
bar of blocks is readable. A nudge header does not: it is drawn in the ordinary
message font, where a run of `█` closes into a solid slab and the line is wide
enough to wrap on a watch. So the two surfaces draw differently on purpose.

A goal you can count — sessions, nights, step days — is drawn as one dot per
unit, up to `WEEKLY_PROGRESS_MAX_DOTS`; a filled dot is one session that
happened. A run with no empty dot left in it is a completed target, which is
why nothing else marks one. Beyond that limit, and for goals that sum a
quantity rather than count events, the line prints numbers instead. Anything
past the target is counted after the dots, as `+1`.

The ring replaces the trigger label rather than joining it. `Lifts ●●` says
where the week stands and implies well enough what moved; the label said the
same words on every nudge. It comes back whenever there is no ring to show, so
a nudge is never left without a header. Nudges get one ring rather than three,
and only when that ring has visibly moved since the last nudge that carried a
line — a dot filled, or a numbered ring crossed a tenth of its target, the
target was completed, a different activity took the lead, or a new week
started.

Nudges fire up to twice a day while the rings move three or four times a week,
so an ungated line would repeat itself most of the week. Repetition at the top
of a message is what teaches you to skip the top of the message, and the nudge
itself starts immediately below it. The weekly report is never gated this way:
it arrives once, and showing the week is its job.

A goal that states both a distance and a session count for the same sport — "3
runs, about 15 km" — produces two rings, labelled `Run km` and `Runs`. That is
not a duplicate: two of three runs done against ten of fifteen kilometres says
the runs are coming up short, which neither bar says alone.

### When the strip stands down

A progress bar is a claim that the plan is what matters this week. During a
newborn's first fortnight, a bereavement, flu or a fresh injury it is not, and a
bar reading `behind` then measures you against a commitment that stopped
applying — in the one week you would most notice.

So a small call reads your profile, your journal and recent weekly memory, and
decides how much of the strip fits your current context:

| | What you see |
|---|---|
| **full** | the rings, and a remaining-work or completion label — the default |
| **facts** | the rings alone, no label — travelling, a deload, a niggle |
| **hidden** | no strip at all |

A dotted nudge header looks the same either way: dots state what happened and
never judge the pace, so there is no label on one to drop.

**That call is never shown how your week is going.** It gets your context and
nothing else, so it cannot hide a bar for being unflattering. Falling behind on
an ordinary week is exactly what the strip exists to say, and a bad week is
explicitly not a reason to reduce it.

The decision is cached, because life context changes over days rather than
between two notifications an hour apart. A normal week holds for
`PLAN_FRAME_MAX_AGE_DAYS`; a decision to reduce the strip expires far sooner
and has to be made again, because a strip wrongly shown is something you can see
and object to while a strip wrongly hidden looks exactly like a feature with
nothing to say. Editing your journal re-opens the question immediately.

Every failure — an unparseable answer, a provider outage, a missing reason —
falls back to the full strip, except that an existing suppression survives an
outage rather than resuming cheerful bar-charts mid-bereavement.

`uv run python main.py targets` reports the current decision, its reason, and
the call id behind it, since a suppressed strip is invisible everywhere else.

## Quiet-Week Check-In

Every other notification here reacts to arriving data, which means the system
goes quietest exactly when something has happened: someone who stops training
stops generating the syncs that would make it speak.

So on Friday, if the week is running far below your own normal and nothing in
your notes explains it, the coach asks once:

```
Quiet one so far this week — anything going on, or just how it landed?

  [ 🌍 Life got in the way ]
  [ 😌 Deliberate rest ]
  [ 🤷 Just slipped ]
  [ ✍️ Add a note ]
```

One tap writes a line to `log.md`. That is the whole point: the plan-frame
decision reads `log.md`, so a tap on Friday changes whether next week's progress
strip judges you at all. `Add a note` asks for one line instead, and your own
words are stored rather than the canned phrasing. Replies are linked to the
stored question message, so restarting the daemon does not lose a pending note.
The bot confirms only after saving the journal and answer; a failed save can be
retried. Delayed answers retain the week the question concerned.

**Detection is deterministic.** Sessions so far this week are compared against
the median of your recent completed weeks, scaled to the day. An LLM is asked
only to phrase the question, never to decide there is one. Every recorded
workout counts whatever the sport — a week spent swimming instead of running is
not a quiet week.

It stays silent when:

- notification preferences say so. It is an unprompted message of the same kind
  as a nudge, so muting nudges or setting an earliest hour silences it too. It
  does not consume a nudge from the daily count.
- daily metrics or workout exports are stale or their freshness is unknown.
  `QUIET_WEEK_DATA_MAX_AGE_DAYS` in `src/config.py` sets the freshness window.
  For HTTP profiles, the latest workout upload must also have imported
  successfully; receiving an upload alone is insufficient.
- it is not the check-in weekday, or it has already asked this week
- the plan frame already knows something is going on — asking then would prove
  it had not been listening
- there is not enough history to know what normal looks like, or no weekly
  rhythm to break
- the last `QUIET_WEEK_MAX_UNANSWERED` check-ins went unanswered

A question that could not be delivered is not recorded as asked, so a Telegram
outage cannot retire the feature by looking like two refusals to answer.

Silence is a permitted answer, never chased. You may be in a meeting, or having
exactly the kind of week that made the question worth asking. Nothing is
re-sent, and after two silences the question stops being asked until you answer
one.

Thresholds live in `src/config.py`, each with the reasoning behind its value.

### Where the numbers come from

The targets come from the `## Goals` and `## Weekly Plan` sections of
`strategy.md`. A small LLM call reads that prose for each new week or changed goal text and matches each
stated goal to a metric the database can count; the numbers are then stored for
the week and every notification renders from the same stored values, so the
same bar cannot differ between two messages an hour apart.

The measurement itself never calls an LLM, and runs after verification, so
nothing rewords it. Hand-logged workouts added through `/add` count.

**No stated goal means no strip.** Nothing is assumed — no default step count,
no default sleep target. A goal that names no number produces no bar.

### What can become a bar

Goals are matched against activity categories rather than one metric per sport,
so running, walking, cycling, strength and everything else are all measurable:

| Measured | Covers |
|----------|--------|
| Weekly distance | running, walking, cycling |
| Weekly session count | running, walking, cycling, strength, HIIT, a named activity, or any activity |
| Weekly exercise minutes | the Apple exercise ring |
| Nights reaching a sleep target | sleep |
| Days reaching a step target | steps |

Hikes are recorded as walks throughout zdrowskit, so they count toward walking
distance and walking sessions.

Sports with no category of their own — paddling, basketball, swimming, football
— are counted by naming the workout type your watch recorded, so the menu is
your own history rather than a list somebody had to predict. Only types you
have actually logged can become a bar; one you have never recorded is refused
rather than drawn as a bar stuck at zero. Run `uv run python main.py targets` to
see which of your activities are offered.

Not every sport can carry a distance goal. Apple records no distance for paddle
sports or HIIT, for instance, so those are countable as sessions only.

Goals about pace, resting heart rate, HRV, weight or diet have nothing to count
and are left out rather than approximated.

At most `WEEKLY_TARGET_MAX_RINGS` goals become bars; the rest are dropped by
priority. Ranges take the lower bound.

### Completion labels

During an unfinished week the report's strip shows how much is left, or `done`
when a target is met; a dotted nudge header shows neither, because its dots
already say both. It does not label you `behind` or assume that sessions are spread
evenly across the week. Sunday remains unfinished until the day ends. A report
for a completed week labels an unmet amount `short`.

### Inspecting and correcting it

```bash
uv run python main.py targets            # this week's targets, progress, and provenance
uv run python main.py targets refresh    # re-read strategy.md and re-derive now
uv run python main.py targets clear      # drop this week's targets
```

In Telegram, `/targets` shows the current targets and their source sentences.
Use its **Pause progress**, **Resume progress**, and **Refresh targets** buttons,
or `/targets pause`, `/targets resume`, and `/targets refresh`. A pause lasts
until you resume; it hides the strip, not ordinary nudges or quiet-week questions.
To correct a number, tell the chat what goal to change; the existing strategy-edit
approval flow shows the proposed edit before saving it.

`targets clear` resets extraction, including a cached empty result; it does not
pause progress. Successful extraction with no measurable goals retires obsolete
bars and is cached. A failed extraction keeps old bars only when the goal text
has not changed; changed goals never display the old numbers.

`targets` prints the sentence each number came from and the call id to pass to
`llm-log --id`. Editing the goal sections of `strategy.md` — including by
accepting a coach proposal — re-derives the targets on the next notification
without any further action.

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
- **Failed reports:** a scheduled report that fails on a passing fault — the
  network dropped, the provider was down — is retried on later ticks that same
  day, and you are told once, when the attempts run out. A failure that would
  repeat identically, such as the verifier refusing the draft or the week
  holding too little data, ends the day on the first attempt. The attempt
  budget is `MAX_REPORT_ATTEMPTS_PER_DAY` in `src/config.py`; `main.py events`
  shows each attempt as it happens.

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

### A message counts as sent only when Telegram takes it

The 24-hour repeat guard is armed by delivery, not by the attempt. Every
Telegram send retries a transport fault on its own first; if it still cannot
get through, the condition stays unreported and the next tick tries again.

This is the one rule that keeps the alert channel honest. It used to work the
other way — the alert was recorded before the send, so a delivery failure could
not cause a retry storm — and on 2 Sep 2026 a dropped socket meant the state
said the user had been told while nothing had reached them, with the guard then
suppressing the alert for a day. What went unreported was three days of missing
uploads: the failure hid exactly the outage it existed to announce.

A condition suppressed by your own preferences is different, and is still
recorded. You asked not to hear it, so there is nothing to retry.

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
