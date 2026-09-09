"""Reserve standout delivery before a message is sent."""

from __future__ import annotations

import sqlite3

NAME = "standout delivery reservations"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the durable reservation used around Telegram delivery.

    A reservation is written before sending and removed only after either a
    definite send failure or a successful move into ``standout_announced``.
    If the process or final ledger write fails after Telegram accepts the
    message, the reservation remains and prevents a duplicate announcement.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS standout_delivery_pending (
            key          TEXT PRIMARY KEY,
            kind         TEXT NOT NULL,
            headline     TEXT NOT NULL,
            occurred_on  TEXT NOT NULL,
            reserved_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_standout_delivery_pending_at
            ON standout_delivery_pending (reserved_at);
        """
    )
