"""Tests for weekly target extraction, validation, and caching."""

from __future__ import annotations

import sqlite3

import pytest

from llm_context import UNFILLED_CONTEXT
from weekly_targets import (
    SPEC_BY_KEY,
    StoredTarget,
    clear_targets,
    ensure_weekly_targets,
    extract_goal_text,
    goals_digest,
    load_targets,
    known_activity_types,
    parse_targets_response,
    recorded_activity_types,
    save_targets,
    week_bounds_for,
    week_start_for,
)

STRATEGY = """# Strategy

## Goals — Current focus (next 4-8 weeks)
- Build weekly volume to 30 km without Achilles flare-up
- Maintain 2x/week strength for injury prevention

## Weekly Plan

- Tue: Easy run 5-7 km
- Sat: Long run 10-14 km

## Diet

~2200 kcal, high protein. Do not turn this into a target.
"""


def _target(
    key: str,
    value: float,
    threshold: float | None = None,
    category: str = "",
) -> StoredTarget:
    """Build a StoredTarget for storage and rendering tests."""
    return StoredTarget(
        spec=SPEC_BY_KEY[key],
        category=category,
        target=value,
        threshold=threshold,
        goal_text=f"goal for {key}",
        strategy_hash="hash-a",
        llm_call_id=7,
    )


def _seed_mixed_workouts(conn: sqlite3.Connection) -> None:
    """Seed a profile that runs, paddles, and plays basketball."""
    from models import DailySnapshot, WorkoutSnapshot
    from store import store_snapshots

    def workout(name: str, category: str, hour: int) -> WorkoutSnapshot:
        return WorkoutSnapshot(
            type=name,
            category=category,
            start_utc=f"2026-08-31T{hour:02d}:00:00Z",
            duration_min=45.0,
        )

    store_snapshots(
        conn,
        [
            DailySnapshot(
                date="2026-08-31",
                workouts=[
                    workout("Outdoor Run", "run", 7),
                    workout("Paddle Sports", "other", 9),
                    workout("Paddle Sports", "other", 11),
                    workout("Basketball", "other", 18),
                ],
            )
        ],
    )


class TestWeekBounds:
    def test_week_start_is_monday(self) -> None:
        from datetime import date

        # 2026-09-05 is a Saturday.
        assert week_start_for(date(2026, 9, 5)) == "2026-08-31"

    def test_monday_is_its_own_week_start(self) -> None:
        from datetime import date

        assert week_start_for(date(2026, 8, 31)) == "2026-08-31"

    def test_bounds_span_seven_days(self) -> None:
        assert week_bounds_for("2026-08-31") == ("2026-08-31", "2026-09-06")


class TestExtractGoalText:
    def test_keeps_goal_and_plan_sections(self) -> None:
        text = extract_goal_text(STRATEGY)
        assert "Build weekly volume to 30 km" in text
        assert "Tue: Easy run 5-7 km" in text

    def test_drops_non_goal_sections(self) -> None:
        text = extract_goal_text(STRATEGY)
        assert "2200 kcal" not in text
        assert "Diet" not in text

    def test_missing_file_marker_is_empty(self) -> None:
        assert extract_goal_text("(not provided)") == ""

    def test_unfilled_template_marker_is_empty(self) -> None:
        assert extract_goal_text(UNFILLED_CONTEXT) == ""

    def test_none_is_empty(self) -> None:
        assert extract_goal_text(None) == ""

    def test_strategy_without_goal_sections_is_empty(self) -> None:
        assert extract_goal_text("# Strategy\n\n## Diet\n\nEat well.\n") == ""


class TestGoalsDigest:
    def test_empty_text_has_no_digest(self) -> None:
        assert goals_digest("") == ""

    def test_same_text_same_digest(self) -> None:
        assert goals_digest("run 30 km") == goals_digest("run 30 km")

    def test_changed_text_changes_digest(self) -> None:
        assert goals_digest("run 30 km") != goals_digest("run 35 km")


class TestParseTargetsResponse:
    def test_parses_a_valid_payload(self) -> None:
        raw = """{"targets": [
            {"metric": "distance_km_week", "category": "run", "target": 30,
             "threshold": null, "goal": "Build weekly volume to 30 km"}
        ]}"""
        targets = parse_targets_response(raw, "hash-a")
        assert len(targets) == 1
        assert targets[0].spec.key == "distance_km_week"
        assert targets[0].category == "run"
        assert targets[0].target == 30
        assert targets[0].threshold is None
        assert targets[0].strategy_hash == "hash-a"

    def test_parses_a_fenced_payload(self) -> None:
        raw = (
            '```json\n{"targets": [{"metric": "sessions_week",'
            ' "category": "lift", "target": 2}]}\n```'
        )
        assert [item.slot for item in parse_targets_response(raw, "h")] == [
            ("sessions_week", "lift")
        ]

    def test_a_walker_gets_a_walking_target(self) -> None:
        raw = (
            '{"targets": [{"metric": "distance_km_week", "category": "walk",'
            ' "target": 40}]}'
        )
        targets = parse_targets_response(raw, "h")
        assert targets[0].slot == ("distance_km_week", "walk")
        assert targets[0].label == "Walk km"

    def test_a_cyclist_gets_a_cycling_target(self) -> None:
        raw = (
            '{"targets": [{"metric": "distance_km_week", "category": "cycle",'
            ' "target": 220}]}'
        )
        targets = parse_targets_response(raw, "h")
        assert targets[0].slot == ("distance_km_week", "cycle")
        assert targets[0].label == "Ride km"

    def test_a_sport_without_a_category_uses_any_sessions(self) -> None:
        """Swimming and rowing land in the schema's "other" bucket."""
        raw = (
            '{"targets": [{"metric": "sessions_week", "category": "any", "target": 4}]}'
        )
        targets = parse_targets_response(raw, "h")
        assert targets[0].label == "Sessions"

    def test_category_is_required_where_the_metric_takes_one(self) -> None:
        assert (
            parse_targets_response(
                '{"targets": [{"metric": "distance_km_week", "target": 30}]}', "h"
            )
            == []
        )

    def test_unknown_category_is_dropped(self) -> None:
        raw = (
            '{"targets": [{"metric": "distance_km_week", "category": "swim",'
            ' "target": 5}]}'
        )
        assert parse_targets_response(raw, "h") == []

    def test_distance_has_no_category_for_strength(self) -> None:
        raw = (
            '{"targets": [{"metric": "distance_km_week", "category": "lift",'
            ' "target": 5}]}'
        )
        assert parse_targets_response(raw, "h") == []

    def test_range_is_tightened_per_category(self) -> None:
        """220 km is a plausible cycling week and an impossible running one."""
        cycling = (
            '{"targets": [{"metric": "distance_km_week", "category": "cycle",'
            ' "target": 220}]}'
        )
        running = (
            '{"targets": [{"metric": "distance_km_week", "category": "run",'
            ' "target": 320}]}'
        )
        assert len(parse_targets_response(cycling, "h")) == 1
        assert parse_targets_response(running, "h") == []

    def test_a_named_activity_type_is_accepted_when_recorded(self) -> None:
        """Paddling has no category, so the goal names the type Apple wrote."""
        raw = (
            '{"targets": [{"metric": "sessions_week",'
            ' "category": "type:Paddle Sports", "target": 3}]}'
        )
        targets = parse_targets_response(
            raw, "h", frozenset({"Paddle Sports", "Outdoor Run"})
        )
        assert targets[0].slot == ("sessions_week", "type:Paddle Sports")
        assert targets[0].label == "Paddle Sports"

    def test_an_invented_activity_type_is_dropped(self) -> None:
        """A type never recorded would be a bar stuck at zero all week."""
        raw = (
            '{"targets": [{"metric": "sessions_week",'
            ' "category": "type:Kitesurfing", "target": 2}]}'
        )
        assert parse_targets_response(raw, "h", frozenset({"Paddle Sports"})) == []

    def test_an_activity_type_takes_the_recorded_spelling(self) -> None:
        """Measurement is an exact match, so casing cannot be left to the model."""
        raw = (
            '{"targets": [{"metric": "sessions_week",'
            ' "category": "type:paddle sports", "target": 3}]}'
        )
        targets = parse_targets_response(raw, "h", frozenset({"Paddle Sports"}))
        assert targets[0].category == "type:Paddle Sports"

    def test_a_type_is_rejected_where_the_metric_forbids_one(self) -> None:
        raw = (
            '{"targets": [{"metric": "distance_km_week",'
            ' "category": "type:Paddle Sports", "target": 20}]}'
        )
        assert parse_targets_response(raw, "h", frozenset({"Paddle Sports"})) == []

    def test_hiit_is_a_category_of_its_own(self) -> None:
        raw = (
            '{"targets": [{"metric": "sessions_week", "category": "hiit",'
            ' "target": 4}]}'
        )
        targets = parse_targets_response(raw, "h")
        assert targets[0].slot == ("sessions_week", "hiit")
        assert targets[0].label == "HIIT"

    def test_threshold_metric_keeps_its_threshold(self) -> None:
        raw = '{"targets": [{"metric": "sleep_nights_week", "target": 5, "threshold": 7}]}'
        targets = parse_targets_response(raw, "h")
        assert targets[0].threshold == 7

    def test_threshold_metric_without_threshold_is_dropped(self) -> None:
        raw = '{"targets": [{"metric": "sleep_nights_week", "target": 5}]}'
        assert parse_targets_response(raw, "h") == []

    def test_unknown_metric_is_dropped(self) -> None:
        raw = '{"targets": [{"metric": "vo2max_week", "target": 50}]}'
        assert parse_targets_response(raw, "h") == []

    def test_target_above_range_is_dropped(self) -> None:
        raw = '{"targets": [{"metric": "exercise_min_week", "target": 9000}]}'
        assert parse_targets_response(raw, "h") == []

    def test_target_below_range_is_dropped(self) -> None:
        raw = (
            '{"targets": [{"metric": "distance_km_week", "category": "run",'
            ' "target": 0}]}'
        )
        assert parse_targets_response(raw, "h") == []

    def test_threshold_outside_range_is_dropped(self) -> None:
        raw = (
            '{"targets": [{"metric": "step_days_week", "target": 7,'
            ' "threshold": 900000}]}'
        )
        assert parse_targets_response(raw, "h") == []

    def test_non_numeric_target_is_dropped(self) -> None:
        raw = '{"targets": [{"metric": "exercise_min_week", "target": "lots"}]}'
        assert parse_targets_response(raw, "h") == []

    def test_duplicate_slot_keeps_the_first(self) -> None:
        raw = """{"targets": [
            {"metric": "distance_km_week", "category": "run", "target": 30},
            {"metric": "distance_km_week", "category": "run", "target": 45}
        ]}"""
        targets = parse_targets_response(raw, "h")
        assert [item.target for item in targets] == [30]

    def test_same_metric_in_two_categories_is_not_a_duplicate(self) -> None:
        """Someone who runs and cycles needs both bars, not one of them."""
        raw = """{"targets": [
            {"metric": "distance_km_week", "category": "run", "target": 30},
            {"metric": "distance_km_week", "category": "cycle", "target": 150}
        ]}"""
        targets = parse_targets_response(raw, "h")
        assert [item.slot for item in targets] == [
            ("distance_km_week", "run"),
            ("distance_km_week", "cycle"),
        ]

    def test_output_is_capped_and_ordered_by_vocabulary(self) -> None:
        raw = """{"targets": [
            {"metric": "step_days_week", "target": 7, "threshold": 10000},
            {"metric": "sleep_nights_week", "target": 5, "threshold": 7},
            {"metric": "sessions_week", "category": "lift", "target": 2},
            {"metric": "distance_km_week", "category": "run", "target": 30}
        ]}"""
        targets = parse_targets_response(raw, "h")
        assert [item.slot for item in targets] == [
            ("distance_km_week", "run"),
            ("sessions_week", "lift"),
            ("sleep_nights_week", ""),
        ]

    def test_empty_array_is_accepted(self) -> None:
        assert parse_targets_response('{"targets": []}', "h") == []

    def test_unparseable_output_is_empty(self) -> None:
        assert parse_targets_response("I could not find any goals.", "h") == []

    def test_json_array_instead_of_object_is_empty(self) -> None:
        assert parse_targets_response('[{"metric": "distance_km_week"}]', "h") == []


class TestActivityTypeDiscovery:
    def test_only_uncategorised_types_are_offered(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Running has a category; offering "Outdoor Run" would split the goal."""
        _seed_mixed_workouts(in_memory_db)
        offered = [name for name, _ in recorded_activity_types(in_memory_db)]
        assert "Paddle Sports" in offered
        assert "Outdoor Run" not in offered

    def test_types_are_ordered_by_how_often_they_happen(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed_mixed_workouts(in_memory_db)
        assert [name for name, _ in recorded_activity_types(in_memory_db)] == [
            "Paddle Sports",
            "Basketball",
        ]

    def test_the_menu_is_bounded(self, in_memory_db: sqlite3.Connection) -> None:
        _seed_mixed_workouts(in_memory_db)
        assert len(recorded_activity_types(in_memory_db, limit=1)) == 1

    def test_known_types_span_every_category(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _seed_mixed_workouts(in_memory_db)
        known = known_activity_types(in_memory_db)
        assert {"Paddle Sports", "Basketball", "Outdoor Run"} <= known

    def test_an_empty_database_offers_nothing(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        assert recorded_activity_types(in_memory_db) == []
        assert known_activity_types(in_memory_db) == frozenset()


class TestTargetStorage:
    def test_round_trip_preserves_fields(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        save_targets(
            in_memory_db,
            "2026-08-31",
            [_target("sleep_nights_week", 5, 7.0)],
        )
        loaded = load_targets(in_memory_db, "2026-08-31")
        assert len(loaded) == 1
        assert loaded[0].spec.key == "sleep_nights_week"
        assert loaded[0].target == 5
        assert loaded[0].threshold == 7.0
        assert loaded[0].goal_text == "goal for sleep_nights_week"
        assert loaded[0].llm_call_id == 7

    def test_load_preserves_saved_order(self, in_memory_db: sqlite3.Connection) -> None:
        save_targets(
            in_memory_db,
            "2026-08-31",
            [
                _target("sessions_week", 2, category="lift"),
                _target("distance_km_week", 30, category="run"),
            ],
        )
        loaded = load_targets(in_memory_db, "2026-08-31")
        assert [item.slot for item in loaded] == [
            ("sessions_week", "lift"),
            ("distance_km_week", "run"),
        ]

    def test_save_replaces_the_week(self, in_memory_db: sqlite3.Connection) -> None:
        save_targets(
            in_memory_db,
            "2026-08-31",
            [_target("distance_km_week", 30, category="run")],
        )
        save_targets(
            in_memory_db,
            "2026-08-31",
            [_target("distance_km_week", 35, category="run")],
        )
        loaded = load_targets(in_memory_db, "2026-08-31")
        assert [item.target for item in loaded] == [35]

    def test_weeks_do_not_leak_into_each_other(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        save_targets(
            in_memory_db,
            "2026-08-31",
            [_target("distance_km_week", 30, category="run")],
        )
        assert load_targets(in_memory_db, "2026-09-07") == []

    def test_clear_removes_only_that_week(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        save_targets(
            in_memory_db,
            "2026-08-31",
            [_target("distance_km_week", 30, category="run")],
        )
        save_targets(
            in_memory_db,
            "2026-09-07",
            [_target("distance_km_week", 32, category="run")],
        )
        assert clear_targets(in_memory_db, "2026-08-31") == 1
        assert load_targets(in_memory_db, "2026-08-31") == []
        assert len(load_targets(in_memory_db, "2026-09-07")) == 1

    def test_a_named_activity_type_survives_the_round_trip(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Parse and storage were tested separately; the gap between them was a
        bug that dropped every named-sport target on the way back out."""
        save_targets(
            in_memory_db,
            "2026-08-31",
            [_target("sessions_week", 3, category="type:Paddle Sports")],
        )
        loaded = load_targets(in_memory_db, "2026-08-31")
        assert [item.slot for item in loaded] == [
            ("sessions_week", "type:Paddle Sports")
        ]
        assert loaded[0].label == "Paddle Sports"

    def test_a_type_survives_beside_a_plain_category(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        save_targets(
            in_memory_db,
            "2026-08-31",
            [
                _target("sessions_week", 3, category="type:Basketball"),
                _target("sessions_week", 2, category="lift"),
            ],
        )
        assert len(load_targets(in_memory_db, "2026-08-31")) == 2

    def test_a_type_on_a_metric_that_forbids_one_is_skipped(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Distance takes no workout type, so a row claiming one cannot be drawn."""
        in_memory_db.execute(
            "INSERT INTO weekly_target (week_start, metric, category, target, "
            "source, position, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-08-31",
                "distance_km_week",
                "type:Paddle Sports",
                20,
                "strategy",
                0,
                "2026-08-31T00:00:00Z",
            ),
        )
        in_memory_db.commit()
        assert load_targets(in_memory_db, "2026-08-31") == []

    def test_retired_metric_key_is_skipped(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A key this build no longer knows how to measure must not be drawn."""
        in_memory_db.execute(
            "INSERT INTO weekly_target (week_start, metric, target, source, "
            "position, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-08-31", "retired_metric", 3, "strategy", 0, "2026-08-31T00:00:00Z"),
        )
        in_memory_db.commit()
        assert load_targets(in_memory_db, "2026-08-31") == []


class TestEnsureWeeklyTargets:
    @pytest.fixture(autouse=True)
    def _no_real_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail loudly if a test reaches the network instead of the cache."""
        self.calls: list[str] = []

        def fake_derive(goal_text: str, **kwargs: object) -> list[StoredTarget]:
            self.calls.append(goal_text)
            return [
                StoredTarget(
                    spec=SPEC_BY_KEY["distance_km_week"],
                    category="run",
                    target=30,
                    threshold=None,
                    goal_text="Build weekly volume to 30 km",
                    strategy_hash=goals_digest(goal_text),
                    llm_call_id=11,
                )
            ]

        monkeypatch.setattr("weekly_targets.derive_targets", fake_derive)

    def test_derives_once_and_then_reads_the_cache(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        first = ensure_weekly_targets(
            in_memory_db, strategy_md=STRATEGY, week_start="2026-08-31"
        )
        second = ensure_weekly_targets(
            in_memory_db, strategy_md=STRATEGY, week_start="2026-08-31"
        )
        assert len(self.calls) == 1
        assert [item.slot for item in first] == [("distance_km_week", "run")]
        assert [item.target for item in second] == [30]

    def test_edited_goals_trigger_a_rederive(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        ensure_weekly_targets(
            in_memory_db, strategy_md=STRATEGY, week_start="2026-08-31"
        )
        edited = STRATEGY.replace("30 km", "35 km")
        ensure_weekly_targets(in_memory_db, strategy_md=edited, week_start="2026-08-31")
        assert len(self.calls) == 2

    def test_a_new_week_derives_again(self, in_memory_db: sqlite3.Connection) -> None:
        ensure_weekly_targets(
            in_memory_db, strategy_md=STRATEGY, week_start="2026-08-31"
        )
        ensure_weekly_targets(
            in_memory_db, strategy_md=STRATEGY, week_start="2026-09-07"
        )
        assert len(self.calls) == 2

    def test_force_rederives_within_the_same_week(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        ensure_weekly_targets(
            in_memory_db, strategy_md=STRATEGY, week_start="2026-08-31"
        )
        ensure_weekly_targets(
            in_memory_db, strategy_md=STRATEGY, week_start="2026-08-31", force=True
        )
        assert len(self.calls) == 2

    def test_no_goals_never_calls_the_model(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        assert (
            ensure_weekly_targets(
                in_memory_db, strategy_md=UNFILLED_CONTEXT, week_start="2026-08-31"
            )
            == []
        )
        assert self.calls == []

    def test_removing_the_goals_clears_stored_targets(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        ensure_weekly_targets(
            in_memory_db, strategy_md=STRATEGY, week_start="2026-08-31"
        )
        assert (
            ensure_weekly_targets(
                in_memory_db, strategy_md="(not provided)", week_start="2026-08-31"
            )
            == []
        )
        assert load_targets(in_memory_db, "2026-08-31") == []

    def test_a_failed_derivation_keeps_the_existing_targets(
        self, in_memory_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ensure_weekly_targets(
            in_memory_db, strategy_md=STRATEGY, week_start="2026-08-31"
        )
        monkeypatch.setattr("weekly_targets.derive_targets", lambda *a, **k: [])
        kept = ensure_weekly_targets(
            in_memory_db,
            strategy_md=STRATEGY.replace("30 km", "35 km"),
            week_start="2026-08-31",
        )
        assert [item.target for item in kept] == [30]
