"""Inspect the rare facts a nudge may announce, without waiting for one."""

from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import date
from pathlib import Path

from config import (
    STANDOUT_COOLDOWN_DAYS,
    STANDOUT_MIN_MARGIN_PCT_EXTENT,
    STANDOUT_MIN_MARGIN_PCT_PACE,
    STANDOUT_MIN_POPULATION,
    STANDOUT_MIN_SPAN_DAYS,
    STANDOUT_RECENCY_DAYS,
)
from standouts import (
    Standout,
    announced_keys,
    cooldown_remaining_days,
    find_candidates,
    last_announced_at,
    pending_keys,
)
from store import open_db

logger = logging.getLogger(__name__)

_NO_CANDIDATES_HELP = f"""No standout is currently detectable. That is the normal state — the
whole surface is budgeted at one announcement every {STANDOUT_COOLDOWN_DAYS} days.

Every fact has to clear all three of these:

  - ranked against at least {STANDOUT_MIN_POPULATION} comparable sessions
  - drawn from at least {STANDOUT_MIN_SPAN_DAYS} days of recorded history, so "ever" means something
  - dated within the last {STANDOUT_RECENCY_DAYS} days, so it is still the thing that just happened

A record has to beat the previous best as well: by {STANDOUT_MIN_MARGIN_PCT_PACE:g}% for a pace,
and by {STANDOUT_MIN_MARGIN_PCT_EXTENT:g}% for a distance or a duration. The two differ because
running ten percent faster is a career-defining jump while going ten percent
further is a normal progression. A lifetime-distance crossing beats no previous
best, so no margin applies to it.

Population and span are separate on purpose. Thirty sessions inside four months
clears a count while still saying almost nothing, and a young profile failing
the pair is exactly the intent: on a short history, "best recorded" mostly
restates how little was recorded."""


def _describe(item: Standout, *, announced: bool) -> str:
    """Render one candidate with the evidence behind it."""
    margin = (
        f"{item.margin_pct:.1f}% over previous"
        if item.margin_pct is not None
        else "threshold crossing"
    )
    status = "  [already announced]" if announced else ""
    return (
        f"  {item.kind}{status}\n"
        f"    {item.headline}\n"
        f"    set {item.occurred_on} · {margin} · "
        f"ranked against {item.population} sessions over {item.span_days} days\n"
        f"    key: {item.key}"
    )


def cmd_standouts(args: argparse.Namespace) -> None:
    """Handle the 'standouts' subcommand: show what could be announced and why not.

    A standout fires about once a month, so the only practical way to see
    whether detection works is to ask it directly. This prints every candidate
    the deterministic pass found, whether the ledger has already spent each one,
    and how long the cooldown has left to run.

    Args:
        args: Parsed CLI arguments carrying ``db``.
    """
    conn = open_db(Path(args.db))
    today = date.today()

    try:
        remaining = cooldown_remaining_days(conn)
        last = last_announced_at(conn)
        seen = announced_keys(conn)
        pending = pending_keys(conn)
    except sqlite3.Error as exc:
        print(
            "Announcement ledger unreadable, so no standout can fire: "
            f"{exc}\n\n"
            "Suppression depends on it, and an unreadable ledger reads as "
            "'nothing announced yet' — which would clear the cooldown and "
            "re-announce everything. Detection fails closed instead. Run "
            "'db migrate' if the table is missing.",
        )
        return

    if last is None and remaining:
        print(f"Cooldown: {remaining} day(s) left (delivery reservation pending).")
    elif last is None:
        print("Cooldown: clear (nothing announced yet).")
    elif remaining:
        print(
            f"Cooldown: {remaining} day(s) left "
            f"(last announced {last.date().isoformat()})."
        )
    else:
        print(f"Cooldown: clear (last announced {last.date().isoformat()}).")

    if pending:
        print(
            f"Delivery reservations: {len(pending)} unresolved; they remain "
            "suppressed to prevent duplicate sends."
        )

    candidates = find_candidates(conn, today=today)
    if not candidates:
        print()
        print(_NO_CANDIDATES_HELP)
        return

    fresh = [item for item in candidates if item.key not in seen | pending]

    print()
    print(f"Candidates ({len(candidates)} found, {len(fresh)} available):")
    print()
    for item in candidates:
        description = _describe(item, announced=item.key in seen)
        if item.key in pending:
            description = description.replace(
                f"  {item.kind}", f"  {item.kind}  [delivery pending]", 1
            )
        print(description)
        print()

    if not fresh:
        print("Every candidate has already been announced. Nothing would fire.")
    elif remaining:
        print(
            f"{len(fresh)} candidate(s) are eligible but the cooldown blocks them "
            f"for another {remaining} day(s)."
        )
    else:
        print(
            "The picker would normally choose one of these. It declines only "
            "when the journal explicitly contradicts the recorded candidate."
        )
