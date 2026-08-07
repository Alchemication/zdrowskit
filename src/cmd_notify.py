"""CLI for notification preferences."""

from __future__ import annotations

import argparse
from pathlib import Path

from config import NOTIFICATION_PREFS_PATH
from notification_prefs import (
    apply_notification_changes,
    format_notification_summary,
    load_notification_prefs,
    save_notification_prefs,
)

RESET_TARGETS = ("all", "nudges", "weekly_insights")


def cmd_notify(args: argparse.Namespace) -> None:
    """Handle the ``notify`` subcommand."""
    path = Path(getattr(args, "notification_prefs_path", NOTIFICATION_PREFS_PATH))
    action = getattr(args, "notify_cmd", None)
    if action is None or action == "show":
        prefs = load_notification_prefs(path)
        print(format_notification_summary(prefs, include_examples=True))
        return

    if action == "reset":
        target = getattr(args, "target", "all")
        prefs = load_notification_prefs(path)
        change = (
            {"action": "reset_all"}
            if target == "all"
            else {"action": "reset", "path": target}
        )
        updated = apply_notification_changes(prefs, [change])
        save_notification_prefs(updated, path=path)
        print(f"Reset notification settings: {target}.")
        print(format_notification_summary(updated))
        return

    raise SystemExit(f"Unknown notify command: {action}")
