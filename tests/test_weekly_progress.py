"""Tests for weekly progress measurement and the rendered strip."""

from __future__ import annotations

import sqlite3
import unicodedata
from datetime import date

import pytest

from config import WEEKLY_PROGRESS_MAX_DOTS
from models import DailySnapshot, WorkoutSnapshot
from store import insert_manual_workout, store_snapshots
from weekly_progress import (
    STATUS_BEHIND,
    STATUS_DONE,
    STATUS_ON_PACE,
    RingProgress,
    measure_week,
    pick_headline_ring,
    record_progress_line_shown,
    render_bar,
    render_dots,
    render_progress_block,
    render_progress_line,
    ring_label,
    week_label_for,
    weekly_progress_block,
    weekly_progress_nudge_line,
)
from weekly_targets import (
    SPEC_BY_KEY,
    StoredTarget,
    extract_goal_text,
    goals_digest,
    save_targets,
)

WEEK_START = "2026-08-31"  # Monday
WEDNESDAY = date(2026, 9, 2)
SUNDAY = date(2026, 9, 6)


def _target(
    key: str,
    value: float,
    threshold: float | None = None,
    category: str = "",
) -> StoredTarget:
    """Build a StoredTarget for measurement and rendering tests."""
    return StoredTarget(
        spec=SPEC_BY_KEY[key],
        category=category,
        target=value,
        threshold=threshold,
        goal_text=None,
        strategy_hash="hash-a",
        llm_call_id=None,
    )


def _ring(
    key: str,
    value: float,
    actual: float,
    *,
    threshold: float | None = None,
    category: str = "",
    day: int = 4,
    last_date: str | None = "2026-09-03",
) -> RingProgress:
    """Build a measured ring without touching the database."""
    return RingProgress(
        target=_target(key, value, threshold, category),
        actual=actual,
        last_date=last_date,
        days_elapsed=day,
    )


def _seed_week(conn: sqlite3.Connection) -> None:
    """Seed Mon-Wed of the test week with runs, a lift, steps, and sleep."""
    store_snapshots(
        conn,
        [
            DailySnapshot(
                date="2026-08-31",
                steps=11000,
                exercise_min=40,
                sleep_total_h=7.5,
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc="2026-08-31T07:00:00Z",
                        duration_min=45.0,
                        gpx_distance_km=8.0,
                    )
                ],
            ),
            DailySnapshot(
                date="2026-09-01",
                steps=6000,
                exercise_min=20,
                sleep_total_h=6.0,
                workouts=[
                    WorkoutSnapshot(
                        type="Traditional Strength Training",
                        category="lift",
                        start_utc="2026-09-01T17:00:00Z",
                        duration_min=50.0,
                    )
                ],
            ),
            DailySnapshot(
                date="2026-09-02",
                steps=12500,
                exercise_min=35,
                sleep_total_h=8.0,
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc="2026-09-02T07:00:00Z",
                        duration_min=35.0,
                        gpx_distance_km=6.5,
                    )
                ],
            ),
        ],
    )


def _seed_other_sports(conn: sqlite3.Connection) -> None:
    """Seed the sports a runner-shaped vocabulary would have missed."""
    store_snapshots(
        conn,
        [
            DailySnapshot(
                date="2026-09-01",
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Walk",
                        category="walk",
                        start_utc="2026-09-01T08:00:00Z",
                        duration_min=70.0,
                        gpx_distance_km=9.0,
                    ),
                    WorkoutSnapshot(
                        type="Pool Swim",
                        category="other",
                        start_utc="2026-09-01T19:00:00Z",
                        duration_min=40.0,
                    ),
                    # Hiking parses to the walk category, so it lands on the
                    # walk rings rather than needing one of its own.
                    WorkoutSnapshot(
                        type="Hiking",
                        category="walk",
                        start_utc="2026-09-01T13:00:00Z",
                        duration_min=150.0,
                        gpx_distance_km=11.5,
                    ),
                    WorkoutSnapshot(
                        type="Paddle Sports",
                        category="other",
                        start_utc="2026-09-01T16:00:00Z",
                        duration_min=75.0,
                    ),
                ],
            ),
            DailySnapshot(
                date="2026-09-02",
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Cycling",
                        category="cycle",
                        start_utc="2026-09-02T09:00:00Z",
                        duration_min=95.0,
                        gpx_distance_km=42.0,
                    ),
                    WorkoutSnapshot(
                        type="Paddle Sports",
                        category="other",
                        start_utc="2026-09-02T16:00:00Z",
                        duration_min=60.0,
                    ),
                    WorkoutSnapshot(
                        type="High Intensity Interval Training",
                        category="hiit",
                        start_utc="2026-09-02T19:00:00Z",
                        duration_min=25.0,
                    ),
                ],
            ),
        ],
    )


class TestRenderBar:
    def test_empty_ring_shows_no_fill(self) -> None:
        assert render_bar(0.0, complete=False, started=False) == "░" * 10

    def test_complete_ring_is_full(self) -> None:
        assert render_bar(1.0, complete=True, started=True) == "█" * 10

    def test_started_ring_never_reads_empty(self) -> None:
        """A logged session that leaves the bar blank looks like a broken bar."""
        bar = render_bar(0.01, complete=False, started=True)
        assert bar.startswith("█")

    def test_nearly_complete_ring_never_reads_full(self) -> None:
        """29 of 30 km must not render as a finished week."""
        bar = render_bar(29 / 30, complete=False, started=True)
        assert bar.endswith("░")

    def test_bar_is_fixed_width(self) -> None:
        for fraction in (0.0, 0.13, 0.5, 0.99, 1.0):
            bar = render_bar(fraction, complete=fraction >= 1, started=fraction > 0)
            assert len(bar) == 10


class TestPaceVerdict:
    def test_first_day_is_never_behind(self) -> None:
        assert (
            _ring("distance_km_week", 30, 0.0, category="run", day=1).status
            == STATUS_ON_PACE
        )

    def test_met_target_is_done(self) -> None:
        assert (
            _ring("distance_km_week", 30, 30.0, category="run", day=3).status
            == STATUS_DONE
        )

    def test_overshoot_is_done(self) -> None:
        assert (
            _ring("distance_km_week", 30, 44.0, category="run", day=5).status
            == STATUS_DONE
        )

    def test_one_day_of_slack_absorbs_a_lumpy_week(self) -> None:
        # Day 4 of a 30 km week: exact pace wants 17.1, the slack asks 12.9.
        assert (
            _ring("distance_km_week", 30, 14.0, category="run", day=4).status
            == STATUS_ON_PACE
        )

    def test_falling_past_the_slack_is_behind(self) -> None:
        assert (
            _ring("distance_km_week", 30, 5.0, category="run", day=4).status
            == STATUS_BEHIND
        )

    def test_counted_goals_round_the_floor_down(self) -> None:
        """Nobody runs 0.4 of a session, so a fractional floor cannot be owed."""
        assert (
            _ring("sessions_week", 3, 0, category="run", day=2).status == STATUS_ON_PACE
        )

    def test_last_day_drops_the_slack(self) -> None:
        assert (
            _ring("distance_km_week", 30, 27.0, category="run", day=6).status
            == STATUS_ON_PACE
        )
        assert (
            _ring("distance_km_week", 30, 27.0, category="run", day=7).status
            == STATUS_BEHIND
        )


class TestRenderProgressBlock:
    def test_no_rings_renders_nothing(self) -> None:
        assert render_progress_block([], week_start=WEEK_START) is None

    def test_block_is_fenced_for_a_monospace_box(self) -> None:
        block = render_progress_block(
            [_ring("distance_km_week", 30, 21.4, category="run")], week_start=WEEK_START
        )
        assert block is not None
        assert block.startswith("```\n")
        assert block.endswith("\n```")

    def test_header_names_the_week_and_the_day(self) -> None:
        block = render_progress_block(
            [_ring("distance_km_week", 30, 21.4, category="run")], week_start=WEEK_START
        )
        assert "Week 2026-W36 · day 4 of 7" in block

    def test_bars_are_column_aligned(self) -> None:
        block = render_progress_block(
            [
                _ring("distance_km_week", 30, 21.4, category="run"),
                _ring("sleep_nights_week", 5, 2, threshold=7.0),
            ],
            week_start=WEEK_START,
        )
        bar_columns = {line.index("█") for line in block.splitlines() if "█" in line}
        assert len(bar_columns) == 1

    def test_threshold_appears_in_the_label(self) -> None:
        block = render_progress_block(
            [_ring("sleep_nights_week", 5, 2, threshold=7.0)], week_start=WEEK_START
        )
        assert "Sleep ≥7h" in block

    def test_distance_and_session_rings_cannot_be_confused(self) -> None:
        """One goal states both, so "Run" beside "Runs" was a one-letter tell."""
        block = render_progress_block(
            [
                _ring("distance_km_week", 15, 10.3, category="run"),
                _ring("sessions_week", 3, 2, category="run"),
            ],
            week_start=WEEK_START,
        )
        labels = [line.split("  ")[0].strip() for line in block.splitlines()[2:-1]]
        assert labels == ["Run km", "Runs"]

    def test_a_long_activity_type_is_shortened(self) -> None:
        """A 31-character label would wrap the line and misalign every bar."""
        block = render_progress_block(
            [
                _ring(
                    "sessions_week",
                    4,
                    2,
                    category="type:High Intensity Interval Training",
                )
            ],
            week_start=WEEK_START,
        )
        assert "High Intensity…" in block
        assert max(len(line) for line in block.splitlines()) < 60

    def test_step_threshold_is_abbreviated(self) -> None:
        block = render_progress_block(
            [_ring("step_days_week", 7, 3, threshold=10000)], week_start=WEEK_START
        )
        assert "Steps ≥10k" in block

    def test_values_and_status_are_present(self) -> None:
        block = render_progress_block(
            [_ring("distance_km_week", 30, 21.4, category="run")], week_start=WEEK_START
        )
        assert "21.4/30" in block
        assert "8.6 left" in block

    def test_whole_measurements_lose_the_decimal_point(self) -> None:
        block = render_progress_block(
            [_ring("distance_km_week", 30, 21.0, category="run")], week_start=WEEK_START
        )
        assert "21/30" in block


class TestRenderDots:
    def test_countable_ring_draws_one_dot_per_unit(self) -> None:
        assert render_dots(_ring("sessions_week", 4, 2, category="lift")) == "●●○○"

    def test_completed_ring_has_no_empty_dot_left(self) -> None:
        assert render_dots(_ring("sessions_week", 2, 2, category="lift")) == "●●"

    def test_only_a_complete_ring_ever_fills_every_dot(self) -> None:
        # This is the whole completion signal: no separate glyph marks "done",
        # so an unfinished ring must never render without an empty dot.
        for actual in (0, 0.5, 1, 1.5, 2.99):
            dots = render_dots(_ring("sessions_week", 3, actual, category="lift"))
            assert dots is not None
            assert "○" in dots

    def test_untouched_ring_is_all_empty(self) -> None:
        assert render_dots(_ring("sessions_week", 3, 0, category="lift")) == "○○○"

    def test_partial_unit_never_fills_a_dot(self) -> None:
        # Only `total`-shaped rings measure fractionally, but a count ring is
        # stored as a float and must not round a half-session up into a dot.
        assert render_dots(_ring("sessions_week", 3, 1.9, category="lift")) == "●○○"

    def test_summed_rings_get_no_dots(self) -> None:
        assert render_dots(_ring("distance_km_week", 30, 21.4, category="run")) is None

    def test_target_past_the_dot_limit_gets_no_dots(self) -> None:
        big = WEEKLY_PROGRESS_MAX_DOTS + 1
        assert render_dots(_ring("sessions_week", big, 2, category="lift")) is None

    def test_target_at_the_dot_limit_still_draws(self) -> None:
        dots = render_dots(
            _ring("sessions_week", WEEKLY_PROGRESS_MAX_DOTS, 1, category="lift")
        )
        assert dots is not None
        assert len(dots) == WEEKLY_PROGRESS_MAX_DOTS


class TestRenderProgressLine:
    def test_countable_ring_is_a_label_and_dots(self) -> None:
        line = render_progress_line(_ring("sessions_week", 2, 2, category="lift"))
        assert line == "Lifts ●●"

    def test_the_line_carries_no_double_width_glyph(self) -> None:
        # Emoji are East Asian "Wide": about double the advance of a text
        # glyph, which is what wrapped the line onto a second row on a watch.
        for ring in (
            _ring("sessions_week", 2, 2, category="lift"),
            _ring("sessions_week", 4, 2, category="lift"),
            _ring("sleep_nights_week", 7, 5, threshold=7.0),
            _ring("distance_km_week", 30, 21.4, category="run"),
        ):
            line = render_progress_line(ring)
            wide = [ch for ch in line if unicodedata.east_asian_width(ch) == "W"]
            assert not wide, f"{line!r} carries {wide!r}"

    def test_no_block_glyph_ever_reaches_the_proportional_line(self) -> None:
        # The slab this replaced: FULL BLOCK closes into a solid rectangle
        # outside a <pre> block, which is the one place this line is used.
        for ring in (
            _ring("sessions_week", 4, 2, category="lift"),
            _ring("distance_km_week", 30, 21.4, category="run"),
        ):
            line = render_progress_line(ring)
            assert "█" not in line
            assert "░" not in line

    def test_overshoot_is_counted_beside_the_dots(self) -> None:
        line = render_progress_line(_ring("sessions_week", 2, 3, category="lift"))
        assert line == "Lifts ●● +1"

    def test_summed_ring_keeps_its_numbers_and_verdict(self) -> None:
        line = render_progress_line(_ring("distance_km_week", 30, 21.4, category="run"))
        assert line == "Run km 21.4/30 · 8.6 left"

    def test_summed_ring_without_a_verdict_drops_the_caption(self) -> None:
        line = render_progress_line(
            _ring("distance_km_week", 30, 21.4, category="run"), show_verdict=False
        )
        assert line == "Run km 21.4/30"

    def test_line_is_one_line(self) -> None:
        line = render_progress_line(_ring("sleep_nights_week", 5, 2, threshold=7.0))
        assert "\n" not in line


class TestPickHeadlineRing:
    def test_no_rings_gives_nothing(self) -> None:
        assert pick_headline_ring([]) is None

    def test_most_recently_advanced_ring_wins(self) -> None:
        older = _ring(
            "distance_km_week", 30, 21.4, category="run", last_date="2026-09-01"
        )
        newer = _ring("sessions_week", 2, 1, category="lift", last_date="2026-09-03")
        assert pick_headline_ring([older, newer]) is newer

    def test_untouched_rings_never_headline_over_a_touched_one(self) -> None:
        touched = _ring("sessions_week", 2, 1, category="lift", last_date="2026-09-01")
        untouched = _ring("distance_km_week", 30, 0.0, category="run", last_date=None)
        assert pick_headline_ring([untouched, touched]) is touched

    def test_with_nothing_moved_the_ring_behind_leads(self) -> None:
        on_pace = _ring("sessions_week", 2, 0, category="lift", day=2, last_date=None)
        behind = _ring(
            "distance_km_week", 30, 0.0, category="run", day=6, last_date=None
        )
        assert pick_headline_ring([on_pace, behind]) is behind


class TestMeasureWeek:
    def test_no_targets_measures_nothing(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        assert (
            measure_week(in_memory_db, [], week_start=WEEK_START, today=WEDNESDAY) == []
        )

    def test_run_distance_sums_the_week_so_far(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed_week(in_memory_db)
        rings = measure_week(
            in_memory_db,
            [_target("distance_km_week", 30, category="run")],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        assert rings[0].actual == pytest.approx(14.5)
        assert rings[0].last_date == "2026-09-02"

    def test_sessions_are_counted(self, in_memory_db: sqlite3.Connection) -> None:
        _seed_week(in_memory_db)
        rings = measure_week(
            in_memory_db,
            [
                _target("sessions_week", 3, category="run"),
                _target("sessions_week", 2, category="lift"),
            ],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        assert [ring.actual for ring in rings] == [2, 1]

    def test_threshold_days_only_count_when_they_clear_the_bar(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed_week(in_memory_db)
        rings = measure_week(
            in_memory_db,
            [
                _target("sleep_nights_week", 5, 7.0),
                _target("step_days_week", 7, 10000),
            ],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        # Sleep: 7.5 and 8.0 clear 7h, 6.0 does not. Steps: 11000 and 12500.
        assert [ring.actual for ring in rings] == [2, 2]

    def test_exercise_minutes_sum(self, in_memory_db: sqlite3.Connection) -> None:
        _seed_week(in_memory_db)
        rings = measure_week(
            in_memory_db,
            [_target("exercise_min_week", 150)],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        assert rings[0].actual == 95

    def test_a_hand_logged_workout_moves_the_bar(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A session entered through /add must count; ignoring it reads as broken."""
        _seed_week(in_memory_db)
        insert_manual_workout(
            in_memory_db,
            {
                "type": "Traditional Strength Training",
                "category": "lift",
                "duration_min": 45.0,
                "counts_as_lift": 1,
            },
            "2026-09-02",
        )
        rings = measure_week(
            in_memory_db,
            [_target("sessions_week", 2, category="lift")],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        assert rings[0].actual == 2

    def test_a_walker_is_measured_in_walk_kilometres(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """The strip has to work for someone who never runs."""
        _seed_other_sports(in_memory_db)
        rings = measure_week(
            in_memory_db,
            [_target("distance_km_week", 40, category="walk")],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        # A 9.0 km walk plus an 11.5 km hike: hiking is recorded as walking.
        assert rings[0].actual == pytest.approx(20.5)
        assert ring_label(rings[0].target) == "Walk km"

    def test_a_cyclist_is_measured_in_ride_kilometres(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed_other_sports(in_memory_db)
        rings = measure_week(
            in_memory_db,
            [_target("distance_km_week", 150, category="cycle")],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        assert rings[0].actual == pytest.approx(42.0)
        assert ring_label(rings[0].target) == "Ride km"

    def test_distance_does_not_mix_categories(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A cycling week must not inflate a running bar."""
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(
                    date="2026-09-01",
                    workouts=[
                        WorkoutSnapshot(
                            type="Outdoor Run",
                            category="run",
                            start_utc="2026-09-01T07:00:00Z",
                            duration_min=40.0,
                            gpx_distance_km=7.0,
                        ),
                        WorkoutSnapshot(
                            type="Outdoor Cycling",
                            category="cycle",
                            start_utc="2026-09-01T17:00:00Z",
                            duration_min=95.0,
                            gpx_distance_km=42.0,
                        ),
                    ],
                )
            ],
        )
        rings = measure_week(
            in_memory_db,
            [
                _target("distance_km_week", 30, category="run"),
                _target("distance_km_week", 150, category="cycle"),
            ],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        assert [ring.actual for ring in rings] == [
            pytest.approx(7.0),
            pytest.approx(42.0),
        ]

    def test_any_sessions_count_every_activity(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Swimming lands in the schema's "other" bucket and still counts."""
        _seed_other_sports(in_memory_db)
        rings = measure_week(
            in_memory_db,
            [_target("sessions_week", 4, category="any")],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        assert rings[0].actual == 7
        assert ring_label(rings[0].target) == "Sessions"

    def test_walk_sessions_are_counted_separately(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed_other_sports(in_memory_db)
        rings = measure_week(
            in_memory_db,
            [_target("sessions_week", 3, category="walk")],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        # The walk and the hike; the swim and the ride are not walks.
        assert rings[0].actual == 2

    def test_a_named_activity_type_is_counted_on_its_own(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Paddling has no category, so a paddler's goal names the type."""
        _seed_other_sports(in_memory_db)
        rings = measure_week(
            in_memory_db,
            [_target("sessions_week", 3, category="type:Paddle Sports")],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        assert rings[0].actual == 2
        assert ring_label(rings[0].target) == "Paddle Sports"

    def test_a_named_type_excludes_every_other_activity(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """`any` would count this profile's runs as paddling; a type must not."""
        _seed_week(in_memory_db)
        _seed_other_sports(in_memory_db)
        rings = measure_week(
            in_memory_db,
            [_target("sessions_week", 3, category="type:Paddle Sports")],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        assert rings[0].actual == 2

    def test_hiit_is_counted_as_its_own_category(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed_other_sports(in_memory_db)
        rings = measure_week(
            in_memory_db,
            [_target("sessions_week", 4, category="hiit")],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        assert rings[0].actual == 1
        assert ring_label(rings[0].target) == "HIIT"

    def test_later_days_are_not_counted_early(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed_week(in_memory_db)
        rings = measure_week(
            in_memory_db,
            [_target("distance_km_week", 30, category="run")],
            week_start=WEEK_START,
            today=date(2026, 8, 31),
        )
        assert rings[0].actual == pytest.approx(8.0)
        assert rings[0].days_elapsed == 1

    def test_days_elapsed_never_exceeds_the_week(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed_week(in_memory_db)
        rings = measure_week(
            in_memory_db,
            [_target("distance_km_week", 30, category="run")],
            week_start=WEEK_START,
            today=date(2026, 9, 20),
        )
        assert rings[0].days_elapsed == 7

    def test_an_empty_week_measures_zero(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        rings = measure_week(
            in_memory_db,
            [_target("distance_km_week", 30, category="run")],
            week_start=WEEK_START,
            today=WEDNESDAY,
        )
        assert rings[0].actual == 0.0
        assert rings[0].last_date is None


class TestEndToEnd:
    def test_no_goals_renders_no_strip(self, in_memory_db: sqlite3.Connection) -> None:
        assert (
            weekly_progress_block(
                in_memory_db, strategy_md="(not provided)", today=WEDNESDAY
            )
            is None
        )

    def test_stored_targets_render_without_calling_a_model(
        self, in_memory_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "weekly_targets.derive_targets",
            lambda *a, **k: pytest.fail("cached targets must not re-derive"),
        )
        _seed_week(in_memory_db)
        save_targets(
            in_memory_db,
            WEEK_START,
            [
                _target("distance_km_week", 30, category="run"),
                _target("sessions_week", 2, category="lift"),
            ],
        )
        strategy = "## Goals\n- Run 30 km\n"
        # Match the digest the cache checks so the stored rows are reused.
        in_memory_db.execute(
            "UPDATE weekly_target SET strategy_hash = ?",
            (
                __import__("weekly_targets").goals_digest(
                    __import__("weekly_targets").extract_goal_text(strategy)
                ),
            ),
        )
        in_memory_db.commit()

        block = weekly_progress_block(
            in_memory_db, strategy_md=strategy, today=WEDNESDAY
        )
        assert block is not None
        assert "14.5/30" in block

        shown = weekly_progress_nudge_line(
            in_memory_db, strategy_md=strategy, today=WEDNESDAY
        )
        assert shown is not None
        assert shown[0].startswith("Run km 14.5/30")

    def test_a_measurement_failure_is_not_fatal(
        self, in_memory_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "weekly_progress.measure_week",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert (
            weekly_progress_block(
                in_memory_db, strategy_md="(not provided)", today=WEDNESDAY
            )
            is None
        )


class TestNudgeLineIsGatedOnChange:
    """Nudges fire twice a day; the bars move three or four times a week."""

    def _line(
        self, conn: sqlite3.Connection, today: date, *, delivered: bool = True
    ) -> str | None:
        """Compose the line and, unless the send failed, mark it delivered."""
        shown = weekly_progress_nudge_line(conn, strategy_md=self.strategy, today=today)
        if shown is None:
            return None
        line, fingerprint = shown
        if delivered:
            record_progress_line_shown(conn, fingerprint, line)
        return line

    @pytest.fixture(autouse=True)
    def _stored_targets(
        self, in_memory_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.strategy = "## Goals\n- Run 30 km\n"
        monkeypatch.setattr(
            "weekly_targets.derive_targets",
            lambda *a, **k: pytest.fail("cached targets must not re-derive"),
        )
        save_targets(
            in_memory_db, WEEK_START, [_target("distance_km_week", 30, category="run")]
        )
        digest = goals_digest(extract_goal_text(self.strategy))
        in_memory_db.execute("UPDATE weekly_target SET strategy_hash = ?", (digest,))
        in_memory_db.commit()

    def test_the_first_nudge_shows_the_line(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed_week(in_memory_db)
        assert self._line(in_memory_db, WEDNESDAY) is not None

    def test_an_unchanged_week_says_nothing_twice(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed_week(in_memory_db)
        assert self._line(in_memory_db, WEDNESDAY) is not None
        assert self._line(in_memory_db, WEDNESDAY) is None

    def test_a_new_session_brings_the_line_back(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed_week(in_memory_db)
        assert self._line(in_memory_db, WEDNESDAY) is not None
        assert self._line(in_memory_db, WEDNESDAY) is None
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(
                    date="2026-09-03",
                    workouts=[
                        WorkoutSnapshot(
                            type="Outdoor Run",
                            category="run",
                            start_utc="2026-09-03T07:00:00Z",
                            duration_min=50.0,
                            gpx_distance_km=9.0,
                        )
                    ],
                )
            ],
        )
        line = self._line(in_memory_db, date(2026, 9, 3))
        assert line is not None
        assert "23.5/30" in line

    def test_movement_too_small_to_see_stays_quiet(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A 200 m stroll changes the number without changing the bar."""
        _seed_week(in_memory_db)
        assert self._line(in_memory_db, WEDNESDAY) is not None
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(
                    date="2026-09-02",
                    steps=12500,
                    exercise_min=35,
                    sleep_total_h=8.0,
                    workouts=[
                        WorkoutSnapshot(
                            type="Outdoor Run",
                            category="run",
                            start_utc="2026-09-02T07:00:00Z",
                            duration_min=35.0,
                            gpx_distance_km=6.5,
                        ),
                        WorkoutSnapshot(
                            type="Outdoor Run",
                            category="run",
                            start_utc="2026-09-02T20:00:00Z",
                            duration_min=2.0,
                            gpx_distance_km=0.2,
                        ),
                    ],
                )
            ],
        )
        assert self._line(in_memory_db, WEDNESDAY) is None

    def test_time_passing_alone_does_not_repeat_progress(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A rest day does not create a new claim about the same progress."""
        _seed_week(in_memory_db)
        assert self._line(in_memory_db, WEDNESDAY) is not None
        line = self._line(in_memory_db, SUNDAY)
        assert line is None

    def test_a_new_week_is_always_news(self, in_memory_db: sqlite3.Connection) -> None:
        _seed_week(in_memory_db)
        assert self._line(in_memory_db, WEDNESDAY) is not None
        save_targets(
            in_memory_db,
            "2026-09-07",
            [_target("distance_km_week", 30, category="run")],
        )
        digest = goals_digest(extract_goal_text(self.strategy))
        in_memory_db.execute("UPDATE weekly_target SET strategy_hash = ?", (digest,))
        in_memory_db.commit()
        assert self._line(in_memory_db, date(2026, 9, 9)) is not None

    def test_a_failed_send_does_not_suppress_the_next_nudge(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A line nobody saw must not count as one they have already been told."""
        _seed_week(in_memory_db)
        assert self._line(in_memory_db, WEDNESDAY, delivered=False) is not None
        assert self._line(in_memory_db, WEDNESDAY) is not None

    def test_no_goals_never_records_anything(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        assert (
            weekly_progress_nudge_line(
                in_memory_db, strategy_md="(not provided)", today=WEDNESDAY
            )
            is None
        )
        assert (
            in_memory_db.execute("SELECT COUNT(*) FROM progress_line_shown").fetchone()[
                0
            ]
            == 0
        )

    def test_the_report_block_is_not_gated(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A weekly report always shows the week, changed or not."""
        _seed_week(in_memory_db)
        assert self._line(in_memory_db, WEDNESDAY) is not None
        block = weekly_progress_block(
            in_memory_db, strategy_md=self.strategy, today=WEDNESDAY
        )
        assert block is not None


class TestWeekLabel:
    def test_label_matches_the_iso_week(self) -> None:
        assert week_label_for(WEEK_START) == "2026-W36"

    def test_sunday_of_the_same_week(self) -> None:
        assert week_label_for("2026-09-07") == "2026-W37"


class TestNeutralProgress:
    def test_sunday_before_a_planned_run_has_no_pace_judgment(self) -> None:
        ring = _ring("distance_km_week", 30, 20, category="run", day=7)
        line = render_progress_line(ring)
        assert "10 left" in line
        assert "behind" not in line
        completed = render_progress_block(
            [ring], week_start=WEEK_START, week_complete=True
        )
        assert "10 short" in completed
