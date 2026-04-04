"""Versioned SQLite migrations for zdrowskit."""

from __future__ import annotations

import importlib.util
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Callable

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent
_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    key         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL
)
"""
_LEGACY_INITIAL_KEY = "20260404_153000__001_initial_schema"
_LEGACY_COST_KEY = "20260404_154500__002_add_llm_call_cost"
_LEGACY_SLEEP_KEY = "20260404_160000__003_add_daily_sleep_columns"
_LEGACY_LIFT_KEY = "20260404_161500__004_add_workout_counts_as_lift"
_SLEEP_COLS = [
    "sleep_total_h",
    "sleep_in_bed_h",
    "sleep_efficiency_pct",
    "sleep_deep_h",
    "sleep_core_h",
    "sleep_rem_h",
    "sleep_awake_h",
]


@dataclass(frozen=True)
class Migration:
    """A single versioned schema migration."""

    key: str
    name: str
    upgrade: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class MigrationStatus:
    """Status of one available migration relative to a database."""

    key: str
    name: str
    status: str
    applied_at: str | None = None


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    """Ensure the schema_migrations table exists."""
    conn.execute(_SCHEMA_MIGRATIONS_SQL)


def _migration_module_name(path: Path) -> str:
    sanitized = path.stem.replace("-", "_").replace(".", "_")
    return f"zdrowskit_migration_{sanitized}"


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(_migration_module_name(path), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load migration module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover_migrations() -> list[Migration]:
    """Load all available migration files in sorted order."""
    migrations: list[Migration] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = _load_module(path)
        name = getattr(module, "NAME", path.stem)
        upgrade = getattr(module, "upgrade", None)
        if not callable(upgrade):
            raise RuntimeError(f"Migration missing upgrade() function: {path.name}")
        migrations.append(Migration(key=path.stem, name=name, upgrade=upgrade))
    return migrations


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _detect_legacy_applied_keys(conn: sqlite3.Connection) -> set[str]:
    """Infer legacy migration state for pre-schema_migrations databases."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if not ({"daily", "workout", "llm_call"} & tables):
        return set()

    applied: set[str] = set()
    if {"daily", "workout", "llm_call"}.issubset(tables):
        applied.add(_LEGACY_INITIAL_KEY)

    if "llm_call" in tables and "cost" in _table_columns(conn, "llm_call"):
        applied.add(_LEGACY_COST_KEY)

    if "daily" in tables and set(_SLEEP_COLS).issubset(_table_columns(conn, "daily")):
        applied.add(_LEGACY_SLEEP_KEY)

    if "workout" in tables and "counts_as_lift" in _table_columns(conn, "workout"):
        applied.add(_LEGACY_LIFT_KEY)

    return applied


def _recorded_migrations(conn: sqlite3.Connection) -> dict[str, str]:
    if not _table_exists(conn, "schema_migrations"):
        return {}
    return {
        row["key"]: row["applied_at"]
        for row in conn.execute(
            "SELECT key, applied_at FROM schema_migrations ORDER BY key"
        ).fetchall()
    }


def list_migrations(conn: sqlite3.Connection) -> list[MigrationStatus]:
    """Return available migrations with applied/pending status."""
    recorded = _recorded_migrations(conn)
    legacy = _detect_legacy_applied_keys(conn) if not recorded else set()
    statuses: list[MigrationStatus] = []
    for migration in discover_migrations():
        if migration.key in recorded:
            statuses.append(
                MigrationStatus(
                    key=migration.key,
                    name=migration.name,
                    status="applied",
                    applied_at=recorded[migration.key],
                )
            )
        elif migration.key in legacy:
            statuses.append(
                MigrationStatus(
                    key=migration.key,
                    name=migration.name,
                    status="legacy",
                )
            )
        else:
            statuses.append(
                MigrationStatus(
                    key=migration.key,
                    name=migration.name,
                    status="pending",
                )
            )
    return statuses


def apply_migrations(conn: sqlite3.Connection) -> list[MigrationStatus]:
    """Apply all pending migrations and record them in schema_migrations."""
    ensure_migration_table(conn)
    applied_now: list[MigrationStatus] = []
    recorded = _recorded_migrations(conn)
    legacy = _detect_legacy_applied_keys(conn)
    now = datetime.now(timezone.utc).isoformat()

    for migration in discover_migrations():
        if migration.key in recorded:
            continue

        if migration.key in legacy:
            with conn:
                conn.execute(
                    "INSERT INTO schema_migrations (key, name, applied_at) VALUES (?, ?, ?)",
                    (migration.key, migration.name, now),
                )
            logger.info("Adopted legacy migration %s", migration.key)
            applied_now.append(
                MigrationStatus(
                    key=migration.key,
                    name=migration.name,
                    status="adopted",
                    applied_at=now,
                )
            )
            continue

        logger.info("Applying migration %s", migration.key)
        with conn:
            migration.upgrade(conn)
            conn.execute(
                "INSERT INTO schema_migrations (key, name, applied_at) VALUES (?, ?, ?)",
                (migration.key, migration.name, now),
            )
        applied_now.append(
            MigrationStatus(
                key=migration.key,
                name=migration.name,
                status="applied",
                applied_at=now,
            )
        )

    return applied_now


def get_live_schema(conn: sqlite3.Connection) -> str:
    """Return the live SQLite schema as SQL text."""
    rows = conn.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE type IN ('table', 'index')
          AND name NOT LIKE 'sqlite_%'
          AND sql IS NOT NULL
        ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name
        """
    ).fetchall()
    return "\n\n".join(f"{row['sql']};" for row in rows)
