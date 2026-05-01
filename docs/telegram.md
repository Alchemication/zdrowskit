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
/review [current|last]
/coach [current|last]
/add
/log
/notify
/models
/clear
/status
/events [N] [category]
/context [name]
/tutorial
/help
```

`/tutorial` opens a 9-step guided tour of the system with Next/Back/Exit buttons.

`/status` shows bot state, data coverage, recent activity, and notification state.

## `/notify`

`/notify` shows and changes notification preferences through a structured proposal flow. See [Notifications](notifications.md#notification-preferences-via-telegram) for examples, supported settings, and the storage path.

## `/models`

`/models` opens a button-based model routing panel.

- Features are grouped as Chat, Reports, Coach, Nudges, and Utilities.
- Every model button is tagged with its capability tier.
- Chat exposes Reasoning and Temperature pickers.
- `Reset all` restores built-in defaults.
- Picking `Auto` for fallback stores JSON `null` and defers to the profile fallback at resolve time.

For model defaults, the `model_prefs.json` location, environment overrides, and fallback behavior, see [LLM setup](llm.md).
