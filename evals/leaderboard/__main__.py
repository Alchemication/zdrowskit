"""CLI for rendering the eval leaderboard from recorded history."""

from __future__ import annotations

import argparse
from pathlib import Path

from evals.leaderboard.html import write_leaderboard_html
from evals.leaderboard.markdown import write_leaderboard_markdown
from evals.leaderboard.record import (
    HTML_PATH,
    MARKDOWN_PATH,
    RUNS_PATH,
    get_repo_context,
    load_run_records,
)


def main() -> None:
    """CLI entry point for leaderboard rendering."""
    parser = argparse.ArgumentParser(description="Render the eval leaderboard.")
    subparsers = parser.add_subparsers(dest="command")
    render_parser = subparsers.add_parser(
        "render",
        help="Render leaderboard.md from recorded JSONL run history.",
    )
    render_parser.add_argument("--runs-path", type=Path, default=RUNS_PATH)
    render_parser.add_argument("--markdown-path", type=Path, default=MARKDOWN_PATH)
    render_html_parser = subparsers.add_parser(
        "render-html",
        help="Render leaderboard.html from recorded JSONL run history.",
    )
    render_html_parser.add_argument("--runs-path", type=Path, default=RUNS_PATH)
    render_html_parser.add_argument("--html-path", type=Path, default=HTML_PATH)
    render_html_parser.add_argument(
        "--nav-base",
        default=None,
        help=(
            "Relative path to the public-site root (e.g. '../'), which adds the "
            "shared site header and footer. Omit for the standalone local file."
        ),
    )
    args = parser.parse_args()

    if args.command not in {"render", "render-html"}:
        parser.print_help()
        raise SystemExit(2)

    runs = load_run_records(args.runs_path)
    head_sha = str(get_repo_context().get("git_sha", "unknown"))
    if args.command == "render":
        write_leaderboard_markdown(runs, args.markdown_path, head_sha=head_sha)
        print(f"Rendered leaderboard with {len(runs)} run(s) to {args.markdown_path}")
        return
    write_leaderboard_html(
        runs, args.html_path, head_sha=head_sha, nav_base=args.nav_base
    )
    print(f"Rendered HTML leaderboard with {len(runs)} run(s) to {args.html_path}")


if __name__ == "__main__":
    main()
