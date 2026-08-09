"""Eval leaderboard: recording runs and rendering the scorecard."""

from __future__ import annotations

from evals.leaderboard.html import render_leaderboard_html, write_leaderboard_html
from evals.leaderboard.markdown import (
    render_leaderboard_markdown,
    write_leaderboard_markdown,
)
from evals.leaderboard.record import (
    HTML_PATH,
    MARKDOWN_PATH,
    RUNS_PATH,
    RecordRunOutcome,
    build_run_record,
    compute_case_set_id,
    compute_run_fingerprint,
    get_repo_context,
    load_run_records,
    record_run,
)
from evals.leaderboard.scorecard import build_scorecard

__all__ = [
    "HTML_PATH",
    "MARKDOWN_PATH",
    "RUNS_PATH",
    "RecordRunOutcome",
    "build_run_record",
    "build_scorecard",
    "compute_case_set_id",
    "compute_run_fingerprint",
    "get_repo_context",
    "load_run_records",
    "record_run",
    "render_leaderboard_html",
    "render_leaderboard_markdown",
    "write_leaderboard_html",
    "write_leaderboard_markdown",
]
