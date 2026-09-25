"""Computed goal adherence and the triggers that stop the coach skipping.

The weekly coach is the one feature allowed to propose strategy changes, and
for months it answered SKIP every week: it was told "when in doubt, SKIP" and
handed no evidence that anything was off. A goal set "for the next few weeks"
went stale for months without a word.

This module gives the coach that evidence, computed rather than judged: how
each stored weekly target fared over the recent completed weeks, and whether
the goals' own review date has passed. Two conditions force a review — a target
missed in most recent weeks, and a passed review date. Each is recorded when it
fires, so the same one cannot be raised every Monday. A target met every week is
reported, never forced: under a consistency goal that is the plan working.

Public API:
    GoalCheck         — the rendered facts plus the triggers that fired.
    Trigger           — one condition forcing a review.
    build_goal_check  — compute both for the coach prompt.
    record_triggers   — mark triggers raised once a review is delivered.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from challenges import active_challenge, last_ended_on, open_proposal
from config import (
    ADHERENCE_MIN_WEEKS,
    ADHERENCE_MISS_SHARE,
    ADHERENCE_WINDOW_WEEKS,
    CHALLENGE_COOLDOWN_WEEKS,
    CHALLENGE_DUE_MET_SHARE,
    COACH_TRIGGER_COOLDOWN_WEEKS,
)
from weekly_progress import measure_week, ring_label
from weekly_targets import StoredTarget, load_targets, week_start_for

logger = logging.getLogger(__name__)

_REVIEW_DATE = re.compile(r"review by (\d{4}-\d{2}-\d{2})", re.IGNORECASE)


@dataclass(frozen=True)
class Trigger:
    """One condition that forces the coach to propose instead of skipping.

    Attributes:
        key: Stable identity used for the cooldown ledger.
        sentence: What the coach is told, with the figures behind it.
    """

    key: str
    sentence: str


@dataclass
class GoalCheck:
    """Goal adherence facts for the coach prompt.

    Attributes:
        text: Markdown for the prompt section.
        triggers: Conditions that fired this run, after cooldowns.
        challenge_due: Whether the coach must propose a challenge this run.
    """

    text: str
    triggers: list[Trigger] = field(default_factory=list)
    challenge_due: bool = False


@dataclass
class _WeekResult:
    """How one target fared in one completed week."""

    week_start: str
    actual: float
    target: float

    @property
    def met(self) -> bool:
        return self.actual >= self.target


def _week_label(week_start: str) -> str:
    """Return a compact ISO week label such as ``W38``."""
    return f"W{date.fromisoformat(week_start).isocalendar().week:02d}"


def _fmt(value: float, decimals: int) -> str:
    """Format a measured or target value without a dangling decimal point."""
    if decimals <= 0 or float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.{decimals}f}"


def _completed_target_weeks(conn: sqlite3.Connection, today: date) -> list[str]:
    """Return recent completed weeks that had stored targets, newest first."""
    current_monday = week_start_for(today)
    try:
        rows = conn.execute(
            "SELECT DISTINCT week_start FROM weekly_target "
            "WHERE week_start < ? ORDER BY week_start DESC LIMIT ?",
            (current_monday, ADHERENCE_WINDOW_WEEKS),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("Could not read weekly target history: %s", exc)
        return []
    return [row[0] for row in rows]


def _adherence(
    conn: sqlite3.Connection, weeks: list[str]
) -> tuple[
    dict[tuple[str, str], list[_WeekResult]], dict[tuple[str, str], StoredTarget]
]:
    """Measure each week's stored targets over the whole of that week."""
    results: dict[tuple[str, str], list[_WeekResult]] = {}
    latest: dict[tuple[str, str], StoredTarget] = {}
    for week_start in weeks:
        targets = load_targets(conn, week_start)
        sunday = date.fromisoformat(week_start) + timedelta(days=6)
        for ring in measure_week(conn, targets, week_start=week_start, today=sunday):
            slot = ring.target.slot
            results.setdefault(slot, []).append(
                _WeekResult(week_start, ring.actual, ring.target.target)
            )
            latest.setdefault(slot, ring.target)
    return results, latest


def _last_raised(conn: sqlite3.Connection, key: str) -> date | None:
    """Return when a trigger last forced a review, or None."""
    try:
        row = conn.execute(
            "SELECT last_raised FROM coach_trigger WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("Could not read coach trigger ledger: %s", exc)
        return None
    return date.fromisoformat(row[0][:10]) if row else None


def _cooling_down(conn: sqlite3.Connection, key: str, today: date) -> bool:
    """Return whether a trigger fired too recently to fire again."""
    raised = _last_raised(conn, key)
    if raised is None:
        return False
    return (today - raised).days < COACH_TRIGGER_COOLDOWN_WEEKS * 7


def _review_dates(strategy_md: str | None) -> list[date]:
    """Return the review dates written into the strategy, earliest first."""
    found: list[date] = []
    for match in _REVIEW_DATE.finditer(strategy_md or ""):
        try:
            found.append(date.fromisoformat(match.group(1)))
        except ValueError:
            continue
    return sorted(found)


def build_goal_check(
    conn: sqlite3.Connection,
    strategy_md: str | None,
    *,
    today: date,
) -> GoalCheck:
    """Compute goal adherence facts and the triggers that force a review.

    Args:
        conn: Open database connection.
        strategy_md: Current strategy.md text, for its review dates.
        today: The day the coach runs.

    Returns:
        The prompt section and any triggers that fired, cooldowns applied.
    """
    lines: list[str] = []
    triggers: list[Trigger] = []
    all_met_most_weeks = False

    weeks = _completed_target_weeks(conn, today)
    if not weeks:
        lines.append("No completed weeks with weekly targets yet.")
    else:
        results, latest = _adherence(conn, weeks)
        span = f"{_week_label(weeks[-1])}–{_week_label(weeks[0])}"
        noun = "week" if len(weeks) == 1 else "weeks"
        lines.append(
            f"Weekly targets over the last {len(weeks)} completed {noun} "
            f"with targets ({span}), oldest first:"
        )
        most_recent = weeks[0]
        current_shares: list[float] = []
        for slot, history in results.items():
            item = latest[slot]
            history = sorted(history, key=lambda r: r.week_start)
            decimals = item.spec.decimals
            met = sum(1 for r in history if r.met)
            per_week = ", ".join(
                f"{_week_label(r.week_start)} {_fmt(r.actual, decimals)}"
                f"/{_fmt(r.target, decimals)}"
                for r in history
            )
            label = ring_label(item)
            unit = item.spec.unit
            lines.append(
                f"- {label} ({_fmt(item.target, decimals)} {unit}/week): "
                f"met {met} of {len(history)} — {per_week}"
            )
            missed = len(history) - met
            still_current = any(r.week_start == most_recent for r in history)
            if still_current:
                current_shares.append(met / len(history))
            if (
                still_current
                and len(history) >= ADHERENCE_MIN_WEEKS
                and missed / len(history) >= ADHERENCE_MISS_SHARE
            ):
                key = f"miss:{item.slot_label}"
                if not _cooling_down(conn, key, today):
                    triggers.append(
                        Trigger(
                            key=key,
                            sentence=(
                                f"{label} ({_fmt(item.target, decimals)} "
                                f"{unit}/week) was missed in {missed} of the "
                                f"last {len(history)} weeks: {per_week}."
                            ),
                        )
                    )
        all_met_most_weeks = (
            len(weeks) >= ADHERENCE_MIN_WEEKS
            and bool(current_shares)
            and min(current_shares) >= CHALLENGE_DUE_MET_SHARE
        )
        if len(weeks) < ADHERENCE_MIN_WEEKS:
            lines.append(
                f"Fewer than {ADHERENCE_MIN_WEEKS} completed weeks: too few to "
                "call any target persistently missed."
            )

    review_dates = _review_dates(strategy_md)
    if not review_dates:
        lines.append("The goals carry no review date.")
    else:
        due = review_dates[0]
        days = (due - today).days
        if days >= 0:
            lines.append(f"Goals' review date: {due.isoformat()}, in {days} days.")
        else:
            lines.append(
                f"Goals' review date: {due.isoformat()}, passed {-days} days ago."
            )
            key = f"review:{due.isoformat()}"
            if not _cooling_down(conn, key, today):
                triggers.append(
                    Trigger(
                        key=key,
                        sentence=(
                            f"The goals' review date ({due.isoformat()}) passed "
                            f"{-days} days ago."
                        ),
                    )
                )

    challenge_due, challenge_line = _challenge_due(
        conn, all_met_most_weeks=all_met_most_weeks, today=today
    )
    lines.append(challenge_line)

    lines.append("")
    if triggers or challenge_due:
        lines.append("**Review required this week** — SKIP is not an option:")
        lines.extend(f"- {trigger.sentence}" for trigger in triggers)
        if challenge_due:
            lines.append("- A challenge is due: propose one with `propose_challenge`.")
    else:
        lines.append("Review required this week: no.")
    return GoalCheck(
        text="\n".join(lines), triggers=triggers, challenge_due=challenge_due
    )


def _challenge_due(
    conn: sqlite3.Connection, *, all_met_most_weeks: bool, today: date
) -> tuple[bool, str]:
    """Decide in code whether a challenge is due, and say why in one line.

    Left to the model, "occasional" meant anything from one proposal in eight
    identical weeks to four in four. A challenge is due only when every current
    target was met in most recent weeks, nothing is active or awaiting a
    decision, and ``CHALLENGE_COOLDOWN_WEEKS`` have passed since the last one
    stopped.
    """
    try:
        if active_challenge(conn) is not None:
            return False, "Challenge due: no — one is active."
        if open_proposal(conn) is not None:
            return False, "Challenge due: no — a proposal awaits the user's decision."
        ended = last_ended_on(conn)
    except sqlite3.Error as exc:
        logger.warning("Could not read challenges: %s", exc)
        return False, "Challenge due: no — challenge history unavailable."
    if ended is not None and (today - ended).days < CHALLENGE_COOLDOWN_WEEKS * 7:
        return False, (
            f"Challenge due: no — the last one stopped on {ended.isoformat()}, "
            f"within {CHALLENGE_COOLDOWN_WEEKS} weeks."
        )
    if not all_met_most_weeks:
        return False, "Challenge due: no — the targets are not yet met most weeks."
    return True, (
        "Challenge due: yes — every current target was met in most recent weeks "
        "and no challenge ran lately."
    )


def record_triggers(
    conn: sqlite3.Connection,
    triggers: list[Trigger],
    *,
    today: date,
    llm_call_id: int | None,
) -> None:
    """Mark triggers as raised once the review addressing them is delivered.

    Recorded only after delivery: a coach that skipped despite a trigger has
    not raised it, so the trigger should fire again next week.

    Args:
        conn: Open database connection.
        triggers: Triggers the delivered review addressed.
        today: The day of the review.
        llm_call_id: The coach call, for ``llm-log --id``.
    """
    if not triggers:
        return
    stamp = today.isoformat()
    try:
        with conn:
            conn.executemany(
                """
                INSERT INTO coach_trigger (key, first_raised, last_raised, llm_call_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    last_raised = excluded.last_raised,
                    llm_call_id = excluded.llm_call_id
                """,
                [(t.key, stamp, stamp, llm_call_id) for t in triggers],
            )
    except sqlite3.Error as exc:
        logger.warning(
            "Could not record coach triggers at %s: %s",
            datetime.now(timezone.utc).isoformat(),
            exc,
        )
