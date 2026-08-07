# Telegram

Telegram is used for nudges, chat, daemon-triggered reports, approvals, rejections, and model/notification controls.

## Configuration

Add your bot credentials to `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHI...
```

Link numeric Telegram user IDs in `profiles.toml`; `TELEGRAM_CHAT_ID` is used
only as legacy input by `profile adopt` when `--telegram-id` is omitted.
Usernames are never used for authorization.

To find the first operator's ID, message the new bot before starting the daemon
and inspect `message.from.id` in the official Bot API `getUpdates` response.
Once the daemon is running, it owns that update stream and reports the numeric
ID of later unknown senders to the operator.

Register bot commands for Telegram autocomplete and the command menu:

```bash
uv run python main.py telegram-setup
```

## Interactive Chat

The daemon runs one Telegram long-polling listener and routes authorized
private-chat updates to the matching profile runtime.

Only a private chat where `chat.id` equals the sender's numeric user ID is
accepted. Unknown, disabled, group-chat, and mismatched identities are denied.
When possible, the operator receives a notice containing an unknown sender's
numeric ID so they can deliberately add that person. Callback buttons are
validated and dispatched through the same profile route.

- Ask analytical questions; the LLM queries your database with SQL and charts the results.
- Reply to a nudge or report; the bot knows which message you are replying to.
- Share updates naturally, such as "my weight is 76kg now"; the LLM proposes context file edits with Accept/Reject buttons.
- Thumbs down a bad output, pick a category, optionally reply with more detail, and undo it if you tapped it during testing or a demo.
- Conversation buffer: last 20 messages in memory, resets on daemon restart.

## Commands

Telegram commands include:

```text
/add
/codex
/claude
/clear
/status
/advanced
```

`/advanced` shows less-used commands that remain typeable but are hidden from
the Telegram menu: `/notify`, `/review`, `/coach [current|last]`,
`/models`, `/context [name]`, `/events [N] [category]`,
`/llm_log [N|id ID|trace ID]`, and `/tutorial`.

Use `/events usage [N]` to inspect privacy-safe Telegram usage metrics over
the last `N` days (default 30). It records command names and normalized inline
button actions only; command arguments, message text, callback tokens, and
button payload values are not stored.

`/tutorial` opens an 8-step guided tour of the system with Next/Back/Exit buttons.

`/status` shows bot state, Telegram delivery/handler health, data coverage,
recent activity, and notification state.

`/codex` and `/claude` are operator-only mirror commands for the two supported coding
agents — both run the local CLI against the repo with workspace-edit
permissions. `/codex <prompt>` uses the OpenAI Codex CLI in workspace-write
sandbox mode; `/claude <prompt>` uses the Anthropic Claude Code CLI in
`acceptEdits` permission mode. With no arguments either command opens a
compact button panel for that agent: turn mode on/off, switch from the other
agent, or start a new session. Follow-up calls resume the saved session for
that agent; `/<agent> new <prompt>` starts a fresh one, `/<agent> reset [prompt]`
clears saved context, and `/<agent> stop` clears it and turns mode off. Replies
to the last agent reply continue that specific agent's session.

Codex and Claude turns show an animated progress panel with a friendly status,
elapsed time, and a note that the final answer will replace the panel. The final
agent answer includes how long the turn took.

Workspace permissions let either agent edit files in the repo checkout. They
do not grant write access to external state directories such as
`~/Documents/zdrowskit` or the default SQLite DB directory unless those paths
are added separately.

Use `/<agent> on [prompt]` to route plain non-command Telegram messages to
that agent without retyping the slash command. Only one agent mode is active
at a time — `/claude on` while Codex mode is active switches to Claude — but
Codex and Claude keep separate saved sessions. Agent mode refreshes after each
turn and turns itself off after 30 minutes of inactivity. Use `/<agent> off`,
the panel's `Turn off`, or the `Back to chat` button below active-mode agent
replies to return plain messages to the normal health chat immediately.

When running under launchd, the agent commands use
`ZDROWSKIT_CODEX_EXECUTABLE` and `ZDROWSKIT_CLAUDE_EXECUTABLE` values written
by `uv run python main.py daemon-install` if available. Re-run
`daemon-install` after installing or moving either CLI.

## `/notify`

`/notify` shows and changes notification preferences through a structured proposal flow. See [Notifications](notifications.md#notification-preferences-via-telegram) for examples, supported settings, and the storage path.

## `/models`

`/models` opens a button-based model routing panel.

- Features are grouped as Chat, Reports, Coach, Nudges, and Utilities.
- Every model button is tagged with its capability tier.
- Chat exposes Reasoning and Temperature.
- `Reset all` restores built-in defaults.
- Picking `Auto` for fallback stores JSON `null` and defers to the profile fallback at resolve time.

For model defaults, the `model_prefs.json` location, environment overrides, and fallback behavior, see [LLM setup](llm.md).
