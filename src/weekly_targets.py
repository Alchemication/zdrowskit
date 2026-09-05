"""Measurable weekly targets derived from the prose goals in `strategy.md`.

Goals are written as sentences — "build weekly volume to 30 km", "sleep 7+
hours at least 5 of 7 nights". A progress bar needs a number and a metric the
database can actually count, so something has to turn one into the other.

That translation is the only part of the progress strip that uses an LLM, and
it runs at most once a week per profile: the derived targets are stored against
the week's Monday and the hash of the goal text they came from, so every
notification in that week renders from the same stored numbers. A bar that
wobbled between two messages an hour apart would be worse than no bar.

The vocabulary is closed on purpose. A goal that does not map to one of
:data:`TARGET_SPECS` is dropped rather than approximated — the failure mode
this guards against is a confident bar drawn against a target the user never
set, which is the same mistake as handing a computed judgement to a prompt as
fact.

Public API:
    TargetSpec          — one measurable weekly metric the strip can draw.
    TARGET_SPECS        — the closed vocabulary, in display priority order.
    StoredTarget        — a target row as persisted for one week.
    week_start_for      — the Monday of the week containing a date.
    recorded_activity_types — uncategorised workout types this profile logs.
    extract_goal_text   — the goal-bearing sections of strategy.md.
    goals_digest        — cache key over that text and this vocabulary.
    load_targets        — read a week's stored targets.
    save_targets        — replace a week's stored targets.
    clear_targets       — drop a week's stored targets.
    derive_targets      — one LLM call turning goal prose into targets.
    ensure_weekly_targets — cached derive-or-reuse for the current week.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from config import (
    PROMPTS_DIR,
    WEEKLY_TARGET_MAX_RINGS,
    WEEKLY_TARGET_TYPE_CHOICES,
)
from llm_context import UNFILLED_CONTEXT

logger = logging.getLogger(__name__)

TARGETS_PROMPT = "targets_prompt.md"

# Bumped whenever the vocabulary or its measurement changes meaning. It is
# folded into the cache key so stored targets from an older definition are
# re-derived instead of being measured by rules they were never written for.
_VOCABULARY_VERSION = 2

# The two ways load_context reports "there is nothing here": a missing file and
# a file still holding its shipped template. Either one means no stated goals.
_NO_STRATEGY = frozenset({"(not provided)", UNFILLED_CONTEXT})


@dataclass(frozen=True)
class TargetSpec:
    """One measurable weekly metric a progress ring can be drawn for.

    Metrics are parameterised by activity rather than enumerated per sport.
    ``distance_km_week`` and ``sessions_week`` each cover running, walking and
    cycling, so a walker and a cyclist are first-class without a new key, a new
    column, or a new branch in the measurement. Adding a sport is a line in
    :data:`CATEGORY_LABELS`, not a migration.

    Attributes:
        key: Stable identifier, also the value the LLM must emit.
        label: Column label for metrics that take no category. Category-bearing
            metrics take their label from the category instead.
        unit: Noun printed after the numbers, e.g. "km" or "nights".
        shape: ``total`` sums a quantity over the week; ``count`` counts
            qualifying days or sessions.
        decimals: Decimal places used when printing the measured value.
        min_target: Smallest target accepted from the model.
        max_target: Largest target accepted from the model.
        categories: Activity categories this metric accepts. Empty when the
            metric takes none.
        allows_activity_type: Whether ``category`` may instead name one
            recorded workout type, prefixed with ``type:``.
        category_max: Per-category tightening of ``max_target``, because a
            plausible weekly cycling distance is nonsense as a running one.
        threshold_unit: ``h`` or ``steps`` when the metric counts days that
            clear a per-day bar, else None.
        min_threshold: Smallest threshold accepted, when one is required.
        max_threshold: Largest threshold accepted, when one is required.
    """

    key: str
    unit: str
    shape: str
    decimals: int
    min_target: float
    max_target: float
    label: str = ""
    categories: tuple[str, ...] = ()
    category_max: dict[str, float] = field(default_factory=dict)
    allows_activity_type: bool = False
    threshold_unit: str | None = None
    min_threshold: float | None = None
    max_threshold: float | None = None

    @property
    def needs_threshold(self) -> bool:
        """True when this metric counts days clearing a per-day bar."""
        return self.threshold_unit is not None

    @property
    def needs_category(self) -> bool:
        """True when this metric must be told which activity it measures."""
        return bool(self.categories)

    def max_for(self, category: str) -> float:
        """Return the upper bound accepted for one category."""
        return self.category_max.get(category, self.max_target)


# Distance rings carry the unit in the label, session rings the plural noun.
# One goal sentence routinely states both — "3 runs, ~15 km" — and the two
# rings then sit adjacent, so "Run" against "Runs" was a one-letter difference
# between a distance and a count that could disagree with each other. "Run km"
# against "Runs" cannot be misread.
#
# ``any`` is the answer for a sport with no category of its own — rowing, yoga,
# climbing all land in the schema's "other" bucket, and a ring reading "Other"
# tells the reader nothing that "Sessions" does not.
CATEGORY_LABELS: dict[str, str] = {
    "run": "Run km",
    "walk": "Walk km",
    "cycle": "Ride km",
    "lift": "Lift",
    "hiit": "HIIT",
    "any": "Session",
}
CATEGORY_PLURALS: dict[str, str] = {
    "run": "Runs",
    "walk": "Walks",
    "cycle": "Rides",
    "lift": "Lifts",
    "hiit": "HIIT",
    "any": "Sessions",
}

# Marks a `category` value that names one recorded workout type rather than one
# of the closed categories, e.g. ``type:Paddle Sports``. It reuses the category
# column because a ring is still one ring per (metric, thing measured), and the
# primary key already says so.
TYPE_PREFIX = "type:"


def activity_type_of(category: str) -> str | None:
    """Return the workout type a category names, or None for a plain category."""
    if category.startswith(TYPE_PREFIX):
        return category[len(TYPE_PREFIX) :].strip() or None
    return None


# Ordered by how directly each metric reflects a training decision, because the
# order is also the tie-break when more goals map than there are ring slots.
TARGET_SPECS: tuple[TargetSpec, ...] = (
    TargetSpec(
        key="distance_km_week",
        unit="km",
        shape="total",
        decimals=1,
        min_target=1,
        max_target=1000,
        categories=("run", "walk", "cycle"),
        # A 900 km week is a plausible cycling block and an impossible running
        # one, so the shared ceiling is loosened per category rather than set
        # to whichever sport happens to be most demanding.
        category_max={"run": 300, "walk": 200, "cycle": 1000},
    ),
    TargetSpec(
        key="sessions_week",
        unit="sessions",
        shape="count",
        decimals=0,
        min_target=1,
        max_target=21,
        categories=("run", "walk", "lift", "cycle", "hiit", "any"),
        # Paddling, basketball and swimming share the schema's "other"
        # bucket, so a category cannot separate them and `any` would count a
        # paddler's runs as paddling. Naming the recorded workout type is the
        # only way those goals become a ring, and unlike a category list it
        # generalises to whatever the person's own watch has logged without
        # anyone having to predict the sport.
        allows_activity_type=True,
    ),
    TargetSpec(
        key="exercise_min_week",
        label="Exercise",
        unit="min",
        shape="total",
        decimals=0,
        min_target=10,
        max_target=2000,
    ),
    TargetSpec(
        key="sleep_nights_week",
        label="Sleep",
        unit="nights",
        shape="count",
        decimals=0,
        min_target=1,
        max_target=7,
        threshold_unit="h",
        min_threshold=3,
        max_threshold=14,
    ),
    TargetSpec(
        key="step_days_week",
        label="Steps",
        unit="days",
        shape="count",
        decimals=0,
        min_target=1,
        max_target=7,
        threshold_unit="steps",
        min_threshold=1000,
        max_threshold=50000,
    ),
)

SPEC_BY_KEY: dict[str, TargetSpec] = {spec.key: spec for spec in TARGET_SPECS}
_SPEC_ORDER: dict[str, int] = {spec.key: i for i, spec in enumerate(TARGET_SPECS)}


@dataclass(frozen=True)
class StoredTarget:
    """A weekly target as persisted for one week.

    Attributes:
        spec: The metric this target is set against.
        category: Activity this target measures, or the empty string for a
            metric that takes none.
        target: The number to reach by Sunday night.
        threshold: Per-day bar for threshold metrics, else None.
        goal_text: The strategy.md sentence this came from, for provenance.
        strategy_hash: Digest of the goal text the derivation read.
        llm_call_id: The derivation call, for ``llm-log --id``.
    """

    spec: TargetSpec
    category: str
    target: float
    threshold: float | None
    goal_text: str | None
    strategy_hash: str | None
    llm_call_id: int | None

    @property
    def slot(self) -> tuple[str, str]:
        """Return the identity of this ring — one per metric and category."""
        return (self.spec.key, self.category)

    @property
    def slot_label(self) -> str:
        """Return a log- and CLI-friendly name for this ring's identity."""
        return f"{self.spec.key}/{self.category}" if self.category else self.spec.key

    @property
    def label(self) -> str:
        """Return the ring's left-column label."""
        if not self.spec.needs_category:
            return self.spec.label
        activity_type = activity_type_of(self.category)
        if activity_type:
            return activity_type
        source = CATEGORY_PLURALS if self.spec.shape == "count" else CATEGORY_LABELS
        return source.get(self.category, self.category.title())


def week_start_for(day: date) -> str:
    """Return the ISO date of the Monday of *day*'s week.

    Args:
        day: Any date.

    Returns:
        ISO date string of that week's Monday, matching the Monday-start weeks
        used by reports and by ``build_llm_data``.
    """
    return (day - timedelta(days=day.weekday())).isoformat()


def week_bounds_for(week_start: str) -> tuple[str, str]:
    """Return the inclusive (Monday, Sunday) ISO dates of a week."""
    monday = date.fromisoformat(week_start)
    return week_start, (monday + timedelta(days=6)).isoformat()


def extract_goal_text(strategy_md: str | None) -> str:
    """Return the goal-bearing sections of strategy.md, or an empty string.

    Only the ``## Goals …`` and ``## Weekly Plan`` sections are read. Diet and
    sleep prose describe habits rather than countable weekly commitments, and
    feeding them in produced targets the user had not agreed to.

    Args:
        strategy_md: Raw contents of strategy.md, or None.

    Returns:
        The concatenated goal sections with their headings, stripped. Empty
        when strategy.md is missing, unfilled, or has no goal sections.
    """
    if not strategy_md or strategy_md.strip() in _NO_STRATEGY:
        return ""

    wanted: list[str] = []
    keeping = False
    for line in strategy_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].strip().lower()
            keeping = heading.startswith("goals") or heading.startswith("weekly plan")
            if keeping:
                wanted.append(stripped)
            continue
        if keeping and stripped:
            wanted.append(stripped)
    return "\n".join(wanted).strip()


def goals_digest(goal_text: str) -> str:
    """Return the cache key for a block of goal text.

    The vocabulary version is folded in so that changing what the keys mean
    invalidates every stored target rather than leaving old numbers to be
    measured by new rules.

    Args:
        goal_text: Output of :func:`extract_goal_text`.

    Returns:
        A hex digest, or an empty string when there is no goal text.
    """
    if not goal_text:
        return ""
    payload = f"v{_VOCABULARY_VERSION}\n{goal_text}".encode()
    return hashlib.sha256(payload).hexdigest()


def recorded_activity_types(
    conn: sqlite3.Connection,
    *,
    limit: int = WEEKLY_TARGET_TYPE_CHOICES,
) -> list[tuple[str, int]]:
    """Return the uncategorised workout types this profile actually records.

    Only types in the ``other`` bucket are offered. Running, walking, cycling
    and strength already have categories of their own, and naming a type where
    a category exists would split one goal across two ways of measuring it.

    Args:
        conn: Open database connection.
        limit: Most-recorded types to return.

    Returns:
        (type, session_count) pairs, most-recorded first.
    """
    try:
        rows = conn.execute(
            """
            SELECT type, COUNT(*) AS n FROM workout_all
            WHERE category = 'other' AND type IS NOT NULL AND type != ''
            GROUP BY type ORDER BY n DESC, type ASC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("Activity type lookup failed: %s", exc)
        return []
    return [(row["type"], row["n"]) for row in rows]


def known_activity_types(conn: sqlite3.Connection) -> frozenset[str]:
    """Return every workout type this profile has ever recorded.

    Used to reject a type the model invented. A hallucinated "Kitesurfing"
    would otherwise become a bar stuck at zero for the whole week, which reads
    as a broken feature rather than an unmet goal.
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT type FROM workout_all WHERE type IS NOT NULL"
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("Activity type lookup failed: %s", exc)
        return frozenset()
    return frozenset(row["type"] for row in rows)


def _category_is_measurable(spec: TargetSpec, category: str) -> bool:
    """Return True when a stored category still has a measurement behind it.

    Both shapes have to pass here: one of the closed categories, or a
    ``type:``-prefixed workout type on a metric that accepts one. Checking only
    the closed set silently dropped every named-sport target on the way back
    out of the database, so a paddler's goal saved fine and then rendered as no
    targets at all.
    """
    if not spec.needs_category:
        return True
    if category in spec.categories:
        return True
    return spec.allows_activity_type and activity_type_of(category) is not None


def load_targets(conn: sqlite3.Connection, week_start: str) -> list[StoredTarget]:
    """Read the stored targets for one week, in display order.

    Args:
        conn: Open database connection.
        week_start: ISO Monday of the week.

    Returns:
        Stored targets, ordered as they should be rendered. Empty when the week
        has none, or when the table predates this feature.
    """
    try:
        rows = conn.execute(
            """
            SELECT metric, category, target, threshold, goal_text,
                   strategy_hash, llm_call_id
            FROM weekly_target
            WHERE week_start = ?
            ORDER BY position ASC, metric ASC, category ASC
            """,
            (week_start,),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("Weekly targets unavailable: %s", exc)
        return []

    targets: list[StoredTarget] = []
    for row in rows:
        spec = SPEC_BY_KEY.get(row["metric"])
        if spec is None:
            # A key retired by a later vocabulary. Drop it rather than draw a
            # bar whose measurement no longer exists.
            continue
        category = row["category"] or ""
        if not _category_is_measurable(spec, category):
            # A category retired from the vocabulary, or a row written by a
            # build that spelled them differently. Nothing here can measure it.
            continue
        targets.append(
            StoredTarget(
                spec=spec,
                category=category,
                target=float(row["target"]),
                threshold=(
                    float(row["threshold"]) if row["threshold"] is not None else None
                ),
                goal_text=row["goal_text"],
                strategy_hash=row["strategy_hash"],
                llm_call_id=row["llm_call_id"],
            )
        )
    return targets


def save_targets(
    conn: sqlite3.Connection,
    week_start: str,
    targets: list[StoredTarget],
    *,
    source: str = "strategy",
) -> None:
    """Replace a week's stored targets.

    Args:
        conn: Open database connection.
        week_start: ISO Monday of the week.
        targets: Targets to store, in display order.
        source: Where the numbers came from — ``strategy`` or ``manual``.
    """
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("DELETE FROM weekly_target WHERE week_start = ?", (week_start,))
        conn.executemany(
            """
            INSERT INTO weekly_target (
                week_start, metric, category, target, threshold, source,
                goal_text, strategy_hash, llm_call_id, position, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    week_start,
                    item.spec.key,
                    item.category,
                    item.target,
                    item.threshold,
                    source,
                    item.goal_text,
                    item.strategy_hash,
                    item.llm_call_id,
                    position,
                    now,
                )
                for position, item in enumerate(targets)
            ],
        )


def clear_targets(conn: sqlite3.Connection, week_start: str) -> int:
    """Drop a week's stored targets and return how many rows went."""
    with conn:
        cursor = conn.execute(
            "DELETE FROM weekly_target WHERE week_start = ?", (week_start,)
        )
    return cursor.rowcount or 0


def _match_activity_type(name: str, known: frozenset[str]) -> str | None:
    """Return the recorded spelling of *name*, or None when it was never logged.

    Matched case-insensitively and returned in the database's own spelling, so
    the stored target measures with an exact equality the model's capitalisation
    cannot break.
    """
    folded = name.casefold()
    for recorded in known:
        if recorded.casefold() == folded:
            return recorded
    return None


def _coerce_target(
    raw: dict, strategy_hash: str, known_types: frozenset[str]
) -> StoredTarget | None:
    """Validate one model-proposed target, or return None to drop it.

    Every reason to drop is logged: a silently discarded goal looks identical
    to a goal the model never saw, and the two need different fixes.

    Args:
        raw: One object from the model's ``targets`` array.
        strategy_hash: Digest recorded on the accepted target.
        known_types: Workout types this profile has actually recorded.

    Returns:
        A StoredTarget, or None when the item is unusable.
    """
    if not isinstance(raw, dict):
        logger.warning("Dropping non-object target: %r", raw)
        return None

    spec = SPEC_BY_KEY.get(str(raw.get("metric", "")))
    if spec is None:
        logger.warning("Dropping target with unknown metric: %r", raw.get("metric"))
        return None

    category = ""
    if spec.needs_category:
        raw_category = str(raw.get("category") or "").strip()
        activity_type = (
            activity_type_of(raw_category) if spec.allows_activity_type else None
        )
        if activity_type is not None:
            matched = _match_activity_type(activity_type, known_types)
            if matched is None:
                logger.warning(
                    "Dropping %s: activity type %r has never been recorded",
                    spec.key,
                    activity_type,
                )
                return None
            category = f"{TYPE_PREFIX}{matched}"
        else:
            category = raw_category.lower()
            if category not in spec.categories:
                logger.warning(
                    "Dropping %s: category %r is not one of %s",
                    spec.key,
                    raw.get("category"),
                    ", ".join(spec.categories),
                )
                return None

    try:
        target = float(raw["target"])
    except (KeyError, TypeError, ValueError):
        logger.warning("Dropping %s: target is not a number", spec.key)
        return None
    upper = spec.max_for(category)
    if not spec.min_target <= target <= upper:
        logger.warning(
            "Dropping %s%s: target %g outside %g..%g",
            spec.key,
            f"/{category}" if category else "",
            target,
            spec.min_target,
            upper,
        )
        return None

    threshold: float | None = None
    if spec.needs_threshold:
        try:
            threshold = float(raw["threshold"])
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "Dropping %s: threshold is required and not a number", spec.key
            )
            return None
        low, high = spec.min_threshold, spec.max_threshold
        if low is not None and high is not None and not low <= threshold <= high:
            logger.warning(
                "Dropping %s: threshold %g outside %g..%g",
                spec.key,
                threshold,
                low,
                high,
            )
            return None

    goal_text = str(raw.get("goal") or "").strip() or None
    return StoredTarget(
        spec=spec,
        category=category,
        target=target,
        threshold=threshold,
        goal_text=goal_text,
        strategy_hash=strategy_hash,
        llm_call_id=None,
    )


def parse_targets_response(
    text: str,
    strategy_hash: str,
    known_types: frozenset[str] = frozenset(),
) -> list[StoredTarget]:
    """Parse and validate the derivation call's JSON payload.

    Args:
        text: Raw model output, optionally fenced.
        strategy_hash: Digest recorded on each accepted target.
        known_types: Workout types this profile has recorded, used to reject an
            invented one. Empty rejects every type-addressed target.

    Returns:
        Validated targets, deduplicated by metric and category, in vocabulary
        order, capped at :data:`config.WEEKLY_TARGET_MAX_RINGS`. Empty when the
        model declined or the payload was unusable.
    """
    from llm import strip_json_fences

    try:
        payload = json.loads(strip_json_fences(text))
    except (TypeError, ValueError) as exc:
        logger.warning("Target derivation returned unparseable JSON: %s", exc)
        return []
    if not isinstance(payload, dict):
        logger.warning("Target derivation returned %s, not an object", type(payload))
        return []

    raw_items = payload.get("targets")
    if not isinstance(raw_items, list):
        return []

    accepted: dict[tuple[str, str], StoredTarget] = {}
    for raw in raw_items:
        item = _coerce_target(raw, strategy_hash, known_types)
        if item is None:
            continue
        # First mention wins: the model is asked to list goals in the user's
        # own priority order, so a later duplicate is a restatement.
        accepted.setdefault(item.slot, item)

    ordered = sorted(
        accepted.values(),
        key=lambda item: (
            _SPEC_ORDER[item.spec.key],
            item.spec.categories.index(item.category)
            if item.spec.needs_category and item.category in item.spec.categories
            else len(item.spec.categories),
        ),
    )
    return ordered[:WEEKLY_TARGET_MAX_RINGS]


def build_targets_messages(
    goal_text: str,
    *,
    week_start: str,
    activity_types: list[tuple[str, int]] | None = None,
    prompts_dir: Path = PROMPTS_DIR,
) -> list[dict[str, str]]:
    """Render the target-derivation prompt into a single-message payload.

    There is no soul and no health data: this call makes no judgement about the
    person and must not invent a goal from what the data shows. It reads stated
    goals and matches them to metric keys.

    Args:
        goal_text: Output of :func:`extract_goal_text`.
        week_start: ISO Monday the targets are for.
        activity_types: This profile's recorded uncategorised workout types and
            their session counts, offered as ring candidates.
        prompts_dir: Directory holding the prompt file.

    Returns:
        Messages ready for ``call_llm``.
    """
    template = (prompts_dir / TARGETS_PROMPT).read_text(encoding="utf-8")
    vocabulary = "\n".join(_describe_spec(spec) for spec in TARGET_SPECS)
    content = template.format(
        goals=goal_text,
        week_start=week_start,
        vocabulary=vocabulary,
        activity_types=_describe_activity_types(activity_types or []),
        max_targets=WEEKLY_TARGET_MAX_RINGS,
    )
    return [{"role": "user", "content": content}]


def _describe_activity_types(activity_types: list[tuple[str, int]]) -> str:
    """Render this profile's recorded activity types for the prompt."""
    if not activity_types:
        return (
            "(none recorded — do not use a `type:` category; there is nothing "
            "to measure it against)"
        )
    return "\n".join(
        f"- `{TYPE_PREFIX}{name}` — recorded {count} time(s)"
        for name, count in activity_types
    )


def _describe_spec(spec: TargetSpec) -> str:
    """Render one vocabulary entry for the prompt."""
    if spec.shape == "total":
        measured = f"sum of {spec.unit} across the week"
    elif spec.needs_threshold:
        measured = (
            f"number of {spec.unit} in the week that reach `threshold` "
            f"{spec.threshold_unit}"
        )
    else:
        measured = f"number of {spec.unit} in the week"

    line = f"- `{spec.key}` — {measured}."
    if spec.needs_category:
        options = ", ".join(f"`{name}`" for name in spec.categories)
        line += f" `category` is required, one of {options}"
        if spec.allows_activity_type:
            line += ", or one of the recorded activity types listed below"
        line += "."
        maxima = {name: spec.max_for(name) for name in spec.categories}
        if len(set(maxima.values())) == 1:
            line += (
                f" `target` is a number between {spec.min_target:g} and "
                f"{spec.max_target:g}."
            )
        else:
            bounds = ", ".join(
                f"{name} {spec.min_target:g}-{limit:g}"
                for name, limit in maxima.items()
            )
            line += f" `target` ranges by category: {bounds}."
    else:
        line += (
            f" `category` is null. `target` is a number between "
            f"{spec.min_target:g} and {spec.max_target:g}."
        )
    if spec.needs_threshold:
        line += (
            f" `threshold` is required, between {spec.min_threshold:g} "
            f"and {spec.max_threshold:g} {spec.threshold_unit}."
        )
    return line


def derive_targets(
    goal_text: str,
    *,
    week_start: str,
    conn: sqlite3.Connection | None = None,
    trace_id: int | None = None,
    model_prefs_path: Path | None = None,
    metadata: dict | None = None,
) -> list[StoredTarget]:
    """Turn stated goals into measurable weekly targets with one LLM call.

    Failure is never fatal and never partial: any exception yields an empty
    list, which renders as no progress strip at all. A notification must not be
    lost because a decorative header could not be computed.

    Args:
        goal_text: Output of :func:`extract_goal_text`.
        week_start: ISO Monday the targets are for.
        conn: Open database connection for call logging.
        trace_id: Trace to attach this call to.
        model_prefs_path: Profile model preferences.
        metadata: Extra metadata recorded on the logged call.

    Returns:
        Validated targets, or an empty list.
    """
    from config import MAX_TOKENS_TARGETS
    from llm import call_llm
    from model_prefs import resolve_model_route

    if not goal_text:
        return []

    digest = goals_digest(goal_text)
    activity_types = recorded_activity_types(conn) if conn is not None else []
    known_types = known_activity_types(conn) if conn is not None else frozenset()
    try:
        messages = build_targets_messages(
            goal_text, week_start=week_start, activity_types=activity_types
        )
        route = resolve_model_route("targets", path=model_prefs_path).call_kwargs()
        result = call_llm(
            messages,
            **route,
            max_tokens=MAX_TOKENS_TARGETS,
            conn=conn,
            request_type="targets",
            trace_id=trace_id,
            metadata={"week_start": week_start, **(metadata or {})},
        )
    except Exception as exc:  # noqa: BLE001 - a missing strip must not break a send
        logger.error("Weekly target derivation failed: %s", exc)
        return []

    targets = parse_targets_response(result.text, digest, known_types)
    if not targets:
        logger.info("No measurable weekly targets found in strategy.md goals")
        return []
    return [
        StoredTarget(
            spec=item.spec,
            category=item.category,
            target=item.target,
            threshold=item.threshold,
            goal_text=item.goal_text,
            strategy_hash=item.strategy_hash,
            llm_call_id=result.llm_call_id,
        )
        for item in targets
    ]


def ensure_weekly_targets(
    conn: sqlite3.Connection,
    *,
    strategy_md: str | None,
    week_start: str,
    trace_id: int | None = None,
    model_prefs_path: Path | None = None,
    force: bool = False,
) -> list[StoredTarget]:
    """Return this week's targets, deriving them only when they are missing.

    A derivation happens on the first notification of a new week, and again
    whenever the goal sections of strategy.md change — the stored hash is what
    detects an edit, so accepting a coach proposal reshapes the rings on the
    next message without any extra plumbing.

    Args:
        conn: Open database connection.
        strategy_md: Raw contents of strategy.md.
        week_start: ISO Monday of the week to target.
        trace_id: Trace to attach a derivation call to.
        model_prefs_path: Profile model preferences.
        force: Re-derive even when a matching cache entry exists.

    Returns:
        The week's targets, or an empty list when the profile has no
        measurable goals.
    """
    goal_text = extract_goal_text(strategy_md)
    digest = goals_digest(goal_text)

    existing = load_targets(conn, week_start)
    if not goal_text:
        if existing:
            # The goals were removed. Stale bars would keep measuring against
            # a commitment that no longer exists, so drop them.
            clear_targets(conn, week_start)
        return []
    if existing and not force and existing[0].strategy_hash == digest:
        return existing

    derived = derive_targets(
        goal_text,
        week_start=week_start,
        conn=conn,
        trace_id=trace_id,
        model_prefs_path=model_prefs_path,
    )
    if not derived:
        # Keep whatever the week already had: a transport failure should not
        # silently retire targets the user has been watching all week.
        return existing

    save_targets(conn, week_start, derived)
    logger.info(
        "Derived %d weekly target(s) for %s: %s",
        len(derived),
        week_start,
        ", ".join(item.slot_label for item in derived),
    )
    return derived
