"""Noticing a week that has gone quiet, and asking about it once.

Every other notification in zdrowskit is reactive: a nudge fires when data
arrives. That leaves the system quietest exactly when something has happened,
because someone who stops training stops generating the syncs that would make
it speak. Absence triggers nothing.

This closes that. When a week is running far below the person's own normal by
Friday, the coach asks — once — what is going on, and offers three taps and an
optional sentence. The answer is written to ``log.md``, where the plan-frame
decision reads it, so a tap on Friday changes whether next week's progress
strip judges them at all.

That loop is the point. Detection is the cheap part and deliberately
deterministic: an LLM is asked only to phrase the question, never to decide
whether there is one. The expensive part is having somewhere to put the answer.

Two rules keep it from becoming nagging. It never asks twice in a week, and it
stops asking after ``QUIET_WEEK_MAX_UNANSWERED`` silences — because two
silences are an answer, and the person who most needs to be left alone is the
one who has twice had nothing to say.

Public API:
    WeekActivity      — this week's sessions against the person's own normal.
    CheckinChoice     — one button, and the journal line it writes.
    CHECKIN_CHOICES   — the offered answers.
    measure_week_activity — count sessions and learn what normal looks like.
    should_ask_checkin    — the whole gate, deterministic.
    compose_checkin       — question text plus inline keyboard.
    record_asked / record_answer — the ledger.
"""

from __future__ import annotations

import logging
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from config import (
    PROMPTS_DIR,
    QUIET_WEEK_BASELINE_WEEKS,
    QUIET_WEEK_CHECK_WEEKDAY,
    QUIET_WEEK_MAX_UNANSWERED,
    QUIET_WEEK_MIN_BASELINE_SESSIONS,
    QUIET_WEEK_MIN_WEEKS,
    QUIET_WEEK_DATA_MAX_AGE_DAYS,
    QUIET_WEEK_SHORTFALL_RATIO,
)

logger = logging.getLogger(__name__)

CHECKIN_PROMPT = "checkin_prompt.md"
DAYS_IN_WEEK = 7

_FALLBACK_QUESTION = (
    "Quieter week than usual so far. Anything going on, or just how the week landed?"
)


@dataclass(frozen=True)
class CheckinChoice:
    """One offered answer.

    Attributes:
        key: Stable identifier carried in the Telegram callback payload.
        label: Button text.
        journal: The line written to log.md when this is chosen. Written in the
            person's own voice, because log.md is their journal and a later
            prompt reads it as something they said.
    """

    key: str
    label: str
    journal: str


# Fixed rather than generated. The question above them is written fresh each
# time, but the answers are the interface: buttons that changed wording week to
# week would make the same tap mean different things in the record.
CHECKIN_CHOICES: tuple[CheckinChoice, ...] = (
    CheckinChoice(
        key="life",
        label="🌍 Life got in the way",
        journal="Life got in the way this week; training took a back seat.",
    ),
    CheckinChoice(
        key="rest",
        label="😌 Deliberate rest",
        journal="Took this week easy on purpose.",
    ),
    CheckinChoice(
        key="slipped",
        label="🤷 Just slipped",
        journal="Training slipped this week, no particular reason.",
    ),
    CheckinChoice(
        key="note",
        label="✍️ Add a note",
        journal="",
    ),
)

CHOICE_BY_KEY: dict[str, CheckinChoice] = {c.key: c for c in CHECKIN_CHOICES}


@dataclass(frozen=True)
class WeekActivity:
    """This week's training so far, against what this person normally does.

    Attributes:
        sessions: Workouts recorded from Monday up to and including today.
        baseline_per_week: Median sessions in their recent completed weeks.
        weeks_of_history: Completed weeks the median was drawn from.
        days_elapsed: Days of the week counted so far, 1 through 7.
    """

    sessions: int
    baseline_per_week: float
    weeks_of_history: int
    days_elapsed: int

    @property
    def expected_by_now(self) -> float:
        """Sessions a normal week would have produced by this point."""
        return self.baseline_per_week * self.days_elapsed / DAYS_IN_WEEK

    @property
    def is_quiet(self) -> bool:
        """True when this week is running far below the person's own pace."""
        if self.expected_by_now <= 0:
            return False
        return self.sessions <= self.expected_by_now * QUIET_WEEK_SHORTFALL_RATIO


def _week_start(day: date) -> date:
    """Return the Monday of *day*'s week."""
    return day - timedelta(days=day.weekday())


def measure_week_activity(
    conn: sqlite3.Connection, *, today: date
) -> WeekActivity | None:
    """Count this week's sessions and learn what a normal week looks like.

    Every recorded workout counts, whatever the sport. The question being asked
    is whether this person's life is running normally, and a week spent
    swimming instead of running is not a quiet week.

    Args:
        conn: Open database connection.
        today: The day being measured.

    Returns:
        The measurement, or None when the database could not answer.
    """
    monday = _week_start(today)
    window_start = monday - timedelta(weeks=QUIET_WEEK_BASELINE_WEEKS)
    try:
        rows = conn.execute(
            "SELECT date, COUNT(*) AS n FROM workout_all "
            "WHERE date >= ? AND date <= ? GROUP BY date",
            (window_start.isoformat(), today.isoformat()),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("Quiet-week measurement failed: %s", exc)
        return None

    # Bucket by the Monday each day belongs to, so completed weeks and the
    # running one never mix.
    per_week: dict[date, int] = {}
    for row in rows:
        try:
            bucket = _week_start(date.fromisoformat(row["date"]))
        except ValueError:
            continue
        per_week[bucket] = per_week.get(bucket, 0) + row["n"]

    completed = [
        per_week.get(window_start + timedelta(weeks=index), 0)
        for index in range(QUIET_WEEK_BASELINE_WEEKS)
    ]
    # A median rather than a mean: one holiday fortnight at zero should not
    # redefine normal, and neither should one training camp.
    ordered = sorted(completed)
    middle = len(ordered) // 2
    baseline = (
        float(ordered[middle])
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )

    return WeekActivity(
        sessions=per_week.get(monday, 0),
        baseline_per_week=baseline,
        weeks_of_history=sum(1 for count in completed if count > 0),
        days_elapsed=max(1, min(DAYS_IN_WEEK, (today - monday).days + 1)),
    )


def already_asked(conn: sqlite3.Connection, week_start: str) -> bool:
    """Return True when this week's check-in has already gone out."""
    try:
        row = conn.execute(
            "SELECT 1 FROM checkin WHERE week_start = ?", (week_start,)
        ).fetchone()
    except sqlite3.Error as exc:
        # Assume asked. Failing towards silence is right for a message whose
        # worst failure mode is repeating itself.
        logger.warning("Check-in ledger unavailable: %s", exc)
        return True
    return row is not None


def consecutive_silences(conn: sqlite3.Connection) -> int:
    """Return how many of the most recent check-ins went unanswered."""
    try:
        rows = conn.execute(
            "SELECT answered_at FROM checkin ORDER BY week_start DESC LIMIT ?",
            (QUIET_WEEK_MAX_UNANSWERED,),
        ).fetchall()
    except sqlite3.Error:
        return QUIET_WEEK_MAX_UNANSWERED
    silences = 0
    for row in rows:
        if row["answered_at"]:
            break
        silences += 1
    return silences


def checkin_data_current(
    conn: sqlite3.Connection, *, health_dir: Path, source: str, now: datetime
) -> bool:
    """Require recent daily data and a recent workout export before inferring absence.

    HTTP additionally requires that the latest workout upload was successfully
    imported. File timestamps alone would accept an uploaded but failed payload.
    """
    cutoff = now - timedelta(days=QUIET_WEEK_DATA_MAX_AGE_DAYS)
    try:
        row = conn.execute(
            "SELECT MAX(date) AS day FROM daily WHERE steps IS NOT NULL "
            "OR exercise_min IS NOT NULL OR resting_hr IS NOT NULL"
        ).fetchone()
        if not row or not row["day"] or row["day"] < cutoff.date().isoformat():
            return False
        if source == "http":
            state = json.loads((health_dir / ".ingest_state.json").read_text())
            upload = state.get("uploads", {}).get("workouts", {})
            marker = f"{upload.get('sha256')}:{upload.get('session_id')}"
            if (
                state.get("last_error")
                or state.get("last_import_uploads", {}).get("workouts") != marker
            ):
                return False
            received = datetime.fromisoformat(upload["received_at"])
            return cutoff <= received <= now
        files = list((health_dir / "Workouts").glob("*.json"))
        return any(
            cutoff.timestamp() <= file.stat().st_mtime <= now.timestamp()
            for file in files
        )
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False


def should_ask_checkin(
    conn: sqlite3.Connection,
    *,
    today: date,
    plan_frame_knows: Callable[[], bool] | None = None,
    data_current: bool = False,
) -> tuple[bool, str, WeekActivity | None]:
    """Decide whether to ask about this week. Deterministic throughout.

    Args:
        conn: Open database connection.
        today: The day being considered.
        data_current: True only when recent imports support inferring absence.
        plan_frame_knows: Called to ask whether the plan-frame decision has
            already established that something is going on; asking then would
            prove the system was not listening. Deferred rather than passed as
            a value because resolving it can cost an LLM call, and the cheap
            gates reject this on six days out of seven.

    Returns:
        A (should_ask, reason, activity) triple. The reason is for the log and
        explains the negative cases, which is where this will need debugging.
    """
    if today.weekday() != QUIET_WEEK_CHECK_WEEKDAY:
        return False, "not the check-in weekday", None

    if not data_current:
        return False, "sync freshness unknown or stale", None

    week_start = _week_start(today).isoformat()
    if already_asked(conn, week_start):
        return False, "already asked this week", None

    silences = consecutive_silences(conn)
    if silences >= QUIET_WEEK_MAX_UNANSWERED:
        return False, f"{silences} unanswered check-ins; backing off", None

    activity = measure_week_activity(conn, today=today)
    if activity is None:
        return False, "measurement unavailable", None
    if activity.weeks_of_history < QUIET_WEEK_MIN_WEEKS:
        return False, "not enough history to know what normal is", activity
    if activity.baseline_per_week < QUIET_WEEK_MIN_BASELINE_SESSIONS:
        return False, "no established weekly rhythm to break", activity
    if not activity.is_quiet:
        return False, "week is running normally", activity

    # Last, because it is the only gate that can cost a model call.
    if plan_frame_knows is not None and plan_frame_knows():
        return False, "context already explains this week", activity

    return True, "week is far below this person's own pace", activity


def checkin_keyboard(week_start: str) -> list[list[dict[str, str]]]:
    """Return the inline keyboard offering the answers."""
    rows = [
        [
            {
                "text": choice.label,
                "callback_data": f"checkin:{week_start}:{choice.key}",
            }
        ]
        for choice in CHECKIN_CHOICES
    ]
    return rows


def build_checkin_messages(
    *,
    me: str | None,
    log: str | None,
    history: str | None,
    activity: WeekActivity,
    today: date,
    prompts_dir: Path = PROMPTS_DIR,
) -> list[dict[str, str]]:
    """Render the prompt that writes the question.

    The model phrases a question that has already been decided on. It is given
    the shortfall so it can be concrete, and the person's context so it can
    sound like it knows them — but it has no say in whether to ask, which is
    what keeps this from becoming a second opinion about someone's week.

    Args:
        me: Contents of me.md.
        log: Recent entries from log.md.
        history: Recent weekly memory entries.
        activity: The measured shortfall.
        today: The day the question is being asked.
        prompts_dir: Directory holding the prompt file.

    Returns:
        Messages ready for ``call_llm``.
    """
    template = (prompts_dir / CHECKIN_PROMPT).read_text(encoding="utf-8")
    content = template.format(
        me=me or "(not provided)",
        log=log or "(not provided)",
        history=history or "(nothing yet)",
        today=today.isoformat(),
        weekday=today.strftime("%A"),
        sessions=activity.sessions,
        expected=f"{activity.expected_by_now:.1f}",
        baseline=f"{activity.baseline_per_week:g}",
    )
    return [{"role": "user", "content": content}]


def compose_checkin(
    conn: sqlite3.Connection,
    *,
    activity: WeekActivity,
    today: date,
    me: str | None = None,
    log: str | None = None,
    history: str | None = None,
    trace_id: int | None = None,
    model_prefs_path: Path | None = None,
) -> tuple[str, list[list[dict[str, str]]], int | None]:
    """Return the question, its keyboard, and the call that wrote it.

    A failure here falls back to a fixed sentence rather than dropping the
    check-in: the decision to ask was already made deterministically, and the
    buttons are the part that carries the value.

    Args:
        conn: Open database connection.
        activity: The measured shortfall.
        today: The day the question is being asked.
        me: Contents of me.md.
        log: Recent entries from log.md.
        history: Recent weekly memory entries.
        trace_id: Trace to attach the call to.
        model_prefs_path: Profile model preferences.

    Returns:
        A (text, keyboard, llm_call_id) triple.
    """
    from config import MAX_TOKENS_CHECKIN
    from llm import call_llm
    from model_prefs import resolve_model_route

    question = _FALLBACK_QUESTION
    llm_call_id: int | None = None
    try:
        route = resolve_model_route("checkin", path=model_prefs_path).call_kwargs()
        result = call_llm(
            build_checkin_messages(
                me=me, log=log, history=history, activity=activity, today=today
            ),
            **route,
            max_tokens=MAX_TOKENS_CHECKIN,
            conn=conn,
            request_type="checkin",
            trace_id=trace_id,
            metadata={"sessions": activity.sessions, "date": today.isoformat()},
        )
        text = result.text.strip()
        if text:
            question = text
            llm_call_id = result.llm_call_id
    except Exception as exc:  # noqa: BLE001 - the buttons matter, the prose does not
        logger.error("Check-in phrasing failed, using the fixed question: %s", exc)

    week_start = _week_start(today).isoformat()
    return question, checkin_keyboard(week_start), llm_call_id


def record_asked(
    conn: sqlite3.Connection,
    *,
    week_start: str,
    activity: WeekActivity,
    message_id: int | None,
    llm_call_id: int | None,
    now: datetime | None = None,
) -> None:
    """Record that this week's check-in went out."""
    moment = (now or datetime.now(timezone.utc)).isoformat()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO checkin (week_start, asked_at, sessions, "
                "expected, message_id, llm_call_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    week_start,
                    moment,
                    activity.sessions,
                    activity.expected_by_now,
                    message_id,
                    llm_call_id,
                ),
            )
    except sqlite3.Error as exc:
        logger.warning("Could not record check-in: %s", exc)


def record_answer(
    conn: sqlite3.Connection,
    *,
    week_start: str,
    answer: str,
    note: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Record an answer and return the journal line it should write.

    Args:
        conn: Open database connection.
        week_start: ISO Monday of the week being answered for.
        answer: One of :data:`CHOICE_BY_KEY`.
        note: Free text, when the person wrote their own.
        now: Override for the current moment, for tests.

    Returns:
        The line to append to log.md, or None when nothing should be written —
        a bare "add a note" tap that has not been followed by any text yet.
    """
    choice = CHOICE_BY_KEY.get(answer)
    if choice is None:
        logger.warning("Unknown check-in answer %r", answer)
        return None

    moment = (now or datetime.now(timezone.utc)).isoformat()
    try:
        with conn:
            conn.execute(
                "UPDATE checkin SET answered_at = ?, answer = ?, note = ? "
                "WHERE week_start = ?",
                (moment, answer, note, week_start),
            )
    except sqlite3.Error as exc:
        logger.warning("Could not record check-in answer: %s", exc)
        raise

    text = (note or "").strip() or choice.journal
    return text or None


def journal_entry(line: str, *, today: date) -> str:
    """Format an answer as a dated log.md bullet, matching the file's shape."""
    return f"- {today.isoformat()} — {line}"
