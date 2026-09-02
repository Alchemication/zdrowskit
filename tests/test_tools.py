"""Tests for the run_sql tool and execution helpers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from db.migrations import apply_migrations
from models import DailySnapshot, WorkoutSnapshot
from store import store_snapshots
from tools import execute_run_sql, execute_tool, run_sql_tool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create a SQLite database file with schema and sample data."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn)

    snapshots = [
        DailySnapshot(
            date="2026-03-10",
            steps=12000,
            distance_km=9.8,
            resting_hr=54,
            hrv_ms=55.0,
            sleep_total_h=7.0,
            workouts=[
                WorkoutSnapshot(
                    type="Outdoor Run",
                    category="run",
                    start_utc="2026-03-10T07:00:00Z",
                    duration_min=35.0,
                    hr_avg=155.0,
                    active_energy_kj=900.0,
                    gpx_distance_km=5.2,
                ),
            ],
        ),
        DailySnapshot(
            date="2026-03-11",
            steps=8000,
            distance_km=5.5,
            resting_hr=50,
            hrv_ms=62.0,
        ),
        DailySnapshot(
            date="2026-03-12",
            steps=10000,
            distance_km=7.0,
            resting_hr=51,
            hrv_ms=60.0,
            workouts=[
                WorkoutSnapshot(
                    type="Traditional Strength Training",
                    category="lift",
                    start_utc="2026-03-12T17:00:00Z",
                    duration_min=60.0,
                    hr_avg=110.0,
                    active_energy_kj=600.0,
                ),
            ],
        ),
    ]
    store_snapshots(conn, snapshots)
    conn.close()
    return db


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


class TestRunSqlToolDefinition:
    """Verify the tool schema is well-formed."""

    def test_returns_single_tool(self) -> None:
        tools = run_sql_tool()
        assert len(tools) == 1
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "run_sql"

    def test_query_is_required(self) -> None:
        schema = run_sql_tool()[0]["function"]["parameters"]
        assert "query" in schema["required"]


# ---------------------------------------------------------------------------
# SELECT-only validation
# ---------------------------------------------------------------------------


class TestSelectOnlyValidation:
    """The tool must reject non-SELECT statements."""

    def test_rejects_insert(self, db_path: Path) -> None:
        result = json.loads(
            execute_run_sql(db_path, {"query": "INSERT INTO daily VALUES (1)"})
        )
        assert "error" in result

    def test_rejects_update(self, db_path: Path) -> None:
        result = json.loads(
            execute_run_sql(db_path, {"query": "UPDATE daily SET steps=0"})
        )
        assert "error" in result

    def test_rejects_delete(self, db_path: Path) -> None:
        result = json.loads(execute_run_sql(db_path, {"query": "DELETE FROM daily"}))
        assert "error" in result

    def test_rejects_drop(self, db_path: Path) -> None:
        result = json.loads(execute_run_sql(db_path, {"query": "DROP TABLE daily"}))
        assert "error" in result

    def test_rejects_empty_query(self, db_path: Path) -> None:
        result = json.loads(execute_run_sql(db_path, {"query": ""}))
        assert "error" in result

    def test_allows_select(self, db_path: Path) -> None:
        result = json.loads(
            execute_run_sql(db_path, {"query": "SELECT COUNT(*) AS cnt FROM daily"})
        )
        assert isinstance(result, list)
        assert result[0]["cnt"] == 3


class TestCommonTableExpressions:
    """WITH is a read shape and must reach the database.

    Rejecting it left the coach with an error instead of data on questions it
    could only phrase as a CTE, and no way to tell that the refusal was about
    the first word rather than the content.
    """

    def test_allows_single_cte(self, db_path: Path) -> None:
        result = json.loads(
            execute_run_sql(
                db_path,
                {
                    "query": (
                        "WITH busy AS (SELECT date, steps FROM daily "
                        "WHERE steps > 5000) SELECT COUNT(*) AS cnt FROM busy"
                    )
                },
            )
        )
        assert isinstance(result, list), result
        assert result[0]["cnt"] >= 1

    def test_allows_chained_ctes(self, db_path: Path) -> None:
        """The real 2 Sep nudge query shape: two CTEs joined at the end."""
        result = json.loads(
            execute_run_sql(
                db_path,
                {
                    "query": (
                        "WITH recent AS ("
                        "  SELECT date, hrv_ms, resting_hr FROM daily"
                        "  WHERE hrv_ms IS NOT NULL"
                        "), runs AS ("
                        "  SELECT date, duration_min FROM workout_all"
                        "  WHERE category = 'run'"
                        ") "
                        "SELECT r.date, r.hrv_ms, u.duration_min "
                        "FROM runs u JOIN recent r ON r.date = u.date "
                        "ORDER BY r.date DESC"
                    )
                },
            )
        )
        assert isinstance(result, list), result
        assert all("hrv_ms" in row for row in result)

    def test_is_case_insensitive(self, db_path: Path) -> None:
        result = json.loads(
            execute_run_sql(
                db_path,
                {"query": "with x as (select 1 as n) select n from x"},
            )
        )
        assert isinstance(result, list), result
        assert result[0]["n"] == 1

    def test_rejects_cte_wrapping_a_write(self, db_path: Path) -> None:
        """Allowing WITH must not open a path to a write statement."""
        result = json.loads(
            execute_run_sql(
                db_path,
                {
                    "query": (
                        "WITH x AS (SELECT 1) "
                        "INSERT INTO daily (date) VALUES ('2020-01-01')"
                    )
                },
            )
        )
        assert "error" in result

    def test_write_leaves_no_row_behind(self, db_path: Path) -> None:
        """The rejection above must be a refusal, not a silent success."""
        execute_run_sql(
            db_path,
            {
                "query": (
                    "WITH x AS (SELECT 1) "
                    "INSERT INTO daily (date) VALUES ('2020-01-01')"
                )
            },
        )
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM daily WHERE date = '2020-01-01'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert rows == 0


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------


class TestQueryExecution:
    """Verify correct data retrieval from the database."""

    def test_basic_select(self, db_path: Path) -> None:
        rows = json.loads(
            execute_run_sql(
                db_path,
                {"query": "SELECT date, steps FROM daily ORDER BY date"},
            )
        )
        assert len(rows) == 3
        assert rows[0]["date"] == "2026-03-10"
        assert rows[0]["steps"] == 12000

    def test_date_filter(self, db_path: Path) -> None:
        rows = json.loads(
            execute_run_sql(
                db_path,
                {"query": "SELECT date FROM daily WHERE date >= '2026-03-11'"},
            )
        )
        assert len(rows) == 2

    def test_aggregation(self, db_path: Path) -> None:
        rows = json.loads(
            execute_run_sql(
                db_path,
                {"query": "SELECT AVG(resting_hr) AS avg_hr FROM daily"},
            )
        )
        assert len(rows) == 1
        assert abs(rows[0]["avg_hr"] - 51.67) < 0.1

    def test_join_daily_workout(self, db_path: Path) -> None:
        rows = json.loads(
            execute_run_sql(
                db_path,
                {
                    "query": (
                        "SELECT d.date, w.type, w.duration_min "
                        "FROM daily d JOIN workout w ON d.date = w.date "
                        "ORDER BY d.date"
                    ),
                },
            )
        )
        assert len(rows) == 2
        assert rows[0]["type"] == "Outdoor Run"
        assert rows[1]["type"] == "Traditional Strength Training"

    def test_workout_category_filter(self, db_path: Path) -> None:
        rows = json.loads(
            execute_run_sql(
                db_path,
                {
                    "query": ("SELECT * FROM workout WHERE category = 'run'"),
                },
            )
        )
        assert len(rows) == 1

    def test_pace_computation(self, db_path: Path) -> None:
        """The LLM computes pace as duration_min / gpx_distance_km."""
        rows = json.loads(
            execute_run_sql(
                db_path,
                {
                    "query": (
                        "SELECT duration_min / gpx_distance_km AS pace_min_km "
                        "FROM workout WHERE gpx_distance_km IS NOT NULL"
                    ),
                },
            )
        )
        assert len(rows) == 1
        assert abs(rows[0]["pace_min_km"] - 35.0 / 5.2) < 0.01


# ---------------------------------------------------------------------------
# Row limit enforcement
# ---------------------------------------------------------------------------


class TestRowLimit:
    """The tool must respect and cap the limit parameter."""

    def test_default_limit(self, db_path: Path) -> None:
        """Default limit returns all rows when under the cap."""
        rows = json.loads(execute_run_sql(db_path, {"query": "SELECT * FROM daily"}))
        assert len(rows) == 3  # All rows returned (under default limit)

    def test_explicit_limit(self, db_path: Path) -> None:
        rows = json.loads(
            execute_run_sql(
                db_path,
                {"query": "SELECT * FROM daily ORDER BY date", "limit": 2},
            )
        )
        assert len(rows) == 2

    def test_limit_capped_at_max(self, db_path: Path) -> None:
        """Limit above _MAX_LIMIT should be capped."""
        rows = json.loads(
            execute_run_sql(
                db_path,
                {"query": "SELECT * FROM daily", "limit": 999},
            )
        )
        # Should not error — just capped. Our test DB only has 3 rows anyway.
        assert isinstance(rows, list)


# ---------------------------------------------------------------------------
# Read-only safety
# ---------------------------------------------------------------------------


class TestReadOnlySafety:
    """The read-only connection must prevent writes even if SELECT check is bypassed."""

    def test_readonly_connection_blocks_writes(self, db_path: Path) -> None:
        """Even a crafty subquery cannot write via the read-only connection."""
        execute_run_sql(
            db_path,
            {"query": "SELECT * FROM daily; DELETE FROM daily; --"},
        )
        # Should either error or return the SELECT results without deleting.
        # Verify data is intact.
        check = json.loads(
            execute_run_sql(db_path, {"query": "SELECT COUNT(*) AS cnt FROM daily"})
        )
        assert check[0]["cnt"] == 3


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------


class TestExecuteTool:
    """Verify the dispatcher routes correctly."""

    def test_run_sql_dispatched(self, db_path: Path) -> None:
        result = json.loads(
            execute_tool(
                "run_sql",
                {"query": "SELECT COUNT(*) AS cnt FROM daily"},
                db_path,
            )
        )
        assert result[0]["cnt"] == 3

    def test_unknown_tool(self, db_path: Path) -> None:
        result = json.loads(execute_tool("nonexistent", {}, db_path))
        assert "error" in result


class TestStatementTerminator:
    """A trailing semicolon must not read as a broken query.

    The LIMIT wrapper puts the query inside a subquery, where a terminator is
    a syntax error. The model sees "near ';': syntax error", concludes its
    query was wrong, and spends a round-trip rewriting one that was correct.
    """

    def test_select_with_semicolon(self, db_path: Path) -> None:
        result = json.loads(
            execute_run_sql(db_path, {"query": "SELECT COUNT(*) AS cnt FROM daily;"})
        )
        assert isinstance(result, list), result
        assert result[0]["cnt"] == 3

    def test_cte_with_semicolon(self, db_path: Path) -> None:
        result = json.loads(
            execute_run_sql(
                db_path,
                {"query": "WITH x AS (SELECT 1 AS n) SELECT n FROM x;"},
            )
        )
        assert isinstance(result, list), result
        assert result[0]["n"] == 1

    def test_semicolon_with_trailing_whitespace(self, db_path: Path) -> None:
        result = json.loads(execute_run_sql(db_path, {"query": "SELECT 1 AS n ;  \n"}))
        assert isinstance(result, list), result
        assert result[0]["n"] == 1

    def test_bare_semicolon_is_an_empty_query(self, db_path: Path) -> None:
        result = json.loads(execute_run_sql(db_path, {"query": ";"}))
        assert "error" in result
