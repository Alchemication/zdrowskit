"""Store coach challenges, their decisions and their measured outcomes."""

from __future__ import annotations

import sqlite3

NAME = "coach challenges"


def upgrade(conn: sqlite3.Connection) -> None:
    """Create the challenge ledger.

    A challenge is a temporary, measurable push the coach proposes and the user
    accepts or rejects: one weekly metric from the targets vocabulary, a
    per-week number, and a length in weeks. Every proposal is kept, including
    rejected and expired ones, because what a person declines is as much the
    coach's history as what they finish. The outcome is computed by code when
    the challenge ends and frozen with the rule version that scored it.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS challenge (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            status        TEXT NOT NULL,
            title         TEXT NOT NULL,
            goal          TEXT NOT NULL,
            rationale     TEXT,
            metric        TEXT NOT NULL,
            category      TEXT NOT NULL DEFAULT '',
            target        REAL NOT NULL,
            threshold     REAL,
            weeks         INTEGER NOT NULL,
            start_week    TEXT,
            end_date      TEXT,
            proposed_at   TEXT NOT NULL,
            decided_at    TEXT,
            closed_at     TEXT,
            outcome_json  TEXT,
            rule_version  INTEGER,
            reason        TEXT,
            reason_prompt_id INTEGER,
            llm_call_id   INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_challenge_status ON challenge (status);
        """
    )
