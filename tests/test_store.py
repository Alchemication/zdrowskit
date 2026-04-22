"""Tests for src/store.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from db.migrations import apply_migrations, get_live_schema, list_migrations
from models import DailySnapshot, WorkoutSnapshot
from store import (
    connect_db,
    delete_feedback,
    default_db_path,
    load_date_range,
    load_feedback_entries,
    load_feedback_for_call,
    load_snapshots,
    log_feedback,
    log_llm_call,
    open_db,
    store_snapshots,
    update_feedback_reason,
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


class TestLogLlmCall:
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

        assert len(applied) == 7
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


class TestDefaultDbPath:
    def test_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("zdrowskit_DB", raising=False)
        path = default_db_path()
        assert path.name == "health.db"
        assert "zdrowskit" in str(path)

    def test_respects_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("zdrowskit_DB", "/tmp/custom.db")
        path = default_db_path()
        assert path.name == "custom.db"
        assert str(path).endswith("/tmp/custom.db")
