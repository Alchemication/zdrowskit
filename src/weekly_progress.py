"""Week-to-date progress against stored weekly targets, and how it is drawn.

Everything here is deterministic: given the targets in ``weekly_target`` and the
rows already in the database, the same week renders the same strip every time.
No LLM sees this text, and the verifier never gets a chance to reword it — the
numbers are measured, not written.

Activities are counted from ``workout_all`` rather than the aggregator's
snapshot path, so a session logged by hand through ``/add`` moves the bar. A
progress bar that ignored a workout the user entered themselves would be read,
correctly, as broken. Sleep nights are counted under the night's start date,
matching how the sleep columns are stored.

Measurement is per activity category, not per sport-specific metric, so the
same two queries serve a runner, a walker and a cyclist. A sport the schema
gives no category of its own — paddling, basketball, swimming — is counted by
naming the workout type the watch recorded, which needs no new metric, no new
column, and no one to have predicted the sport.

Public API:
    RingProgress            — one measured target and its pace verdict.
    measure_week            — measure stored targets against the week so far.
    render_progress_block   — the multi-line strip for reports.
    render_progress_line    — the single-line form for nudges.
    ring_label / ring_unit  — the label and unit one ring prints.
    pick_headline_ring      — the one ring a nudge should lead with.
    weekly_progress_block   — end-to-end block for a report, or None.
    weekly_progress_nudge_line — the nudge's line, when it has news.
    record_progress_line_shown — mark a line delivered, after it is sent.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from config import (
    WEEKLY_PROGRESS_BAR_CELLS,
    WEEKLY_PROGRESS_MAX_LABEL_CHARS,
    WEEKLY_PROGRESS_PACE_SLACK_DAYS,
)
from plan_frame import PlanFrame
from weekly_targets import (
    StoredTarget,
    activity_type_of,
    ensure_weekly_targets,
    week_start_for,
    week_bounds_for,
)

logger = logging.getLogger(__name__)

DAYS_IN_WEEK = 7

_BAR_FILLED = "█"  # FULL BLOCK
_BAR_EMPTY = "░"  # LIGHT SHADE

STATUS_DONE = "done"
STATUS_ON_PACE = "on pace"
STATUS_BEHIND = "behind"

# Behind sorts first so a nudge leads with the ring that needs attention when
# two rings moved on the same day.
_STATUS_RANK = {STATUS_BEHIND: 0, STATUS_ON_PACE: 1, STATUS_DONE: 2}


@dataclass(frozen=True)
class RingProgress:
    """One weekly target measured against the week so far.

    Attributes:
        target: The stored target being measured.
        actual: Week-to-date measured value.
        last_date: ISO date of the most recent contributing row, or None when
            nothing has contributed yet.
        days_elapsed: Days of the week counted so far, 1 through 7.
    """

    target: StoredTarget
    actual: float
    last_date: str | None
    days_elapsed: int

    @property
    def fraction(self) -> float:
        """Share of the target reached, clamped to 0..1."""
        if self.target.target <= 0:
            return 0.0
        return max(0.0, min(1.0, self.actual / self.target.target))

    @property
    def pace_floor(self) -> float:
        """Lowest value that still counts as on pace today.

        Slack of one day absorbs the lumpiness of a real week — three runs and
        two lifts land on whichever days the week allows, so exact elapsed pace
        would report a problem on most midweek checks. The slack is dropped on
        the final day, where anything short of the target genuinely is short.
        """
        slack = (
            WEEKLY_PROGRESS_PACE_SLACK_DAYS if self.days_elapsed < DAYS_IN_WEEK else 0
        )
        counted = max(self.days_elapsed - slack, 0)
        floor = self.target.target * counted / DAYS_IN_WEEK
        if self.target.spec.shape == "count":
            # Nobody runs 0.43 of a session. Rounding a counted goal down keeps
            # the verdict answerable: "behind" then always means a whole
            # session or night is missing, not a fraction of one.
            return float(int(floor))
        return floor

    @property
    def status(self) -> str:
        """One of ``done``, ``on pace``, or ``behind``."""
        if self.actual >= self.target.target:
            return STATUS_DONE
        if self.actual >= self.pace_floor:
            return STATUS_ON_PACE
        return STATUS_BEHIND


def _fmt_number(value: float, decimals: int) -> str:
    """Format a measured value, trimming a decimal point nothing follows."""
    if decimals <= 0 or float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.{decimals}f}"


def _fmt_threshold(value: float, unit: str | None) -> str:
    """Format a per-day threshold for a ring label."""
    if unit == "steps":
        if value >= 1000:
            return f"{value / 1000:g}k"
        return f"{value:.0f}"
    return f"{value:g}{unit or ''}"


def ring_label(item: StoredTarget) -> str:
    """Return the strip's left-column label, threshold included when there is one.

    The base label comes from the target itself, so a distance ring reads
    "Run", "Walk" or "Ride" from one metric key rather than one key per sport,
    and a type-addressed ring reads the workout type Apple recorded.
    """
    if item.spec.needs_threshold and item.threshold is not None:
        bar = _fmt_threshold(item.threshold, item.spec.threshold_unit)
        return _shorten(f"{item.label} ≥{bar}")
    return _shorten(item.label)


def _shorten(label: str) -> str:
    """Trim a label that would push the bars off a phone screen.

    Every label is padded to the widest one, so a single long workout type —
    Apple writes "High Intensity Interval Training" — costs every row its
    alignment and wraps the line.
    """
    limit = WEEKLY_PROGRESS_MAX_LABEL_CHARS
    return label if len(label) <= limit else label[: limit - 1].rstrip() + "…"


def render_bar(fraction: float, *, complete: bool, started: bool) -> str:
    """Render one progress bar.

    Two guards matter more than the arithmetic. A bar that reads full at 29 of
    30 km is a lie the reader acts on, and a bar that reads empty after a real
    session makes the strip look broken, so a started ring always shows at
    least one cell and an unfinished one always shows at least one gap.

    Args:
        fraction: Share of the target reached, 0..1.
        complete: True when the target has actually been met.
        started: True when anything at all has been measured.

    Returns:
        A fixed-width bar of :data:`config.WEEKLY_PROGRESS_BAR_CELLS` cells.
    """
    cells = WEEKLY_PROGRESS_BAR_CELLS
    filled = int(round(max(0.0, min(1.0, fraction)) * cells))
    if started:
        filled = max(filled, 1)
    if not complete:
        filled = min(filled, cells - 1)
    filled = max(0, min(cells, filled))
    return _BAR_FILLED * filled + _BAR_EMPTY * (cells - filled)


def _measure_one(
    conn: sqlite3.Connection,
    item: StoredTarget,
    start: str,
    end: str,
) -> tuple[float, str | None]:
    """Measure one target over an inclusive date range.

    Args:
        conn: Open database connection.
        item: The target to measure.
        start: Inclusive ISO start date (the week's Monday).
        end: Inclusive ISO end date (today, or the week's Sunday).

    Returns:
        A (value, last_contributing_date) pair. The date is None when nothing
        contributed.
    """
    key = item.spec.key
    if key == "distance_km_week":
        sql = (
            "SELECT COALESCE(SUM(gpx_distance_km), 0) AS v, MAX(date) AS d "
            "FROM workout_all "
            "WHERE category = ? AND gpx_distance_km IS NOT NULL "
            "AND date BETWEEN ? AND ?"
        )
        params: tuple = (item.category, start, end)
    elif key == "sessions_week":
        # A strength session is what counts_as_lift says it is, not what the
        # category column says: that flag is the product's own definition, and
        # it already folds in the duration rule for functional training.
        activity_type = activity_type_of(item.category)
        if activity_type:
            predicate, extra = "type = ?", (activity_type,)
        elif item.category == "lift":
            predicate, extra = "counts_as_lift = 1", ()
        elif item.category == "any":
            predicate, extra = "1 = 1", ()
        else:
            predicate, extra = "category = ?", (item.category,)
        sql = (
            f"SELECT COUNT(*) AS v, MAX(date) AS d FROM workout_all "
            f"WHERE {predicate} AND date BETWEEN ? AND ?"
        )
        params = (*extra, start, end)
    elif key == "exercise_min_week":
        sql = (
            "SELECT COALESCE(SUM(exercise_min), 0) AS v, MAX(date) AS d "
            "FROM daily WHERE exercise_min > 0 AND date BETWEEN ? AND ?"
        )
        params = (start, end)
    elif key == "sleep_nights_week":
        # DISTINCT because sleep_all unions imported and hand-logged nights,
        # which can both cover the same date.
        sql = (
            "SELECT COUNT(DISTINCT date) AS v, MAX(date) AS d FROM sleep_all "
            "WHERE sleep_total_h >= ? AND date BETWEEN ? AND ?"
        )
        params = (item.threshold, start, end)
    elif key == "step_days_week":
        sql = (
            "SELECT COUNT(*) AS v, MAX(date) AS d FROM daily "
            "WHERE steps >= ? AND date BETWEEN ? AND ?"
        )
        params = (item.threshold, start, end)
    else:  # pragma: no cover - the vocabulary is closed and validated on load
        raise ValueError(f"No measurement defined for target {key}")

    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0.0, None
    return float(row["v"] or 0.0), row["d"]


def measure_week(
    conn: sqlite3.Connection,
    targets: list[StoredTarget],
    *,
    week_start: str,
    today: date,
) -> list[RingProgress]:
    """Measure stored targets against the week so far.

    Args:
        conn: Open database connection.
        targets: The week's stored targets, in display order.
        week_start: ISO Monday of the week.
        today: The day being reported on.

    Returns:
        One RingProgress per target, in the same order. Empty when there are no
        targets or the measurement failed.
    """
    if not targets:
        return []

    monday_iso, sunday_iso = week_bounds_for(week_start)
    monday = date.fromisoformat(monday_iso)
    days_elapsed = max(1, min(DAYS_IN_WEEK, (today - monday).days + 1))
    end = min(today.isoformat(), sunday_iso)

    rings: list[RingProgress] = []
    try:
        for item in targets:
            actual, last_date = _measure_one(conn, item, monday_iso, end)
            rings.append(
                RingProgress(
                    target=item,
                    actual=actual,
                    last_date=last_date,
                    days_elapsed=days_elapsed,
                )
            )
    except (sqlite3.Error, ValueError) as exc:
        logger.warning("Weekly progress measurement failed: %s", exc)
        return []
    return rings


def week_label_for(week_start: str) -> str:
    """Return the ISO week label for a week's Monday, e.g. ``2026-W36``."""
    iso = date.fromisoformat(week_start).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def ring_unit(ring: RingProgress) -> str:
    """Return the unit printed after a ring's numbers.

    Suppressed when the label already carries it: "Sessions 2/4 sessions" and
    "Run km 21.4/30 km" both spend a column saying the same word twice.
    """
    unit = ring.target.spec.unit
    label = ring_label(ring.target).lower()
    if label == unit.lower() or label.endswith(f" {unit.lower()}"):
        return ""
    return unit


def _value_text(ring: RingProgress) -> str:
    """Return the ``actual/target`` compound for one ring."""
    spec = ring.target.spec
    actual = _fmt_number(ring.actual, spec.decimals)
    target = _fmt_number(ring.target.target, spec.decimals)
    return f"{actual}/{target}"


def progress_caption(ring: RingProgress, *, week_complete: bool = False) -> str:
    """Describe remaining work without assuming a daily training schedule."""
    if ring.actual >= ring.target.target:
        return STATUS_DONE
    remaining = _fmt_number(ring.target.target - ring.actual, ring.target.spec.decimals)
    suffix = "short" if week_complete else "left"
    return f"{remaining} {suffix}"


def render_progress_block(
    rings: list[RingProgress],
    *,
    week_start: str,
    show_verdict: bool = True,
    week_complete: bool = False,
) -> str | None:
    """Render the multi-line progress strip for a report.

    The block is fenced so Telegram renders it inside ``<pre>``; without a
    monospace box the columns do not line up and the bars stop being readable
    at a glance.

    Args:
        rings: Measured rings, in display order.
        week_start: ISO Monday of the week.
        show_verdict: Whether to show completion or remaining-work labels.
        week_complete: True only after the measured week has ended.

    Returns:
        A fenced markdown block, or None when there is nothing to draw.
    """
    if not rings:
        return None

    labels = [ring_label(ring.target) for ring in rings]
    values = [_value_text(ring) for ring in rings]
    label_w = max(len(text) for text in labels)
    value_w = max(len(text) for text in values)
    units = [ring_unit(ring) for ring in rings]
    unit_w = max(len(text) for text in units)

    days = rings[0].days_elapsed
    lines = [f"Week {week_label_for(week_start)} · day {days} of {DAYS_IN_WEEK}"]
    for ring, label, value, unit in zip(rings, labels, values, units):
        bar = render_bar(
            ring.fraction,
            complete=ring.status == STATUS_DONE,
            started=ring.actual > 0,
        )
        verdict = (
            progress_caption(ring, week_complete=week_complete) if show_verdict else ""
        )
        line = (
            f"{label:<{label_w}}  {bar}  {value:>{value_w}} {unit:<{unit_w}}  {verdict}"
        )
        lines.append(line.rstrip())
    return "```\n" + "\n".join(lines) + "\n```"


def render_progress_line(ring: RingProgress, *, show_verdict: bool = True) -> str:
    """Render the single-line progress form used in a nudge header.

    Args:
        ring: The ring to show.
        show_verdict: Whether to append a completion or remaining-work label.

    Returns:
        One line, short enough to sit beside a trigger label without wrapping
        on a phone.
    """
    bar = render_bar(
        ring.fraction,
        complete=ring.status == STATUS_DONE,
        started=ring.actual > 0,
    )
    parts = [
        ring_label(ring.target),
        _value_text(ring),
        ring_unit(ring),
        bar,
        progress_caption(ring) if show_verdict else "",
    ]
    return " ".join(part for part in parts if part)


def pick_headline_ring(rings: list[RingProgress]) -> RingProgress | None:
    """Return the one ring a nudge should lead with.

    The most recently advanced ring is the one the arriving data actually
    moved, which is what makes the line worth reading on a message that fires
    because new data landed. Nothing having moved yet falls back to whichever
    ring is furthest behind, tie-broken by the vocabulary's own priority order
    — which is the list order the rings arrive in.

    Args:
        rings: Measured rings, in display order.

    Returns:
        The chosen ring, or None when there are none.
    """
    if not rings:
        return None

    advanced = [ring for ring in rings if ring.last_date]
    if advanced:
        return min(
            advanced,
            key=lambda ring: (
                _negated_date_key(ring.last_date),
                _STATUS_RANK[ring.status],
                rings.index(ring),
            ),
        )
    return min(
        rings,
        key=lambda ring: (_STATUS_RANK[ring.status], rings.index(ring)),
    )


def _negated_date_key(iso_date: str | None) -> str:
    """Return a sort key that orders ISO dates newest-first under ``min``."""
    # ISO dates compare lexicographically; inverting each digit turns an
    # ascending comparison into a descending one without a second sort pass.
    return "".join(str(9 - int(ch)) if ch.isdigit() else ch for ch in (iso_date or ""))


def _rings_for(
    conn: sqlite3.Connection,
    *,
    strategy_md: str | None,
    today: date,
    trace_id: int | None,
    model_prefs_path: Path | None,
) -> tuple[list[RingProgress], str]:
    """Resolve this week's targets and measure them. Never raises."""
    week_start = week_start_for(today)
    targets = ensure_weekly_targets(
        conn,
        strategy_md=strategy_md,
        week_start=week_start,
        trace_id=trace_id,
        model_prefs_path=model_prefs_path,
    )
    return measure_week(conn, targets, week_start=week_start, today=today), week_start


def weekly_progress_block(
    conn: sqlite3.Connection,
    *,
    strategy_md: str | None,
    today: date | None = None,
    trace_id: int | None = None,
    model_prefs_path: Path | None = None,
    frame: PlanFrame | None = None,
) -> str | None:
    """Return the full progress strip for a report, or None.

    None is the normal answer for a profile whose strategy.md states no
    measurable weekly goal. Any failure below also returns None: the strip is
    an addition to a notification, never a precondition for sending one.

    Args:
        conn: Open database connection.
        strategy_md: Raw contents of strategy.md.
        today: Day to report against. Defaults to the local date.
        trace_id: Trace to attach a target derivation call to.
        model_prefs_path: Profile model preferences.
        frame: How much of the strip this person's context warrants. Defaults
            to the full strip.

    Returns:
        A fenced markdown block, or None.
    """
    frame = frame or PlanFrame()
    if not frame.shows_strip:
        return None
    try:
        rings, week_start = _rings_for(
            conn,
            strategy_md=strategy_md,
            today=today or date.today(),
            trace_id=trace_id,
            model_prefs_path=model_prefs_path,
        )
        return render_progress_block(
            rings,
            week_start=week_start,
            show_verdict=frame.shows_verdict,
            week_complete=week_bounds_for(week_start)[1] < date.today().isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 - never block a notification
        logger.error("Weekly progress block failed: %s", exc)
        return None


def ring_fingerprint(ring: RingProgress, week_start: str) -> str:
    """Return what has to change before a ring is worth saying again.

    Deliberately coarser than the measured value. A 200 m walk changes the
    number without changing anything the reader would act on, so the
    fingerprint tracks only what is visible: which ring is leading, whether it is complete,
    and how many bar cells are filled. Crossing a tenth of the target
    is movement; drifting inside one is not.

    The week is included so the Monday reset always counts as news.

    Args:
        ring: The measured ring the line would show.
        week_start: ISO Monday of the week.

    Returns:
        An opaque comparison key.
    """
    filled = render_bar(
        ring.fraction,
        complete=ring.status == STATUS_DONE,
        started=ring.actual > 0,
    ).count(_BAR_FILLED)
    slot = ring.target.slot_label
    return f"{week_start}|{slot}|{ring.actual >= ring.target.target}|{filled}"


def _load_shown_fingerprint(conn: sqlite3.Connection) -> str | None:
    """Return the fingerprint of the last progress line actually shown."""
    try:
        row = conn.execute(
            "SELECT fingerprint FROM progress_line_shown WHERE id = 1"
        ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("Progress line state unavailable: %s", exc)
        return None
    return row["fingerprint"] if row else None


def record_progress_line_shown(
    conn: sqlite3.Connection, fingerprint: str, line: str
) -> None:
    """Record a progress line as delivered.

    Called after the message carrying it has actually gone out. Recording
    at composition time instead would let a failed send suppress the line
    from the next nudge, on the strength of one the person never saw.
    """
    try:
        with conn:
            conn.execute(
                "INSERT INTO progress_line_shown (id, fingerprint, line, shown_at) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET fingerprint = excluded.fingerprint, "
                "line = excluded.line, shown_at = excluded.shown_at",
                (fingerprint, line, datetime.now(timezone.utc).isoformat()),
            )
    except sqlite3.Error as exc:
        # Worst case the next nudge repeats a line. Never lose a nudge over it.
        logger.warning("Could not record shown progress line: %s", exc)


def weekly_progress_nudge_line(
    conn: sqlite3.Connection,
    *,
    strategy_md: str | None,
    today: date | None = None,
    trace_id: int | None = None,
    model_prefs_path: Path | None = None,
    frame: PlanFrame | None = None,
) -> tuple[str, str] | None:
    """Return the nudge's progress line, but only when it would say something new.

    Nudges fire up to twice a day while the bars move three or four times a
    week, so most of those lines would be identical to the one before. An
    unchanged line at the top of a message is worse than no line: it is the
    part the reader learns to skip past, and the nudge starts immediately
    below it.

    This does not record anything. The caller passes the returned fingerprint
    to :func:`record_progress_line_shown` once the message has actually been
    delivered, so a failed send cannot suppress the line from the next nudge.

    Args:
        conn: Open database connection.
        strategy_md: Raw contents of strategy.md.
        today: Day to report against. Defaults to the local date.
        trace_id: Trace to attach a target derivation call to.
        model_prefs_path: Profile model preferences.
        frame: How much of the strip this person's context warrants. A hidden
            frame returns None without recording anything, so the line the
            person last actually saw stays the one it is compared against.

    Returns:
        A (line, fingerprint) pair, or None when nothing visible has changed
        since the last nudge that carried one.
    """
    frame = frame or PlanFrame()
    if not frame.shows_strip:
        return None
    try:
        rings, week_start = _rings_for(
            conn,
            strategy_md=strategy_md,
            today=today or date.today(),
            trace_id=trace_id,
            model_prefs_path=model_prefs_path,
        )
        ring = pick_headline_ring(rings)
        if ring is None:
            return None

        fingerprint = f"{ring_fingerprint(ring, week_start)}|{frame.mode}"
        if fingerprint == _load_shown_fingerprint(conn):
            logger.info("Weekly progress unchanged since the last nudge; omitting")
            return None

        return render_progress_line(ring, show_verdict=frame.shows_verdict), (
            fingerprint
        )
    except Exception as exc:  # noqa: BLE001 - never block a notification
        logger.error("Weekly progress nudge line failed: %s", exc)
        return None
