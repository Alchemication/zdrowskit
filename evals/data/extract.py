"""Snapshot real context files and health data as eval blueprints.

Blueprints are committed to the repo and pinned — evals always use
the committed snapshots for reproducibility.  Re-run this script
deliberately when you want to refresh the baseline data.

Usage:
    uv run python -m evals.data.extract
    uv run python -m evals.data.extract --name sparse_week
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent.parent / "src"
sys.path.insert(0, str(_SRC_DIR))

from config import CONTEXT_DIR  # noqa: E402
from llm import build_llm_data  # noqa: E402
from store import default_db_path, open_db  # noqa: E402

_BLUEPRINTS_DIR = Path(__file__).resolve().parent / "blueprints"

_CONTEXT_FILES = [
    "me.md",
    "goals.md",
    "plan.md",
    "log.md",
    "history.md",
    "baselines.md",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot eval blueprint data.")
    parser.add_argument(
        "--name",
        default="baseline",
        help="Blueprint name. 'baseline' writes to evals/data/blueprints/.",
    )
    args = parser.parse_args()

    out_dir = (
        _BLUEPRINTS_DIR if args.name == "baseline" else _BLUEPRINTS_DIR / args.name
    )
    context_out = out_dir / "context"
    context_out.mkdir(parents=True, exist_ok=True)

    # 1. Copy context files.
    for name in _CONTEXT_FILES:
        src = CONTEXT_DIR / name
        dst = context_out / name
        if src.exists():
            shutil.copy2(src, dst)
            print(f"  context/{name}")
        else:
            print(f"  context/{name} — not found, skipping")

    # 2. Snapshot health data.
    db_path = default_db_path()
    if not db_path.exists():
        print(f"  Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = open_db(db_path)
    health_data = build_llm_data(conn, months=6, week="current")
    conn.close()

    hd_path = out_dir / "health_data.json"
    hd_path.write_text(
        json.dumps(health_data, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"  health_data.json ({len(health_data['current_week']['days'])} days)")

    # 3. Save metadata for date pinning.
    today = date.today()
    week_label = health_data.get("week_label", "")
    meta = {
        "extracted_at": today.isoformat(),
        "weekday": today.strftime("%A"),
        "week_label": week_label,
        "week_complete": health_data.get("week_complete", False),
    }
    meta_path = out_dir / "metadata.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"  metadata.json (date={today}, week={week_label})")

    print(f"Done. Commit blueprints/ to pin blueprint '{args.name}'.")


if __name__ == "__main__":
    main()
