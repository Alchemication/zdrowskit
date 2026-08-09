"""Markdown rendering for the eval leaderboard."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from evals.framework import EvalCase
from evals.leaderboard.record import MARKDOWN_PATH
from evals.leaderboard.scorecard import (
    build_scorecard,
    display_reasoning_effort,
)

_INTRO = (
    "Feedback-derived regression scorecard for zdrowskit evals. The production "
    "table is what the daemon ships today; the per-feature tables are "
    "alternatives measured against it. Not a general benchmark."
)
_STABILITY_NOTE = (
    "Strict = cases passing every attempt. Attempt = attempt-weighted, the "
    "score one run would be expected to report. They diverge exactly when "
    "cases are flaky, and a flaky case is the one result a single run reports "
    "with false confidence."
)


def render_leaderboard_markdown(
    runs: list[dict[str, Any]],
    *,
    inventory: list[EvalCase] | None = None,
    head_sha: str | None = None,
    stale_check: Callable[[str], bool | None] | None = None,
) -> str:
    """Render leaderboard markdown from persisted run records."""
    scorecard = build_scorecard(
        runs, inventory=inventory, head_sha=head_sha, stale_check=stale_check
    )
    lines = ["# Feedback Eval Leaderboard", "", _INTRO, ""]
    if not runs:
        lines.extend(
            [
                "No recorded eval runs yet.",
                "",
                "Use `uv run python -m evals.run --repeat 3 --record` to add the "
                "first run.",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(_production_lines(scorecard))
    lines.extend(_variation_lines(scorecard))
    lines.extend(["---", "", _STABILITY_NOTE, ""])
    return "\n".join(lines)


def write_leaderboard_markdown(
    runs: list[dict[str, Any]],
    markdown_path: Path | None = None,
    *,
    inventory: list[EvalCase] | None = None,
    head_sha: str | None = None,
    stale_check: Callable[[str], bool | None] | None = None,
) -> str:
    """Write the rendered leaderboard markdown to disk."""
    path = markdown_path or MARKDOWN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    content = render_leaderboard_markdown(
        runs, inventory=inventory, head_sha=head_sha, stale_check=stale_check
    )
    path.write_text(content, encoding="utf-8")
    return content


def _production_lines(scorecard: dict[str, Any]) -> list[str]:
    """Render the production scorecard table."""
    lines = [
        "## Production",
        "",
        f"Latest run on production routes per feature, against the "
        f"{scorecard['total_cases']} cases in `evals/cases` today.",
        "",
        "| Feature | Route | Cases | Strict | Attempt | Flaky | Repeat "
        "| Avg Latency | Cost/run | Revision | Recorded |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for entry in scorecard["production"]:
        row = entry["row"]
        if row is None:
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(entry["feature"]),
                        "_never recorded_",
                        f"0/{entry['total_cases']}",
                        "-",
                        "-",
                        "-",
                        "-",
                        "-",
                        "-",
                        "-",
                        "-",
                    ]
                )
                + " |"
            )
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    str(entry["feature"]),
                    ", ".join(row["routes"]) or "-",
                    _coverage_label(row, entry["total_cases"]),
                    _format_percent(row["strict_accuracy"]),
                    _format_percent(row["accuracy"]),
                    str(row["flaky_count"]),
                    str(row["repeat"]),
                    _format_optional_seconds(row["avg_latency_s"]),
                    _format_optional_cost(row["cost_per_repeat"]),
                    _revision_cell(row),
                    str(row["created_at"])[:10],
                ]
            )
            + " |"
        )
    lines.append("")
    lines.extend(_production_notes(scorecard))
    return lines


def _production_notes(scorecard: dict[str, Any]) -> list[str]:
    """Render coverage and staleness warnings under the production table."""
    notes: list[str] = []
    uncovered = scorecard["uncovered_features"]
    if uncovered:
        notes.append(
            "**No production run recorded for:** "
            + ", ".join(f"`{feature}`" for feature in uncovered)
            + ". A green table above says nothing about these."
        )
    for entry in scorecard["production"]:
        row = entry["row"]
        if row is None or not row["missing_case_ids"]:
            continue
        notes.append(
            f"**`{entry['feature']}` is missing {len(row['missing_case_ids'])} "
            f"case(s)** added since that run: "
            + ", ".join(f"`{case_id}`" for case_id in row["missing_case_ids"])
        )
    stale = [
        entry["row"]["feature"]
        for entry in scorecard["production"]
        if entry["row"] is not None and entry["row"]["stale"]
    ]
    if stale:
        notes.append(
            "**Measured code that has since changed:** "
            + ", ".join(f"`{feature}`" for feature in stale)
            + ". Re-record to score the code as it stands."
        )
    single_sample = [
        entry["row"]["feature"]
        for entry in scorecard["production"]
        if entry["row"] is not None and entry["row"]["repeat"] < 2
    ]
    if single_sample:
        notes.append(
            "**Single sample (repeat=1):** "
            + ", ".join(f"`{feature}`" for feature in single_sample)
            + ". Stability is unmeasured; rerun with `--repeat 3` or more."
        )
    if not notes:
        return []
    return [note + "\n" for note in notes]


def _variation_lines(scorecard: dict[str, Any]) -> list[str]:
    """Render one comparison table per feature."""
    lines: list[str] = []
    for section in scorecard["features"]:
        lines.extend(
            [
                f"## {section['feature']} · {section['total_cases']} cases",
                "",
                "| Model | Reasoning | Repeat | Cases | Strict | Attempt | Flaky "
                "| Avg Latency | Cost/run | Revision | Failing |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: "
                "| --- | --- |",
            ]
        )
        for row in section["rows"]:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _model_cell(row),
                        display_reasoning_effort(row["reasoning_effort"]),
                        str(row["repeat"]),
                        _coverage_label(row, section["total_cases"]),
                        _format_percent(row["strict_accuracy"]),
                        _format_percent(row["accuracy"]),
                        str(row["flaky_count"]),
                        _format_optional_seconds(row["avg_latency_s"]),
                        _format_optional_cost(row["cost_per_repeat"]),
                        _revision_cell(row),
                        _failing_cell(row),
                    ]
                )
                + " |"
            )
        lines.append("")
        lines.extend(_stability_lines(section["rows"][0]))
    return lines


def _stability_lines(row: dict[str, Any]) -> list[str]:
    """Render the per-case stability breakdown for the leading row."""
    unstable = [case for case in row["case_rows"] if case["outcome"] != "pass"]
    if not unstable:
        return [
            f"Leading row (`{row['model_display']}`) passed every case on every "
            f"attempt.\n"
        ]
    lines = [
        f"Leading row (`{row['model_display']}`, repeat={row['repeat']}) "
        "per-case stability:",
        "",
    ]
    for case in unstable:
        marker = {"flaky": "FLAKY", "fail": "fail", "errored": "errored"}[
            case["outcome"]
        ]
        failures = ", ".join(case["failure_names"]) or "-"
        lines.append(
            f"- `{case['case_id']}` {case['rate_label']} {marker} — {failures}"
        )
    lines.append("")
    return lines


def _model_cell(row: dict[str, Any]) -> str:
    """Render the model cell, flagging production rows."""
    if row["is_production"]:
        return f"**{row['model_display']}**"
    return row["model_display"]


def _coverage_label(row: dict[str, Any], total_cases: int) -> str:
    """Render how much of today's case inventory a row actually covers."""
    return f"{len(row['case_ids'])}/{total_cases}"


def _revision_cell(row: dict[str, Any]) -> str:
    """Render a revision label, marking rows behind the current commit."""
    return f"{row['revision_label']} (stale)" if row["stale"] else row["revision_label"]


def _failing_cell(row: dict[str, Any]) -> str:
    """Render failing and flaky case ids with their pass rates."""
    parts = [
        f"`{case['case_id']}` {case['rate_label']}"
        for case in row["case_rows"]
        if case["outcome"] != "pass"
    ]
    return "<br>".join(parts) if parts else "-"


def _format_percent(value: float) -> str:
    """Format a percentage value for markdown output."""
    return f"{float(value):.1f}%"


def _format_optional_seconds(value: Any) -> str:
    """Format a seconds value or return a placeholder."""
    if value is None:
        return "-"
    return f"{float(value):.2f}s"


def _format_optional_cost(value: Any) -> str:
    """Format a USD cost value or return a placeholder."""
    if value is None:
        return "-"
    return f"${float(value):.4f}"
