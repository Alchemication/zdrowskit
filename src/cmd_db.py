"""Database admin subcommand: migrations, schema, status.

Extracted from commands.py to keep individual modules under ~1000 lines.
Public API re-exported from commands.py for backward compatibility.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from db.migrations import (
    apply_migrations,
    discover_migrations,
    get_live_schema,
    list_migrations,
)
from store import connect_db


def _format_bytes(size_bytes: int) -> str:
    """Format a byte count into a compact human-readable string."""
    value = float(size_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024


def cmd_db(args: argparse.Namespace) -> None:
    """Handle the 'db' subcommand family for migration and schema admin."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table

    console = Console(width=140)
    db_path = Path(args.db).expanduser().resolve()

    if args.db_cmd == "migrate":
        conn = connect_db(db_path, migrate=False)
        changes = apply_migrations(conn)
        if not changes:
            console.print(
                Panel(
                    "Database schema is already up to date.",
                    title="DB Migrate",
                    border_style="green",
                )
            )
            return

        table = Table(title="Applied Migrations", show_lines=False)
        table.add_column("Status", style="cyan", no_wrap=True)
        table.add_column("Key", style="magenta", overflow="fold")
        table.add_column("Name", style="green")
        table.add_column("Applied At", style="dim", no_wrap=True)
        for change in changes:
            ts = change.applied_at[:19] if change.applied_at else "—"
            table.add_row(change.status, change.key, change.name, ts)
        console.print(
            Panel(
                f"Applied {len(changes)} migration(s) to [bold]{db_path}[/bold].",
                title="DB Migrate",
                border_style="green",
            )
        )
        console.print(table)
        return

    if not db_path.exists():
        if args.db_cmd == "status":
            console.print(
                Panel(
                    f"Database file does not exist:\n[bold]{db_path}[/bold]",
                    title="DB Status",
                    border_style="yellow",
                )
            )
            table = Table(title="Available Migrations", show_lines=False)
            table.add_column("Status", style="cyan", no_wrap=True)
            table.add_column("Key", style="magenta", overflow="fold")
            table.add_column("Name", style="green")
            for migration in discover_migrations():
                table.add_row("pending", migration.key, migration.name)
            console.print(table)
            return

        if args.db_cmd == "schema":
            console.print(
                Panel(
                    f"Database file does not exist:\n[bold]{db_path}[/bold]",
                    title="DB Schema",
                    border_style="yellow",
                )
            )
            return

    conn = connect_db(db_path, migrate=False)

    if args.db_cmd == "status":
        statuses = list_migrations(conn)
        current = next(
            (status for status in reversed(statuses) if status.status == "applied"),
            None,
        )
        current_label = current.key if current else "(none recorded yet)"
        file_size = db_path.stat().st_size if db_path.exists() else 0
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        used_bytes = (page_count - freelist_count) * page_size
        tables = [
            row[0]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
        ]
        row_counts: dict[str, int] = {}
        for table_name in tables:
            quoted = table_name.replace('"', '""')
            row_counts[table_name] = int(
                conn.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
            )

        table_sizes: dict[str, int] = {}
        dbstat_available = True
        try:
            rows = conn.execute(
                """
                SELECT name, SUM(pgsize) AS size_bytes
                FROM dbstat
                WHERE aggregate = TRUE
                  AND name NOT LIKE 'sqlite_%'
                GROUP BY name
                ORDER BY name
                """
            ).fetchall()
            table_sizes = {
                str(row["name"]): int(row["size_bytes"] or 0)
                for row in rows
                if str(row["name"]) in row_counts
            }
        except sqlite3.DatabaseError:
            dbstat_available = False

        summary = (
            f"[cyan]Database:[/cyan] {db_path}\n"
            f"[cyan]Current migration:[/cyan] {current_label}"
            f"\n[cyan]File size:[/cyan] {_format_bytes(file_size)}"
            f"\n[cyan]Tables:[/cyan] {len(tables)}"
            f"\n[cyan]SQLite pages:[/cyan] {page_count:,} × {page_size:,} B"
            f"\n[cyan]Used / free:[/cyan] {_format_bytes(used_bytes)} / {_format_bytes(freelist_count * page_size)}"
        )
        console.print(Panel(summary, title="DB Status", border_style="blue"))

        object_table = Table(title="Table Stats", show_lines=False)
        object_table.add_column("Table", style="magenta")
        object_table.add_column("Rows", justify="right", style="cyan")
        if dbstat_available:
            object_table.add_column("Approx Size", justify="right", style="green")
            object_table.add_column("Share", justify="right", style="dim")

        total_sized_bytes = sum(table_sizes.values())
        for table_name in tables:
            cells = [table_name, f"{row_counts[table_name]:,}"]
            if dbstat_available:
                size_bytes = table_sizes.get(table_name, 0)
                share = (
                    f"{(size_bytes / total_sized_bytes) * 100:.1f}%"
                    if total_sized_bytes
                    else "0.0%"
                )
                cells.extend([_format_bytes(size_bytes), share])
            object_table.add_row(*cells)
        console.print(object_table)
        if not dbstat_available:
            console.print(
                Panel(
                    "Per-table size estimates are unavailable because SQLite dbstat is not enabled in this runtime.",
                    title="DB Status Note",
                    border_style="yellow",
                )
            )

        table = Table(title="Migration Status", show_lines=False)
        table.add_column("Status", style="cyan", no_wrap=True)
        table.add_column("Key", style="magenta", overflow="fold")
        table.add_column("Name", style="green")
        table.add_column("Applied At", style="dim", no_wrap=True)
        for status in statuses:
            ts = status.applied_at[:19] if status.applied_at else "—"
            table.add_row(status.status, status.key, status.name, ts)
        console.print(table)
        return

    if args.db_cmd == "schema":
        schema = get_live_schema(conn)
        if schema:
            console.print(
                Panel(
                    f"[bold]Database:[/bold] {db_path}",
                    title="DB Schema",
                    border_style="blue",
                )
            )
            console.print(Syntax(schema, "sql", line_numbers=True, word_wrap=False))
        else:
            console.print(
                Panel(
                    "No schema objects found in the database.",
                    title="DB Schema",
                    border_style="yellow",
                )
            )
        return

    raise ValueError(f"Unknown db subcommand: {args.db_cmd}")
