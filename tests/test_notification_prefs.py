"""Tests for notification preference storage and evaluation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from notification_prefs import (
    DEFAULT_NOTIFICATION_PREFS,
    apply_notification_changes,
    effective_notification_prefs,
    evaluate_data_health_delivery,
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
                {"action": "set", "path": "nudges.max_per_day", "value": 4},
                {
                    "action": "set",
                    "path": "weekly_insights.weekday",
                    "value": "tuesday",
                },
            ],
        )

        effective = effective_notification_prefs(updated)
        assert effective["nudges"]["earliest_time"] == "11:00"
        assert effective["nudges"]["max_per_day"] == 4
        assert effective["weekly_insights"]["weekday"] == "tuesday"

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

    def test_data_health_defers_overnight_until_morning(self) -> None:
        decision = evaluate_data_health_delivery(
            DEFAULT_NOTIFICATION_PREFS,
            now=datetime.fromisoformat("2026-04-05T02:00:00+00:00"),
        )

        assert decision["status"] == "deferred"
        assert decision["reason"] == "quiet_hours"
        # Held until the same morning's 08:00 window close, not dropped.
        assert decision["until"] == "2026-04-05T08:00:00+00:00"

    def test_data_health_late_evening_defers_to_next_morning(self) -> None:
        decision = evaluate_data_health_delivery(
            DEFAULT_NOTIFICATION_PREFS,
            now=datetime.fromisoformat("2026-04-05T23:00:00+00:00"),
        )

        assert decision["status"] == "deferred"
        # Past the evening open, the close rolls to the following day.
        assert decision["until"] == "2026-04-06T08:00:00+00:00"

    def test_data_health_daytime_is_allowed(self) -> None:
        decision = evaluate_data_health_delivery(
            DEFAULT_NOTIFICATION_PREFS,
            now=datetime.fromisoformat("2026-04-05T12:00:00+00:00"),
        )

        assert decision["status"] == "allowed"

    def test_data_health_mute_outranks_quiet_hours(self) -> None:
        prefs = apply_notification_changes(
            DEFAULT_NOTIFICATION_PREFS,
            [
                {
                    "action": "mute_until",
                    "target": "data_health",
                    "expires_at": "2026-04-12T00:00:00+00:00",
                    "source_text": "mute sync alerts for a week",
                }
            ],
            now=datetime.fromisoformat("2026-04-05T12:00:00+00:00"),
        )

        decision = evaluate_data_health_delivery(
            prefs,
            now=datetime.fromisoformat("2026-04-05T12:00:00+00:00"),
        )

        assert decision["status"] == "suppressed"
        assert decision["reason"] == "temporary_mute"

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
        frozen_now = datetime.fromisoformat("2026-04-05T10:00:00+00:00")
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
            now=frozen_now,
        )

        text = format_notification_summary(
            prefs,
            now=frozen_now,
        )

        assert "Active temporary mutes:" in text
        assert "Nudges: muted until 2026-04-05T23:59:00+00:00" in text

    def test_summary_can_show_daily_nudge_cap(self) -> None:
        text = format_notification_summary(
            DEFAULT_NOTIFICATION_PREFS,
            now=datetime.fromisoformat("2026-04-05T10:00:00+00:00"),
        )

        assert "Max nudges per day: 2" in text

    def test_invalid_nudge_cap_is_rejected(self) -> None:
        from notification_prefs import validate_notification_changes

        try:
            validate_notification_changes(
                [{"action": "set", "path": "nudges.max_per_day", "value": 0}]
            )
        except ValueError as exc:
            assert "between 1 and 6" in str(exc)
        else:
            raise AssertionError("Expected ValueError for invalid nudge cap")
