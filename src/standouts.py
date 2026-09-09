"""Rare computed facts about a person's own history, and the choice to say one.

A nudge answers what changed today. A standout answers something the person
cannot see at all: that this run was the fastest across everything recorded,
that this was the longest session of its kind, that lifetime distance just
crossed a round number. The Health app shows the session. It does not rank the
session against every other one.

Three rules shape the whole module, and each exists because the obvious design
fails a specific way.

**Facts are computed, never generated.** Every candidate below is derived in
SQL, gated on its own comparison population, and rendered into its final
sentence before any model sees it. The one LLM call chooses between finished
sentences or declines them all. Asked instead whether an activity *deserves* a
reward, a model finds a reason — on an unremarkable Tuesday it manufactures
significance out of consistency or effort, because the question presupposes an
answer exists.

**Sufficiency is a sample count, not a calendar.** Recorded months and recorded
evidence are only loosely related, and the cold-start failure this guards
against has already happened here once: in `milestones.py` the only recorded
run became a lifetime PR. See ``STANDOUT_MIN_POPULATION``.

**Scarcity is imposed, not discovered.** Once several kinds of record exist,
something is true most weeks, and a trophy that arrives most weeks is worth
nothing. The cooldown is what makes a standout mean anything, so firing nothing
for six weeks is a correct outcome rather than a fault.

Public API:
    Standout                    — one candidate fact, already phrased.
    find_candidates             — every eligible fact, gates applied.
    find_standout               — the full path: gates, selection, one or none.
    reserve_standout_delivery   — durable claim made before delivery starts.
    record_standout_announced   — finalize a reservation after delivery.
    cooldown_remaining_days     — how long until the next one may fire.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from config import (
    MAX_TOKENS_STANDOUT,
    PROMPTS_DIR,
    STANDOUT_EFFECT_BY_KIND_PREFIX,
    STANDOUT_EFFECT_DEFAULT,
    STANDOUT_COOLDOWN_DAYS,
    STANDOUT_MIN_MARGIN_PCT_EXTENT,
    STANDOUT_MIN_MARGIN_PCT_PACE,
    STANDOUT_MIN_POPULATION,
    STANDOUT_MIN_SPAN_DAYS,
    STANDOUT_RECENCY_DAYS,
    STANDOUT_VOLUME_STEP_KM,
    TELEGRAM_MESSAGE_EFFECTS,
)
from milestones import format_pace

logger = logging.getLogger(__name__)

STANDOUT_PROMPT = "standout_prompt.md"

# Contiguous split windows worth ranking. One kilometre is excluded on purpose:
# a single fast kilometre is a sprint finish or a downhill, and calling it a
# record rewards terrain rather than fitness.
_PACE_WINDOWS: tuple[tuple[int, str], ...] = ((5, "5 km"), (10, "10 km"))

# Categories ranked by how far one session went, and the plural noun used when
# naming the comparison set they were ranked against.
_DISTANCE_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("run", "run", "runs"),
    ("walk", "walk", "walks"),
    ("cycle", "ride", "rides"),
)

# Past participle used when naming cumulative distance, so a crossing reads as
# something the person did rather than as a column total.
_VOLUME_VERBS: dict[str, str] = {"run": "run", "walk": "walked", "cycle": "ridden"}

# Categories with no distance, ranked by how long one session lasted.
_DURATION_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("lift", "strength session", "strength sessions"),
    ("hiit", "interval session", "interval sessions"),
)


@dataclass(frozen=True)
class Standout:
    """One fact that is true, rare, and already written.

    Attributes:
        key: Stable identity of the achievement itself, not of this run of the
            detector. Announcing writes it to the ledger, and the same record
            re-derived on the next sync finds its own key and stops.
        kind: Generator that produced it, for logs, evals and the CLI.
        headline: The exact sentence the person reads. Never regenerated.
        occurred_on: ISO date of the activity that set it.
        population: Size of the comparison set it was ranked against.
        span_days: Days between the oldest and newest member of that set.
        margin_pct: How far it beat the previous best, or None when the fact is
            a threshold crossing rather than a record.
        source_workout_ids: Workouts whose arrival can make this fact news.
    """

    key: str
    kind: str
    headline: str
    occurred_on: str
    population: int
    span_days: int
    margin_pct: float | None = None
    source_workout_ids: tuple[str, ...] = ()


def _scope_phrase(population: int, span_days: int, plural_noun: str) -> str:
    """Describe the comparison set in terms the data actually supports.

    A claim that names a period asserts something about recorded span, which is
    a different fact from sample size. Saying "in two years" on eight months of
    history is false however many sessions those months hold, so the period is
    only used once the span is really there and the sentence falls back to
    counting otherwise.

    Args:
        population: Number of comparable sessions or days.
        span_days: Days between the oldest and newest of them.
        plural_noun: What the members are called, e.g. ``"runs"``.

    Returns:
        A fragment such as ``"in 2 years of tracking"``.
    """
    years = int(span_days // 365)
    if years >= 2:
        return f"in {years} years of tracking"
    if span_days >= 365:
        return "in a year of tracking"
    return f"across {population:,} recorded {plural_noun}"


def _margin_pct(best: float, runner_up: float) -> float:
    """Return how far *best* improves on *runner_up*, as a percentage.

    Direction is normalised by the caller: both values are passed such that a
    larger gap is always a bigger improvement.
    """
    if runner_up <= 0:
        return 0.0
    return abs(runner_up - best) / runner_up * 100.0


def _span_days(dates: list[str]) -> int:
    """Return the number of days spanned by a list of ISO dates."""
    if not dates:
        return 0
    try:
        parsed = sorted(date.fromisoformat(value) for value in dates if value)
    except ValueError:
        return 0
    if not parsed:
        return 0
    return (parsed[-1] - parsed[0]).days


def _is_recent(occurred_on: str, today: date) -> bool:
    """Return True when an achievement is still the thing that just happened."""
    try:
        when = date.fromisoformat(occurred_on)
    except ValueError:
        return False
    return 0 <= (today - when).days <= STANDOUT_RECENCY_DAYS


def _pace_candidates(conn: sqlite3.Connection, today: date) -> list[Standout]:
    """Rank the fastest contiguous split windows across every recorded run."""
    found: list[Standout] = []
    for km_count, label in _PACE_WINDOWS:
        rows = conn.execute(
            f"""
            WITH windows AS (
                SELECT
                    ws.start_utc,
                    w.date AS date,
                    COUNT(*) OVER win AS row_count,
                    COUNT(ws.pace_min_km) OVER win AS value_count,
                    SUM(ws.pace_min_km) OVER win AS total_pace
                FROM workout_split AS ws
                JOIN workout AS w ON w.start_utc = ws.start_utc
                WHERE w.category = 'run'
                WINDOW win AS (
                    PARTITION BY ws.start_utc
                    ORDER BY ws.km_index
                    ROWS BETWEEN CURRENT ROW AND {km_count - 1} FOLLOWING
                )
            )
            SELECT start_utc, date, MIN(total_pace) / {km_count}.0 AS pace
            FROM windows
            WHERE row_count = {km_count} AND value_count = {km_count}
            GROUP BY start_utc, date
            ORDER BY pace ASC
            """
        ).fetchall()

        if len(rows) < max(2, STANDOUT_MIN_POPULATION):
            continue
        best, runner_up = rows[0], rows[1]
        if not _is_recent(best["date"], today):
            continue
        margin = _margin_pct(best["pace"], runner_up["pace"])
        if margin < STANDOUT_MIN_MARGIN_PCT_PACE:
            continue

        span = _span_days([row["date"] for row in rows])
        if span < STANDOUT_MIN_SPAN_DAYS:
            continue
        scope = _scope_phrase(len(rows), span, "runs")
        pace = format_pace(best["pace"])
        previous = format_pace(runner_up["pace"])
        found.append(
            Standout(
                key=f"pace_window_{km_count}|{best['date']}|{best['pace']:.4f}",
                kind=f"pace_window_{km_count}",
                headline=(
                    f"Fastest {label} stretch {scope} — **{pace}**, "
                    f"ahead of your previous **{previous}**."
                ),
                occurred_on=best["date"],
                population=len(rows),
                span_days=span,
                margin_pct=margin,
                source_workout_ids=(best["start_utc"],),
            )
        )
    return found


def _extreme_candidates(
    conn: sqlite3.Connection,
    today: date,
    *,
    column: str,
    categories: tuple[tuple[str, str, str], ...],
    kind_prefix: str,
    unit: str,
    decimals: int,
) -> list[Standout]:
    """Rank sessions of each category by one column, largest first.

    Distance and duration records differ only in the column they sort and the
    categories they apply to, so they share this. Running both over the same
    category would let one session produce two near-identical candidates.

    Args:
        conn: Open database connection.
        today: Day the detection is running for.
        column: Numeric column to rank on.
        categories: Triples of database category, singular name, plural noun.
        kind_prefix: Prefix for the generated candidate kind.
        unit: Unit printed after the value.
        decimals: Decimal places for the printed value.

    Returns:
        Every candidate that cleared the population, recency and margin gates.
    """
    found: list[Standout] = []
    for category, singular, plural in categories:
        rows = conn.execute(
            f"""
            SELECT start_utc, date, {column} AS value
            FROM workout_all
            WHERE category = ? AND {column} IS NOT NULL AND {column} > 0
            ORDER BY value DESC
            """,
            (category,),
        ).fetchall()

        if len(rows) < max(2, STANDOUT_MIN_POPULATION):
            continue
        best, runner_up = rows[0], rows[1]
        if not _is_recent(best["date"], today):
            continue
        margin = _margin_pct(best["value"], runner_up["value"])
        if margin < STANDOUT_MIN_MARGIN_PCT_EXTENT:
            continue

        span = _span_days([row["date"] for row in rows])
        if span < STANDOUT_MIN_SPAN_DAYS:
            continue
        scope = _scope_phrase(len(rows), span, plural)
        value = f"{best['value']:.{decimals}f}"
        previous = f"{runner_up['value']:.{decimals}f}"
        found.append(
            Standout(
                key=f"{kind_prefix}_{category}|{best['date']}|{best['value']:.4f}",
                kind=f"{kind_prefix}_{category}",
                headline=(
                    f"Longest {singular} {scope} — **{value} {unit}**, "
                    f"past your previous **{previous} {unit}**."
                ),
                occurred_on=best["date"],
                population=len(rows),
                span_days=span,
                margin_pct=margin,
                source_workout_ids=(best["start_utc"],),
            )
        )
    return found


def _volume_candidates(conn: sqlite3.Connection, today: date) -> list[Standout]:
    """Detect lifetime distance crossing a round threshold.

    The only standout that needs no ranking: crossing a round number is true
    regardless of how it compares to anything. It still takes the population
    gate, because a total assembled from a handful of imported sessions is a
    statement about the import rather than about the person.
    """
    cutoff = (today - timedelta(days=STANDOUT_RECENCY_DAYS)).isoformat()
    found: list[Standout] = []
    for category, singular, plural in _DISTANCE_CATEGORIES:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS sessions,
                MIN(date) AS first_date,
                MAX(date) AS last_date,
                SUM(gpx_distance_km) AS total,
                SUM(CASE WHEN date < ? THEN gpx_distance_km ELSE 0 END) AS before
            FROM workout_all
            WHERE category = ? AND gpx_distance_km IS NOT NULL
            """,
            (cutoff, category),
        ).fetchone()

        if row is None or row["total"] is None:
            continue
        if row["sessions"] < STANDOUT_MIN_POPULATION:
            continue

        step = STANDOUT_VOLUME_STEP_KM
        crossed = int(row["total"] // step)
        if crossed <= int((row["before"] or 0) // step) or crossed < 1:
            continue

        threshold = crossed * step
        span = _span_days([row["first_date"], row["last_date"]])
        if span < STANDOUT_MIN_SPAN_DAYS:
            continue
        scope = _scope_phrase(row["sessions"], span, plural)
        recent_ids = tuple(
            result["start_utc"]
            for result in conn.execute(
                "SELECT start_utc FROM workout_all "
                "WHERE category = ? AND gpx_distance_km IS NOT NULL "
                "AND date >= ? ORDER BY start_utc",
                (category, cutoff),
            ).fetchall()
        )
        found.append(
            Standout(
                key=f"volume_{category}|{threshold}",
                kind=f"volume_{category}",
                headline=(
                    f"You have now {_VOLUME_VERBS[category]} **{threshold:,} km** "
                    f"{scope}."
                ),
                occurred_on=row["last_date"],
                population=row["sessions"],
                span_days=span,
                source_workout_ids=recent_ids,
            )
        )
    return found


def find_candidates(
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
    eligible_workout_ids: set[str] | None = None,
) -> list[Standout]:
    """Return every fact that is currently true, rare and sufficiently evidenced.

    Every gate that does not need a model runs here: comparison population,
    recency of the achievement, and the margin over the previous best where
    applicable. What survives is a list of finished sentences, any of which
    could be sent as-is.

    Args:
        conn: Open database connection.
        today: Day to detect against. Defaults to the local date.
        eligible_workout_ids: When provided, keep only facts caused by one of
            these newly inserted or changed workouts. An empty set therefore
            produces no candidates.

    Returns:
        Candidates, best margin first. Never raises.
    """
    today = today or date.today()
    generators = (
        lambda: _pace_candidates(conn, today),
        lambda: _extreme_candidates(
            conn,
            today,
            column="gpx_distance_km",
            categories=_DISTANCE_CATEGORIES,
            kind_prefix="distance",
            unit="km",
            decimals=1,
        ),
        lambda: _extreme_candidates(
            conn,
            today,
            column="duration_min",
            categories=_DURATION_CATEGORIES,
            kind_prefix="duration",
            unit="min",
            decimals=0,
        ),
        lambda: _volume_candidates(conn, today),
    )

    found: list[Standout] = []
    for generator in generators:
        try:
            found.extend(generator())
        except sqlite3.Error as exc:
            # One generator failing must not cost the others. A missing split
            # table on an old profile is exactly this case.
            logger.warning("Standout generator failed: %s", exc)
    if eligible_workout_ids is not None:
        found = [
            item
            for item in found
            if eligible_workout_ids.intersection(item.source_workout_ids)
        ]
    return sorted(found, key=lambda item: item.margin_pct or 0.0, reverse=True)


def effect_for(standout: Standout) -> str | None:
    """Return the Telegram effect id that should play for one standout.

    Args:
        standout: The fact being announced.

    Returns:
        An effect id, or None when the configured name is not in the
        catalogue — a missing animation must never cost the message.
    """
    prefix = standout.kind.split("_", 1)[0]
    name = STANDOUT_EFFECT_BY_KIND_PREFIX.get(prefix, STANDOUT_EFFECT_DEFAULT)
    effect = TELEGRAM_MESSAGE_EFFECTS.get(name)
    if effect is None:
        logger.warning("Unknown standout effect %r; sending without one", name)
    return effect


def announced_keys(conn: sqlite3.Connection) -> set[str]:
    """Return the keys of every achievement already announced.

    Raises:
        sqlite3.Error: The ledger could not be read. Deliberately propagated
            rather than answered with an empty set, which is indistinguishable
            from a clean ledger and would re-announce everything. See
            :func:`find_standout` for where it is caught.
    """
    rows = conn.execute("SELECT key FROM standout_announced").fetchall()
    return {row["key"] for row in rows}


def pending_keys(conn: sqlite3.Connection) -> set[str]:
    """Return achievements with an unresolved delivery reservation."""
    rows = conn.execute("SELECT key FROM standout_delivery_pending").fetchall()
    return {row["key"] for row in rows}


def suppressed_keys(conn: sqlite3.Connection) -> set[str]:
    """Return achievements already delivered or reserved for delivery."""
    return announced_keys(conn) | pending_keys(conn)


def last_announced_at(conn: sqlite3.Connection) -> datetime | None:
    """Return when a standout was last announced, or None if never.

    Raises:
        sqlite3.Error: The ledger could not be read. An unreadable ledger must
            not read as "nothing announced yet": that answer clears the
            cooldown, and losing both suppressions at once is the one failure
            that turns this feature into the thing it was built not to be.
    """
    row = conn.execute(
        "SELECT MAX(announced_at) AS latest FROM standout_announced"
    ).fetchone()
    if row is None or not row["latest"]:
        return None
    try:
        parsed = datetime.fromisoformat(row["latest"])
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def cooldown_remaining_days(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> int:
    """Return whole days left before another standout may fire.

    Raises:
        sqlite3.Error: The ledger could not be read.
    """
    moment = now or datetime.now(timezone.utc)
    row = conn.execute(
        """
        SELECT MAX(stamped_at) AS latest
        FROM (
            SELECT announced_at AS stamped_at FROM standout_announced
            UNION ALL
            SELECT reserved_at AS stamped_at FROM standout_delivery_pending
        )
        """
    ).fetchone()
    if row is None or not row["latest"]:
        return 0
    try:
        last = datetime.fromisoformat(row["latest"])
    except ValueError:
        return 0
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed = moment - last
    remaining = timedelta(days=STANDOUT_COOLDOWN_DAYS) - elapsed
    return max(0, remaining.days + (1 if remaining.seconds else 0))


def reserve_standout_delivery(
    conn: sqlite3.Connection,
    standout: Standout,
    *,
    now: datetime | None = None,
) -> bool:
    """Durably reserve one standout before attempting delivery.

    Returns:
        True when this call acquired the reservation. False when the fact was
        already delivered or another sender already reserved it.

    Raises:
        sqlite3.Error: The reservation could not be read or written. Callers
            must fail closed and continue without the standout.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=STANDOUT_COOLDOWN_DAYS)
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO standout_delivery_pending
                (key, kind, headline, occurred_on, reserved_at)
            SELECT ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM standout_announced WHERE key = ?
            )
            AND NOT EXISTS (
                SELECT 1 FROM standout_delivery_pending WHERE key = ?
            )
            AND NOT EXISTS (
                SELECT 1 FROM standout_announced
                WHERE julianday(announced_at) > julianday(?)
            )
            AND NOT EXISTS (
                SELECT 1 FROM standout_delivery_pending
                WHERE julianday(reserved_at) > julianday(?)
            )
            """,
            (
                standout.key,
                standout.kind,
                standout.headline,
                standout.occurred_on,
                moment.isoformat(),
                standout.key,
                standout.key,
                cutoff.isoformat(),
                cutoff.isoformat(),
            ),
        )
    return cursor.rowcount == 1


def release_standout_delivery(
    conn: sqlite3.Connection,
    standout: Standout,
) -> None:
    """Release a reservation after a definite delivery failure."""
    try:
        with conn:
            conn.execute(
                "DELETE FROM standout_delivery_pending WHERE key = ?",
                (standout.key,),
            )
    except sqlite3.Error as exc:
        # Keeping an uncertain reservation fails closed. It may suppress one
        # future standout, but it cannot cause a duplicate delivery.
        logger.error(
            "Could not release failed standout delivery %s; keeping it suppressed: %s",
            standout.key,
            exc,
        )


def record_standout_announced(
    conn: sqlite3.Connection,
    standout: Standout,
    *,
    now: datetime | None = None,
) -> None:
    """Move a delivered standout from its reservation into the ledger."""
    moment = now or datetime.now(timezone.utc)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO standout_announced "
                "(key, kind, headline, occurred_on, announced_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    standout.key,
                    standout.kind,
                    standout.headline,
                    standout.occurred_on,
                    moment.isoformat(),
                ),
            )
            conn.execute(
                "DELETE FROM standout_delivery_pending WHERE key = ?",
                (standout.key,),
            )
    except sqlite3.Error as exc:
        # The transaction rolls the delete back too, leaving the durable
        # reservation in place. The message may be absent from the historical
        # ledger, but it cannot be delivered twice.
        logger.error(
            "Could not finalize announced standout %s; its delivery "
            "reservation remains suppressed: %s",
            standout.key,
            exc,
        )


def build_standout_messages(
    candidates: list[Standout],
    *,
    me: str | None,
    log: str | None,
    today: str,
    prompts_dir: Path = PROMPTS_DIR,
) -> list[dict[str, str]]:
    """Render the selection prompt over already-finished sentences.

    Every numeric bar has been applied before this. What the call is given
    instead is the person: the one thing that can make announcing a true,
    qualified record the wrong move today, and the one thing no threshold in
    `config.py` can encode.

    Args:
        candidates: Eligible facts, each already phrased and gated.
        me: Contents of me.md.
        log: Recent entries from log.md, where a reason not to celebrate lives.
        today: ISO date the selection is for.
        prompts_dir: Directory holding the prompt file.

    Returns:
        Messages ready for ``call_llm``.
    """
    template = (prompts_dir / STANDOUT_PROMPT).read_text(encoding="utf-8")
    # The margin is rendered explicitly rather than left to be inferred from
    # the headline. Asked to judge whether an improvement is decisive while
    # shown only the two paces it sits between, the picker declined a record
    # that had nearly tripled a seven-year best. It was not being stubborn; it
    # was being asked for arithmetic it had not been given.
    rendered = "\n".join(
        f"- key: `{item.key}`\n"
        f"  says: {item.headline}\n"
        f"  improvement over the previous best: "
        + (
            "a threshold crossing, no previous best to beat"
            if item.margin_pct is None
            else f"{item.margin_pct:.0f}%"
        )
        + f"\n  compared against: {item.population} sessions "
        f"spanning {item.span_days} days"
        for item in candidates
    )
    content = template.format(
        candidates=rendered,
        me=me or "(not provided)",
        log=log or "(nothing recent)",
        today=today,
        cooldown_days=STANDOUT_COOLDOWN_DAYS,
    )
    return [{"role": "user", "content": content}]


def parse_standout_response(text: str, valid_keys: set[str]) -> str | None:
    """Return the chosen key, or None for a decline or an unusable answer.

    Args:
        text: Raw model output, optionally fenced.
        valid_keys: Keys the model was offered.

    Returns:
        One key from *valid_keys*, or None. A key that was not offered is
        treated as a decline: the model does not get to invent an achievement
        by naming one, which is the whole reason selection replaced generation.
    """
    from llm import strip_json_fences

    try:
        payload = json.loads(strip_json_fences(text))
    except (TypeError, ValueError) as exc:
        logger.warning("Standout selection returned unparseable JSON: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None

    pick = payload.get("pick")
    if pick is None:
        logger.info(
            "Standout declined: %s", str(payload.get("reason") or "no reason given")
        )
        return None
    key = str(pick).strip()
    if key not in valid_keys:
        logger.warning("Standout selection named an unoffered key %r; declining", key)
        return None
    return key


def find_standout(
    conn: sqlite3.Connection,
    *,
    me: str | None,
    log: str | None,
    history: str | None,
    eligible_workout_ids: set[str] | None = None,
    today: date | None = None,
    now: datetime | None = None,
    trace_id: int | None = None,
    model_prefs_path: Path | None = None,
) -> Standout | None:
    """Return the one standout worth announcing right now, or None.

    Ordered so that the free checks run first. The cooldown, the ledger and the
    candidate SQL cost nothing, and on the overwhelming majority of nudges one
    of them ends the search before either LLM call is made — including the
    plan-frame resolution, which is why this takes the raw context rather than
    a resolved frame.

    Args:
        conn: Open database connection.
        me: Contents of me.md.
        log: Recent entries from log.md.
        history: Recent weekly memory entries.
        eligible_workout_ids: Workouts inserted or changed by the import that
            triggered this nudge. When supplied, unrelated recent records are
            excluded.
        today: Day to detect against. Defaults to the local date.
        now: Override for the current moment, for tests.
        trace_id: Trace to attach the selection call to.
        model_prefs_path: Profile model preferences.

    Returns:
        A standout to announce, or None. Never raises.
    """
    from llm import call_llm
    from model_prefs import resolve_model_route
    from plan_frame import MODE_FULL, resolve_plan_frame

    today = today or date.today()
    try:
        remaining = cooldown_remaining_days(conn, now=now)
        if remaining:
            logger.debug("Standout on cooldown for another %d day(s)", remaining)
            return None

        seen = suppressed_keys(conn)
        candidates = [
            item
            for item in find_candidates(
                conn,
                today=today,
                eligible_workout_ids=eligible_workout_ids,
            )
            if item.key not in seen
        ]
        if not candidates:
            return None

        # Only now is the frame worth resolving. A celebration has to clear a
        # stricter bar than the progress strip: `facts` mode exists to strip a
        # verdict about the person off the numbers, and an announcement is a
        # verdict in its entirety.
        frame = resolve_plan_frame(
            conn,
            me=me,
            log=log,
            history=history,
            today=today.isoformat(),
            trace_id=trace_id,
            model_prefs_path=model_prefs_path,
        )
        if frame.mode != MODE_FULL:
            logger.info(
                "Standout withheld: plan frame is %s (%s)", frame.mode, frame.reason
            )
            return None

        messages = build_standout_messages(
            candidates, me=me, log=log, today=today.isoformat()
        )
        route = resolve_model_route("standout", path=model_prefs_path).call_kwargs()
        result = call_llm(
            messages,
            **route,
            max_tokens=MAX_TOKENS_STANDOUT,
            conn=conn,
            request_type="standout",
            trace_id=trace_id,
            metadata={"today": today.isoformat(), "candidates": len(candidates)},
        )
    except Exception as exc:  # noqa: BLE001 - a standout must never block a nudge
        # Includes an unreadable ledger, which is why the reads above raise
        # rather than returning empty. Announcing nothing is always safe here;
        # the nudge carrying it goes out regardless.
        logger.error("Standout detection failed, announcing nothing: %s", exc)
        return None

    key = parse_standout_response(result.text, {item.key for item in candidates})
    if key is None:
        return None
    chosen = next(item for item in candidates if item.key == key)
    logger.info("Standout selected: %s (%s)", chosen.kind, chosen.key)
    return chosen
