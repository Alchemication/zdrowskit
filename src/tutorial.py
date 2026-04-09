"""Wizard-style /tutorial command content and rendering.

A stateless wizard whose current step lives entirely in the Telegram
inline-keyboard ``callback_data`` (e.g. ``tut:3``).  No per-user state
to persist or expire — every callback reconstructs the view from the
step number alone, and a single message is edited in place as the
user navigates.

Public API:
    TUTORIAL_STEPS: the ordered list of (emoji, title, body) tuples.
    render_step(idx): returns ``(markdown_text, inline_keyboard)``.
"""

from __future__ import annotations

# Each step is (anchor_emoji, title, body_markdown).  Body is plain
# markdown — the daemon's existing md_to_telegram_html pipeline turns
# it into Telegram HTML on send.
TUTORIAL_STEPS: list[tuple[str, str, str]] = [
    (
        "👋",
        "Welcome",
        (
            "The day my son was born, Apple sent me a notification that my "
            "rings were behind. That was the moment I knew something had to "
            "change.\n\n"
            "zdrowskit is the result — a 24/7 ultra-personal trainer built to "
            "help you actually *reach* your health and fitness goals. Not by "
            "counting closed circles. By knowing you.\n\n"
            "This tour is 9 short steps. Tap *Next* to begin, *Exit* anytime."
        ),
    ),
    (
        "💾",
        "What data lives here",
        (
            "Everything stays in a local SQLite database on your machine:\n"
            "- Apple Health metrics (HRV, RHR, sleep, weight, …)\n"
            "- Workouts and GPX route files\n"
            "- Sleep stages and durations\n"
            "- Your own journal in `me.md`, `strategy.md`, `log.md`\n\n"
            "The journal is what makes the coaching personal — numbers say "
            "*what* happened, the journal says *why*."
        ),
    ),
    (
        "📊",
        "Metrics that matter",
        (
            "A handful of signals do most of the work:\n"
            "- *HRV* — recovery and stress balance. The first thing to drop "
            "when you're overdoing it.\n"
            "- *Resting HR* — long-term aerobic fitness trend.\n"
            "- *VO2max* — aerobic capacity ceiling. Moves slowly.\n"
            "- *Sleep stages* — recovery quality, not just duration.\n"
            "- *Weekly training load* — volume × intensity, the overreach radar."
        ),
    ),
    (
        "🔔",
        "Nudges — the core",
        (
            "The killer feature. Every day, after syncing last night's sleep, "
            "your training history, and your current plan, the system tells you "
            "to *push*, *stick to the routine*, or *rest*.\n\n"
            "> Not based on a ring. Based on you.\n\n"
            "Use /notify to mute, reschedule, or rewrite the rules in plain "
            "English."
        ),
    ),
    (
        "📅",
        "Weekly report — /review",
        (
            "Run /review for a narrative recap of the past week: what changed, "
            "what's trending, and charts where they help.\n\n"
            "Reply to the report in chat to dig deeper — \"why was Wednesday's "
            "HRV so low?\" — and the conversation stays grounded in that week's "
            "data."
        ),
    ),
    (
        "🧭",
        "Coach — /coach",
        (
            "/coach reads your journal *and* your numbers, then proposes "
            "concrete changes to your training plan or goals.\n\n"
            "> Numbers say what happened. The journal says why. The LLM "
            "connects both.\n\n"
            "Every proposal comes with *Approve* / *Reject* buttons. Nothing "
            "changes without your confirmation."
        ),
    ),
    (
        "💬",
        "Chat — ask anything",
        (
            "Just talk to the bot. Questions get answered with real SQL against "
            "your own data, and charts are rendered on demand. Try things like:\n"
            "1. *What was my resting heart rate yesterday?*\n"
            "2. *Show my weekly running mileage for the last 8 weeks as a chart.*\n"
            "3. *How does my HRV correlate with sleep duration over the last month?*\n"
            "4. *Given last week vs the previous, am I overreaching?*"
        ),
    ),
    (
        "⚖️",
        "Honest trade-offs",
        (
            "What you should know before trusting any of this:\n"
            "- *Your data goes to a third-party LLM.* Storage is local, but "
            "every coaching call sends the relevant slice — metrics, workouts, "
            "journal excerpts — to Anthropic's API. No way around that today.\n"
            "- *Context files are the intelligence ceiling.* `me.md`, "
            "`strategy.md`, `log.md` determine everything. Writing them well is "
            "harder than it looks.\n"
            "- *iOS export is fragile.* The phone must be unlocked for health "
            "data to sync. Gaps happen silently.\n"
            "- *LLM reliability isn't fully solved.* Even Opus 4.6 occasionally "
            "ignores instructions. Sanity-check anything important."
        ),
    ),
    (
        "✅",
        "You're set",
        (
            "That's the tour. From here:\n"
            "- /help — every command at a glance\n"
            "- /context — view or edit your context files\n"
            "- /notify — tune when and how nudges fire\n"
            "- /tutorial — reopen this tour anytime\n\n"
            "Now go ask the bot something."
        ),
    ),
]

# Unicode block characters for the progress bar.  Same width on every
# Telegram client (mobile + desktop), no emoji width quirks.
_PROGRESS_FILLED = "▰"
_PROGRESS_EMPTY = "▱"


def _progress_bar(idx: int, total: int) -> str:
    """Render a unicode progress bar for the given step.

    Args:
        idx: Zero-based current step index.
        total: Total number of steps.

    Returns:
        A string like ``▰▰▰▰▱▱▱▱▱  Step 4 / 9``.
    """
    filled = idx + 1
    bar = _PROGRESS_FILLED * filled + _PROGRESS_EMPTY * (total - filled)
    return f"{bar}  Step {filled} / {total}"


def _header(idx: int) -> str:
    """Build the 3-line header for a tutorial step.

    Args:
        idx: Zero-based step index.

    Returns:
        Markdown header with anchor emoji, title, progress bar, and a
        trailing blank line.
    """
    emoji, title, _body = TUTORIAL_STEPS[idx]
    total = len(TUTORIAL_STEPS)
    return f"{emoji}  *{title}*\n{_progress_bar(idx, total)}\n"


def _keyboard(idx: int) -> list[list[dict[str, str]]]:
    """Build the inline keyboard for the given step.

    Buttons encode their *destination* step in the callback data so the
    handler stays a one-liner.  Special destinations: ``tut:exit`` and
    ``tut:done``.

    Args:
        idx: Zero-based current step index.

    Returns:
        A single-row inline keyboard suitable for Telegram's
        ``inline_keyboard`` field.
    """
    total = len(TUTORIAL_STEPS)
    row: list[dict[str, str]] = []
    if idx > 0:
        row.append({"text": "◀ Back", "callback_data": f"tut:{idx - 1}"})
    if idx < total - 1:
        row.append({"text": "Next ▶", "callback_data": f"tut:{idx + 1}"})
        row.append({"text": "✕ Exit", "callback_data": "tut:exit"})
    else:
        row.append({"text": "✓ Done", "callback_data": "tut:done"})
    return [row]


def render_step(idx: int) -> tuple[str, list[list[dict[str, str]]]]:
    """Render the markdown body and inline keyboard for a tutorial step.

    Args:
        idx: Zero-based step index.  Must be in
            ``range(len(TUTORIAL_STEPS))``.

    Returns:
        Tuple of ``(markdown_text, inline_keyboard)``.

    Raises:
        IndexError: If ``idx`` is out of range.
    """
    if not 0 <= idx < len(TUTORIAL_STEPS):
        raise IndexError(f"tutorial step {idx} out of range")
    _emoji, _title, body = TUTORIAL_STEPS[idx]
    text = f"{_header(idx)}\n{body}"
    return text, _keyboard(idx)
