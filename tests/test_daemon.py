"""Tests for nudge scheduling, scheduled coach behavior, and Telegram feedback flow."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import daemon as daemon_module
from commands import CommandResult
from context_edit import (
    ContextEdit,
    PendingContextEdit,
    PendingEdits,
    append_coach_feedback,
    new_feedback_entry,
)
from daemon import ZdrowskitDaemon
from notification_prefs import load_notification_prefs
from store import log_llm_call, open_db


def _make_daemon(tmp_path: Path) -> ZdrowskitDaemon:
    daemon_module.STATE_FILE = tmp_path / "state.json"
    daemon = ZdrowskitDaemon("test-model", tmp_path / "test.db", tmp_path)
    daemon._notification_prefs_path = tmp_path / "notification_prefs.json"
    return daemon


class TestWeeklyReportScheduling:
    def test_weekly_report_runs_coach_after_insights(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        events: list[str] = []

        def _mock_insights(args):
            events.append("insights")
            return CommandResult(text="report text")

        with (
            patch.object(daemon, "_run_import"),
            patch.object(
                daemon, "_record_report", side_effect=lambda _: events.append("record")
            ),
            patch(
                "commands.cmd_insights",
                side_effect=_mock_insights,
            ),
            patch.object(daemon, "_attach_feedback_button"),
            patch.object(
                daemon,
                "_run_coach",
                side_effect=lambda **kwargs: events.append(
                    f"coach:{kwargs['week']}:{kwargs['skip_import']}"
                ),
            ),
        ):
            daemon._run_weekly_report()

        assert events == ["insights", "record", "coach:last:True"]


class TestNudgeScheduling:
    def test_run_nudge_queues_before_10am(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        fake_now = daemon_module.datetime(2026, 4, 5, 9, 30)
        fake_datetime = MagicMock()
        fake_datetime.now.return_value = fake_now

        with (
            patch.object(daemon_module, "datetime", fake_datetime),
            patch("commands.cmd_nudge") as cmd_nudge,
        ):
            daemon._run_nudge("new_data")

        assert daemon._state["quiet_queue"][0]["trigger"] == "new_data"
        cmd_nudge.assert_not_called()
        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert state["quiet_queue"][0]["trigger"] == "new_data"

    def test_disabled_nudges_skip_without_queueing(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._notification_prefs_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "overrides": {"nudges": {"enabled": False}},
                    "temporary_mutes": [],
                }
            ),
            encoding="utf-8",
        )

        with patch("commands.cmd_nudge") as cmd_nudge:
            daemon._run_nudge("new_data")

        assert daemon._state.get("quiet_queue") is None
        cmd_nudge.assert_not_called()

    def test_temporary_mute_skips_weekly_report_without_llm_call(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._notification_prefs_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "overrides": {},
                    "temporary_mutes": [
                        {
                            "target": "weekly_insights",
                            "expires_at": "2099-01-01T12:00:00+00:00",
                            "source_text": "mute weekly insights this week",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with patch("commands.cmd_insights") as cmd_insights:
            daemon._run_weekly_report()

        cmd_insights.assert_not_called()

    def test_custom_weekly_schedule_is_used(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._notification_prefs_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "overrides": {
                        "weekly_insights": {
                            "weekday": "tuesday",
                            "time": "08:30",
                        }
                    },
                    "temporary_mutes": [],
                }
            ),
            encoding="utf-8",
        )
        fake_now = daemon_module.datetime(2026, 4, 7, 9, 0)
        fake_datetime = MagicMock()
        fake_datetime.now.return_value = fake_now

        with patch.object(daemon_module, "datetime", fake_datetime):
            prefs = daemon._load_notification_prefs(now=fake_now.astimezone())
            assert daemon_module.datetime.now.return_value == fake_now
            from notification_prefs import scheduled_report_due

            assert scheduled_report_due(
                prefs,
                "weekly_insights",
                now=fake_now.astimezone(),
            )

    def test_expired_mute_resumes_normal_behavior_without_replay(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._notification_prefs_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "overrides": {},
                    "temporary_mutes": [
                        {
                            "target": "nudges",
                            "expires_at": "2026-04-05T08:00:00+00:00",
                            "source_text": "mute nudges today",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        prefs = load_notification_prefs(
            daemon._notification_prefs_path,
            now=daemon_module.datetime.fromisoformat("2026-04-05T09:00:00+00:00"),
        )

        assert prefs["temporary_mutes"] == []

    def test_configured_nudge_cap_is_used(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._notification_prefs_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "overrides": {"nudges": {"max_per_day": 1}},
                    "temporary_mutes": [],
                }
            ),
            encoding="utf-8",
        )
        daemon._state["nudge_date"] = daemon_module.date.today().isoformat()
        daemon._state["nudge_count_today"] = 1

        assert daemon._can_send_nudge() is False


class TestCoachFeedbackFlow:
    def test_reject_records_feedback_and_prompts_for_reason(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "strategy.md").write_text(
            "## Weekly Structure\n\nKeep volume steady\n", encoding="utf-8"
        )
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 321
        daemon._chat._pending_edits = PendingEdits()

        edit = ContextEdit(
            file="strategy",
            action="replace_section",
            section="## Weekly Structure",
            content="## Weekly Structure\n\nCut volume by 20%\n",
            summary="Back off next week",
        )
        edit_id = daemon._pending_edits.store(edit, source="coach", preview="diff")

        daemon._handle_telegram_callback(
            {
                "id": "cb_1",
                "data": f"ctx_reject:{edit_id}",
                "message": {"message_id": 42},
            }
        )

        feedback = (tmp_path / "coach_feedback.md").read_text(encoding="utf-8")
        assert "Decision: rejected" in feedback
        assert "Source: coach" in feedback
        daemon._poller.send_reply.assert_called_once_with(
            "Optional: reply with why you rejected this suggestion.",
            reply_to_message_id=42,
            force_reply=True,
        )
        assert daemon._pending_rejection_reasons[321].startswith("cf_")
        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert "321" in state["pending_rejection_reasons"]

    def test_reason_reply_updates_matching_feedback_entry(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        edit = ContextEdit(
            file="strategy",
            action="replace_section",
            section="## Weekly Structure",
            content="## Weekly Structure\n\nCut volume by 20%\n",
            summary="Back off next week",
        )
        pending = PendingContextEdit(edit=edit, source="coach", preview="diff")
        entry = new_feedback_entry(pending, "rejected")
        append_coach_feedback(tmp_path, entry)
        daemon._pending_rejection_reasons[555] = entry.feedback_id

        handled = daemon._consume_rejection_reason(
            {"message_id": 555},
            "Travel week, so I want to keep the plan steady.",
        )

        assert handled is True
        content = (tmp_path / "coach_feedback.md").read_text(encoding="utf-8")
        assert "Reason: Travel week, so I want to keep the plan steady." in content

    def test_chat_proposal_keeps_chat_source_in_pending_edit(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "strategy.md").write_text(
            "## Weekly Structure\n\nKeep volume steady\n", encoding="utf-8"
        )
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._chat._pending_edits = PendingEdits()

        edit = ContextEdit(
            file="strategy",
            action="replace_section",
            section="## Weekly Structure",
            content="## Weekly Structure\n\nAdd a recovery day\n",
            summary="Add extra recovery day",
        )

        daemon._propose_context_edit(edit, source="chat")

        stored = next(iter(daemon._pending_edits._edits.values()))[0]
        assert stored.source == "chat"
        assert "+++ strategy.md (proposed)" in stored.preview


class TestTelegramFeedbackFlow:
    def test_fb_neg_swaps_to_category_keyboard(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        daemon._handle_telegram_callback(
            {
                "id": "cb_1",
                "data": "fb_neg:42:nudge",
                "message": {"message_id": 99},
            }
        )

        daemon._poller.edit_message_reply_markup.assert_called_once()
        buttons = daemon._poller.edit_message_reply_markup.call_args[0][1]
        callback_data = buttons[0][0]["callback_data"]
        assert callback_data == "fb_cat:42:nudge:inaccurate"

    def test_fb_cat_logs_reason_prompt_with_force_reply(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 555
        conn = open_db(tmp_path / "test.db")
        log_llm_call(
            conn,
            request_type="chat",
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_text="response",
        )

        daemon._handle_telegram_callback(
            {
                "id": "cb_2",
                "data": "fb_cat:1:chat:inaccurate",
                "message": {"message_id": 88, "text": "That run was solid."},
            }
        )

        row = conn.execute("SELECT * FROM llm_feedback").fetchone()
        assert row["llm_call_id"] == 1
        assert row["category"] == "inaccurate"
        assert row["message_type"] == "chat"
        assert daemon._pending_feedback_reasons[555] == row["id"]
        daemon._poller.send_reply.assert_called_once_with(
            "Reply to explain more (optional).",
            reply_to_message_id=88,
            force_reply=True,
        )
        daemon._poller.edit_message_with_keyboard.assert_called_once()

    def test_feedback_reason_persists_across_restart(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 777
        conn = open_db(tmp_path / "test.db")
        log_llm_call(
            conn,
            request_type="insights",
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_text="response",
        )

        daemon._handle_telegram_callback(
            {
                "id": "cb_3",
                "data": "fb_cat:1:insights:wrong_tone",
                "message": {"message_id": 90, "text": "Report footer"},
            }
        )

        restarted = _make_daemon(tmp_path)

        assert restarted._pending_feedback_reasons[777] > 0
        handled = restarted._consume_feedback_reason(
            {"message_id": 777},
            "This was too harsh after a decent week.",
        )

        row = conn.execute("SELECT reason FROM llm_feedback").fetchone()
        assert handled is True
        assert row["reason"] == "This was too harsh after a decent week."

    def test_fb_undo_deletes_feedback_and_restores_button(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        conn = open_db(tmp_path / "test.db")
        conn.execute(
            """
            INSERT INTO llm_call (
                timestamp, request_type, model, messages_json, response_text,
                params_json, input_tokens, output_tokens, total_tokens,
                latency_s, cost, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-04-03T10:00:00+00:00",
                "chat",
                "test-model",
                "[]",
                "response",
                None,
                0,
                0,
                0,
                0.1,
                None,
                None,
            ),
        )
        conn.commit()
        feedback_id = conn.execute(
            """
            INSERT INTO llm_feedback (llm_call_id, category, reason, created_at, message_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            (1, "inaccurate", None, "2026-04-03T10:01:00+00:00", "chat"),
        ).lastrowid
        conn.commit()
        daemon._pending_feedback_reasons[333] = feedback_id
        daemon._save_pending_reason_state()

        daemon._handle_telegram_callback(
            {
                "id": "cb_4",
                "data": f"fb_undo:{feedback_id}:1:chat:inaccurate",
                "message": {
                    "message_id": 50,
                    "text": "That run was solid.\n\n👎 Inaccurate",
                },
            }
        )

        remaining = conn.execute("SELECT COUNT(*) FROM llm_feedback").fetchone()[0]
        assert remaining == 0
        assert 333 not in daemon._pending_feedback_reasons
        daemon._poller.edit_message_with_keyboard.assert_called_once()
        restored_text = daemon._poller.edit_message_with_keyboard.call_args[0][1]
        assert restored_text == "That run was solid."

    def test_insights_feedback_edits_last_chunk(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        daemon._attach_feedback_button(
            CommandResult(text="report", llm_call_id=12, telegram_message_id=44),
            "insights",
        )

        daemon._poller.edit_message_reply_markup.assert_called_once()
        assert daemon._poller.edit_message_reply_markup.call_args.args[0] == 44
        daemon._poller.send_message_with_keyboard.assert_not_called()


class TestNotifyFlow:
    def test_notify_without_args_shows_summary(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        daemon._handle_command("/notify", 77)

        daemon._poller.send_reply.assert_called_once()
        sent = daemon._poller.send_reply.call_args.args[0]
        assert "Current notification settings:" in sent
        assert "Examples:" in sent

    def test_notify_accept_persists_json(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._notify_flow._pending_proposals["np_1"] = (
            daemon_module.PendingNotifyProposal(
                request_text="no nudges before 11am",
                preview="Proposed notification changes:\n- Nudge earliest time: 10:00 -> 11:00",
                summary="Move nudges to after 11:00.",
                changes=[
                    {
                        "action": "set",
                        "path": "nudges.earliest_time",
                        "value": "11:00",
                    }
                ],
            )
        )

        daemon._handle_telegram_callback(
            {
                "id": "cb_notify",
                "data": "notify_accept:np_1",
                "message": {"message_id": 10},
            }
        )

        prefs = load_notification_prefs(daemon._notification_prefs_path)
        assert prefs["overrides"]["nudges"]["earliest_time"] == "11:00"
        daemon._poller.edit_message.assert_called_once()

    def test_notify_reject_leaves_json_unchanged(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._notify_flow._pending_proposals["np_2"] = (
            daemon_module.PendingNotifyProposal(
                request_text="turn off midweek report",
                preview="Proposed notification changes:\n- Midweek report: Thursday 09:00 (on) -> Thursday 09:00 (off)",
                summary="Turn off midweek report.",
                changes=[
                    {
                        "action": "set",
                        "path": "midweek_report.enabled",
                        "value": False,
                    }
                ],
            )
        )

        daemon._handle_telegram_callback(
            {
                "id": "cb_notify_reject",
                "data": "notify_reject:np_2",
                "message": {"message_id": 12},
            }
        )

        prefs = load_notification_prefs(daemon._notification_prefs_path)
        assert prefs["overrides"] == {}
        daemon._poller.edit_message.assert_called_once()

    def test_notify_clarification_reply_continues_request(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._notify_flow._pending_clarifications[222] = (
            daemon_module.PendingNotifyClarification(
                request_text="move reports to Tuesday"
            )
        )

        with patch(
            "commands.interpret_notify_request",
            return_value={
                "status": "proposal",
                "intent": "set",
                "changes": [
                    {
                        "action": "set",
                        "path": "weekly_insights.weekday",
                        "value": "tuesday",
                    }
                ],
                "summary": "Move weekly insights to Tuesday.",
                "clarification_question": None,
                "reason": "clarified weekly insights",
            },
        ):
            handled = daemon._notify_flow.consume_clarification(
                {"message_id": 222},
                "weekly insights",
                {"message_id": 333},
            )

        assert handled is True
        daemon._poller.send_message_with_keyboard.assert_called_once()

    def test_stale_notify_proposal_expires_after_restart(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        daemon._handle_telegram_callback(
            {
                "id": "cb_notify_expired",
                "data": "notify_accept:missing",
                "message": {"message_id": 15},
            }
        )

        daemon._poller.edit_message.assert_called_once()


class TestTelegramCommands:
    def test_review_runs_last_week_insights_flow(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        with (
            patch.object(daemon, "_run_import"),
            patch(
                "commands.cmd_insights",
                return_value=CommandResult(text="report"),
            ) as cmd_insights,
            patch.object(daemon, "_attach_feedback_button"),
            patch.object(daemon, "_record_report"),
        ):
            daemon._handle_command("/review", 42)

        daemon._poller.send_reply.assert_called_once_with(
            "Running review for last week .",
            reply_to_message_id=42,
        )
        args = cmd_insights.call_args.args[0]
        assert args.week == "last"
        assert args.telegram is True

    def test_review_accepts_current_week_argument(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        with (
            patch.object(daemon, "_run_import"),
            patch(
                "commands.cmd_insights",
                return_value=CommandResult(text="report"),
            ) as cmd_insights,
            patch.object(daemon, "_attach_feedback_button"),
            patch.object(daemon, "_record_report"),
        ):
            daemon._handle_command("/review current", 24)

        daemon._poller.send_reply.assert_called_once_with(
            "Running review for this week so far .",
            reply_to_message_id=24,
        )
        args = cmd_insights.call_args.args[0]
        assert args.week == "current"

    def test_review_rejects_invalid_argument(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        with patch.object(daemon, "_run_review") as run_review:
            daemon._handle_command("/review tomorrow", 11)

        daemon._poller.send_reply.assert_called_once_with(
            "Use /review or /review current or /review last.",
            reply_to_message_id=11,
        )
        run_review.assert_not_called()

    def test_status_includes_system_and_data_summary(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state.update(
            {
                "nudge_count_today": 2,
                "last_nudge_ts": "2026-04-05T08:15:00+00:00",
                "last_report_ts": "2026-04-05T09:00:00+00:00",
                "last_coach_date": "2026-04-05",
                "quiet_queue": [{"trigger": "new_data"}],
            }
        )
        conn = open_db(tmp_path / "test.db")
        conn.execute(
            """
            INSERT INTO daily (date, steps, exercise_min, stand_hours, imported_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("2026-04-04", 10000, 45, 12, "2026-04-05T09:30:00+00:00"),
        )
        conn.execute(
            """
            INSERT INTO workout (
                start_utc, date, type, category, duration_min, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-04-04T07:00:00+00:00",
                "2026-04-04",
                "Outdoor Run",
                "run",
                45,
                "2026-04-05T09:30:00+00:00",
            ),
        )
        conn.commit()

        daemon._handle_command("/status", 77)

        daemon._poller.send_reply.assert_called_once()
        sent = daemon._poller.send_reply.call_args.args[0]
        assert "System status:" in sent
        assert "- Nudges today: 2/3" in sent
        assert "- Last report: 2026-04-05 " in sent
        assert "- Last coach run: 2026-04-05 " in sent
        assert "- Queued nudges: 1" in sent
        assert "- Active mutes: none" in sent
        assert "- Data: 1 days, 1 workouts (2026-04-04 to 2026-04-04)" in sent

    def test_status_handles_missing_state_fields(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        daemon._handle_command("/status", 88)

        daemon._poller.send_reply.assert_called_once()
        sent = daemon._poller.send_reply.call_args.args[0]
        assert "- Last nudge: never" in sent
        assert "- Last report: never" in sent
        assert "- Last coach run: never" in sent
        assert "- Data: database is empty" in sent

    def test_help_mentions_review_and_context_usage(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        (tmp_path / "me.md").write_text("About me\n", encoding="utf-8")

        daemon._handle_command("/help", 55)

        daemon._poller.send_reply.assert_called_once()
        sent = daemon._poller.send_reply.call_args.args[0]
        assert "/review [current|last] — Weekly report (default: last)" in sent
        assert "/context [name] — View context files" in sent
        assert "Available context files:" in sent
        assert "me" in sent


class TestFailureCapture:
    """Tests for _capture_last_error and _notify_user_failure.

    These exist because background command runs (review, nudge, coach) used
    to fail silently — only the daemon log recorded the error. We now
    forward the most recent ERROR-level log message to Telegram so the user
    knows what broke without reading daemon logs.
    """

    def test_capture_records_last_error_message(self) -> None:
        import logging

        test_logger = logging.getLogger("commands")
        with daemon_module._capture_last_error() as cap:
            test_logger.info("not captured")
            test_logger.error("first error")
            test_logger.error("second error")
        # Only the most recent ERROR should be retained.
        assert cap.last_message == "second error"

    def test_capture_ignores_non_error_levels(self) -> None:
        import logging

        test_logger = logging.getLogger("commands")
        with daemon_module._capture_last_error() as cap:
            test_logger.info("info")
            test_logger.warning("warn")
            test_logger.debug("debug")
        assert cap.last_message is None

    def test_capture_snapshot_pattern_isolates_underlying_error(self) -> None:
        """Regression: the daemon's own ``logger.error`` inside the except
        block must not clobber the captured underlying error. The fix is to
        snapshot ``cap.last_message`` *before* the daemon logs its own
        wrapper line — without the snapshot, the wrapper message overwrites
        the real one and the user only sees a useless 'X failed' line."""
        import logging

        cmd_logger = logging.getLogger("commands")
        wrapper_logger = logging.getLogger("daemon")
        captured: str | None = None
        with daemon_module._capture_last_error() as cap:
            try:
                cmd_logger.error("LLM call failed: BadRequestError details")
                raise SystemExit(1)
            except SystemExit:
                # MUST snapshot before the daemon's own error log line.
                captured = cap.last_message
                wrapper_logger.error("Manual review report failed (last)")
        # The snapshot preserves the real underlying error, not the
        # daemon's wrapper message.
        assert captured == "LLM call failed: BadRequestError details"
        # Sanity check: without the snapshot, cap.last_message would now
        # hold the wrapper message instead.
        assert cap.last_message == "Manual review report failed (last)"

    def test_capture_handler_removed_on_exception(self) -> None:
        import logging

        root = logging.getLogger()
        before = len(root.handlers)
        try:
            with daemon_module._capture_last_error():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        # The handler must be removed even when the wrapped block raises.
        assert len(root.handlers) == before

    def test_notify_user_failure_sends_truncated_error(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        daemon._notify_user_failure("Weekly review", "LLM call failed: details")

        daemon._poller.send_message_with_keyboard.assert_called_once()
        sent_text = daemon._poller.send_message_with_keyboard.call_args.args[0]
        assert "Weekly review failed" in sent_text
        assert "LLM call failed: details" in sent_text

    def test_notify_user_failure_truncates_long_errors(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        long_error = "x" * 1000
        daemon._notify_user_failure("Nudge", long_error)

        sent_text = daemon._poller.send_message_with_keyboard.call_args.args[0]
        # Should be truncated, not fully expanded.
        assert len(sent_text) < 700
        assert sent_text.endswith("...")

    def test_notify_user_failure_falls_back_when_no_error_text(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        daemon._notify_user_failure("Coaching review", None)

        sent_text = daemon._poller.send_message_with_keyboard.call_args.args[0]
        assert "Coaching review failed" in sent_text
        assert "check daemon logs" in sent_text

    def test_notify_user_failure_no_op_without_poller(self, tmp_path: Path) -> None:
        """If Telegram isn't configured, _poller is unset — the helper
        must do nothing rather than raising AttributeError."""
        daemon = _make_daemon(tmp_path)
        # Do not set daemon._poller — simulate no Telegram configured.
        # Should not raise.
        daemon._notify_user_failure("Manual review", "some error")
