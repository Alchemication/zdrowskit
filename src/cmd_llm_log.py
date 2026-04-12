"""LLM call history query subcommand.

Extracted from commands.py to keep individual modules under ~1000 lines.
Public API re-exported from commands.py for backward compatibility.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from store import load_feedback_entries, load_feedback_for_call, open_db

logger = logging.getLogger(__name__)

_LLM_LOG_NEARBY_WINDOW_S = 120
_LLM_LOG_MAX_PANEL_CHARS = 20000


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _try_parse_json_text(content: str) -> str | None:
    """Pretty-print JSON content when possible."""
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return json.dumps(parsed, indent=2, sort_keys=True)


def _format_llm_log_content(content: object) -> str:
    """Normalize logged message content for display."""
    if content is None:
        return ""
    if isinstance(content, str):
        pretty = _try_parse_json_text(content)
        return pretty if pretty is not None else content
    return json.dumps(content, indent=2, sort_keys=True)


def _clip_llm_log_text(content: str, limit: int = _LLM_LOG_MAX_PANEL_CHARS) -> str:
    """Clip extremely large content for terminal display."""
    if len(content) <= limit:
        return content
    return content[:limit] + f"\n\n… [truncated, {len(content):,} chars total]"


def _normalize_llm_log_transcript(
    messages: list[dict],
    response_text: str,
) -> list[dict[str, object]]:
    """Build a normalized transcript for llm-log detail mode."""
    transcript: list[dict[str, object]] = []
    for index, msg in enumerate(messages, start=1):
        role = str(msg.get("role", "unknown"))
        entry: dict[str, object] = {
            "index": index,
            "role": role,
            "content": msg.get("content", ""),
        }
        if role == "assistant" and msg.get("tool_calls"):
            tool_calls: list[dict[str, object]] = []
            for tool_index, tc in enumerate(msg.get("tool_calls", []), start=1):
                fn = tc.get("function") or {}
                tool_calls.append(
                    {
                        "index": tool_index,
                        "id": tc.get("id"),
                        "type": tc.get("type", "function"),
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments", ""),
                    }
                )
            entry["tool_calls"] = tool_calls
        if role == "tool":
            entry["tool_call_id"] = msg.get("tool_call_id")
        transcript.append(entry)

    transcript.append(
        {
            "index": len(messages) + 1,
            "role": "assistant_final",
            "content": response_text,
            "highlighted": True,
        }
    )
    return transcript


def _load_nearby_llm_calls(
    conn,
    target_row,
    window_s: int = _LLM_LOG_NEARBY_WINDOW_S,
) -> list[dict[str, object]]:
    """Load nearby same-type LLM calls around a selected row."""
    target_ts = datetime.fromisoformat(target_row["timestamp"])
    rows = conn.execute(
        """
        SELECT id, timestamp, request_type, model, input_tokens, output_tokens,
               total_tokens, latency_s
        FROM llm_call
        WHERE request_type = ?
        ORDER BY timestamp ASC, id ASC
        """,
        (target_row["request_type"],),
    ).fetchall()

    nearby: list[dict[str, object]] = []
    for row in rows:
        row_ts = datetime.fromisoformat(row["timestamp"])
        delta_s = abs((row_ts - target_ts).total_seconds())
        if delta_s > window_s:
            continue
        nearby.append(
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "request_type": row["request_type"],
                "model": row["model"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "total_tokens": row["total_tokens"],
                "latency_s": row["latency_s"],
                "delta_s": round(delta_s, 3),
                "selected": row["id"] == target_row["id"],
            }
        )
    return nearby


# ---------------------------------------------------------------------------
# Subcommand handler
# ---------------------------------------------------------------------------


def cmd_llm_log(args: argparse.Namespace) -> None:
    """Handle the 'llm-log' subcommand: query LLM call history from the database.

    Three modes:
      default   — list recent calls with summary info (last N, default 10).
      --stats   — aggregate usage summary by request type and model.
      --id N    — show full detail for a specific call.
      --feedback — list recent thumbs-down feedback joined to LLM calls.

    Args:
        args: Parsed CLI arguments with db, last, stats, id, feedback,
            and json attributes.
    """
    conn = open_db(Path(args.db))

    # --- Detail mode ---
    if args.id:
        row = conn.execute("SELECT * FROM llm_call WHERE id = ?", (args.id,)).fetchone()
        if row is None:
            print(f"No LLM call found with id={args.id}")
            sys.exit(1)

        messages = json.loads(row["messages_json"])
        transcript = _normalize_llm_log_transcript(messages, row["response_text"])
        nearby_calls = _load_nearby_llm_calls(conn, row)

        if args.json:
            detail = {k: row[k] for k in row.keys()}
            detail["messages"] = messages
            detail["transcript"] = transcript
            detail["nearby_calls"] = nearby_calls
            print(json.dumps(detail, indent=2))
            return

        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table

        console = Console()

        meta_table = Table(title=f"LLM Call #{row['id']}", show_lines=False)
        meta_table.add_column("Field", style="cyan")
        meta_table.add_column("Value")
        meta_table.add_row("Timestamp", row["timestamp"])
        meta_table.add_row("Request type", row["request_type"])
        meta_table.add_row("Model", row["model"])
        meta_table.add_row("Input tokens", f"{row['input_tokens']:,}")
        meta_table.add_row("Output tokens", f"{row['output_tokens']:,}")
        meta_table.add_row("Total tokens", f"{row['total_tokens']:,}")
        meta_table.add_row("Latency", f"{row['latency_s']:.1f}s")
        if row["cost"] is not None:
            meta_table.add_row("Cost", f"${row['cost']:.4f}")
        else:
            meta_table.add_row("Cost", "unavailable")

        if row["params_json"]:
            meta_table.add_row("Params", row["params_json"])
        if row["metadata_json"]:
            meta_table.add_row("Metadata", row["metadata_json"])
        console.print(meta_table)

        if nearby_calls:
            nearby_table = Table(
                title="Nearby Calls (~2 min, same type)", show_lines=False
            )
            nearby_table.add_column("ID", justify="right", style="dim")
            nearby_table.add_column("When")
            nearby_table.add_column("Model", style="dim")
            nearby_table.add_column("Latency", justify="right")
            nearby_table.add_column("In tok", justify="right")
            nearby_table.add_column("Out tok", justify="right")
            nearby_table.add_column("Delta", justify="right")
            for nearby in nearby_calls:
                row_style = "bold green" if nearby["selected"] else ""
                marker = ">" if nearby["selected"] else ""
                nearby_table.add_row(
                    f"{marker}{nearby['id']}",
                    str(nearby["timestamp"])[:19],
                    str(nearby["model"]).split("/")[-1],
                    f"{float(nearby['latency_s']):.1f}s",
                    f"{int(nearby['input_tokens']):,}",
                    f"{int(nearby['output_tokens']):,}",
                    f"{float(nearby['delta_s']):.0f}s",
                    style=row_style,
                )
            console.print(nearby_table)

        feedback_rows = load_feedback_for_call(conn, args.id)
        if feedback_rows:
            feedback_table = Table(title="Feedback", show_lines=False)
            feedback_table.add_column("ID", justify="right", style="dim")
            feedback_table.add_column("When")
            feedback_table.add_column("Category", style="cyan")
            feedback_table.add_column("Message", style="dim")
            feedback_table.add_column("Reason")
            for feedback in feedback_rows:
                feedback_table.add_row(
                    str(feedback["id"]),
                    feedback["created_at"][:16],
                    feedback["category"],
                    feedback["message_type"],
                    feedback["reason"] or "—",
                )
            console.print(feedback_table)

        for entry in transcript[:-1]:
            role = str(entry["role"])
            content = _clip_llm_log_text(
                _format_llm_log_content(entry.get("content", ""))
            )

            if role == "assistant" and entry.get("tool_calls"):
                sections: list[str] = []
                if content.strip():
                    sections.append(content)
                for tool_call in entry["tool_calls"]:
                    tool_parts = [
                        f"Tool call #{tool_call['index']}",
                        f"Name: {tool_call['name'] or '(unknown)'}",
                    ]
                    if tool_call.get("id"):
                        tool_parts.append(f"ID: {tool_call['id']}")
                    args_text = _clip_llm_log_text(
                        _format_llm_log_content(tool_call.get("arguments", ""))
                    )
                    if args_text.strip():
                        tool_parts.append(f"Arguments:\n{args_text}")
                    sections.append("\n".join(tool_parts))
                content = "\n\n".join(sections).strip()
            elif role == "tool":
                tool_id = entry.get("tool_call_id")
                if tool_id:
                    content = f"Tool call ID: {tool_id}\n\n{content}".strip()

            title = role.replace("_", " ").title()
            border_style = {
                "system": "blue",
                "user": "cyan",
                "assistant": "magenta",
                "tool": "yellow",
            }.get(role, "dim")
            console.print(
                Panel(
                    content or "[dim](empty)[/dim]",
                    title=f"[bold]{title}[/bold]",
                    border_style=border_style,
                )
            )

        final_response = _clip_llm_log_text(
            _format_llm_log_content(transcript[-1]["content"])
        )
        console.print(
            Panel(
                final_response or "[dim](empty)[/dim]",
                title=f"[bold green]Final Response for Call #{row['id']}[/bold green]",
                border_style="green",
            )
        )
        return

    # --- Stats mode ---
    if args.stats:
        rows = conn.execute(
            """
            SELECT
                request_type,
                model,
                COUNT(*)           AS calls,
                SUM(input_tokens)  AS total_input,
                SUM(output_tokens) AS total_output,
                SUM(total_tokens)  AS total_tokens,
                AVG(latency_s)     AS avg_latency,
                SUM(cost)          AS total_cost,
                MIN(timestamp)     AS first_call,
                MAX(timestamp)     AS last_call
            FROM llm_call
            GROUP BY request_type, model
            ORDER BY last_call DESC
            """
        ).fetchall()

        if not rows:
            print("No LLM calls logged yet.")
            return

        if args.json:
            output = [{k: r[k] for k in r.keys()} for r in rows]
            print(json.dumps(output, indent=2))
            return

        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title="LLM Usage Summary", show_lines=False)
        table.add_column("Type", style="cyan")
        table.add_column("Model", style="dim")
        table.add_column("Calls", justify="right")
        table.add_column("Input tok", justify="right")
        table.add_column("Output tok", justify="right")
        table.add_column("Avg latency", justify="right")
        table.add_column("Cost", justify="right", style="green")
        table.add_column("Last call")

        grand_cost = 0.0
        grand_calls = 0
        for r in rows:
            cost = r["total_cost"] or 0.0
            grand_cost += cost
            grand_calls += r["calls"]
            table.add_row(
                r["request_type"],
                r["model"],
                f"{r['calls']:,}",
                f"{r['total_input']:,}",
                f"{r['total_output']:,}",
                f"{r['avg_latency']:.1f}s",
                f"${cost:.4f}" if r["total_cost"] is not None else "—",
                r["last_call"][:16],
            )

        console.print(table)
        console.print(f"\n[bold]Total:[/bold] {grand_calls} calls, ${grand_cost:.4f}")
        return

    # --- Feedback mode ---
    if getattr(args, "feedback", False):
        rows = load_feedback_entries(conn, limit=args.last)
        if not rows:
            print("No feedback logged yet.")
            return

        if args.json:
            output = [{k: row[k] for k in row.keys()} for row in rows]
            print(json.dumps(output, indent=2))
            return

        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title=f"Recent Feedback (last {args.last})", show_lines=False)
        table.add_column("Feedback", justify="right", style="dim")
        table.add_column("When")
        table.add_column("Message", style="dim")
        table.add_column("Call", justify="right")
        table.add_column("Type")
        table.add_column("Model", style="dim")
        table.add_column("Category", style="cyan", no_wrap=True)
        table.add_column("Reason")

        for row in rows:
            reason = row["reason"] or "—"
            table.add_row(
                str(row["feedback_id"]),
                row["created_at"][:16],
                row["message_type"],
                str(row["llm_call_id"]),
                row["request_type"],
                row["model"].split("/")[-1],
                row["category"],
                reason,
            )

        console.print(table)
        return

    # --- List mode (default) ---
    rows = conn.execute(
        """
        SELECT
            c.id,
            c.timestamp,
            c.request_type,
            c.model,
            c.input_tokens,
            c.output_tokens,
            c.total_tokens,
            c.latency_s,
            c.cost,
            c.metadata_json,
            COUNT(f.id) AS feedback_count
        FROM llm_call AS c
        LEFT JOIN llm_feedback AS f
          ON f.llm_call_id = c.id
        GROUP BY
            c.id, c.timestamp, c.request_type, c.model,
            c.input_tokens, c.output_tokens, c.total_tokens,
            c.latency_s, c.cost, c.metadata_json
        ORDER BY c.id DESC
        LIMIT ?
        """,
        (args.last,),
    ).fetchall()

    if not rows:
        print("No LLM calls logged yet.")
        return

    if args.json:
        output = [{k: r[k] for k in r.keys()} for r in rows]
        print(json.dumps(output, indent=2))
        return

    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title=f"Recent LLM Calls (last {args.last})", show_lines=False)
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Timestamp")
    table.add_column("Type", style="cyan")
    table.add_column("Model", style="dim")
    table.add_column("In tok", justify="right")
    table.add_column("Out tok", justify="right")
    table.add_column("Latency", justify="right")
    table.add_column("Cost", justify="right", style="green")
    table.add_column("Feedback", justify="right")
    table.add_column("Metadata", style="dim")

    for r in rows:
        meta = r["metadata_json"] or ""
        table.add_row(
            str(r["id"]),
            r["timestamp"][:16],
            r["request_type"],
            r["model"].split("/")[-1],
            f"{r['input_tokens']:,}",
            f"{r['output_tokens']:,}",
            f"{r['latency_s']:.1f}s",
            f"${r['cost']:.4f}" if r["cost"] is not None else "—",
            str(r["feedback_count"] or 0),
            meta,
        )

    console.print(table)
