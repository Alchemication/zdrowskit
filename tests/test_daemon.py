"""Tests for nudge scheduling, scheduled coach behavior, and Telegram feedback flow."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import daemon as daemon_module
import daemon_runners as daemon_runners_module
from cmd_llm import CommandResult
from context_edit import (
    ContextEdit,
    PendingContextEdit,
    PendingEdits,
    append_coach_feedback,
    new_feedback_entry,
)
from daemon import ZdrowskitDaemon
from events import query_events
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
                "cmd_llm.cmd_insights",
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
    def test_health_file_change_records_detected_event_before_debounce(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)

        class FakeTimer:
            instances: list["FakeTimer"] = []

            def __init__(self, interval: float, callback) -> None:
                self.interval = interval
                self.callback = callback
                self.cancelled = False
                self.started = False
                FakeTimer.instances.append(self)

            def cancel(self) -> None:
                self.cancelled = True

            def start(self) -> None:
                self.started = True

        with patch.object(daemon_module.threading, "Timer", FakeTimer):
            daemon._schedule_health()
            daemon._schedule_health()

        conn = open_db(tmp_path / "test.db")
        rows = query_events(conn, category="import")

        assert len(rows) == 1
        assert rows[0]["kind"] == "detected"
        assert "import scheduled" in rows[0]["summary"]
        assert rows[0]["details"]["debounce_s"] == daemon_module.HEALTH_DEBOUNCE_S
        assert daemon._health_debounce_count == 2
        assert len(FakeTimer.instances) == 2
        assert FakeTimer.instances[0].cancelled is True
        assert FakeTimer.instances[1].started is True

    def test_health_fire_records_started_event_with_debounced_count(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._health_debounce_count = 3

        with (
            patch.object(daemon._runners, "_data_snapshot", side_effect=[{}, {}]),
            patch.object(daemon._runners, "_run_import"),
            patch.object(
                daemon._runners, "_format_data_delta", return_value="No new rows"
            ),
            patch.object(daemon._runners, "_run_nudge"),
        ):
            daemon._fire_health()

        conn = open_db(tmp_path / "test.db")
        rows = query_events(conn, category="import")

        assert rows[0]["kind"] == "started"
        assert rows[0]["details"]["file_events"] == 3
        assert rows[0]["details"]["debounce_s"] == daemon_module.HEALTH_DEBOUNCE_S
        assert daemon._health_debounce_count == 0

    def test_run_nudge_queues_before_10am(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        fake_now = daemon_module.datetime(2026, 4, 5, 9, 30)
        fake_datetime = MagicMock()
        fake_datetime.now.return_value = fake_now

        with (
            patch.object(daemon_module, "datetime", fake_datetime),
            patch.object(daemon_runners_module, "datetime", fake_datetime),
            patch("cmd_llm.cmd_nudge") as cmd_nudge,
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

        with patch("cmd_llm.cmd_nudge") as cmd_nudge:
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

        with patch("cmd_llm.cmd_insights") as cmd_insights:
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
        daemon._state["nudge_date"] = daemon_module.datetime.now().date().isoformat()
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
            "cmd_llm.interpret_notify_request",
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


class TestLogFlow:
    @staticmethod
    def _two_step_flow():
        from cmd_llm import LogFlow, LogFlowStep

        return LogFlow(
            steps=[
                LogFlowStep(
                    id="state",
                    question="How did today feel?",
                    options=["solid", "tired"],
                    multi_select=False,
                    optional=False,
                ),
                LogFlowStep(
                    id="life",
                    question="Anything going on?",
                    options=["son sick", "travel"],
                    multi_select=True,
                    optional=True,
                    ask_end_date_if_selected=["son sick", "travel"],
                ),
            ],
            llm_call_id=None,
            model="test-model",
        )

    def test_log_flow_happy_path(self, tmp_path: Path) -> None:
        from datetime import date, timedelta

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        # Make the placeholder send return a known message id.
        daemon._poller.send_reply.return_value = 500

        log_path = tmp_path / "log.md"
        log_path.write_text("# Weekly Log\n\n## 2026-04-19\n\nOld entry.\n")

        with patch(
            "cmd_llm.build_log_flow", return_value=self._two_step_flow()
        ) as build_flow:
            daemon._handle_command("/log", 100)

        build_flow.assert_called_once()
        # One pending session exists keyed by a random token.
        assert len(daemon._log_flow._pending) == 1
        token = next(iter(daemon._log_flow._pending))
        # Pin session_date so the `until` assertion below is midnight-safe.
        session_date = date(2026, 4, 20)
        daemon._log_flow._pending[token].session_date = session_date

        # Tap "tired" on step 0.
        daemon._handle_telegram_callback(
            {
                "id": "cb1",
                "data": f"log_toggle:{token}:0:1",
                "message": {"message_id": 500},
            }
        )
        # Advance to step 1.
        daemon._handle_telegram_callback(
            {
                "id": "cb2",
                "data": f"log_next:{token}",
                "message": {"message_id": 500},
            }
        )
        # Select "travel" on step 1 (multi-select, triggers end-date picker).
        daemon._handle_telegram_callback(
            {
                "id": "cb3",
                "data": f"log_toggle:{token}:1:1",
                "message": {"message_id": 500},
            }
        )
        # Tap done — should switch to end-date picker first.
        daemon._handle_telegram_callback(
            {
                "id": "cb4",
                "data": f"log_done:{token}",
                "message": {"message_id": 500},
            }
        )
        pending = daemon._log_flow._pending[token]
        assert pending.awaiting_end_date is True

        # Pick "tomorrow" — commits the bullet.
        daemon._handle_telegram_callback(
            {
                "id": "cb5",
                "data": f"log_enddate:{token}:tomorrow",
                "message": {"message_id": 500},
            }
        )

        # Session cleared; log.md has the appended bullet with `until`.
        assert token not in daemon._log_flow._pending
        content = log_path.read_text()
        assert "[tired]" in content
        assert "[travel]" in content
        tomorrow = (session_date + timedelta(days=1)).isoformat()
        assert f"until {tomorrow}" in content

    def test_log_flow_expired_session_message(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        daemon._handle_telegram_callback(
            {
                "id": "cb_expired",
                "data": "log_toggle:lf_nonexistent:0:0",
                "message": {"message_id": 999},
            }
        )

        daemon._poller.edit_message.assert_called_once()
        msg = daemon._poller.edit_message.call_args.args[1]
        assert "expired" in msg.lower()

    def test_log_flow_note_intercept(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 700

        log_path = tmp_path / "log.md"
        log_path.write_text("# Weekly Log\n")

        one_step = self._two_step_flow().steps[0]
        from cmd_llm import LogFlow

        simple_flow = LogFlow(steps=[one_step], llm_call_id=None, model="t")
        with (
            patch("cmd_llm.build_log_flow", return_value=simple_flow),
            patch("cmd_llm.build_log_step_followup", return_value=None),
        ):
            daemon._handle_command("/log", 200)
            token = next(iter(daemon._log_flow._pending))

            # Select an option so done can commit.
            daemon._handle_telegram_callback(
                {
                    "id": "cb_sel",
                    "data": f"log_toggle:{token}:0:0",
                    "message": {"message_id": 700},
                }
            )
            # Tap `+ note` — session now awaits free-text.
            daemon._handle_telegram_callback(
                {
                    "id": "cb_note",
                    "data": f"log_note:{token}",
                    "message": {"message_id": 700},
                }
            )
            assert daemon._log_flow._awaiting_note_token == token
            assert daemon._log_flow._pending[token].awaiting_note is True

            # Patch the chat LLM path so we can detect it was NOT called.
            with patch.object(daemon._chat, "_chat_reply") as chat_reply:
                daemon._handle_telegram_message(
                    {"message_id": 800, "text": "legs felt heavy on the warm-up"}
                )

            chat_reply.assert_not_called()
            pending = daemon._log_flow._pending[token]
            assert pending.note == "legs felt heavy on the warm-up"
            assert pending.awaiting_note is False

            # Finish with done — note appears in bullet tail.
            daemon._handle_telegram_callback(
                {
                    "id": "cb_done",
                    "data": f"log_done:{token}",
                    "message": {"message_id": 700},
                }
            )
        assert token not in daemon._log_flow._pending
        content = log_path.read_text()
        assert "legs felt heavy on the warm-up" in content

    def test_log_flow_note_wait_state_ignores_commands(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 710

        one_step = self._two_step_flow().steps[0]
        from cmd_llm import LogFlow

        simple_flow = LogFlow(steps=[one_step], llm_call_id=None, model="t")
        with patch("cmd_llm.build_log_flow", return_value=simple_flow):
            daemon._handle_command("/log", 201)
        token = next(iter(daemon._log_flow._pending))

        daemon._handle_telegram_callback(
            {
                "id": "cb_note",
                "data": f"log_note:{token}",
                "message": {"message_id": 710},
            }
        )

        with patch.object(daemon, "_build_status_lines", return_value=["bot ok"]):
            daemon._handle_telegram_message({"message_id": 801, "text": "/status"})

        pending = daemon._log_flow._pending[token]
        assert pending.awaiting_note is True
        assert pending.note is None
        daemon._poller.send_reply.assert_any_call("bot ok", reply_to_message_id=801)

    def test_log_flow_uses_session_date_for_end_date_and_commit(
        self, tmp_path: Path
    ) -> None:
        from datetime import date, timedelta

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 720

        log_path = tmp_path / "log.md"
        log_path.write_text("# Weekly Log\n")

        with patch("cmd_llm.build_log_flow", return_value=self._two_step_flow()):
            daemon._handle_command("/log", 202)
        token = next(iter(daemon._log_flow._pending))
        daemon._log_flow._pending[token].session_date = date(2026, 4, 19)

        daemon._handle_telegram_callback(
            {
                "id": "cb_s0",
                "data": f"log_toggle:{token}:0:1",
                "message": {"message_id": 720},
            }
        )
        daemon._handle_telegram_callback(
            {
                "id": "cb_next",
                "data": f"log_next:{token}",
                "message": {"message_id": 720},
            }
        )
        daemon._handle_telegram_callback(
            {
                "id": "cb_s1",
                "data": f"log_toggle:{token}:1:1",
                "message": {"message_id": 720},
            }
        )
        daemon._handle_telegram_callback(
            {
                "id": "cb_done",
                "data": f"log_done:{token}",
                "message": {"message_id": 720},
            }
        )
        daemon._handle_telegram_callback(
            {
                "id": "cb_end",
                "data": f"log_enddate:{token}:tomorrow",
                "message": {"message_id": 720},
            }
        )

        content = log_path.read_text()
        assert "- 2026-04-19" in content
        assert f"until {(date(2026, 4, 19) + timedelta(days=1)).isoformat()}" in content

    def test_log_flow_cancel_clears_session_and_note_wait_state(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 730

        one_step = self._two_step_flow().steps[0]
        from cmd_llm import LogFlow

        simple_flow = LogFlow(steps=[one_step], llm_call_id=None, model="t")
        with patch("cmd_llm.build_log_flow", return_value=simple_flow):
            daemon._handle_command("/log", 203)
        token = next(iter(daemon._log_flow._pending))

        daemon._handle_telegram_callback(
            {
                "id": "cb_note",
                "data": f"log_note:{token}",
                "message": {"message_id": 730},
            }
        )
        assert daemon._log_flow._awaiting_note_token == token

        daemon._handle_telegram_callback(
            {
                "id": "cb_cancel",
                "data": f"log_cancel:{token}",
                "message": {"message_id": 730},
            }
        )

        assert token not in daemon._log_flow._pending
        assert daemon._log_flow._awaiting_note_token is None
        daemon._poller.edit_message.assert_any_call(730, "Cancelled.")
        daemon._poller.edit_message_reply_markup.assert_any_call(730, None)

    def test_log_flow_done_rejects_empty_optional_submission(
        self, tmp_path: Path
    ) -> None:
        from cmd_llm import LogFlow, LogFlowStep

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 740

        optional_flow = LogFlow(
            steps=[
                LogFlowStep(
                    id="life",
                    question="Anything going on?",
                    options=["travel", "sick"],
                    multi_select=True,
                    optional=True,
                )
            ],
            llm_call_id=None,
            model="t",
        )
        log_path = tmp_path / "log.md"
        log_path.write_text("# Weekly Log\n")

        with patch("cmd_llm.build_log_flow", return_value=optional_flow):
            daemon._handle_command("/log", 210)
        token = next(iter(daemon._log_flow._pending))

        daemon._handle_telegram_callback(
            {
                "id": "cb_done_empty",
                "data": f"log_done:{token}",
                "message": {"message_id": 740},
            }
        )

        # Session still open; nothing written; user told to pick or note.
        assert token in daemon._log_flow._pending
        assert log_path.read_text() == "# Weekly Log\n"
        daemon._poller.answer_callback_query.assert_any_call(
            "cb_done_empty", "Pick at least one option or add a note."
        )

    def test_log_flow_truncates_long_note_to_bullet_cap(self, tmp_path: Path) -> None:
        from datetime import date

        from context_edit import MAX_LOG_BULLET_CHARS, _validate_log_append_content

        from cmd_llm import LogFlow, LogFlowStep

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 750

        flow = LogFlow(
            steps=[
                LogFlowStep(
                    id="state",
                    question="How did today feel?",
                    options=["solid", "tired"],
                    multi_select=False,
                    optional=False,
                )
            ],
            llm_call_id=None,
            model="t",
        )
        log_path = tmp_path / "log.md"
        log_path.write_text("# Weekly Log\n")

        with (
            patch("cmd_llm.build_log_flow", return_value=flow),
            patch("cmd_llm.build_log_step_followup", return_value=None),
        ):
            daemon._handle_command("/log", 211)
            token = next(iter(daemon._log_flow._pending))
            daemon._log_flow._pending[token].session_date = date(2026, 4, 20)

            daemon._handle_telegram_callback(
                {
                    "id": "cb_sel",
                    "data": f"log_toggle:{token}:0:0",
                    "message": {"message_id": 750},
                }
            )
            # Long note with embedded newlines — must collapse and fit under cap.
            long_note = ("legs heavy\nbut\n" + "word " * 60).strip()
            daemon._log_flow._pending[token].note = long_note
            daemon._handle_telegram_callback(
                {
                    "id": "cb_done",
                    "data": f"log_done:{token}",
                    "message": {"message_id": 750},
                }
            )

        bullet = log_path.read_text().strip().splitlines()[-1]
        assert len(bullet) <= MAX_LOG_BULLET_CHARS
        assert "\n" not in bullet
        assert bullet.endswith("\u2026")
        # Belt-and-suspenders: the bullet should satisfy the same validator
        # the LLM `update_context` path uses.
        _validate_log_append_content(bullet)

    @staticmethod
    def _one_step_flow():
        from cmd_llm import LogFlow, LogFlowStep

        return LogFlow(
            steps=[
                LogFlowStep(
                    id="state",
                    question="How did today feel?",
                    options=["solid", "tired"],
                    multi_select=False,
                    optional=False,
                )
            ],
            llm_call_id=None,
            model="t",
        )

    def test_log_flow_reactive_followup_appends_step_2(self, tmp_path: Path) -> None:
        from cmd_llm import LogFlowStep

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 810

        log_path = tmp_path / "log.md"
        log_path.write_text("# Weekly Log\n")

        followup_step = LogFlowStep(
            id="life",
            question="What dragged it?",
            options=["sleep poor", "stress work"],
            multi_select=True,
            optional=True,
        )
        with (
            patch("cmd_llm.build_log_flow", return_value=self._one_step_flow()),
            patch(
                "cmd_llm.build_log_step_followup", return_value=followup_step
            ) as followup,
        ):
            daemon._handle_command("/log", 220)
            token = next(iter(daemon._log_flow._pending))

            # Pick "tired" on step 0.
            daemon._handle_telegram_callback(
                {
                    "id": "cb_sel",
                    "data": f"log_toggle:{token}:0:1",
                    "message": {"message_id": 810},
                }
            )
            # Tap done — should fire followup, append step 2, not commit yet.
            daemon._handle_telegram_callback(
                {
                    "id": "cb_done0",
                    "data": f"log_done:{token}",
                    "message": {"message_id": 810},
                }
            )

            followup.assert_called_once()
            call_kwargs = followup.call_args.kwargs
            assert call_kwargs["prior_answer"] == ["tired"]
            assert call_kwargs["prior_step"].id == "state"

            pending = daemon._log_flow._pending[token]
            assert pending.followup_consulted is True
            assert len(pending.flow.steps) == 2
            assert pending.step_index == 1
            # Nothing written yet — we're still interviewing.
            assert log_path.read_text() == "# Weekly Log\n"

            # Pick "sleep poor" on the appended step and finish.
            daemon._handle_telegram_callback(
                {
                    "id": "cb_sel1",
                    "data": f"log_toggle:{token}:1:0",
                    "message": {"message_id": 810},
                }
            )
            daemon._handle_telegram_callback(
                {
                    "id": "cb_done1",
                    "data": f"log_done:{token}",
                    "message": {"message_id": 810},
                }
            )

            # Second done should NOT fire followup again.
            assert followup.call_count == 1

        assert token not in daemon._log_flow._pending
        content = log_path.read_text()
        assert "[tired]" in content
        assert "[sleep poor]" in content

    def test_log_flow_reactive_followup_null_commits_immediately(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 820

        log_path = tmp_path / "log.md"
        log_path.write_text("# Weekly Log\n")

        with (
            patch("cmd_llm.build_log_flow", return_value=self._one_step_flow()),
            patch("cmd_llm.build_log_step_followup", return_value=None) as followup,
        ):
            daemon._handle_command("/log", 221)
            token = next(iter(daemon._log_flow._pending))

            daemon._handle_telegram_callback(
                {
                    "id": "cb_sel",
                    "data": f"log_toggle:{token}:0:0",
                    "message": {"message_id": 820},
                }
            )
            daemon._handle_telegram_callback(
                {
                    "id": "cb_done",
                    "data": f"log_done:{token}",
                    "message": {"message_id": 820},
                }
            )

        followup.assert_called_once()
        assert token not in daemon._log_flow._pending
        content = log_path.read_text()
        assert "[solid]" in content

    def test_log_flow_reactive_followup_exception_falls_through_to_commit(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 830

        log_path = tmp_path / "log.md"
        log_path.write_text("# Weekly Log\n")

        with (
            patch("cmd_llm.build_log_flow", return_value=self._one_step_flow()),
            patch(
                "cmd_llm.build_log_step_followup",
                side_effect=RuntimeError("llm down"),
            ),
        ):
            daemon._handle_command("/log", 222)
            token = next(iter(daemon._log_flow._pending))

            daemon._handle_telegram_callback(
                {
                    "id": "cb_sel",
                    "data": f"log_toggle:{token}:0:0",
                    "message": {"message_id": 830},
                }
            )
            daemon._handle_telegram_callback(
                {
                    "id": "cb_done",
                    "data": f"log_done:{token}",
                    "message": {"message_id": 830},
                }
            )

        # LLM failure should not block the bullet — we commit anyway.
        assert token not in daemon._log_flow._pending
        content = log_path.read_text()
        assert "[solid]" in content


class TestTelegramCommands:
    def test_review_runs_last_week_insights_flow(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        with (
            patch.object(daemon, "_run_import"),
            patch(
                "cmd_llm.cmd_insights",
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
                "cmd_llm.cmd_insights",
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


class TestModelsFlow:
    def test_models_command_shows_button_panel(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        prefs_path = tmp_path / "model_prefs.json"

        with patch("model_prefs.MODEL_PREFS_PATH", prefs_path):
            daemon._handle_command("/models", 90)

        daemon._poller.send_message_with_keyboard.assert_called_once()
        text = daemon._poller.send_message_with_keyboard.call_args.args[0]
        buttons = daemon._poller.send_message_with_keyboard.call_args.args[1]
        assert "Model routes:" in text
        labels = [button["text"] for row in buttons for button in row]
        assert any("Chat" in label for label in labels)
        assert any("Reset all" in label for label in labels)
        assert any(label == "❌ cancel" for label in labels)

    def test_models_cancel_clears_keyboard(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        daemon._handle_telegram_callback(
            {
                "id": "cb_model_cancel",
                "data": "model_cancel",
                "message": {"message_id": 901},
            }
        )

        daemon._poller.answer_callback_query.assert_called_with(
            "cb_model_cancel", "Cancelled."
        )
        daemon._poller.edit_message.assert_called_with(901, "Cancelled.")
        daemon._poller.edit_message_reply_markup.assert_called_with(901, None)

    def test_models_chat_group_panel_offers_reasoning_and_temperature(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        prefs_path = tmp_path / "model_prefs.json"

        with patch("model_prefs.MODEL_PREFS_PATH", prefs_path):
            daemon._handle_telegram_callback(
                {
                    "id": "cb_group_chat",
                    "data": "model_group:chat",
                    "message": {"message_id": 902},
                }
            )

        daemon._poller.edit_message_with_keyboard.assert_called_once()
        kb = daemon._poller.edit_message_with_keyboard.call_args.args[2]
        labels = [button["text"] for row in kb for button in row]
        assert any("Reasoning" in label for label in labels)
        assert any("Temperature" in label for label in labels)
        assert any("Change model" in label for label in labels)
        assert any("Reset" in label for label in labels)

    def test_models_set_reasoning_persists_choice(self, tmp_path: Path) -> None:
        from model_prefs import resolve_model_route

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        prefs_path = tmp_path / "model_prefs.json"

        with patch("model_prefs.MODEL_PREFS_PATH", prefs_path):
            daemon._handle_telegram_callback(
                {
                    "id": "cb_reason",
                    "data": "model_set_reasoning:chat:medium",
                    "message": {"message_id": 902},
                }
            )
            route = resolve_model_route("chat")

        assert route.reasoning_effort == "medium"

    def test_models_set_temperature_persists_choice(self, tmp_path: Path) -> None:
        from model_prefs import resolve_model_route

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        prefs_path = tmp_path / "model_prefs.json"

        with patch("model_prefs.MODEL_PREFS_PATH", prefs_path):
            daemon._handle_telegram_callback(
                {
                    "id": "cb_temp",
                    "data": "model_set_temperature:chat:0.3",
                    "message": {"message_id": 902},
                }
            )
            route = resolve_model_route("chat")

        assert route.temperature == 0.3

    def test_models_reset_all_restores_defaults(self, tmp_path: Path) -> None:
        from config import ANTHROPIC_OPUS_4_7_MODEL, PRIMARY_PRO_MODEL
        from model_prefs import resolve_model_route, set_feature_route

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        prefs_path = tmp_path / "model_prefs.json"

        with patch("model_prefs.MODEL_PREFS_PATH", prefs_path):
            set_feature_route("chat", primary=PRIMARY_PRO_MODEL)
            daemon._handle_telegram_callback(
                {
                    "id": "cb_reset_all",
                    "data": "model_reset_all",
                    "message": {"message_id": 902},
                }
            )
            route = resolve_model_route("chat")

        assert route.primary == ANTHROPIC_OPUS_4_7_MODEL

    def test_models_auto_fallback_falls_through_to_profile(
        self, tmp_path: Path
    ) -> None:
        from config import (
            ANTHROPIC_OPUS_4_7_MODEL,
            FALLBACK_PRO_MODEL,
            PRIMARY_PRO_MODEL,
        )
        from model_prefs import resolve_model_route, selectable_models

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        prefs_path = tmp_path / "model_prefs.json"
        models = selectable_models()
        primary_idx = models.index(ANTHROPIC_OPUS_4_7_MODEL)

        with patch("model_prefs.MODEL_PREFS_PATH", prefs_path):
            daemon._handle_telegram_callback(
                {
                    "id": "cb_primary",
                    "data": f"model_primary:nudges:{primary_idx}",
                    "message": {"message_id": 903},
                }
            )
            daemon._handle_telegram_callback(
                {
                    "id": "cb_fallback",
                    "data": f"model_fallback:nudges:{primary_idx}:auto",
                    "message": {"message_id": 903},
                }
            )
            token = next(iter(daemon._model_flow._pending))
            daemon._handle_telegram_callback(
                {
                    "id": "cb_accept",
                    "data": f"model_accept:{token}",
                    "message": {"message_id": 903},
                }
            )
            route = resolve_model_route("nudge")

        # "Auto" applies the profile fallback (resolved at read time, not stored).
        assert route.primary == ANTHROPIC_OPUS_4_7_MODEL
        assert route.fallback == FALLBACK_PRO_MODEL
        # The "pro" profile's primary remains the configured default.
        assert resolve_model_route("insights").primary == PRIMARY_PRO_MODEL

    def test_models_primary_fallback_accept_flow(self, tmp_path: Path) -> None:
        from config import ANTHROPIC_HAIKU_MODEL, PRIMARY_FLASH_MODEL
        from model_prefs import resolve_model_route, selectable_models

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        prefs_path = tmp_path / "model_prefs.json"
        models = selectable_models()
        primary_idx = models.index(PRIMARY_FLASH_MODEL)
        fallback_idx = models.index(ANTHROPIC_HAIKU_MODEL)

        with patch("model_prefs.MODEL_PREFS_PATH", prefs_path):
            daemon._handle_telegram_callback(
                {
                    "id": "cb_primary",
                    "data": f"model_primary:nudges:{primary_idx}",
                    "message": {"message_id": 903},
                }
            )
            daemon._handle_telegram_callback(
                {
                    "id": "cb_fallback",
                    "data": f"model_fallback:nudges:{primary_idx}:{fallback_idx}",
                    "message": {"message_id": 903},
                }
            )
            token = next(iter(daemon._model_flow._pending))
            daemon._handle_telegram_callback(
                {
                    "id": "cb_accept",
                    "data": f"model_accept:{token}",
                    "message": {"message_id": 903},
                }
            )
            route = resolve_model_route("nudge")

        assert route.primary == PRIMARY_FLASH_MODEL
        assert route.fallback == ANTHROPIC_HAIKU_MODEL

    def test_models_malformed_callback_logs_warning(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        daemon._handle_telegram_callback(
            {
                "id": "cb_bad",
                "data": "model_primary:nudges:notanint",
                "message": {"message_id": 904},
            }
        )

        daemon._poller.answer_callback_query.assert_called_with(
            "cb_bad", "Invalid action."
        )


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
