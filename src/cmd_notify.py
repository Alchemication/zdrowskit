"""CLI for notification preferences."""

from __future__ import annotations

import argparse

from config import NOTIFICATION_PREFS_PATH
from notification_prefs import (
    apply_notification_changes,
    format_notification_summary,
    load_notification_prefs,
    save_notification_prefs,
)

RESET_TARGETS = ("all", "nudges", "weekly_insights", "midweek_report")


def cmd_notify(args: argparse.Namespace) -> None:
    """Handle the ``notify`` subcommand."""
    action = getattr(args, "notify_cmd", None)
    if action is None or action == "show":
        prefs = load_notification_prefs(NOTIFICATION_PREFS_PATH)
        print(format_notification_summary(prefs, include_examples=True))
        return

    if action == "reset":
        target = getattr(args, "target", "all")
        prefs = load_notification_prefs(NOTIFICATION_PREFS_PATH)
        change = (
            {"action": "reset_all"}
            if target == "all"
            else {"action": "reset", "path": target}
        )
        updated = apply_notification_changes(prefs, [change])
        save_notification_prefs(updated, path=NOTIFICATION_PREFS_PATH)
        print(f"Reset notification settings: {target}.")
        print(format_notification_summary(updated))
        return

    raise SystemExit(f"Unknown notify command: {action}")
