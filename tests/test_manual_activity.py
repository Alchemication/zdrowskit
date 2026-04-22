"""Tests for manual activity tables, store functions, and clone logic."""

from __future__ import annotations

import json
import sqlite3
import time
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

    def test_feel_columns_default_to_unset(self, db: sqlite3.Connection) -> None:
        clone = {"type": "Walking", "category": "walk", "duration_min": 30}
        row_id = insert_manual_workout(db, clone, "2026-04-04")
        row = db.execute(
            "SELECT feel, feel_adjusted FROM manual_workout WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["feel"] is None
        assert row["feel_adjusted"] == 0

    def test_feel_columns_persisted(self, db: sqlite3.Connection) -> None:
        clone = {"type": "Outdoor Run", "category": "run", "duration_min": 45}
        row_id = insert_manual_workout(
            db, clone, "2026-04-04", feel="hard", feel_adjusted=True
        )
        row = db.execute(
            "SELECT feel, feel_adjusted FROM manual_workout WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["feel"] == "hard"
        assert row["feel_adjusted"] == 1


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

    def test_feel_columns_persisted(self, db: sqlite3.Connection) -> None:
        insert_manual_sleep(
            db,
            "2026-04-04",
            7.0,
            sleep_in_bed_h=8.75,
            feel="wrecked",
            feel_adjusted=True,
        )
        row = db.execute(
            "SELECT feel, feel_adjusted, sleep_in_bed_h FROM manual_sleep WHERE date = '2026-04-04'"
        ).fetchone()
        assert row["feel"] == "wrecked"
        assert row["feel_adjusted"] == 1
        assert row["sleep_in_bed_h"] == 8.75


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
            from daemon_add_flow import find_workout_clone

            result = find_workout_clone(db, "Outdoor Run", "run")

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
            from daemon_add_flow import find_workout_clone

            result = find_workout_clone(db, "Outdoor Run", "run")
            assert result["type"] == "Outdoor Run"
            assert result["duration_min"] is not None
            assert "most recent" in result.get("source_note", "")

    def test_fallback_no_history(self, db: sqlite3.Connection) -> None:
        """With no workout history at all, should return safe defaults."""
        from daemon_add_flow import find_workout_clone

        result = find_workout_clone(db, "Outdoor Run", "run")
        assert result["type"] == "Outdoor Run"
        assert result["duration_min"] == 30
        assert "no history" in result.get("source_note", "")

    def test_honours_explicit_duration(self, db: sqlite3.Connection) -> None:
        """Explicit duration_min must be reflected in the returned clone."""
        _seed_workouts(db)
        mock_result = MagicMock()
        mock_result.text = json.dumps(
            {
                "type": "Outdoor Run",
                "category": "run",
                "duration_min": 42,
                "source_note": "cloned",
            }
        )
        with patch("llm.call_llm", return_value=mock_result):
            from daemon_add_flow import find_workout_clone

            result = find_workout_clone(db, "Outdoor Run", "run", duration_min=60)
            assert result["duration_min"] == 60


# -----------------------------------------------------------------------
# /add flow state machine
# -----------------------------------------------------------------------


def _make_add_handler(db_path):
    """Build a minimal AddFlowHandler with a mocked poller for unit tests."""
    from daemon_add_flow import AddFlowHandler

    daemon_stub = MagicMock()
    daemon_stub.db = db_path
    daemon_stub._poller = MagicMock()
    daemon_stub._poller.send_message_with_keyboard.return_value = 100
    return AddFlowHandler(daemon_stub)


class TestAddFlowReorder:
    """Verify the upfront-signals-before-LLM flow ordering."""

    def test_type_pick_goes_to_duration_picker_without_llm(self, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        apply_migrations(conn)
        conn.close()

        handler = _make_add_handler(db_path)
        handler._pending["a1"] = __import__("daemon_add_flow").PendingAdd(
            step="pick_type",
            message_id=100,
            created_at=time.monotonic(),
            type_options=[{"type": "Outdoor Run", "category": "run", "count": 3}],
        )

        with patch("llm.call_llm") as mock_llm:
            handler.handle_callback("cb1", "add_type:a1:0", 100)
            assert not mock_llm.called

        pending = handler._pending["a1"]
        assert pending.step == "pick_duration"
        assert pending.workout_type == "Outdoor Run"
        assert pending.category == "run"

    def test_duration_advances_to_date_picker(self, tmp_path) -> None:
        handler = _make_add_handler(tmp_path / "test.db")
        handler._pending["a1"] = __import__("daemon_add_flow").PendingAdd(
            step="pick_duration",
            message_id=100,
            created_at=time.monotonic(),
            workout_type="Outdoor Run",
            category="run",
        )

        handler.handle_callback("cb1", "add_d:a1:45", 100)

        pending = handler._pending["a1"]
        assert pending.chosen_duration_min == 45.0
        assert pending.step == "pick_workout_date"

    def test_date_advances_to_feel_picker(self, tmp_path) -> None:
        handler = _make_add_handler(tmp_path / "test.db")
        handler._pending["a1"] = __import__("daemon_add_flow").PendingAdd(
            step="pick_workout_date",
            message_id=100,
            created_at=time.monotonic(),
            workout_type="Outdoor Run",
            category="run",
            chosen_duration_min=45.0,
        )

        handler.handle_callback("cb1", "add_dt:a1:today", 100)

        pending = handler._pending["a1"]
        assert pending.date is not None
        assert pending.step == "pick_feel"

    def test_workout_feel_runs_clone_with_duration_and_adjusts(self, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        apply_migrations(conn)
        conn.close()

        handler = _make_add_handler(db_path)
        handler._pending["a1"] = __import__("daemon_add_flow").PendingAdd(
            step="pick_feel",
            message_id=100,
            created_at=time.monotonic(),
            workout_type="Outdoor Run",
            category="run",
            chosen_duration_min=45.0,
            date="2026-04-22",
        )

        fake_clone = {
            "type": "Outdoor Run",
            "category": "run",
            "duration_min": 45.0,
            "hr_avg": 150.0,
            "active_energy_kj": 1800.0,
            "source_note": "cloned from Apr 1 run",
        }
        with patch(
            "daemon_add_flow.find_workout_clone", return_value=fake_clone
        ) as mock_find:
            handler.handle_callback("cb1", "add_feel:a1:hard", 100)

            assert mock_find.called
            call_kwargs = mock_find.call_args.kwargs
            assert call_kwargs["duration_min"] == 45.0
            assert call_kwargs["target_date"] == "2026-04-22"

        pending = handler._pending["a1"]
        assert pending.feel == "hard"
        assert pending.feel_adjusted is True
        assert pending.step == "confirm_workout"
        # HR bumped up ~6% for hard feel.
        assert pending.clone_row["hr_avg"] == pytest.approx(150.0 * 1.06, abs=0.1)

    def test_workout_feel_skip_leaves_clone_untouched(self, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        apply_migrations(conn)
        conn.close()

        handler = _make_add_handler(db_path)
        handler._pending["a1"] = __import__("daemon_add_flow").PendingAdd(
            step="pick_feel",
            message_id=100,
            created_at=time.monotonic(),
            workout_type="Outdoor Run",
            category="run",
            chosen_duration_min=45.0,
            date="2026-04-22",
        )

        fake_clone = {
            "type": "Outdoor Run",
            "category": "run",
            "duration_min": 45.0,
            "hr_avg": 150.0,
            "source_note": "cloned",
        }
        with patch("daemon_add_flow.find_workout_clone", return_value=fake_clone):
            handler.handle_callback("cb1", "add_feel:a1:skip", 100)

        pending = handler._pending["a1"]
        assert pending.feel is None
        assert pending.feel_adjusted is False
        assert pending.clone_row["hr_avg"] == 150.0


class TestAddFlowSleep:
    def test_sleep_duration_advances_to_feel_picker(self, tmp_path) -> None:
        handler = _make_add_handler(tmp_path / "test.db")
        handler._pending["a1"] = __import__("daemon_add_flow").PendingAdd(
            step="pick_sleep_dur",
            message_id=100,
            created_at=time.monotonic(),
            date="2026-04-21",
        )

        handler.handle_callback("cb1", "add_sd:a1:7.0", 100)

        pending = handler._pending["a1"]
        assert pending.sleep_total_h == 7.0
        assert pending.step == "pick_sleep_feel"

    def test_sleep_feel_applies_in_bed_factor(self, tmp_path) -> None:
        handler = _make_add_handler(tmp_path / "test.db")
        handler._pending["a1"] = __import__("daemon_add_flow").PendingAdd(
            step="pick_sleep_feel",
            message_id=100,
            created_at=time.monotonic(),
            date="2026-04-21",
            sleep_total_h=7.0,
        )

        handler.handle_callback("cb1", "add_feel:a1:wrecked", 100)

        pending = handler._pending["a1"]
        assert pending.feel == "wrecked"
        assert pending.feel_adjusted is True
        # 7h total × 1.25 factor for wrecked.
        assert pending.sleep_in_bed_h == pytest.approx(7.0 * 1.25, abs=0.01)
        assert pending.step == "confirm_sleep"

    def test_sleep_feel_skip_uses_default_padding(self, tmp_path) -> None:
        handler = _make_add_handler(tmp_path / "test.db")
        handler._pending["a1"] = __import__("daemon_add_flow").PendingAdd(
            step="pick_sleep_feel",
            message_id=100,
            created_at=time.monotonic(),
            date="2026-04-21",
            sleep_total_h=7.0,
        )

        handler.handle_callback("cb1", "add_feel:a1:skip", 100)

        pending = handler._pending["a1"]
        assert pending.feel is None
        assert pending.feel_adjusted is False
        assert pending.sleep_in_bed_h == pytest.approx(7.0 * 1.08, abs=0.01)


class TestAddFlowConfirmPersistsFeel:
    def test_workout_confirm_writes_feel_columns(self, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        apply_migrations(conn)
        conn.close()

        handler = _make_add_handler(db_path)
        handler._pending["a1"] = __import__("daemon_add_flow").PendingAdd(
            step="confirm_workout",
            message_id=100,
            created_at=time.monotonic(),
            workout_type="Outdoor Run",
            category="run",
            date="2026-04-22",
            feel="hard",
            feel_adjusted=True,
            clone_row={
                "type": "Outdoor Run",
                "category": "run",
                "duration_min": 45.0,
                "hr_avg": 159.0,
                "active_energy_kj": 1944.0,
                "source_note": "cloned, adjusted up for 'hard' feel",
            },
        )

        handler.handle_callback("cb1", "add_ok:a1", 100)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT feel, feel_adjusted FROM manual_workout WHERE date = ?",
            ("2026-04-22",),
        ).fetchone()
        conn.close()
        assert row["feel"] == "hard"
        assert row["feel_adjusted"] == 1

    def test_sleep_confirm_writes_feel_columns(self, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        apply_migrations(conn)
        conn.close()

        handler = _make_add_handler(db_path)
        handler._pending["a1"] = __import__("daemon_add_flow").PendingAdd(
            step="confirm_sleep",
            message_id=100,
            created_at=time.monotonic(),
            date="2026-04-22",
            feel="wrecked",
            feel_adjusted=True,
            sleep_total_h=7.0,
            sleep_in_bed_h=8.75,
        )

        handler.handle_callback("cb1", "add_ok:a1", 100)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT feel, feel_adjusted, sleep_in_bed_h FROM manual_sleep WHERE date = ?",
            ("2026-04-22",),
        ).fetchone()
        conn.close()
        assert row["feel"] == "wrecked"
        assert row["feel_adjusted"] == 1
        assert row["sleep_in_bed_h"] == 8.75
