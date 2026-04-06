"""Tests for manual activity tables, store functions, and clone logic."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from db.migrations import apply_migrations
from models import DailySnapshot, WorkoutSnapshot
from store import (
    delete_manual_sleep,
    delete_manual_workout,
    get_frequent_workout_types,
    insert_manual_sleep,
    insert_manual_workout,
    store_snapshots,
)


@pytest.fixture()
def db() -> sqlite3.Connection:
    """In-memory DB with all migrations applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn)
    return conn


def _seed_workouts(db: sqlite3.Connection) -> None:
    """Insert a daily row and several workouts for testing."""
    snap = DailySnapshot(
        date="2026-04-01",
        steps=10000,
        resting_hr=55,
        hrv_ms=50.0,
        workouts=[
            WorkoutSnapshot(
                type="Outdoor Run",
                category="run",
                start_utc="2026-04-01T07:00:00Z",
                duration_min=42.0,
                hr_avg=155.0,
                hr_max=178,
                active_energy_kj=1850.0,
                gpx_distance_km=5.2,
            ),
            WorkoutSnapshot(
                type="Traditional Strength Training",
                category="lift",
                start_utc="2026-04-01T17:00:00Z",
                duration_min=55.0,
                hr_avg=130.0,
                active_energy_kj=950.0,
            ),
        ],
    )
    snap2 = DailySnapshot(
        date="2026-04-02",
        steps=8000,
        workouts=[
            WorkoutSnapshot(
                type="Outdoor Run",
                category="run",
                start_utc="2026-04-02T07:30:00Z",
                duration_min=38.0,
                hr_avg=152.0,
                hr_max=175,
                active_energy_kj=1650.0,
                gpx_distance_km=4.8,
            ),
        ],
    )
    store_snapshots(db, [snap, snap2])


# -----------------------------------------------------------------------
# Migration & views
# -----------------------------------------------------------------------


class TestMigration:
    def test_tables_exist(self, db: sqlite3.Connection) -> None:
        tables = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "manual_workout" in tables
        assert "manual_sleep" in tables

    def test_views_exist(self, db: sqlite3.Connection) -> None:
        views = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            ).fetchall()
        }
        assert "workout_all" in views
        assert "sleep_all" in views

    def test_workout_all_union(self, db: sqlite3.Connection) -> None:
        """workout_all should include both imported and manual workouts."""
        _seed_workouts(db)
        clone = {
            "type": "Walking",
            "category": "walk",
            "duration_min": 30,
            "active_energy_kj": 500,
        }
        insert_manual_workout(db, clone, "2026-04-03")

        rows = db.execute("SELECT * FROM workout_all ORDER BY date").fetchall()
        sources = [r["source"] for r in rows]
        assert "import" in sources
        assert "manual" in sources
        # 3 imported + 1 manual = 4
        assert len(rows) == 4

    def test_sleep_all_union(self, db: sqlite3.Connection) -> None:
        """sleep_all should include both imported and manual sleep."""
        # Insert a daily row with sleep data.
        snap = DailySnapshot(
            date="2026-04-01",
            sleep_total_h=7.5,
            sleep_in_bed_h=8.0,
            sleep_efficiency_pct=93.8,
            sleep_deep_h=1.0,
            sleep_core_h=4.0,
            sleep_rem_h=2.0,
            sleep_awake_h=0.5,
        )
        store_snapshots(db, [snap])
        insert_manual_sleep(db, "2026-04-02", 6.5)

        rows = db.execute("SELECT * FROM sleep_all ORDER BY date").fetchall()
        assert len(rows) == 2
        assert rows[0]["source"] == "import"
        assert rows[1]["source"] == "manual"
        # Manual sleep has NULL stage breakdown.
        assert rows[1]["sleep_deep_h"] is None

    def test_workout_all_source_column(self, db: sqlite3.Connection) -> None:
        """Manual workouts should preserve all cloned fields in the view."""
        _seed_workouts(db)
        clone = {
            "type": "Outdoor Run",
            "category": "run",
            "duration_min": 42,
            "hr_avg": 155.0,
            "hr_max": 178,
            "active_energy_kj": 1850,
            "gpx_distance_km": 5.2,
        }
        insert_manual_workout(db, clone, "2026-04-03", source_note="test clone")

        manual_rows = db.execute(
            "SELECT * FROM workout_all WHERE source = 'manual'"
        ).fetchall()
        assert len(manual_rows) == 1
        r = manual_rows[0]
        assert r["hr_avg"] == 155.0
        assert r["gpx_distance_km"] == 5.2


# -----------------------------------------------------------------------
# Store functions
# -----------------------------------------------------------------------


class TestInsertManualWorkout:
    def test_basic_insert(self, db: sqlite3.Connection) -> None:
        clone = {
            "type": "Outdoor Run",
            "category": "run",
            "duration_min": 42,
            "active_energy_kj": 1850,
        }
        row_id = insert_manual_workout(db, clone, "2026-04-04")
        assert row_id > 0

        row = db.execute(
            "SELECT * FROM manual_workout WHERE id = ?", (row_id,)
        ).fetchone()
        assert row["type"] == "Outdoor Run"
        assert row["duration_min"] == 42
        assert row["date"] == "2026-04-04"
        assert row["start_utc"].startswith("2026-04-04T12:00:00")

    def test_source_note(self, db: sqlite3.Connection) -> None:
        clone = {"type": "Walking", "category": "walk", "duration_min": 30}
        row_id = insert_manual_workout(db, clone, "2026-04-04", source_note="cloned")
        row = db.execute(
            "SELECT source_note FROM manual_workout WHERE id = ?", (row_id,)
        ).fetchone()
        assert row["source_note"] == "cloned"

    def test_missing_optional_fields(self, db: sqlite3.Connection) -> None:
        """Missing fields should default to None."""
        clone = {"type": "Yoga", "category": "other", "duration_min": 60}
        row_id = insert_manual_workout(db, clone, "2026-04-04")
        row = db.execute(
            "SELECT hr_avg, gpx_distance_km FROM manual_workout WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["hr_avg"] is None
        assert row["gpx_distance_km"] is None


class TestInsertManualSleep:
    def test_basic_insert(self, db: sqlite3.Connection) -> None:
        row_id = insert_manual_sleep(db, "2026-04-04", 7.5)
        assert row_id > 0
        row = db.execute(
            "SELECT * FROM manual_sleep WHERE date = '2026-04-04'"
        ).fetchone()
        assert row["sleep_total_h"] == 7.5
        # Auto-estimated in-bed time.
        assert row["sleep_in_bed_h"] == pytest.approx(7.5 * 1.08, abs=0.01)

    def test_explicit_in_bed(self, db: sqlite3.Connection) -> None:
        insert_manual_sleep(db, "2026-04-04", 7.0, sleep_in_bed_h=7.8)
        row = db.execute(
            "SELECT sleep_in_bed_h FROM manual_sleep WHERE date = '2026-04-04'"
        ).fetchone()
        assert row["sleep_in_bed_h"] == 7.8

    def test_replace_on_same_date(self, db: sqlite3.Connection) -> None:
        """Inserting for the same date should replace, not duplicate."""
        insert_manual_sleep(db, "2026-04-04", 6.0)
        insert_manual_sleep(db, "2026-04-04", 8.0)
        rows = db.execute(
            "SELECT * FROM manual_sleep WHERE date = '2026-04-04'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["sleep_total_h"] == 8.0


class TestDeleteManualWorkout:
    def test_delete_existing(self, db: sqlite3.Connection) -> None:
        clone = {"type": "Run", "category": "run", "duration_min": 30}
        row_id = insert_manual_workout(db, clone, "2026-04-04")
        assert delete_manual_workout(db, row_id) is True
        row = db.execute(
            "SELECT * FROM manual_workout WHERE id = ?", (row_id,)
        ).fetchone()
        assert row is None

    def test_delete_nonexistent(self, db: sqlite3.Connection) -> None:
        assert delete_manual_workout(db, 9999) is False


class TestDeleteManualSleep:
    def test_delete_existing(self, db: sqlite3.Connection) -> None:
        insert_manual_sleep(db, "2026-04-04", 7.0)
        assert delete_manual_sleep(db, "2026-04-04") is True
        row = db.execute(
            "SELECT * FROM manual_sleep WHERE date = '2026-04-04'"
        ).fetchone()
        assert row is None

    def test_delete_nonexistent(self, db: sqlite3.Connection) -> None:
        assert delete_manual_sleep(db, "2099-01-01") is False


class TestGetFrequentWorkoutTypes:
    def test_ranking(self, db: sqlite3.Connection) -> None:
        _seed_workouts(db)
        types = get_frequent_workout_types(db, limit=4)
        assert len(types) == 2
        # Outdoor Run appears twice, Strength once.
        assert types[0]["type"] == "Outdoor Run"
        assert types[0]["count"] == 2
        assert types[1]["type"] == "Traditional Strength Training"
        assert types[1]["count"] == 1

    def test_limit(self, db: sqlite3.Connection) -> None:
        _seed_workouts(db)
        types = get_frequent_workout_types(db, limit=1)
        assert len(types) == 1

    def test_includes_manual(self, db: sqlite3.Connection) -> None:
        """Manual workouts should be counted in frequency ranking."""
        _seed_workouts(db)
        clone = {"type": "Walking", "category": "walk", "duration_min": 30}
        for i in range(5):
            insert_manual_workout(db, clone, f"2026-04-0{i + 3}")
        types = get_frequent_workout_types(db, limit=4)
        walk = next(t for t in types if t["type"] == "Walking")
        assert walk["count"] == 5


# -----------------------------------------------------------------------
# Duration scaling
# -----------------------------------------------------------------------


class TestDurationScaling:
    """Test the proportional scaling logic used in the /add flow."""

    def test_scale_energy_and_distance(self) -> None:
        clone = {
            "duration_min": 42.0,
            "active_energy_kj": 1850.0,
            "gpx_distance_km": 5.2,
        }
        new_dur = 30.0
        old_dur = clone["duration_min"]
        ratio = new_dur / old_dur
        new_energy = round(clone["active_energy_kj"] * ratio, 1)
        new_dist = round(clone["gpx_distance_km"] * ratio, 2)

        assert new_energy == pytest.approx(1321.4, abs=0.1)
        assert new_dist == pytest.approx(3.71, abs=0.01)

    def test_scale_with_none_fields(self) -> None:
        """Scaling should not fail when optional fields are None."""
        clone = {
            "duration_min": 45.0,
            "active_energy_kj": None,
            "gpx_distance_km": None,
        }
        new_dur = 30.0
        old_dur = clone["duration_min"]
        ratio = new_dur / old_dur
        if clone["active_energy_kj"]:
            clone["active_energy_kj"] = round(clone["active_energy_kj"] * ratio, 1)
        if clone["gpx_distance_km"]:
            clone["gpx_distance_km"] = round(clone["gpx_distance_km"] * ratio, 2)
        assert clone["active_energy_kj"] is None
        assert clone["gpx_distance_km"] is None


# -----------------------------------------------------------------------
# LLM clone logic
# -----------------------------------------------------------------------


class TestFindWorkoutClone:
    def test_llm_called_with_correct_params(self, db: sqlite3.Connection) -> None:
        """Verify the LLM is called with request_type='add_clone' and haiku model."""
        _seed_workouts(db)

        mock_result = MagicMock()
        mock_result.text = json.dumps(
            {
                "type": "Outdoor Run",
                "category": "run",
                "duration_min": 40,
                "hr_avg": 153,
                "active_energy_kj": 1750,
                "gpx_distance_km": 5.0,
                "source_note": "median of recent runs",
            }
        )

        with patch("llm.call_llm", return_value=mock_result) as mock_call:
            from daemon import ZdrowskitDaemon

            daemon = MagicMock(spec=ZdrowskitDaemon)
            daemon.db = ":memory:"
            result = ZdrowskitDaemon._find_workout_clone(
                daemon, db, "Outdoor Run", "run"
            )

            assert mock_call.called
            call_kwargs = mock_call.call_args
            assert call_kwargs.kwargs.get("request_type") == "add_clone"
            assert "haiku" in call_kwargs.kwargs.get("model", "")
            assert result["type"] == "Outdoor Run"
            assert result["duration_min"] == 40

    def test_fallback_on_llm_failure(self, db: sqlite3.Connection) -> None:
        """On LLM error, should fall back to most recent same-type workout."""
        _seed_workouts(db)

        with patch("llm.call_llm", side_effect=RuntimeError("API error")):
            from daemon import ZdrowskitDaemon

            daemon = MagicMock(spec=ZdrowskitDaemon)
            result = ZdrowskitDaemon._find_workout_clone(
                daemon, db, "Outdoor Run", "run"
            )
            assert result["type"] == "Outdoor Run"
            assert result["duration_min"] is not None
            assert "most recent" in result.get("source_note", "")

    def test_fallback_no_history(self, db: sqlite3.Connection) -> None:
        """With no workout history at all, should return safe defaults."""
        from daemon import ZdrowskitDaemon

        daemon = MagicMock(spec=ZdrowskitDaemon)
        result = ZdrowskitDaemon._find_workout_clone(daemon, db, "Outdoor Run", "run")
        assert result["type"] == "Outdoor Run"
        assert result["duration_min"] == 30
        assert "no history" in result.get("source_note", "")
