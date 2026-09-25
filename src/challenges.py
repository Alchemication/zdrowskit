"""Coach challenges: temporary, measurable pushes toward a stated goal.

A challenge is one weekly metric from the targets vocabulary, a per-week number
and a length in weeks — "4 runs a week for 2 weeks". The coach proposes it, the
user accepts or rejects it, and code measures it: whether a week was met is the
same query the progress strip runs, never a model's reading of a workout. That
is the point. A suggested tempo run once became a permanent strategy rule that
models argued about for months, because nothing measured it and nothing ended
it. A challenge is measured, and it ends.

Lifecycle: ``proposed`` becomes ``active`` on accept, ``rejected`` on reject, or
``expired`` when left untapped. An active challenge becomes ``dropped`` if the
user ends it, and otherwise closes after its last week as ``achieved`` (every
week met), ``partial`` or ``missed``. The outcome is frozen with the rule
version that scored it, so a later change to how a week is counted cannot
rewrite history.

Public API:
    Challenge              — one stored challenge.
    ChallengeError         — a proposal that fails validation, with the reason.
    coerce_proposal        — validate a coach's proposal.
    propose / accept / reject / drop / set_reason — lifecycle writes.
    active_challenge / open_proposal / get_challenge / proposal_for_call — reads.
    set_reason_prompt / challenge_for_reason_prompt — route a reason reply.
    last_ended_on          — when the latest challenge stopped, for the cooldown.
    expire_stale_proposals / close_finished — housekeeping, idempotent.
    describe_challenge     — the one-line spec, e.g. "Runs: 4 sessions a week for 2 weeks".
    describe_active        — the active challenge and its progress, for prompts.
    describe_history       — past challenges and declined proposals, for the coach.
    nudge_challenge_text   — the active challenge, only when this sync is news for it.
    challenge_status_text  — the active challenge or open proposal, for chat and coach.
    outcome_message        — the announcement sent when a challenge closes.
    propose_challenge_tool — the coach's tool definition.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from config import (
    CHALLENGE_HISTORY_COUNT,
    CHALLENGE_NUDGE_WEEK_END_DAYS,
    CHALLENGE_LAST_START_WEEKDAY,
    CHALLENGE_MAX_WEEKS,
    CHALLENGE_PARTIAL_SHARE,
    CHALLENGE_PROPOSAL_TTL_DAYS,
)
from weekly_progress import measure_target, ring_label
from weekly_targets import (
    SPEC_BY_KEY,
    TARGET_SPECS,
    StoredTarget,
    coerce_target,
    week_start_for,
)

logger = logging.getLogger(__name__)

RULE_VERSION = 1
"""Version of the scoring rule stored with each outcome.

Bump it whenever how a challenge week is measured or scored changes, so outcomes
recorded under the old rule stay distinguishable from new ones.
"""

PROPOSED = "proposed"
ACTIVE = "active"
REJECTED = "rejected"
EXPIRED = "expired"
DROPPED = "dropped"
ACHIEVED = "achieved"
PARTIAL = "partial"
MISSED = "missed"
CLOSED_STATUSES = (ACHIEVED, PARTIAL, MISSED, DROPPED)

PROPOSAL_NOTED = (
    "Noted. It is shown to the user with Accept and Reject buttons below the review."
)
"""Tool result for a valid proposal, shared by production and the evals."""

_MAX_TITLE_CHARS = 80
"""Validation bound: a title is a label on a button, not a paragraph."""


class ChallengeError(ValueError):
    """A proposed challenge that cannot be stored, with a reason for the model."""


@dataclass(frozen=True)
class Challenge:
    """One stored challenge.

    Attributes:
        id: Row id.
        status: Lifecycle status, one of the module constants.
        title: The coach's short name for it.
        goal: The strategy goal line it serves, quoted.
        rationale: Why the coach proposed it.
        metric: Targets-vocabulary key.
        category: Activity the metric measures, or the empty string.
        target: Per-week number to reach.
        threshold: Per-day bar for threshold metrics, else None.
        weeks: Length in weeks.
        start_week: ISO Monday of week one, once accepted.
        end_date: ISO Sunday of the last week, once accepted.
        proposed_at: ISO timestamp of the proposal.
        decided_at: ISO timestamp of accept, reject or expiry.
        closed_at: ISO timestamp of the final outcome or drop.
        outcome: Frozen per-week results once closed, else None.
        reason: The user's reason for rejecting or dropping it, if given.
        llm_call_id: The coach call that proposed it.
    """

    id: int
    status: str
    title: str
    goal: str
    rationale: str | None
    metric: str
    category: str
    target: float
    threshold: float | None
    weeks: int
    start_week: str | None
    end_date: str | None
    proposed_at: str
    decided_at: str | None
    closed_at: str | None
    outcome: dict | None
    reason: str | None
    llm_call_id: int | None

    @property
    def stored_target(self) -> StoredTarget:
        """Return the weekly target this challenge measures against."""
        return StoredTarget(
            spec=SPEC_BY_KEY[self.metric],
            category=self.category,
            target=self.target,
            threshold=self.threshold,
            goal_text=self.goal,
            strategy_hash=None,
            llm_call_id=self.llm_call_id,
        )


@dataclass(frozen=True)
class Proposal:
    """A validated challenge proposal, not yet stored."""

    title: str
    goal: str
    rationale: str | None
    target: StoredTarget
    weeks: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt(value: float) -> str:
    return f"{value:.0f}" if float(value).is_integer() else f"{value:g}"


def _row_to_challenge(row: sqlite3.Row) -> Challenge:
    outcome = json.loads(row["outcome_json"]) if row["outcome_json"] else None
    return Challenge(
        id=row["id"],
        status=row["status"],
        title=row["title"],
        goal=row["goal"],
        rationale=row["rationale"],
        metric=row["metric"],
        category=row["category"] or "",
        target=float(row["target"]),
        threshold=row["threshold"],
        weeks=int(row["weeks"]),
        start_week=row["start_week"],
        end_date=row["end_date"],
        proposed_at=row["proposed_at"],
        decided_at=row["decided_at"],
        closed_at=row["closed_at"],
        outcome=outcome,
        reason=row["reason"],
        llm_call_id=row["llm_call_id"],
    )


def coerce_proposal(raw: dict, known_types: frozenset[str]) -> Proposal:
    """Validate a coach's ``propose_challenge`` arguments.

    Args:
        raw: Tool-call arguments.
        known_types: Workout types this profile has recorded, for ``type:``
            categories.

    Returns:
        The validated proposal.

    Raises:
        ChallengeError: With a reason the model can act on.
    """
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ChallengeError("title is required")
    if len(title) > _MAX_TITLE_CHARS:
        raise ChallengeError(f"title must be at most {_MAX_TITLE_CHARS} characters")
    goal = str(raw.get("goal") or "").strip()
    if not goal:
        raise ChallengeError(
            "goal is required: quote the strategy goal this challenge serves"
        )
    try:
        weeks = int(raw.get("weeks"))
    except (TypeError, ValueError) as exc:
        raise ChallengeError("weeks must be a whole number") from exc
    if not 1 <= weeks <= CHALLENGE_MAX_WEEKS:
        raise ChallengeError(f"weeks must be between 1 and {CHALLENGE_MAX_WEEKS}")
    target = coerce_target(
        {
            "metric": raw.get("metric"),
            "category": raw.get("category"),
            "target": raw.get("target"),
            "threshold": raw.get("threshold"),
            "goal": goal,
        },
        strategy_hash="",
        known_types=known_types,
    )
    if target is None:
        raise ChallengeError(
            "metric, category, target or threshold is invalid for the "
            "vocabulary; see the tool description"
        )
    rationale = str(raw.get("rationale") or "").strip() or None
    return Proposal(
        title=title, goal=goal, rationale=rationale, target=target, weeks=weeks
    )


def get_challenge(conn: sqlite3.Connection, challenge_id: int) -> Challenge | None:
    """Return one challenge by id, or None."""
    row = conn.execute(
        "SELECT * FROM challenge WHERE id = ?", (challenge_id,)
    ).fetchone()
    return _row_to_challenge(row) if row else None


def _one_with_status(conn: sqlite3.Connection, status: str) -> Challenge | None:
    row = conn.execute(
        "SELECT * FROM challenge WHERE status = ? ORDER BY id DESC LIMIT 1",
        (status,),
    ).fetchone()
    return _row_to_challenge(row) if row else None


def active_challenge(conn: sqlite3.Connection) -> Challenge | None:
    """Return the active challenge, or None."""
    return _one_with_status(conn, ACTIVE)


def open_proposal(conn: sqlite3.Connection) -> Challenge | None:
    """Return the proposal awaiting a decision, or None."""
    return _one_with_status(conn, PROPOSED)


def propose(
    conn: sqlite3.Connection, proposal: Proposal, *, llm_call_id: int | None
) -> int:
    """Store a proposal, expiring any earlier one still awaiting a decision.

    Args:
        conn: Open database connection.
        proposal: A proposal from :func:`coerce_proposal`.
        llm_call_id: The coach call that made it.

    Returns:
        The new challenge id.

    Raises:
        ChallengeError: When a challenge is already active.
    """
    if active_challenge(conn) is not None:
        raise ChallengeError("a challenge is already active; only one runs at a time")
    now = _now()
    target = proposal.target
    with conn:
        conn.execute(
            "UPDATE challenge SET status = ?, decided_at = ? WHERE status = ?",
            (EXPIRED, now, PROPOSED),
        )
        cursor = conn.execute(
            """
            INSERT INTO challenge (status, title, goal, rationale, metric,
                category, target, threshold, weeks, proposed_at, llm_call_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                PROPOSED,
                proposal.title,
                proposal.goal,
                proposal.rationale,
                target.spec.key,
                target.category,
                target.target,
                target.threshold,
                proposal.weeks,
                now,
                llm_call_id,
            ),
        )
    return int(cursor.lastrowid)


def _start_week_for(accepted_on: date) -> str:
    """Return week one's Monday: this week if accepted early enough, else next."""
    if accepted_on.weekday() <= CHALLENGE_LAST_START_WEEKDAY:
        return week_start_for(accepted_on)
    return week_start_for(accepted_on + timedelta(days=7))


def accept(
    conn: sqlite3.Connection, challenge_id: int, *, today: date
) -> Challenge | None:
    """Start a proposed challenge.

    Returns:
        The active challenge, or None when it is no longer a proposal or
        another challenge is already active.
    """
    current = get_challenge(conn, challenge_id)
    if current is None or current.status != PROPOSED:
        return None
    if active_challenge(conn) is not None:
        return None
    start = _start_week_for(today)
    end = (
        date.fromisoformat(start) + timedelta(days=7 * current.weeks - 1)
    ).isoformat()
    with conn:
        conn.execute(
            "UPDATE challenge SET status = ?, start_week = ?, end_date = ?, "
            "decided_at = ? WHERE id = ?",
            (ACTIVE, start, end, _now(), challenge_id),
        )
    return get_challenge(conn, challenge_id)


def reject(conn: sqlite3.Connection, challenge_id: int) -> Challenge | None:
    """Reject a proposed challenge. Returns None if it was not a proposal."""
    current = get_challenge(conn, challenge_id)
    if current is None or current.status != PROPOSED:
        return None
    with conn:
        conn.execute(
            "UPDATE challenge SET status = ?, decided_at = ? WHERE id = ?",
            (REJECTED, _now(), challenge_id),
        )
    return get_challenge(conn, challenge_id)


def drop(conn: sqlite3.Connection, challenge_id: int) -> Challenge | None:
    """End an active challenge early. Returns None if it was not active."""
    current = get_challenge(conn, challenge_id)
    if current is None or current.status != ACTIVE:
        return None
    with conn:
        conn.execute(
            "UPDATE challenge SET status = ?, closed_at = ? WHERE id = ?",
            (DROPPED, _now(), challenge_id),
        )
    return get_challenge(conn, challenge_id)


def set_reason(conn: sqlite3.Connection, challenge_id: int, reason: str) -> None:
    """Record the user's reason for rejecting or dropping a challenge."""
    with conn:
        conn.execute(
            "UPDATE challenge SET reason = ? WHERE id = ?",
            (reason.strip()[:500], challenge_id),
        )


def proposal_for_call(
    conn: sqlite3.Connection, llm_call_id: int | None
) -> Challenge | None:
    """Return the open proposal a given coach call made, or None."""
    if llm_call_id is None:
        return None
    row = conn.execute(
        "SELECT * FROM challenge WHERE status = ? AND llm_call_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (PROPOSED, llm_call_id),
    ).fetchone()
    return _row_to_challenge(row) if row else None


def set_reason_prompt(
    conn: sqlite3.Connection, challenge_id: int, prompt_id: int
) -> None:
    """Remember the Telegram message asking why, so the reply finds its row."""
    with conn:
        conn.execute(
            "UPDATE challenge SET reason_prompt_id = ? WHERE id = ?",
            (prompt_id, challenge_id),
        )


def challenge_for_reason_prompt(
    conn: sqlite3.Connection, prompt_id: int
) -> Challenge | None:
    """Return the challenge whose reason prompt is *prompt_id*, or None."""
    row = conn.execute(
        "SELECT * FROM challenge WHERE reason_prompt_id = ?", (prompt_id,)
    ).fetchone()
    return _row_to_challenge(row) if row else None


def expire_stale_proposals(conn: sqlite3.Connection, *, today: date) -> int:
    """Expire proposals left untapped past ``CHALLENGE_PROPOSAL_TTL_DAYS``."""
    cutoff = (today - timedelta(days=CHALLENGE_PROPOSAL_TTL_DAYS)).isoformat()
    with conn:
        cursor = conn.execute(
            "UPDATE challenge SET status = ?, decided_at = ? "
            "WHERE status = ? AND substr(proposed_at, 1, 10) < ?",
            (EXPIRED, _now(), PROPOSED, cutoff),
        )
    return cursor.rowcount


def _week_results(
    conn: sqlite3.Connection, challenge: Challenge, *, through: date
) -> list[dict]:
    """Measure each started week of a challenge, up to *through*."""
    if not challenge.start_week:
        return []
    item = challenge.stored_target
    monday = date.fromisoformat(challenge.start_week)
    results: list[dict] = []
    for index in range(challenge.weeks):
        week_start = monday + timedelta(days=7 * index)
        if week_start > through:
            break
        week_end = week_start + timedelta(days=6)
        complete = week_end < through
        actual, _ = measure_target(
            conn,
            item,
            week_start.isoformat(),
            min(week_end, through).isoformat(),
        )
        results.append(
            {
                "week": index + 1,
                "week_start": week_start.isoformat(),
                "actual": actual,
                "target": challenge.target,
                "met": actual >= challenge.target,
                "complete": complete,
            }
        )
    return results


def _score(weeks_met: int, weeks: int) -> str:
    if weeks_met >= weeks:
        return ACHIEVED
    if weeks_met / weeks >= CHALLENGE_PARTIAL_SHARE:
        return PARTIAL
    return MISSED


def close_finished(conn: sqlite3.Connection, *, today: date) -> list[Challenge]:
    """Score and close active challenges whose last week has ended.

    Idempotent: a closed challenge is never rescored.

    Args:
        conn: Open database connection.
        today: The current date. A challenge closes once its end date is past.

    Returns:
        Challenges closed by this call, for announcement.
    """
    closed: list[Challenge] = []
    active = active_challenge(conn)
    if active is None or not active.end_date:
        return closed
    if date.fromisoformat(active.end_date) >= today:
        return closed
    results = _week_results(conn, active, through=today)
    weeks_met = sum(1 for r in results if r["met"])
    status = _score(weeks_met, active.weeks)
    outcome = {"weeks_met": weeks_met, "weeks": active.weeks, "per_week": results}
    with conn:
        conn.execute(
            "UPDATE challenge SET status = ?, closed_at = ?, outcome_json = ?, "
            "rule_version = ? WHERE id = ? AND status = ?",
            (status, _now(), json.dumps(outcome), RULE_VERSION, active.id, ACTIVE),
        )
    updated = get_challenge(conn, active.id)
    if updated is not None:
        closed.append(updated)
    return closed


def describe_challenge(challenge: Challenge | Proposal) -> str:
    """Return the one-line spec, e.g. ``Runs: 4 sessions a week for 2 weeks``."""
    target = (
        challenge.target if isinstance(challenge, Proposal) else challenge.stored_target
    )
    weeks = challenge.weeks
    return (
        f"{ring_label(target)}: {_fmt(target.target)} {target.spec.unit} a week "
        f"for {weeks} {'week' if weeks == 1 else 'weeks'}"
    )


def describe_active(conn: sqlite3.Connection, *, today: date) -> str | None:
    """Describe the active challenge and its progress, for a prompt.

    Returns:
        A few lines of text, or None when no challenge is active.
    """
    active = active_challenge(conn)
    if active is None or not active.start_week or not active.end_date:
        return None
    lines = [
        f"{active.title} — {describe_challenge(active)}, "
        f"{active.start_week} to {active.end_date}.",
        f"Proposed by the coach and accepted by the user. Serves: {active.goal}",
    ]
    if date.fromisoformat(active.start_week) > today:
        lines.append(f"Starts {active.start_week}; nothing to measure yet.")
        return "\n".join(lines)
    unit = active.stored_target.spec.unit
    for result in _week_results(conn, active, through=today):
        state = (
            "met" if result["met"] else ("missed" if result["complete"] else "so far")
        )
        lines.append(
            f"- Week {result['week']}: {_fmt(result['actual'])}/"
            f"{_fmt(result['target'])} {unit} ({state})"
        )
    days_left = (date.fromisoformat(active.end_date) - today).days
    lines.append(f"{days_left} days left.")
    return "\n".join(lines)


NO_CHALLENGE_NEWS = "(no challenge news in this sync — do not mention any challenge)"


def _sync_moves_challenge(
    conn: sqlite3.Connection, challenge: Challenge, workout_ids: set[str]
) -> bool:
    """Return whether any workout from this sync counts toward the challenge."""
    if challenge.metric not in {"sessions_week", "distance_km_week"} or not workout_ids:
        return False
    ids = sorted(workout_ids)
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT type, category, counts_as_lift, gpx_distance_km FROM workout_all "
        f"WHERE start_utc IN ({placeholders})",  # noqa: S608
        ids,
    ).fetchall()
    wanted = challenge.category
    for row in rows:
        if challenge.metric == "distance_km_week" and not row["gpx_distance_km"]:
            continue
        if wanted == "any":
            return True
        if wanted == "lift" and row["counts_as_lift"]:
            return True
        if wanted.startswith("type:") and row["type"] == wanted[len("type:") :]:
            return True
        if wanted not in {"lift", "any"} and row["category"] == wanted:
            return True
    return False


def nudge_challenge_text(
    conn: sqlite3.Connection, *, workout_ids: set[str], today: date
) -> str:
    """Return the active challenge for a nudge, only when this sync is news for it.

    News means a workout from this sync counts toward it, or — for metrics that
    change with nearly every sync, like sleep nights — the challenge week closes
    within ``CHALLENGE_NUDGE_WEEK_END_DAYS``. Deciding this in code, not in the
    prompt, is what keeps a challenge from appearing in every nudge.

    Returns:
        The progress text, or ``NO_CHALLENGE_NEWS``.
    """
    active = active_challenge(conn)
    if active is None or not active.start_week:
        return NO_CHALLENGE_NEWS
    if date.fromisoformat(active.start_week) > today:
        return NO_CHALLENGE_NEWS
    moved = _sync_moves_challenge(conn, active, workout_ids)
    week_ending = active.metric not in {"sessions_week", "distance_km_week"} and (
        6 - today.weekday() < CHALLENGE_NUDGE_WEEK_END_DAYS
    )
    if not (moved or week_ending):
        return NO_CHALLENGE_NEWS
    return describe_active(conn, today=today) or NO_CHALLENGE_NEWS


def challenge_status_text(conn: sqlite3.Connection, *, today: date) -> str:
    """Describe the active challenge or an open proposal, for chat and coach."""
    active = describe_active(conn, today=today)
    if active:
        return f"Active challenge:\n{active}"
    pending = open_proposal(conn)
    if pending is not None:
        return (
            f"A challenge proposed on {pending.proposed_at[:10]} is still awaiting "
            f"the user's decision: {describe_challenge(pending)}."
        )
    return "No challenge is active."


def last_ended_on(conn: sqlite3.Connection) -> date | None:
    """Return when the most recent challenge ended or was declined, or None.

    Counts every way a challenge stops: finished, dropped, rejected, expired.
    """
    row = conn.execute(
        "SELECT MAX(COALESCE(closed_at, decided_at)) FROM challenge "
        "WHERE status NOT IN (?, ?)",
        (ACTIVE, PROPOSED),
    ).fetchone()
    if not row or not row[0]:
        return None
    return date.fromisoformat(str(row[0])[:10])


def describe_history(conn: sqlite3.Connection) -> str:
    """Describe recent past challenges and declined proposals, for the coach."""
    rows = conn.execute(
        "SELECT * FROM challenge WHERE status NOT IN (?, ?) ORDER BY id DESC LIMIT ?",
        (ACTIVE, PROPOSED, CHALLENGE_HISTORY_COUNT),
    ).fetchall()
    if not rows:
        return "No challenges yet."
    lines: list[str] = []
    for row in rows:
        challenge = _row_to_challenge(row)
        spec = describe_challenge(challenge)
        when = (challenge.closed_at or challenge.decided_at or challenge.proposed_at)[
            :10
        ]
        if challenge.outcome:
            detail = (
                f"{challenge.status}, {challenge.outcome['weeks_met']} of "
                f"{challenge.outcome['weeks']} weeks met"
            )
        else:
            detail = challenge.status
        line = f"- {when} · {spec} · {detail}"
        if challenge.reason:
            line += f' · reason: "{challenge.reason}"'
        lines.append(line)
    return "\n".join(lines)


def outcome_message(challenge: Challenge) -> str:
    """Return the Telegram announcement for a closed challenge."""
    outcome = challenge.outcome or {"weeks_met": 0, "weeks": challenge.weeks}
    unit = challenge.stored_target.spec.unit
    headline = {
        ACHIEVED: "✅ Challenge complete",
        PARTIAL: "➖ Challenge partly done",
        MISSED: "Challenge finished",
    }.get(challenge.status, "Challenge finished")
    per_week = ", ".join(
        f"week {r['week']} {_fmt(r['actual'])}/{_fmt(r['target'])}"
        for r in outcome.get("per_week", [])
    )
    return (
        f"{headline}: {challenge.title}\n"
        f"{outcome['weeks_met']} of {outcome['weeks']} weeks met ({per_week} {unit})."
    )


def propose_challenge_tool() -> list[dict]:
    """Return the coach's ``propose_challenge`` tool definition.

    The metric vocabulary is the weekly-targets one, so anything the coach can
    propose is something the progress strip can already measure.
    """
    metrics = [spec.key for spec in TARGET_SPECS]
    categories = sorted({c for spec in TARGET_SPECS for c in spec.categories})
    lines = []
    for spec in TARGET_SPECS:
        extra = []
        if spec.categories:
            extra.append(f"category one of {', '.join(spec.categories)}")
        if spec.threshold_unit:
            extra.append(f"threshold in {spec.threshold_unit} required")
        suffix = f" ({'; '.join(extra)})" if extra else ""
        lines.append(f"{spec.key}: counts {spec.unit} per week{suffix}")
    return [
        {
            "type": "function",
            "function": {
                "name": "propose_challenge",
                "description": (
                    "Propose one temporary challenge: a per-week number on one "
                    "measurable weekly metric, for 1 to "
                    f"{CHALLENGE_MAX_WEEKS} weeks. The user accepts or rejects "
                    "it; code measures it. Metrics: " + "; ".join(lines) + "."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Short name, under 80 characters.",
                        },
                        "goal": {
                            "type": "string",
                            "description": (
                                "The strategy goal line this serves, quoted."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": "One or two sentences citing the data.",
                        },
                        "metric": {"type": "string", "enum": metrics},
                        "category": {
                            "type": "string",
                            "description": (
                                "Activity for metrics that take one: "
                                + ", ".join(categories)
                            ),
                        },
                        "target": {
                            "type": "number",
                            "description": "Per-week number to reach.",
                        },
                        "threshold": {
                            "type": "number",
                            "description": "Per-day bar, for threshold metrics only.",
                        },
                        "weeks": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": CHALLENGE_MAX_WEEKS,
                        },
                    },
                    "required": ["title", "goal", "metric", "target", "weeks"],
                },
            },
        }
    ]
