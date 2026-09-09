"""Inspect the rare facts a nudge may announce, without waiting for one."""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from config import (
    STANDOUT_COOLDOWN_DAYS,
    STANDOUT_MIN_MARGIN_PCT,
    STANDOUT_MIN_POPULATION,
    STANDOUT_RECENCY_DAYS,
)
from standouts import (
    Standout,
    announced_keys,
    cooldown_remaining_days,
    find_candidates,
    last_announced_at,
)
from store import open_db

logger = logging.getLogger(__name__)

_NO_CANDIDATES_HELP = f"""No standout is currently detectable. That is the normal state — the
whole surface is budgeted at one announcement every {STANDOUT_COOLDOWN_DAYS} days.

A fact has to clear all of these before it appears here:

  - at least {STANDOUT_MIN_POPULATION} comparable sessions to be ranked against
  - set within the last {STANDOUT_RECENCY_DAYS} days, so it is still the thing that just happened
  - beating the previous best by at least {STANDOUT_MIN_MARGIN_PCT:g}%

The population gate is the one a young profile fails, and it is deliberate: on
a short history, "best recorded" mostly restates how little was recorded."""


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

    remaining = cooldown_remaining_days(conn)
    last = last_announced_at(conn)
    if last is None:
        print("Cooldown: clear (nothing announced yet).")
    elif remaining:
        print(
            f"Cooldown: {remaining} day(s) left "
            f"(last announced {last.date().isoformat()})."
        )
    else:
        print(f"Cooldown: clear (last announced {last.date().isoformat()}).")

    candidates = find_candidates(conn, today=today)
    if not candidates:
        print()
        print(_NO_CANDIDATES_HELP)
        return

    seen = announced_keys(conn)
    fresh = [item for item in candidates if item.key not in seen]

    print()
    print(f"Candidates ({len(candidates)} found, {len(fresh)} not yet announced):")
    print()
    for item in candidates:
        print(_describe(item, announced=item.key in seen))
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
            "The picker would be asked to choose one of these, or to decline "
            "all of them. Declining is a normal outcome."
        )
