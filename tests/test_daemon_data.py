"""Tests for import snapshots used to scope standout detection."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from daemon_data import changed_workout_ids, data_snapshot
from store import open_db


class TestDataSnapshot:
    """Recent workout fingerprints must notice candidate-relevant changes."""

    def test_split_change_marks_only_its_workout(self, tmp_path: Path) -> None:
        db = tmp_path / "health.db"
        conn = open_db(db)
        for start_utc, pace in (
            ("2026-09-08T08:00:00Z", 6.0),
            ("2026-09-08T09:00:00Z", 7.0),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO daily (date, imported_at) VALUES (?, ?)",
                ("2026-09-08", "2026-09-09T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO workout "
                "(start_utc, date, type, category, duration_min, "
                "gpx_distance_km, imported_at, counts_as_lift) "
                "VALUES (?, ?, 'Run', 'run', 40, 6, ?, 0)",
                (start_utc, "2026-09-08", "2026-09-09T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO workout_split (start_utc, km_index, pace_min_km) "
                "VALUES (?, 1, ?)",
                (start_utc, pace),
            )
        conn.commit()

        before = data_snapshot(db, today=date(2026, 9, 9))
        conn.execute(
            "UPDATE workout_split SET pace_min_km = 5.5 WHERE start_utc = ?",
            ("2026-09-08T08:00:00Z",),
        )
        conn.commit()
        after = data_snapshot(db, today=date(2026, 9, 9))

        assert changed_workout_ids(before, after) == {"2026-09-08T08:00:00Z"}

    def test_missing_snapshot_fails_closed(self) -> None:
        assert changed_workout_ids({}, {"recent_workouts": {"new": []}}) == set()
