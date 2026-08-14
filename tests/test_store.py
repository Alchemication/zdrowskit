"""Tests for src/store.py."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from db.migrations import apply_migrations, get_live_schema, list_migrations
from models import DailySnapshot, WorkoutSnapshot, WorkoutSplit
from store import (
    create_llm_trace,
    connect_db,
    delete_feedback,
    load_date_range,
    load_feedback_entries,
    latest_metric_date,
    load_feedback_for_call,
    load_snapshots,
    log_feedback,
    log_llm_call,
    open_db,
    insert_manual_sleep,
    store_snapshots,
    update_feedback_reason,
    update_llm_call_response,
)


class TestOpenDb:
    def test_creates_tables(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "test.db")
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "daily" in tables
        assert "workout" in tables
        assert "llm_call" in tables
        assert "schema_migrations" in tables
        conn.close()


class TestStoreAndLoad:
    def test_round_trip(self, in_memory_db: sqlite3.Connection) -> None:
        """Store snapshots then load them back — all fields should survive."""
        workout = WorkoutSnapshot(
            type="Outdoor Run",
            category="run",
            start_utc="2026-03-10T07:00:00Z",
            duration_min=35.0,
            hr_min=120,
            hr_avg=155.0,
            hr_max=178,
            active_energy_kj=900.0,
            temperature_c=8.0,
            humidity_pct=65,
            gpx_distance_km=5.2,
            gpx_elevation_gain_m=45.0,
            gpx_avg_speed_ms=2.5,
            gpx_max_speed_p95_ms=3.8,
            splits=[
                WorkoutSplit(
                    km_index=1,
                    pace_min_km=6.0,
                    avg_speed_ms=2.7778,
                    elevation_gain_m=8.0,
                    elevation_loss_m=1.0,
                ),
                WorkoutSplit(
                    km_index=2,
                    pace_min_km=5.8,
                    avg_speed_ms=2.8736,
                    elevation_gain_m=10.0,
                    elevation_loss_m=2.0,
                ),
            ],
        )
        original = DailySnapshot(
            date="2026-03-10",
            steps=12000,
            distance_km=9.8,
            active_energy_kj=2200.0,
            exercise_min=55,
            stand_hours=12,
            resting_hr=54,
            hrv_ms=55.0,
            vo2max=45.2,
            sleep_total_h=7.4,
            sleep_in_bed_h=7.6,
            sleep_efficiency_pct=97.4,
            sleep_deep_h=0.73,
            sleep_core_h=4.69,
            sleep_rem_h=2.01,
            sleep_awake_h=0.17,
            recovery_index=55.0 / 54,
            workouts=[workout],
        )
        store_snapshots(in_memory_db, [original])
        loaded = load_snapshots(in_memory_db)

        assert len(loaded) == 1
        day = loaded[0]
        assert day.date == "2026-03-10"
        assert day.steps == 12000
        assert day.resting_hr == 54
        assert day.hrv_ms == 55.0
        assert day.vo2max == 45.2
        assert day.sleep_total_h == 7.4
        assert day.sleep_in_bed_h == 7.6
        assert day.sleep_efficiency_pct == 97.4
        assert day.sleep_deep_h == 0.73
        assert day.sleep_core_h == 4.69
        assert day.sleep_rem_h == 2.01
        assert day.sleep_awake_h == 0.17
        assert len(day.workouts) == 1

        w = day.workouts[0]
        assert w.type == "Outdoor Run"
        assert w.category == "run"
        assert w.counts_as_lift is False
        assert w.duration_min == 35.0
        assert w.hr_avg == 155.0
        assert w.gpx_distance_km == 5.2
        assert w.temperature_c == 8.0
        assert w.humidity_pct == 65
        assert len(w.splits) == 2
        assert w.splits[0].km_index == 1
        assert w.splits[0].pace_min_km == 6.0
        assert w.splits[1].elevation_gain_m == 10.0

    def test_workout_location_round_trip(
        self,
        in_memory_db: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Workout locality should load via the workout -> location link."""
        now = "2026-05-17T12:00:00+00:00"
        cursor = in_memory_db.execute(
            """
            INSERT INTO location (
                label, locality, region, country, country_code, source,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Crosshaven",
                "Crosshaven",
                "County Cork",
                "Ireland",
                "ie",
                "test",
                now,
                now,
            ),
        )
        location_id = int(cursor.lastrowid)
        monkeypatch.setattr(
            "store.resolve_workout_location",
            lambda conn, lat, lon, seen_at=None: location_id,
        )

        workout = WorkoutSnapshot(
            type="Outdoor Run",
            category="run",
            start_utc="2026-05-17T07:00:00Z",
            duration_min=30.0,
            location_lat=51.8,
            location_lon=-8.3,
        )
        store_snapshots(
            in_memory_db,
            [DailySnapshot(date="2026-05-17", workouts=[workout])],
        )

        loaded = load_snapshots(in_memory_db)

        assert loaded[0].workouts[0].location_id == location_id
        assert loaded[0].workouts[0].location_label == "Crosshaven"
        assert loaded[0].workouts[0].location_country_code == "ie"

    def test_manual_sleep_overrides_imported_sleep(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Manual sleep should replace imported sleep for the same night."""
        original = DailySnapshot(
            date="2026-05-02",
            sleep_total_h=5.46,
            sleep_in_bed_h=6.09,
            sleep_efficiency_pct=89.6,
            sleep_deep_h=0.48,
            sleep_core_h=3.4,
            sleep_rem_h=1.58,
            sleep_awake_h=0.63,
        )
        store_snapshots(in_memory_db, [original])
        insert_manual_sleep(in_memory_db, "2026-05-02", 7.0, sleep_in_bed_h=7.56)

        loaded = load_snapshots(in_memory_db)

        assert len(loaded) == 1
        day = loaded[0]
        assert day.sleep_total_h == 7.0
        assert day.sleep_in_bed_h == 7.56
        assert day.sleep_efficiency_pct is None
        assert day.sleep_deep_h is None
        assert day.sleep_core_h is None
        assert day.sleep_rem_h is None
        assert day.sleep_awake_h is None

    def test_upsert_overwrites(self, in_memory_db: sqlite3.Connection) -> None:
        day1 = DailySnapshot(date="2026-03-10", steps=5000, resting_hr=50)
        store_snapshots(in_memory_db, [day1])

        day1_updated = DailySnapshot(date="2026-03-10", steps=12000, resting_hr=54)
        store_snapshots(in_memory_db, [day1_updated])

        loaded = load_snapshots(in_memory_db)
        assert len(loaded) == 1
        assert loaded[0].steps == 12000
        assert loaded[0].resting_hr == 54

    def test_date_filter(self, in_memory_db: sqlite3.Connection) -> None:
        days = [
            DailySnapshot(date=f"2026-03-{d:02d}", steps=1000 * d) for d in range(9, 16)
        ]
        store_snapshots(in_memory_db, days)

        loaded = load_snapshots(in_memory_db, start="2026-03-11", end="2026-03-13")
        assert len(loaded) == 3
        assert loaded[0].date == "2026-03-11"
        assert loaded[-1].date == "2026-03-13"

    def test_empty_db(self, in_memory_db: sqlite3.Connection) -> None:
        assert load_snapshots(in_memory_db) == []
        assert load_date_range(in_memory_db) is None


class TestHalfImportsDoNotEraseTheOtherHalf:
    def test_workouts_only_import_keeps_existing_metrics(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(
                    date="2026-08-04",
                    steps=9000,
                    hrv_ms=52.0,
                    resting_hr=50,
                    sleep_total_h=7.4,
                )
            ],
        )

        # A Workouts export produces snapshots whose metric fields are all None.
        # Writing those over the stored row erased HRV, sleep and resting HR —
        # the reason the two exports had to be paired before anything imported.
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(
                    date="2026-08-04",
                    workouts=[
                        WorkoutSnapshot(
                            type="Outdoor Run",
                            category="run",
                            start_utc="2026-08-04T07:00:00Z",
                            duration_min=30.0,
                        )
                    ],
                )
            ],
        )

        row = in_memory_db.execute(
            "SELECT steps, hrv_ms, resting_hr, sleep_total_h FROM daily "
            "WHERE date = '2026-08-04'"
        ).fetchone()
        assert tuple(row) == (9000, 52.0, 50, 7.4)

    def test_a_later_metrics_export_still_updates_changed_values(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        store_snapshots(
            in_memory_db,
            [DailySnapshot(date="2026-08-04", steps=9000, hrv_ms=52.0, resting_hr=50)],
        )

        store_snapshots(
            in_memory_db, [DailySnapshot(date="2026-08-04", steps=12000, hrv_ms=48.0)]
        )

        row = in_memory_db.execute(
            "SELECT steps, hrv_ms, resting_hr FROM daily WHERE date = '2026-08-04'"
        ).fetchone()
        # Present values win; absent ones leave the stored reading alone.
        assert tuple(row) == (12000, 48.0, 50)

    def test_metrics_only_import_creates_the_day(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        store_snapshots(in_memory_db, [DailySnapshot(date="2026-08-04", hrv_ms=44.0)])

        row = in_memory_db.execute(
            "SELECT hrv_ms FROM daily WHERE date = '2026-08-04'"
        ).fetchone()
        assert row["hrv_ms"] == 44.0


class TestWorkoutCoverage:
    def _run(self, date: str) -> WorkoutSnapshot:
        """Return a run starting at 07:00 on *date*."""
        return WorkoutSnapshot(
            type="Outdoor Run",
            category="run",
            start_utc=f"{date}T07:00:00Z",
            duration_min=30.0,
        )

    def _count(self, conn: sqlite3.Connection, date: str) -> int:
        """Return stored workouts on *date*."""
        return conn.execute(
            "SELECT COUNT(*) FROM workout WHERE date = ?", (date,)
        ).fetchone()[0]

    def test_a_wider_metrics_window_never_deletes_older_workouts(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(
                    date="2026-07-30", steps=9000, workouts=[self._run("2026-07-30")]
                )
            ],
        )

        # Metrics on a 7-day window, Workouts still on the default 2-day one:
        # the older days arrive carrying no workouts at all. Treating that as
        # "no workouts happened" would erase real training history on every
        # single import.
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(date=f"2026-08-0{d}", steps=7000, workouts=[])
                for d in range(1, 6)
            ]
            + [DailySnapshot(date="2026-07-30", steps=9000, workouts=[])],
        )

        assert self._count(in_memory_db, "2026-07-30") == 1

    def test_a_rest_day_inside_the_workout_span_still_reconciles(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(
                    date="2026-08-02", steps=9000, workouts=[self._run("2026-08-02")]
                )
            ],
        )

        # A later export spans 01-03 and shows nothing on the 2nd, so the user
        # deleted it in Apple Health and the stored row must go.
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(
                    date="2026-08-01", steps=8000, workouts=[self._run("2026-08-01")]
                ),
                DailySnapshot(date="2026-08-02", steps=9000, workouts=[]),
                DailySnapshot(
                    date="2026-08-03", steps=8500, workouts=[self._run("2026-08-03")]
                ),
            ],
        )

        assert self._count(in_memory_db, "2026-08-02") == 0
        assert self._count(in_memory_db, "2026-08-01") == 1

    def test_metrics_only_import_leaves_every_workout_alone(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(
                    date="2026-08-02", steps=9000, workouts=[self._run("2026-08-02")]
                )
            ],
        )

        store_snapshots(
            in_memory_db, [DailySnapshot(date="2026-08-02", steps=9500, workouts=[])]
        )

        assert self._count(in_memory_db, "2026-08-02") == 1


class TestUpsertReplacesWorkouts:
    def test_reimport_does_not_duplicate_workouts(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Re-storing the same day should replace workouts, not duplicate them."""
        workout = WorkoutSnapshot(
            type="Outdoor Run",
            category="run",
            start_utc="2026-03-10T07:00:00Z",
            duration_min=35.0,
        )
        day = DailySnapshot(date="2026-03-10", steps=9000, workouts=[workout])
        store_snapshots(in_memory_db, [day])
        store_snapshots(in_memory_db, [day])

        loaded = load_snapshots(in_memory_db)
        assert len(loaded) == 1
        assert len(loaded[0].workouts) == 1

    def test_upsert_replaces_changed_workouts(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Re-import with different workouts should keep only the new set."""
        run = WorkoutSnapshot(
            type="Outdoor Run",
            category="run",
            start_utc="2026-03-10T07:00:00Z",
            duration_min=35.0,
        )
        lift = WorkoutSnapshot(
            type="Traditional Strength Training",
            category="lift",
            start_utc="2026-03-10T17:00:00Z",
            duration_min=60.0,
        )
        day_v1 = DailySnapshot(date="2026-03-10", workouts=[run])
        store_snapshots(in_memory_db, [day_v1])

        day_v2 = DailySnapshot(date="2026-03-10", workouts=[run, lift])
        store_snapshots(in_memory_db, [day_v2])

        loaded = load_snapshots(in_memory_db)
        assert len(loaded[0].workouts) == 2
        categories = {w.category for w in loaded[0].workouts}
        assert categories == {"run", "lift"}

    def test_counts_as_lift_round_trip(self, in_memory_db: sqlite3.Connection) -> None:
        functional = WorkoutSnapshot(
            type="Functional Strength Training",
            category="lift",
            counts_as_lift=False,
            start_utc="2026-03-10T07:00:00Z",
            duration_min=8.0,
        )
        traditional = WorkoutSnapshot(
            type="Traditional Strength Training",
            category="lift",
            counts_as_lift=True,
            start_utc="2026-03-10T17:00:00Z",
            duration_min=45.0,
        )
        day = DailySnapshot(date="2026-03-10", workouts=[functional, traditional])

        store_snapshots(in_memory_db, [day])
        loaded = load_snapshots(in_memory_db)

        assert [w.counts_as_lift for w in loaded[0].workouts] == [False, True]

    def test_adjacent_run_records_are_collapsed_before_insert(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """Persisted imported workouts should store one row per training run."""
        day = DailySnapshot(
            date="2026-05-11",
            workouts=[
                WorkoutSnapshot(
                    type="Outdoor Run",
                    category="run",
                    start_utc="2026-05-11T04:58:34Z",
                    duration_min=18.8,
                    hr_avg=157.0,
                    hr_max=176,
                    active_energy_kj=1037.0,
                    gpx_distance_km=3.457,
                    gpx_elevation_gain_m=4.94,
                    splits=[
                        WorkoutSplit(km_index=1, pace_min_km=5.6339),
                        WorkoutSplit(km_index=2, pace_min_km=5.6033),
                        WorkoutSplit(km_index=3, pace_min_km=5.147),
                    ],
                ),
                WorkoutSnapshot(
                    type="Outdoor Run",
                    category="run",
                    start_utc="2026-05-11T05:17:25Z",
                    duration_min=9.1,
                    hr_avg=172.0,
                    hr_max=176,
                    active_energy_kj=514.0,
                    gpx_distance_km=1.746,
                    gpx_elevation_gain_m=0.7,
                    splits=[WorkoutSplit(km_index=1, pace_min_km=5.1653)],
                ),
            ],
        )

        store_snapshots(in_memory_db, [day])

        raw_rows = in_memory_db.execute(
            "SELECT * FROM workout WHERE date = ?", ("2026-05-11",)
        ).fetchall()
        loaded = load_snapshots(in_memory_db)

        assert len(raw_rows) == 1
        assert len(loaded[0].workouts) == 1
        workout = loaded[0].workouts[0]
        assert workout.duration_min == pytest.approx(27.9)
        assert workout.gpx_distance_km == pytest.approx(5.203)
        assert workout.hr_avg == pytest.approx(161.9, abs=0.1)
        assert [split.km_index for split in workout.splits] == [1, 2, 3, 4]


class TestRoundTripNullWorkoutFields:
    def test_all_optional_fields_none(self, in_memory_db: sqlite3.Connection) -> None:
        """Workout with only required fields should survive a round-trip."""
        workout = WorkoutSnapshot(
            type="Outdoor Run",
            category="run",
            start_utc="2026-03-10T07:00:00Z",
            duration_min=35.0,
        )
        day = DailySnapshot(date="2026-03-10", workouts=[workout])
        store_snapshots(in_memory_db, [day])

        loaded = load_snapshots(in_memory_db)
        w = loaded[0].workouts[0]
        assert w.hr_min is None
        assert w.hr_avg is None
        assert w.hr_max is None
        assert w.temperature_c is None
        assert w.humidity_pct is None
        assert w.gpx_distance_km is None
        assert w.gpx_elevation_gain_m is None
        assert w.gpx_avg_speed_ms is None
        assert w.gpx_max_speed_p95_ms is None
        # active_energy_kj defaults to 0.0 via `or 0.0` coercion
        assert w.active_energy_kj == 0.0


class TestLoadSnapshotsOneSidedFilters:
    def test_start_only(self, in_memory_db: sqlite3.Connection) -> None:
        days = [
            DailySnapshot(date=f"2026-03-{d:02d}", steps=1000 * d) for d in range(9, 16)
        ]
        store_snapshots(in_memory_db, days)
        loaded = load_snapshots(in_memory_db, start="2026-03-13")
        assert len(loaded) == 3
        assert loaded[0].date == "2026-03-13"
        assert loaded[-1].date == "2026-03-15"

    def test_end_only(self, in_memory_db: sqlite3.Connection) -> None:
        days = [
            DailySnapshot(date=f"2026-03-{d:02d}", steps=1000 * d) for d in range(9, 16)
        ]
        store_snapshots(in_memory_db, days)
        loaded = load_snapshots(in_memory_db, end="2026-03-11")
        assert len(loaded) == 3
        assert loaded[0].date == "2026-03-09"
        assert loaded[-1].date == "2026-03-11"


class TestLoadDateRange:
    def test_returns_min_max(self, in_memory_db: sqlite3.Connection) -> None:
        days = [
            DailySnapshot(date="2026-03-09"),
            DailySnapshot(date="2026-03-15"),
        ]
        store_snapshots(in_memory_db, days)
        result = load_date_range(in_memory_db)
        assert result == ("2026-03-09", "2026-03-15")


class TestLatestMetricDate:
    def test_returns_none_when_nothing_is_stored(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        assert latest_metric_date(in_memory_db) is None

    def test_returns_the_newest_day_holding_a_metric(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(date="2026-08-11", steps=8000),
                DailySnapshot(date="2026-08-12", steps=5416),
            ],
        )

        assert latest_metric_date(in_memory_db) == "2026-08-12"

    def test_a_row_without_metrics_does_not_count_as_data(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        store_snapshots(in_memory_db, [DailySnapshot(date="2026-08-12", steps=5416)])
        # An import creates the row as soon as it sees the date, so a day Auto
        # Export has not actually reported yet still lands in the table empty.
        # Counting it would report the pipeline healthy exactly when it stopped.
        in_memory_db.execute(
            "INSERT INTO daily (date, imported_at) VALUES ('2026-08-13', '2026-08-13')"
        )

        assert latest_metric_date(in_memory_db) == "2026-08-12"

    def test_any_single_metric_is_enough_to_count(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(date="2026-08-12", steps=5416),
                DailySnapshot(date="2026-08-13", sleep_total_h=7.4),
            ],
        )

        assert latest_metric_date(in_memory_db) == "2026-08-13"


class TestLogLlmCall:
    def test_trace_links_related_calls(self, in_memory_db: sqlite3.Connection) -> None:
        trace_id = create_llm_trace(
            in_memory_db,
            "chat",
            metadata={"message_id": 123},
        )
        call_id = log_llm_call(
            in_memory_db,
            request_type="chat",
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_text="response",
            trace_id=trace_id,
        )

        trace = in_memory_db.execute(
            "SELECT * FROM llm_trace WHERE id = ?", (trace_id,)
        ).fetchone()
        call = in_memory_db.execute(
            "SELECT trace_id FROM llm_call WHERE id = ?", (call_id,)
        ).fetchone()

        assert trace["surface"] == "chat"
        assert json.loads(trace["metadata_json"]) == {"message_id": 123}
        assert call["trace_id"] == trace_id

    def test_inserts_record(self, in_memory_db: sqlite3.Connection) -> None:
        row_id = log_llm_call(
            in_memory_db,
            request_type="insights",
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_text="response",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            latency_s=1.5,
            cost=0.01,
        )
        assert row_id is not None
        assert row_id > 0

        row = in_memory_db.execute(
            "SELECT * FROM llm_call WHERE id = ?", (row_id,)
        ).fetchone()
        assert row["request_type"] == "insights"
        assert row["model"] == "test-model"
        assert row["cost"] == 0.01

    def test_update_response_merges_metadata(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        row_id = log_llm_call(
            in_memory_db,
            request_type="chat",
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_text="<tool_call>",
            metadata={"iteration": "final_synthesis"},
        )

        update_llm_call_response(
            in_memory_db,
            row_id,
            "Clean fallback.",
            metadata_patch={"postprocessed_response_text": True},
        )

        row = in_memory_db.execute(
            "SELECT response_text, metadata_json FROM llm_call WHERE id = ?",
            (row_id,),
        ).fetchone()

        assert row["response_text"] == "Clean fallback."
        assert json.loads(row["metadata_json"]) == {
            "iteration": "final_synthesis",
            "postprocessed_response_text": True,
        }


class TestLlmFeedback:
    def test_round_trip_feedback_lifecycle(
        self,
        in_memory_db: sqlite3.Connection,
    ) -> None:
        call_id = log_llm_call(
            in_memory_db,
            request_type="chat",
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_text="response",
        )

        feedback_id = log_feedback(
            in_memory_db,
            llm_call_id=call_id,
            category="inaccurate",
            message_type="chat",
        )
        update_feedback_reason(in_memory_db, feedback_id, "Wrong workout distance.")

        per_call = load_feedback_for_call(in_memory_db, call_id)
        assert len(per_call) == 1
        assert per_call[0]["id"] == feedback_id
        assert per_call[0]["reason"] == "Wrong workout distance."

        joined = load_feedback_entries(in_memory_db, limit=5)
        assert len(joined) == 1
        assert joined[0]["feedback_id"] == feedback_id
        assert joined[0]["request_type"] == "chat"

        assert delete_feedback(in_memory_db, feedback_id) is True
        assert load_feedback_for_call(in_memory_db, call_id) == []


class TestMigrations:
    def test_applies_all_on_fresh_db(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")

        applied = apply_migrations(conn)

        assert len(applied) == 11
        statuses = list_migrations(conn)
        assert all(status.status == "applied" for status in statuses)
        schema = get_live_schema(conn)
        assert "CREATE TABLE daily" in schema
        assert "CREATE TABLE workout" in schema
        assert "CREATE TABLE llm_call" in schema
        assert "CREATE TABLE schema_migrations" in schema
        assert "CREATE TABLE manual_workout" in schema
        assert "CREATE TABLE manual_sleep" in schema
        assert "CREATE TABLE events" in schema
        assert "CREATE TABLE workout_split" in schema
        assert "CREATE TABLE llm_trace" in schema
        assert "CREATE TABLE location" in schema
        assert "CREATE TABLE location_point_cache" in schema
        assert "CREATE TABLE location_point_failed" in schema

    def test_adopts_legacy_schema_and_applies_missing(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE daily (
                date                        TEXT PRIMARY KEY,
                steps                       INTEGER,
                distance_km                 REAL,
                active_energy_kj            REAL,
                exercise_min                INTEGER,
                stand_hours                 INTEGER,
                flights_climbed             REAL,
                resting_hr                  INTEGER,
                hrv_ms                      REAL,
                walking_hr_avg              REAL,
                hr_day_min                  INTEGER,
                hr_day_max                  INTEGER,
                vo2max                      REAL,
                walking_speed_kmh           REAL,
                walking_step_length_cm      REAL,
                walking_asymmetry_pct       REAL,
                walking_double_support_pct  REAL,
                stair_speed_up_ms           REAL,
                stair_speed_down_ms         REAL,
                running_stride_length_m     REAL,
                running_power_w             REAL,
                running_speed_kmh           REAL,
                recovery_index              REAL,
                imported_at                 TEXT NOT NULL
            );
            CREATE TABLE workout (
                start_utc                TEXT PRIMARY KEY,
                date                     TEXT NOT NULL,
                type                     TEXT NOT NULL,
                category                 TEXT NOT NULL,
                duration_min             REAL NOT NULL,
                hr_min                   INTEGER,
                hr_avg                   REAL,
                hr_max                   INTEGER,
                active_energy_kj         REAL,
                intensity_kcal_per_hr_kg REAL,
                temperature_c            REAL,
                humidity_pct             INTEGER,
                gpx_distance_km          REAL,
                gpx_elevation_gain_m     REAL,
                gpx_avg_speed_ms         REAL,
                gpx_max_speed_p95_ms     REAL,
                imported_at              TEXT NOT NULL
            );
            CREATE TABLE llm_call (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT NOT NULL,
                request_type    TEXT NOT NULL,
                model           TEXT NOT NULL,
                messages_json   TEXT NOT NULL,
                response_text   TEXT NOT NULL,
                params_json     TEXT,
                input_tokens    INTEGER NOT NULL,
                output_tokens   INTEGER NOT NULL,
                total_tokens    INTEGER NOT NULL,
                latency_s       REAL NOT NULL,
                metadata_json   TEXT
            );
            INSERT INTO workout (
                start_utc, date, type, category, duration_min, imported_at
            ) VALUES
                ('2026-03-10T07:00:00Z', '2026-03-10', 'Functional Strength Training', 'lift', 8.0, '2026-04-04T15:30:00+00:00'),
                ('2026-03-10T17:00:00Z', '2026-03-10', 'Traditional Strength Training', 'lift', 45.0, '2026-04-04T15:30:00+00:00');
        """)

        applied = apply_migrations(conn)

        cols = {r[1] for r in conn.execute("PRAGMA table_info(workout)").fetchall()}
        assert "counts_as_lift" in cols
        llm_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(llm_call)").fetchall()
        }
        assert "cost" in llm_cols
        assert "trace_id" in llm_cols
        daily_cols = {r[1] for r in conn.execute("PRAGMA table_info(daily)").fetchall()}
        assert "sleep_total_h" in daily_cols
        rows = conn.execute(
            "SELECT type, counts_as_lift FROM workout ORDER BY start_utc"
        ).fetchall()
        assert [row["counts_as_lift"] for row in rows] == [0, 1]
        assert {item.status for item in applied} == {"adopted", "applied"}

    def test_noop_when_current(self, in_memory_db: sqlite3.Connection) -> None:
        statuses = list_migrations(in_memory_db)
        assert all(status.status == "applied" for status in statuses)
        applied = apply_migrations(in_memory_db)
        assert applied == []


class TestConnectDb:
    def test_can_skip_auto_migrate(self, tmp_path: Path) -> None:
        conn = connect_db(tmp_path / "test.db", migrate=False)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert tables == set()
