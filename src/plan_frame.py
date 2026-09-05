"""Whether the training plan is currently the right frame to judge a week by.

A progress bar is a claim that the plan is what matters this week. Usually it
is. During a newborn's first fortnight, a bereavement, flu or a fresh injury it
is not, and a bar reading ``behind`` is then worse than no bar at all — it
measures someone against a commitment that stopped applying, in the one week
they would most notice.

Deciding that needs judgement about a life, which is what the journal and the
profile are for. So one small call reads them and answers a single question,
and its answer is cached: life context changes over days, not between two
notifications an hour apart, and a strip that appeared on one nudge and
vanished from the next would read as a bug.

**The call never sees the measurements.** It is given the person's context and
nothing about how their week is going, so it cannot suppress a bar for being
unflattering — the failure this whole mechanism would otherwise invite. Being
behind on an ordinary week is exactly what the strip exists to say.

Public API:
    PlanFrame          — the decision, its reason, and where it came from.
    MODES              — the three answers, widest first.
    resolve_plan_frame — cached decide-or-reuse for a profile.
    load_plan_frame    — read the cached decision without deciding.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    PLAN_FRAME_MAX_AGE_DAYS,
    PLAN_FRAME_SUPPRESSED_MAX_AGE_DAYS,
    PROMPTS_DIR,
)

logger = logging.getLogger(__name__)

PLAN_FRAME_PROMPT = "plan_frame_prompt.md"

MODE_FULL = "full"
MODE_FACTS = "facts"
MODE_HIDDEN = "hidden"

# Ordered widest to narrowest. `full` is the default in every uncertain case:
# the strip failing loudly is recoverable, the strip failing silently is not.
MODES: tuple[str, ...] = (MODE_FULL, MODE_FACTS, MODE_HIDDEN)

_SUPPRESSED_MODES = frozenset({MODE_FACTS, MODE_HIDDEN})


@dataclass(frozen=True)
class PlanFrame:
    """How much of the progress strip the person's current context warrants.

    Attributes:
        mode: One of :data:`MODES`.
        reason: One short sentence, for the event log and the CLI. Never shown
            to the user inside a notification.
        llm_call_id: The deciding call, for ``llm-log --id``.
        decided_at: ISO timestamp of the decision.
        source: ``llm``, ``cache``, or ``default`` when nothing decided it.
    """

    mode: str = MODE_FULL
    reason: str | None = None
    llm_call_id: int | None = None
    decided_at: str | None = None
    source: str = "default"

    @property
    def shows_strip(self) -> bool:
        """True when any progress should be rendered at all."""
        return self.mode != MODE_HIDDEN

    @property
    def shows_verdict(self) -> bool:
        """True when a bar may also carry its ``on pace`` / ``behind`` word.

        The numbers are facts about the week. The verdict is a judgement about
        the person, and in a week the plan does not fit, that judgement belongs
        to the coach's sentence underneath rather than to a column.
        """
        return self.mode == MODE_FULL


def context_digest(*parts: str | None) -> str:
    """Return a cache key over the context the decision was made from."""
    payload = "\n--\n".join((part or "").strip() for part in parts)
    return hashlib.sha256(payload.encode()).hexdigest()


def load_plan_frame(conn: sqlite3.Connection) -> PlanFrame | None:
    """Return the cached decision, or None when there is none."""
    try:
        row = conn.execute(
            "SELECT mode, reason, context_hash, llm_call_id, decided_at "
            "FROM plan_frame WHERE id = 1"
        ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("Plan frame cache unavailable: %s", exc)
        return None
    if row is None or row["mode"] not in MODES:
        return None
    return PlanFrame(
        mode=row["mode"],
        reason=row["reason"],
        llm_call_id=row["llm_call_id"],
        decided_at=row["decided_at"],
        source="cache",
    )


def _cached_hash(conn: sqlite3.Connection) -> str | None:
    """Return the context hash the cached decision was made from."""
    try:
        row = conn.execute(
            "SELECT context_hash FROM plan_frame WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    return row["context_hash"] if row else None


def _is_stale(frame: PlanFrame, now: datetime) -> bool:
    """Return True when a cached decision has to be made again.

    A suppression expires sooner than a normal week does, because the two
    mistakes are not symmetric: a strip wrongly shown is visible and can be
    complained about, while a strip wrongly hidden looks identical to a feature
    with nothing to say.
    """
    if not frame.decided_at:
        return True
    try:
        decided = datetime.fromisoformat(frame.decided_at)
    except ValueError:
        return True
    if decided.tzinfo is None:
        decided = decided.replace(tzinfo=timezone.utc)
    max_age = (
        PLAN_FRAME_SUPPRESSED_MAX_AGE_DAYS
        if frame.mode in _SUPPRESSED_MODES
        else PLAN_FRAME_MAX_AGE_DAYS
    )
    return now - decided > timedelta(days=max_age)


def _save(
    conn: sqlite3.Connection, frame: PlanFrame, context_hash: str, now: datetime
) -> None:
    """Persist a decision as the current one."""
    try:
        with conn:
            conn.execute(
                "INSERT INTO plan_frame (id, mode, reason, context_hash, "
                "llm_call_id, decided_at) VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET mode = excluded.mode, "
                "reason = excluded.reason, context_hash = excluded.context_hash, "
                "llm_call_id = excluded.llm_call_id, "
                "decided_at = excluded.decided_at",
                (
                    frame.mode,
                    frame.reason,
                    context_hash,
                    frame.llm_call_id,
                    now.isoformat(),
                ),
            )
    except sqlite3.Error as exc:
        logger.warning("Could not cache plan frame: %s", exc)


def build_plan_frame_messages(
    *,
    me: str | None,
    log: str | None,
    history: str | None,
    today: str,
    prompts_dir: Path = PROMPTS_DIR,
) -> list[dict[str, str]]:
    """Render the decision prompt.

    Deliberately carries no health data, no targets, and no measured progress.
    The question is whether the plan still applies to this person's life, and a
    model that could see the numbers would start answering a different question
    — whether the numbers are flattering.

    Args:
        me: Contents of me.md.
        log: Recent entries from log.md.
        history: Recent weekly memory entries.
        today: ISO date the decision is for.
        prompts_dir: Directory holding the prompt file.

    Returns:
        Messages ready for ``call_llm``.
    """
    template = (prompts_dir / PLAN_FRAME_PROMPT).read_text(encoding="utf-8")
    content = template.format(
        me=me or "(not provided)",
        log=log or "(not provided)",
        history=history or "(nothing yet)",
        today=today,
    )
    return [{"role": "user", "content": content}]


def parse_plan_frame_response(text: str) -> PlanFrame | None:
    """Parse the decision payload, or return None when it is unusable.

    Args:
        text: Raw model output, optionally fenced.

    Returns:
        A PlanFrame, or None. None means the caller keeps whatever it had,
        which is the safe direction: an unparseable answer must not suppress
        anything.
    """
    from llm import strip_json_fences

    try:
        payload = json.loads(strip_json_fences(text))
    except (TypeError, ValueError) as exc:
        logger.warning("Plan frame returned unparseable JSON: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None

    mode = str(payload.get("mode", "")).strip().lower()
    if mode not in MODES:
        logger.warning("Plan frame returned unknown mode %r", payload.get("mode"))
        return None
    reason = str(payload.get("reason") or "").strip() or None
    if mode in _SUPPRESSED_MODES and not reason:
        # A suppression with no stated reason cannot be reviewed later, and a
        # decision nobody can review is one nobody can correct.
        logger.warning("Plan frame suppressed the strip without a reason; ignoring")
        return None
    return PlanFrame(mode=mode, reason=reason, source="llm")


def resolve_plan_frame(
    conn: sqlite3.Connection,
    *,
    me: str | None,
    log: str | None,
    history: str | None,
    today: str,
    trace_id: int | None = None,
    model_prefs_path: Path | None = None,
    now: datetime | None = None,
) -> PlanFrame:
    """Return how much of the progress strip this person's week warrants.

    Decides afresh when the context has changed or the previous answer has
    aged out, and reuses the cached answer otherwise. Every failure path
    returns ``full``: a strip that should have been hidden is a visible
    mistake someone can report, while a strip hidden by a crash is
    indistinguishable from one that had nothing to say.

    Args:
        conn: Open database connection.
        me: Contents of me.md.
        log: Recent entries from log.md.
        history: Recent weekly memory entries.
        today: ISO date the decision is for.
        trace_id: Trace to attach the deciding call to.
        model_prefs_path: Profile model preferences.
        now: Override for the current moment, for tests.

    Returns:
        The decision. Never raises.
    """
    from config import MAX_TOKENS_PLAN_FRAME
    from llm import call_llm
    from model_prefs import resolve_model_route

    moment = now or datetime.now(timezone.utc)
    digest = context_digest(me, log, history)

    cached = load_plan_frame(conn)
    if (
        cached is not None
        and _cached_hash(conn) == digest
        and not _is_stale(cached, moment)
    ):
        return cached

    try:
        messages = build_plan_frame_messages(
            me=me, log=log, history=history, today=today
        )
        route = resolve_model_route("plan_frame", path=model_prefs_path).call_kwargs()
        result = call_llm(
            messages,
            **route,
            max_tokens=MAX_TOKENS_PLAN_FRAME,
            conn=conn,
            request_type="plan_frame",
            trace_id=trace_id,
            metadata={"today": today},
        )
    except Exception as exc:  # noqa: BLE001 - a decision must never block a send
        logger.error("Plan frame decision failed: %s", exc)
        return cached or PlanFrame()

    parsed = parse_plan_frame_response(result.text)
    if parsed is None:
        return cached or PlanFrame()

    frame = PlanFrame(
        mode=parsed.mode,
        reason=parsed.reason,
        llm_call_id=result.llm_call_id,
        decided_at=moment.isoformat(),
        source="llm",
    )
    _save(conn, frame, digest, moment)
    if frame.mode != MODE_FULL:
        logger.info(
            "Progress strip reduced to %s: %s (llm_call_id=%s)",
            frame.mode,
            frame.reason,
            frame.llm_call_id,
        )
    return frame
