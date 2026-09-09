"""Remember every standout fact that has already been announced."""

from __future__ import annotations

import sqlite3

NAME = "standout announced"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the ledger behind standout announcements.

    Two different suppressions need one table. A given achievement is announced
    once ever, which the primary key enforces: re-deriving the same record on
    the next sync finds its own key already present and stops. Separately, the
    whole feature is rate-limited to roughly one announcement a month, which is
    the maximum ``announced_at`` across every row.

    Rows accumulate, unlike the single-row progress and plan-frame caches. That
    is the point: a ledger that forgot would re-announce a record the person
    was already told about, and the record itself never changes back.

    ``headline`` stores the exact sentence that was sent. A standout is rare
    enough that when one lands wrongly, the only way to see what the person
    actually read is to have kept it.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS standout_announced (
            key          TEXT PRIMARY KEY,
            kind         TEXT NOT NULL,
            headline     TEXT NOT NULL,
            occurred_on  TEXT NOT NULL,
            announced_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_standout_announced_at
            ON standout_announced (announced_at);
        """
    )
