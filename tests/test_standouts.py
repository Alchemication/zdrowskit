"""Tests for standout detection, gating, phrasing and the announcement ledger."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from config import (
    STANDOUT_COOLDOWN_DAYS,
    TELEGRAM_MESSAGE_EFFECTS,
    STANDOUT_MIN_POPULATION,
    STANDOUT_RECENCY_DAYS,
    STANDOUT_VOLUME_STEP_KM,
)
from standouts import (
    Standout,
    _is_recent,
    _margin_pct,
    _scope_phrase,
    _span_days,
    announced_keys,
    cooldown_remaining_days,
    effect_for,
    find_candidates,
    last_announced_at,
    parse_standout_response,
    record_standout_announced,
)

TODAY = date(2026, 9, 9)


def _add_workout(
    conn: sqlite3.Connection,
    *,
    day: date,
    category: str,
    distance_km: float | None = None,
    duration_min: float = 40.0,
    splits: list[float] | None = None,
) -> str:
    """Insert one workout, its parent daily row, and any splits it carries."""
    iso = day.isoformat()
    start_utc = f"{iso}T09:00:00Z#{category}#{duration_min}#{distance_km}"
    conn.execute(
        "INSERT OR IGNORE INTO daily (date, imported_at) VALUES (?, ?)",
        (iso, "2026-09-09T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO workout (start_utc, date, type, category, duration_min, "
        "gpx_distance_km, imported_at, counts_as_lift) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (
            start_utc,
            iso,
            category.title(),
            category,
            duration_min,
            distance_km,
            "2026-09-09T00:00:00Z",
        ),
    )
    for index, pace in enumerate(splits or [], start=1):
        conn.execute(
            "INSERT INTO workout_split (start_utc, km_index, pace_min_km) "
            "VALUES (?, ?, ?)",
            (start_utc, index, pace),
        )
    conn.commit()
    return start_utc


def _fill_runs(
    conn: sqlite3.Connection,
    *,
    count: int,
    pace: float,
    distance_km: float = 6.0,
    first_day: date = date(2024, 1, 1),
) -> None:
    """Seed a comparison population of unremarkable, identical runs."""
    for offset in range(count):
        _add_workout(
            conn,
            day=first_day + timedelta(days=offset * 3),
            category="run",
            distance_km=distance_km,
            splits=[pace] * 6,
        )


class TestScopePhrase:
    """The phrasing must never claim a period the recorded span cannot support."""

    def test_multi_year_span_names_years(self) -> None:
        assert _scope_phrase(400, 900, "runs") == "in 2 years of tracking"

    def test_single_year_span_names_a_year(self) -> None:
        assert _scope_phrase(400, 400, "runs") == "in a year of tracking"

    def test_short_span_counts_instead_of_naming_a_period(self) -> None:
        # A large sample over a short history may not borrow a period it does
        # not have; this is the wording half of the cold-start guard.
        assert _scope_phrase(1200, 200, "runs") == "across 1,200 recorded runs"

    def test_boundary_below_a_year_still_counts(self) -> None:
        assert _scope_phrase(50, 364, "walks") == "across 50 recorded walks"


class TestMarginAndSpan:
    """Arithmetic helpers behind the gates."""

    def test_margin_is_symmetric_in_direction(self) -> None:
        # Pace improves downward and distance upward; both must read positive.
        assert _margin_pct(4.0, 5.0) == pytest.approx(20.0)
        assert _margin_pct(5.0, 4.0) == pytest.approx(25.0)

    def test_margin_of_zero_baseline_is_not_a_division_error(self) -> None:
        assert _margin_pct(5.0, 0.0) == 0.0

    def test_span_days_measures_first_to_last(self) -> None:
        assert _span_days(["2024-01-01", "2024-03-01", "2024-02-01"]) == 60

    def test_span_days_tolerates_empty_and_malformed(self) -> None:
        assert _span_days([]) == 0
        assert _span_days(["not-a-date"]) == 0

    def test_recency_window_excludes_older_and_future_dates(self) -> None:
        assert _is_recent(TODAY.isoformat(), TODAY)
        assert _is_recent(
            (TODAY - timedelta(days=STANDOUT_RECENCY_DAYS)).isoformat(), TODAY
        )
        assert not _is_recent(
            (TODAY - timedelta(days=STANDOUT_RECENCY_DAYS + 1)).isoformat(), TODAY
        )
        assert not _is_recent((TODAY + timedelta(days=1)).isoformat(), TODAY)


class TestParseStandoutResponse:
    """The picker selects from a closed list; it may not name anything else."""

    def test_valid_key_is_returned(self) -> None:
        text = '{"pick": "distance_run|2026-09-08|21.2", "reason": "big"}'
        assert (
            parse_standout_response(text, {"distance_run|2026-09-08|21.2"})
            == "distance_run|2026-09-08|21.2"
        )

    def test_fenced_json_is_accepted(self) -> None:
        text = '```json\n{"pick": "a", "reason": "x"}\n```'
        assert parse_standout_response(text, {"a"}) == "a"

    def test_null_pick_is_a_decline(self) -> None:
        assert (
            parse_standout_response('{"pick": null, "reason": "ordinary"}', {"a"})
            is None
        )

    def test_unoffered_key_is_a_decline_not_an_announcement(self) -> None:
        # A model naming a key it was never given would otherwise be inventing
        # an achievement, which is the failure selection exists to prevent.
        assert parse_standout_response('{"pick": "invented"}', {"a"}) is None

    def test_unparseable_output_is_a_decline(self) -> None:
        assert parse_standout_response("I think the run was great!", {"a"}) is None

    def test_non_object_json_is_a_decline(self) -> None:
        assert parse_standout_response("[1, 2, 3]", {"a"}) is None


class TestLedger:
    """Once-ever announcement plus the global cooldown, from one table."""

    def _standout(self, key: str = "distance_run|2026-09-08|21.2") -> Standout:
        return Standout(
            key=key,
            kind="distance_run",
            headline="Longest run in 2 years of tracking — **21.2 km**.",
            occurred_on="2026-09-08",
            population=400,
            span_days=900,
            margin_pct=10.0,
        )

    def test_empty_ledger_has_no_cooldown(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        assert last_announced_at(in_memory_db) is None
        assert cooldown_remaining_days(in_memory_db) == 0
        assert announced_keys(in_memory_db) == set()

    def test_recording_starts_the_cooldown(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        now = datetime(2026, 9, 9, tzinfo=timezone.utc)
        record_standout_announced(in_memory_db, self._standout(), now=now)
        assert announced_keys(in_memory_db) == {"distance_run|2026-09-08|21.2"}
        assert cooldown_remaining_days(in_memory_db, now=now) == STANDOUT_COOLDOWN_DAYS

    def test_cooldown_expires_exactly_once_the_window_passes(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        now = datetime(2026, 9, 9, tzinfo=timezone.utc)
        record_standout_announced(in_memory_db, self._standout(), now=now)
        just_before = now + timedelta(days=STANDOUT_COOLDOWN_DAYS - 1)
        after = now + timedelta(days=STANDOUT_COOLDOWN_DAYS)
        assert cooldown_remaining_days(in_memory_db, now=just_before) == 1
        assert cooldown_remaining_days(in_memory_db, now=after) == 0

    def test_reannouncing_the_same_key_does_not_duplicate_the_row(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        record_standout_announced(in_memory_db, self._standout())
        record_standout_announced(in_memory_db, self._standout())
        count = in_memory_db.execute(
            "SELECT COUNT(*) AS n FROM standout_announced"
        ).fetchone()["n"]
        assert count == 1

    def test_naive_timestamps_are_read_back_as_utc(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        in_memory_db.execute(
            "INSERT INTO standout_announced VALUES (?, ?, ?, ?, ?)",
            ("k", "kind", "line", "2026-09-08", "2026-09-09T00:00:00"),
        )
        in_memory_db.commit()
        stamped = last_announced_at(in_memory_db)
        assert stamped is not None and stamped.tzinfo is not None


class TestCandidateGates:
    """Nothing is claimed without enough evidence, recency and a real margin."""

    def test_a_short_history_produces_nothing(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        # The cold-start case: an all-time best that is really a statement
        # about how little has been recorded.
        _fill_runs(in_memory_db, count=5, pace=6.0)
        _add_workout(
            in_memory_db,
            day=TODAY,
            category="run",
            distance_km=42.0,
            splits=[3.5] * 12,
        )
        assert find_candidates(in_memory_db, today=TODAY) == []

    def test_a_clear_record_on_a_deep_history_is_found(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _fill_runs(in_memory_db, count=STANDOUT_MIN_POPULATION + 5, pace=6.0)
        _add_workout(
            in_memory_db,
            day=TODAY,
            category="run",
            distance_km=21.0,
            splits=[5.0] * 10,
        )
        kinds = {item.kind for item in find_candidates(in_memory_db, today=TODAY)}
        assert "distance_run" in kinds
        assert "pace_window_5" in kinds

    def test_the_same_record_set_long_ago_is_not_news(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _fill_runs(in_memory_db, count=STANDOUT_MIN_POPULATION + 5, pace=6.0)
        _add_workout(
            in_memory_db,
            day=TODAY - timedelta(days=STANDOUT_RECENCY_DAYS + 30),
            category="run",
            distance_km=21.0,
            splits=[5.0] * 10,
        )
        assert find_candidates(in_memory_db, today=TODAY) == []

    def test_a_hairline_record_is_not_worth_announcing(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _fill_runs(
            in_memory_db, count=STANDOUT_MIN_POPULATION + 5, pace=6.0, distance_km=20.0
        )
        # A hundredth of a kilometre further and a hair faster: real, and not
        # worth a month of the budget.
        _add_workout(
            in_memory_db,
            day=TODAY,
            category="run",
            distance_km=20.01,
            splits=[5.99] * 10,
        )
        assert find_candidates(in_memory_db, today=TODAY) == []

    def test_headline_reports_the_previous_best_it_beat(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _fill_runs(
            in_memory_db, count=STANDOUT_MIN_POPULATION + 5, pace=6.0, distance_km=10.0
        )
        _add_workout(in_memory_db, day=TODAY, category="run", distance_km=21.0)
        distance = next(
            item
            for item in find_candidates(in_memory_db, today=TODAY)
            if item.kind == "distance_run"
        )
        assert "21.0 km" in distance.headline
        assert "10.0 km" in distance.headline

    def test_lift_records_rank_on_duration_not_distance(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        for offset in range(STANDOUT_MIN_POPULATION + 5):
            _add_workout(
                in_memory_db,
                day=date(2024, 1, 1) + timedelta(days=offset * 3),
                category="lift",
                duration_min=45.0,
            )
        _add_workout(in_memory_db, day=TODAY, category="lift", duration_min=95.0)
        kinds = {item.kind for item in find_candidates(in_memory_db, today=TODAY)}
        assert "duration_lift" in kinds

    def test_a_volume_crossing_needs_a_threshold_between_before_and_after(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        step = STANDOUT_VOLUME_STEP_KM
        population = STANDOUT_MIN_POPULATION + 5
        # Land just under one threshold across the old sessions, then step over
        # it with today's.
        per_session = (step - 5) / population
        _fill_runs(in_memory_db, count=population, pace=6.0, distance_km=per_session)
        _add_workout(in_memory_db, day=TODAY, category="run", distance_km=20.0)
        volume = [
            item
            for item in find_candidates(in_memory_db, today=TODAY)
            if item.kind == "volume_run"
        ]
        assert len(volume) == 1
        assert f"{step:,} km" in volume[0].headline
        assert volume[0].margin_pct is None

    def test_no_crossing_when_the_threshold_was_already_passed(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        population = STANDOUT_MIN_POPULATION + 5
        per_session = (STANDOUT_VOLUME_STEP_KM + 40) / population
        _fill_runs(in_memory_db, count=population, pace=6.0, distance_km=per_session)
        _add_workout(in_memory_db, day=TODAY, category="run", distance_km=1.0)
        kinds = {item.kind for item in find_candidates(in_memory_db, today=TODAY)}
        assert "volume_run" not in kinds

    def test_detection_survives_a_missing_table(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        # A generator failing must cost only its own candidates. A standout is
        # an addition to a nudge, never a precondition for sending one.
        _fill_runs(in_memory_db, count=STANDOUT_MIN_POPULATION + 5, pace=6.0)
        _add_workout(in_memory_db, day=TODAY, category="run", distance_km=21.0)
        in_memory_db.execute("DROP TABLE workout_split")
        in_memory_db.commit()
        kinds = {item.kind for item in find_candidates(in_memory_db, today=TODAY)}
        assert "distance_run" in kinds


class TestEffectSelection:
    """The animation says "rare" before a word is read; it is not a taxonomy."""

    def _standout(self, kind: str) -> Standout:
        return Standout(
            key=f"{kind}|2026-09-08|1.0",
            kind=kind,
            headline="x",
            occurred_on="2026-09-08",
            population=400,
            span_days=900,
        )

    def test_records_take_the_default(self) -> None:
        for kind in ("pace_window_5", "distance_run", "duration_lift"):
            assert effect_for(self._standout(kind)) == TELEGRAM_MESSAGE_EFFECTS["fire"]

    def test_a_crossing_is_the_one_party_shaped_standout(self) -> None:
        assert (
            effect_for(self._standout("volume_run"))
            == TELEGRAM_MESSAGE_EFFECTS["confetti"]
        )

    def test_one_prefix_entry_covers_every_sport(self) -> None:
        for kind in ("volume_run", "volume_walk", "volume_cycle"):
            assert (
                effect_for(self._standout(kind))
                == (TELEGRAM_MESSAGE_EFFECTS["confetti"])
            )

    def test_an_unknown_effect_name_costs_the_animation_not_the_message(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr("standouts.STANDOUT_EFFECT_DEFAULT", "not-an-effect")
        assert effect_for(self._standout("distance_run")) is None
