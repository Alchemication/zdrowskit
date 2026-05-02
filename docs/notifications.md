# Notifications

Each notification type is a distinct LLM call with its own prompt, context, tools, and purpose. They complement each other instead of repeating each other.

| Channel | Purpose | Trigger | Frequency | Length | Tools | Special output |
|---------|---------|---------|-----------|--------|-------|----------------|
| **Insights** | Full weekly report | Scheduled, default Monday 10am, or manual `/review` | 1x/week | ~450 words | `run_sql` | `<chart>` by default 1, skip if misleading; `<memory>` always 1, appended to `history.md` |
| **Coach** | Weekly strategy review, only when proposals exist | After insights, silent on no-change weeks | 1x/week | ~300 words | `run_sql`, `update_context` for `strategy` only | `SKIP` if no changes warranted; bundled message with inline Accept/Reject buttons per edit |
| **Nudge** | Short reactive next-action nudge | Data sync, file edit | Up to 2/day by default | 80 words | `run_sql` | `SKIP` if nothing changes; optional `<chart>` |
| **Chat** | Interactive conversation: answer the current message, ask anything, get charts | Your Telegram message | On demand | 150 words | `run_sql` up to 5/turn, `update_context` any file | Optional `<chart>`; at most one `update_context` |

## Notification Preferences Via Telegram

Use `/notify` in Telegram to inspect and change notification behavior without editing files by hand.

Examples:

- `/notify`
- `/notify no nudges before 11am`
- `/notify send weekly insights on Tuesday at 8`
- `/notify turn off midweek report`
- `/notify mute nudges today`
- `/notify bring weekly insights back to default`
- `/notify set all as default`

How it works:

- A small LLM interprets the request into a strict structured proposal.
- The bot shows the interpreted change back to you with `Accept` / `Reject`.
- Nothing is saved until you tap `Accept`.
- If the request is ambiguous, the bot asks a short clarification question.
- Preferences live in `~/Documents/zdrowskit/notification_prefs.json`.

What can be changed:

- nudges on/off
- nudge earliest send time
- weekly insights on/off, weekday, and time
- midweek report on/off, weekday, and time
- temporary mutes for all notifications or one notification type
- reset one setting or everything back to built-in defaults

## What Triggers Nudges

| Event | Debounce | What it does |
|-------|----------|-------------|
| Health data synced via iCloud | 3 min | One data observation + suggestion for today/tomorrow |
| `log.md` / `strategy.md` / `me.md` edited | 60 sec | Responds to the change: acknowledges, flags tension, or confirms |
| Monday 8-9 AM | scheduled | Full weekly report, then coaching review |
| Thursday 9-10 AM | scheduled | Mid-week progress report |

## Cross-Message Awareness

Each channel sees what the others recently said so the LLM avoids redundancy:

- **Coach** sees recent nudges sent.
- **Nudge** sees last 3 nudges + last coach review summary.
- **Chat** sees last 3 nudges + last coach review summary.
- **Insights** is independent and has its own `history.md` memory.

## Suppression and Rate Limiting

- **Earliest nudge time:** nudges are deferred until the configured earliest send time. Triggers queue and drain as one consolidated nudge once the window opens.
- **Temporary mute / disable:** when a notification type is muted or disabled, the daemon skips the notification LLM call entirely.
- **Report suppression:** nudges are suppressed +/- 1 hour around scheduled reports because the report already covers the big picture.
- **Rate limits:** max 2 nudges/day by default, min 3 hours apart.
- **LLM SKIP:** the nudge LLM can respond `SKIP` if there is nothing genuinely new to say.
- **Coach:** runs at most once per calendar day.
- **No replay after mute:** skipped nudges/reports are not replayed after a temporary mute expires.

For bot setup and interactive chat commands, see [Telegram](telegram.md).
