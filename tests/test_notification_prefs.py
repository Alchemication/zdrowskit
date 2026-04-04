"""Tests for notification preference storage and evaluation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from notification_prefs import (
    DEFAULT_NOTIFICATION_PREFS,
    apply_notification_changes,
    effective_notification_prefs,
    evaluate_nudge_delivery,
    format_notification_summary,
    load_notification_prefs,
    scheduled_report_due,
)


class TestNotificationPrefs:
    def test_load_missing_uses_defaults(self, tmp_path: Path) -> None:
        prefs = load_notification_prefs(tmp_path / "notification_prefs.json")

        assert effective_notification_prefs(prefs) == effective_notification_prefs(
            DEFAULT_NOTIFICATION_PREFS
        )

    def test_invalid_json_falls_back_to_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "notification_prefs.json"
        path.write_text("{not-json", encoding="utf-8")

        prefs = load_notification_prefs(path)

        assert prefs["overrides"] == {}
        assert prefs["temporary_mutes"] == []

    def test_apply_changes_merges_overrides(self) -> None:
        updated = apply_notification_changes(
            DEFAULT_NOTIFICATION_PREFS,
            [
                {"action": "set", "path": "nudges.earliest_time", "value": "11:00"},
                {
                    "action": "set",
                    "path": "weekly_insights.weekday",
                    "value": "tuesday",
                },
            ],
        )

        effective = effective_notification_prefs(updated)
        assert effective["nudges"]["earliest_time"] == "11:00"
        assert effective["weekly_insights"]["weekday"] == "tuesday"
        assert effective["midweek_report"]["weekday"] == "thursday"

    def test_expired_temporary_mutes_are_pruned(self, tmp_path: Path) -> None:
        path = tmp_path / "notification_prefs.json"
        path.write_text(
            """
            {
              "version": 1,
              "overrides": {},
              "temporary_mutes": [
                {
                  "target": "nudges",
                  "expires_at": "2026-04-04T08:00:00+00:00",
                  "source_text": "mute nudges today"
                }
              ]
            }
            """.strip(),
            encoding="utf-8",
        )

        prefs = load_notification_prefs(
            path,
            now=datetime.fromisoformat("2026-04-04T09:00:00+00:00"),
        )

        assert prefs["temporary_mutes"] == []

    def test_nudge_delivery_defers_before_earliest_time(self) -> None:
        prefs = apply_notification_changes(
            DEFAULT_NOTIFICATION_PREFS,
            [{"action": "set", "path": "nudges.earliest_time", "value": "11:00"}],
        )

        decision = evaluate_nudge_delivery(
            prefs,
            now=datetime.fromisoformat("2026-04-04T10:30:00+00:00"),
        )

        assert decision["status"] == "deferred"
        assert decision["reason"] == "earliest_time"

    def test_scheduled_report_due_uses_custom_schedule(self) -> None:
        prefs = apply_notification_changes(
            DEFAULT_NOTIFICATION_PREFS,
            [
                {
                    "action": "set",
                    "path": "weekly_insights.weekday",
                    "value": "tuesday",
                },
                {"action": "set", "path": "weekly_insights.time", "value": "08:30"},
            ],
        )

        assert scheduled_report_due(
            prefs,
            "weekly_insights",
            now=datetime.fromisoformat("2026-04-07T08:45:00+00:00"),
        )
        assert not scheduled_report_due(
            prefs,
            "weekly_insights",
            now=datetime.fromisoformat("2026-04-06T08:45:00+00:00"),
        )

    def test_summary_lists_active_mutes(self) -> None:
        prefs = apply_notification_changes(
            DEFAULT_NOTIFICATION_PREFS,
            [
                {
                    "action": "mute_until",
                    "target": "nudges",
                    "expires_at": "2026-04-05T23:59:00+00:00",
                    "source_text": "mute nudges today",
                }
            ],
        )

        text = format_notification_summary(
            prefs,
            now=datetime.fromisoformat("2026-04-05T10:00:00+00:00"),
        )

        assert "Active temporary mutes:" in text
        assert "Nudges: muted until 2026-04-05T23:59:00+00:00" in text
