"""Store untracked nights as missing sleep rather than zero hours."""

from __future__ import annotations

import sqlite3

NAME = "null untracked sleep"


def upgrade(conn: sqlite3.Connection) -> None:
    """Clear the sleep columns of every night recorded as zero hours asleep.

    Older Auto Export files wrote a zero-hour entry for each night the watch was
    not worn, and the parser stored it. Nearly every day from 2019 to 2024 in the
    operator profile carried ``sleep_total_h = 0``, so any average over sleep
    history — including the SQL the LLM writes — treated those nights as nights
    without sleep. The parser now skips such entries; this clears the ones
    already stored. Overnight respiratory rate and wrist temperature are left
    alone because they come from separate metrics.
    """
    conn.execute(
        """
        UPDATE daily
        SET sleep_total_h = NULL,
            sleep_in_bed_h = NULL,
            sleep_efficiency_pct = NULL,
            sleep_deep_h = NULL,
            sleep_core_h = NULL,
            sleep_rem_h = NULL,
            sleep_awake_h = NULL
        WHERE sleep_total_h = 0
        """
    )
