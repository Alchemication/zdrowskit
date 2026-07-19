"""Fetch Auto Export JSON files from Google Drive.

Example:
    uv run python scripts/drive_fetch.py \
        --service-account ~/Documents/zdrowskit/secrets/service-account.json \
        --metrics-folder-id GOOGLE_DRIVE_FOLDER_ID \
        --workouts-folder-id GOOGLE_DRIVE_FOLDER_ID \
        --data-dir ~/Documents/zdrowskit/Imports/adam
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from google_drive import (  # noqa: E402
    fetch_health_export,
    print_fetch_summary,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Optional argument list. Defaults to ``sys.argv``.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="Fetch Auto Export JSON files from Google Drive."
    )
    parser.add_argument(
        "--service-account",
        required=True,
        metavar="PATH",
        help="Path to the Google service-account JSON key",
    )
    parser.add_argument(
        "--metrics-folder-id",
        required=True,
        help="Google Drive folder ID for the Metrics automation",
    )
    parser.add_argument(
        "--workouts-folder-id",
        required=True,
        help="Google Drive folder ID for the Workouts automation",
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        metavar="PATH",
        help="Local import directory to populate with Metrics/ and Workouts/",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Download files even when the local manifest is current",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files without downloading or writing the manifest",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log each folder and file decision",
    )
    return parser.parse_args(argv)


def _setup_logging(verbose: bool) -> None:
    """Configure stderr logging.

    Args:
        verbose: Whether to emit debug logs.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    """Run the standalone Drive fetcher."""
    args = _parse_args(argv)
    _setup_logging(args.verbose)
    service_account_path = Path(args.service_account).expanduser().resolve()
    data_dir = Path(args.data_dir).expanduser().resolve()

    try:
        stats = fetch_health_export(
            metrics_folder_id=args.metrics_folder_id,
            workouts_folder_id=args.workouts_folder_id,
            data_dir=data_dir,
            service_account_path=service_account_path,
            force=args.force,
            dry_run=args.dry_run,
            timeout=args.timeout,
        )
    except Exception as exc:
        logging.getLogger(__name__).error("%s", exc)
        sys.exit(1)

    print_fetch_summary(stats, dry_run=args.dry_run, data_dir=data_dir)


if __name__ == "__main__":
    main()
