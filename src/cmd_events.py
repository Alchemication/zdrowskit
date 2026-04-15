"""System events query subcommand.

Prints a table of diagnostic events recorded by the daemon (nudge
fired/skip/rate-limit, import delta, coach skip/fire, etc.) so the user
can see how often things happen and how the system reacted.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from events import CATEGORIES, query_events
from store import open_db

logger = logging.getLogger(__name__)


_CATEGORY_STYLE: dict[str, str] = {
    "nudge": "cyan",
    "import": "green",
    "notify": "blue",
    "chat": "magenta",
    "context": "yellow",
    "coach": "bright_magenta",
    "insights": "bright_blue",
    "daemon": "white",
}

_KIND_STYLE: dict[str, str] = {
    "fired": "bold green",
    "llm_skip": "yellow",
    "rate_limited": "bright_yellow",
    "quiet_deferred": "dim cyan",
    "quiet_dropped": "dim red",
    "quiet_drain": "cyan",
    "prefs_suppressed": "bright_yellow",
    "already_ran": "dim",
    "failed": "bold red",
    "new_data": "bold green",
    "no_changes": "dim",
    "edited": "bold yellow",
    "start": "bold white",
    "stop": "dim white",
}


def _parse_since(value: str) -> str:
    """Parse --since values like '3d', '24h', '2026-04-10' into an ISO ts."""
    value = value.strip()
    now = datetime.now(timezone.utc)
    if value.endswith("d") and value[:-1].isdigit():
        return (now - timedelta(days=int(value[:-1]))).isoformat()
    if value.endswith("h") and value[:-1].isdigit():
        return (now - timedelta(hours=int(value[:-1]))).isoformat()
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid --since value: {value!r}. Use e.g. '3d', '24h', "
            "or an ISO date like '2026-04-10'."
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def cmd_events(args: argparse.Namespace) -> None:
    """Handle the 'events' subcommand.

    Args:
        args: Parsed CLI arguments with db, category, kind, since, limit,
              and json attributes.
    """
    since = None
    if getattr(args, "since", None):
        try:
            since = _parse_since(args.since)
        except ValueError as exc:
            logger.error("%s", exc)
            sys.exit(1)

    conn = open_db(Path(args.db))
    rows = query_events(
        conn,
        category=getattr(args, "category", None),
        kind=getattr(args, "kind", None),
        since=since,
        limit=getattr(args, "limit", 100),
    )

    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, default=str))
        return

    if not rows:
        print("No events match the filter.")
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(show_lines=False, pad_edge=False, expand=False)
    table.add_column("id", style="dim", no_wrap=True, justify="right")
    table.add_column("when", no_wrap=True)
    table.add_column("category", no_wrap=True)
    table.add_column("kind", no_wrap=True)
    table.add_column("summary", overflow="fold")
    table.add_column("llm", style="dim", no_wrap=True, justify="right")

    for row in rows:
        ts = row["ts"]
        try:
            local_ts = (
                datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            )
        except ValueError:
            local_ts = ts
        cat = row["category"]
        kind = row["kind"]
        cat_style = _CATEGORY_STYLE.get(cat, "")
        kind_style = _KIND_STYLE.get(kind, "")
        table.add_row(
            str(row["id"]),
            local_ts,
            f"[{cat_style}]{cat}[/{cat_style}]" if cat_style else cat,
            f"[{kind_style}]{kind}[/{kind_style}]" if kind_style else kind,
            row["summary"],
            str(row["llm_call_id"]) if row["llm_call_id"] else "",
        )

    console = Console()
    console.print(table)
    console.print(
        f"[dim]{len(rows)} event(s). Filter: "
        f"category={args.category or 'any'} kind={args.kind or 'any'} "
        f"since={args.since or 'any'} limit={args.limit}[/dim]"
    )


def format_events_for_telegram(rows: list[dict]) -> str:
    """Format events as a grouped-by-day Telegram message.

    Args:
        rows: Events as returned by ``query_events``.

    Returns:
        Markdown-ish plain text grouped by local date, most recent first.
    """
    if not rows:
        return "No system events in the selected window."

    by_day: dict[str, list[dict]] = {}
    for row in rows:
        try:
            local = datetime.fromisoformat(row["ts"]).astimezone()
        except ValueError:
            continue
        day = local.strftime("%a %b %d")
        by_day.setdefault(day, []).append((local, row))

    lines = ["*System events*"]
    for day in by_day:
        lines.append("")
        lines.append(f"*{day}*")
        for local, row in by_day[day]:
            time_s = local.strftime("%H:%M")
            kind_label = f"{row['category']}.{row['kind']}"
            lines.append(f"  {time_s} `{kind_label}` — {row['summary']}")
    return "\n".join(lines)


__all__ = ["cmd_events", "format_events_for_telegram", "CATEGORIES"]
