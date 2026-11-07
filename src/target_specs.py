"""The closed vocabulary of measurable weekly targets.

Each :class:`TargetSpec` is one weekly metric the progress strip and coach
challenges can count from the database. A goal that does not map to one of
:data:`TARGET_SPECS` is dropped rather than approximated: see
:mod:`weekly_targets`, which derives targets from strategy.md against this
vocabulary and stores them.

Public API:
    TargetSpec        — one measurable weekly metric the strip can draw.
    TARGET_SPECS      — the closed vocabulary, in display priority order.
    SPEC_BY_KEY       — the vocabulary indexed by metric key.
    CATEGORY_NOUNS    — display nouns for session categories.
    CATEGORY_PLURALS  — plural display nouns for session categories.
    TYPE_PREFIX       — the category prefix naming an Apple Health workout type.
    activity_type_of  — the workout type a ``type:`` category names.
    StoredTarget      — a target row as persisted for one week.
    target_to_display — a stored target or measured value in display units.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from units import Quantity, UnitSystem, to_display, unit_label


@dataclass(frozen=True)
class TargetSpec:
    """One measurable weekly metric a progress ring can be drawn for.

    Metrics are parameterised by activity rather than enumerated per sport.
    ``distance_km_week`` and ``sessions_week`` each cover running, walking and
    cycling, so a walker and a cyclist are first-class without a new key, a new
    column, or a new branch in the measurement. Adding a sport is a line in
    :data:`CATEGORY_NOUNS`, not a migration.

    Attributes:
        key: Stable identifier, also the value the LLM must emit.
        label: Column label for metrics that take no category. Category-bearing
            metrics take their label from the category instead.
        unit: Noun printed after the numbers, e.g. "km" or "nights".
        shape: ``total`` sums a quantity over the week; ``count`` counts
            qualifying days or sessions.
        decimals: Decimal places used when printing the measured value.
        quantity: Physical quantity of the stored number when it has an
            imperial display form, else None. Targets are always stored in
            ``unit``; only their display and the model's input are converted.
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
    quantity: Quantity | None = None

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
CATEGORY_NOUNS: dict[str, str] = {
    "run": "Run",
    "walk": "Walk",
    "cycle": "Ride",
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
        quantity=Quantity.DISTANCE,
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
        if self.spec.shape == "count":
            return CATEGORY_PLURALS.get(self.category, self.category.title())
        noun = CATEGORY_NOUNS.get(self.category, self.category.title())
        if self.spec.shape == "total" and self.spec.quantity is not None:
            return f"{noun} {self.spec.unit}"
        return noun

    def for_display(self, units: UnitSystem) -> StoredTarget:
        """Return a copy whose target and unit are in the display units.

        The copy exists to be rendered. It must never reach ``save_targets``:
        storage is metric whatever the profile displays.

        Args:
            units: Display unit system.

        Returns:
            This target unchanged for metric or a unitless metric, else a copy
            with the spec's unit relabelled and the target converted.
        """
        if units is UnitSystem.METRIC or self.spec.quantity is None:
            return self
        return replace(
            self,
            spec=replace(self.spec, unit=unit_label(self.spec.quantity, units)),
            target=target_to_display(self.spec, self.target, units),
        )


def target_to_display(spec: TargetSpec, value: float, units: UnitSystem) -> float:
    """Convert a stored target or measured value to the display units.

    Args:
        spec: The metric the value belongs to.
        value: Number in the spec's stored unit.
        units: Display unit system.

    Returns:
        The converted number, or ``value`` for a metric with no imperial form.
    """
    if spec.quantity is None or units is UnitSystem.METRIC:
        return value
    converted = to_display(value, spec.quantity, units)
    # Rounded so a measured value equal to its target in stored units is still
    # equal after both are divided: 24.14016 / 1.609344 is not exactly 15.
    return value if converted is None else round(converted, 6)
