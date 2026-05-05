# Telegram

Telegram is used for nudges, chat, daemon-triggered reports, approvals, rejections, and model/notification controls.

## Configuration

Add your bot credentials to `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHI...
TELEGRAM_CHAT_ID=123456789
```

Register bot commands for Telegram autocomplete and the command menu:

```bash
uv run python main.py telegram-setup
```

## Interactive Chat

The daemon runs a Telegram long-polling listener alongside the file watcher. Send a message and get a coaching response backed by your full health context.

- Ask analytical questions; the LLM queries your database with SQL and charts the results.
- Reply to a nudge or report; the bot knows which message you are replying to.
- Share updates naturally, such as "my weight is 76kg now"; the LLM proposes context file edits with Accept/Reject buttons.
- Thumbs down a bad output, pick a category, optionally reply with more detail, and undo it if you tapped it during testing or a demo.
- Conversation buffer: last 20 messages in memory, resets on daemon restart.

## Commands

Telegram commands include:

```text
/log
/add
/codex
/claude
/clear
/status
/advanced
```

`/advanced` shows less-used commands that remain typeable but are hidden from
the Telegram menu: `/notify`, `/review [current|last]`, `/coach [current|last]`,
`/models`, `/context [name]`, `/events [N] [category]`, and `/tutorial`.

`/tutorial` opens a 9-step guided tour of the system with Next/Back/Exit buttons.

`/status` shows bot state, data coverage, recent activity, and notification state.

`/codex` and `/claude` are mirror commands for the two supported coding
agents — both run the local CLI against the repo with workspace-edit
permissions. `/codex <prompt>` uses the OpenAI Codex CLI in workspace-write
sandbox mode; `/claude <prompt>` uses the Anthropic Claude Code CLI in
`acceptEdits` permission mode. With no arguments either command opens a
compact button panel for that agent: turn mode on/off, switch from the other
agent, or start a new session. Follow-up calls resume the saved session for
that agent; `/<agent> new <prompt>` starts a fresh one, `/<agent> reset [prompt]`
clears saved context, and `/<agent> stop` clears it and turns mode off. Replies
to the last agent reply continue that specific agent's session.

Codex turns show an animated progress panel with a friendly status, elapsed
time, and a note that the final answer will replace the panel. The final Codex
answer includes how long the turn took. Claude turns currently show the animated
placeholder until the final answer arrives.

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
