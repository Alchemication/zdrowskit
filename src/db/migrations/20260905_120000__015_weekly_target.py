"""Store the measurable weekly targets a progress strip is drawn against."""

from __future__ import annotations

import sqlite3

NAME = "weekly targets"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the weekly_target table.

    One row per (week, metric). Targets are derived from the prose goals in
    strategy.md, which is why ``strategy_hash`` and ``llm_call_id`` are stored
    beside the number: a bar the user disputes has to be traceable back to the
    sentence it came from and the call that read it.

    The week is keyed by its Monday so a target cannot be silently reused
    across weeks — a new week either re-derives or shows no strip at all.

    ``category`` is part of the key because the measurable metrics are
    parameterised by activity rather than enumerated per sport: one person's
    week is 30 km of running and another's is 120 km of cycling plus three
    walks, and both have to fit without a schema change. It is the empty string
    for metrics that take no category.

    ``llm_call_id`` carries no foreign key on purpose. It is a pointer for
    ``llm-log --id``, and a constraint here would mean a target row could fail
    to save — inside a notification send — because the call log did not.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS weekly_target (
            week_start     TEXT NOT NULL,
            metric         TEXT NOT NULL,
            category       TEXT NOT NULL DEFAULT '',
            target         REAL NOT NULL,
            threshold      REAL,
            source         TEXT NOT NULL,
            goal_text      TEXT,
            strategy_hash  TEXT,
            llm_call_id    INTEGER,
            position       INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL,
            PRIMARY KEY (week_start, metric, category)
        );

        CREATE INDEX IF NOT EXISTS weekly_target_week
            ON weekly_target(week_start);
        """
    )
