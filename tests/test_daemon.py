"""Tests for nudge scheduling, scheduled coach behavior, and Telegram feedback flow."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import daemon as daemon_module
import notification_prefs as notification_prefs_module
import daemon_runners as daemon_runners_module
from cmd_llm_common import InsufficientWeekData
from cmd_llm_common import CommandResult
from context_edit import (
    ContextEdit,
    PendingContextEdit,
    PendingEdits,
    append_coach_feedback,
    new_feedback_entry,
)
from daemon import ProfileRuntime
from daemon_telegram_chat import _looks_like_internal_tool_markup
from events import query_events, query_telegram_usage
from llm import LLMResult
from notification_prefs import load_notification_prefs
from store import create_llm_trace, log_llm_call, open_db


def _make_daemon(tmp_path: Path) -> ProfileRuntime:
    daemon = ProfileRuntime(
        "test-model",
        tmp_path / "test.db",
        tmp_path,
        state_path=tmp_path / "state.json",
    )
    daemon._notification_prefs_path = tmp_path / "notification_prefs.json"
    daemon.model_prefs_path = tmp_path / "model_prefs.json"
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
                "cmd_insights.cmd_insights",
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

    def test_weekly_report_failure_suppresses_same_day_retry(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)

        with (
            patch.object(daemon, "_run_import"),
            patch("cmd_insights.cmd_insights", side_effect=SystemExit(1)),
            patch.object(daemon, "_notify_user_failure") as notify_failure,
        ):
            daemon._run_weekly_report()

        today = daemon_runners_module.date.today().isoformat()
        assert daemon._state["last_review_skip_date"] == today
        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert state["last_review_skip_date"] == today
        notify_failure.assert_called_once_with(
            "Weekly review",
            None,
            detail=None,
        )

    def test_weekly_report_skips_quietly_when_the_week_has_no_data(
        self, tmp_path: Path
    ) -> None:
        """A profile onboarded mid-week must not be told its report failed.

        The condition resolves itself as data accumulates, so it is a skip,
        not a failure — otherwise every new user gets an error notification on
        every scheduled run until their first complete week lands.
        """
        daemon = _make_daemon(tmp_path)

        with (
            patch.object(daemon, "_run_import"),
            patch(
                "cmd_insights.cmd_insights",
                side_effect=InsufficientWeekData("No health data for 2026-W31."),
            ),
            patch.object(daemon, "_notify_user_failure") as notify_failure,
            patch.object(daemon, "_record_event") as record_event,
        ):
            daemon._run_weekly_report()

        notify_failure.assert_not_called()
        kinds = [call.args[1] for call in record_event.call_args_list]
        assert "insufficient_data" in kinds
        today = daemon_runners_module.date.today().isoformat()
        assert daemon._state["last_review_skip_date"] == today

    def test_weekly_report_does_not_re_run_after_failure_same_day(
        self, tmp_path: Path
    ) -> None:
        """Guard the retry-spam regression: a failed run must mark the day
        as skipped so the next scheduler tick is a no-op."""
        daemon = _make_daemon(tmp_path)
        insights = MagicMock(side_effect=SystemExit(1))

        with (
            patch.object(daemon, "_run_import"),
            patch("cmd_insights.cmd_insights", insights),
            patch.object(daemon, "_notify_user_failure"),
        ):
            daemon._run_weekly_report()
            daemon._run_weekly_report()

        assert insights.call_count == 1

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
            patch("cmd_nudge.cmd_nudge") as cmd_nudge,
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

        with patch("cmd_nudge.cmd_nudge") as cmd_nudge:
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

        with patch("cmd_insights.cmd_insights") as cmd_insights:
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


class TestChatReplyLoop:
    def test_chat_uses_static_working_placeholder(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._chat._conversation = MagicMock()
        daemon._poller.send_reply.return_value = 900
        result = LLMResult(
            text="Push done, pull still outstanding.",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )

        with (
            patch.object(daemon._chat, "_chat_reply", return_value=(result, [], [])),
            patch.object(daemon._chat, "_start_placeholder_animation") as start_anim,
        ):
            daemon._handle_telegram_message(
                {"message_id": 1000, "text": "Did I do both strength sessions?"}
            )

        daemon._poller.send_reply.assert_called_once_with(
            "Working\u2026", reply_to_message_id=1000
        )
        start_anim.assert_not_called()
        daemon._poller.edit_message.assert_called_once_with(
            900, "Push done, pull still outstanding."
        )

    def test_detects_internal_tool_markup(self) -> None:
        assert _looks_like_internal_tool_markup("<｜｜DSML｜｜tool_calls>")
        assert _looks_like_internal_tool_markup('<tool_call name="run_sql">')
        assert _looks_like_internal_tool_markup('{"tool_calls": []}')
        assert not _looks_like_internal_tool_markup("W01: **6:04/km**")

    def test_final_synthesis_retries_internal_tool_markup(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._conversation = MagicMock()
        daemon._chat._conversation.to_messages.return_value = [
            {
                "role": "user",
                "content": "What's my avg running speed per km by week in 2026?",
            }
        ]
        conn = open_db(daemon.db)

        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="run_sql",
                arguments='{"query": "SELECT 1"}',
            ),
        )
        first_result = LLMResult(
            text="",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            tool_calls=[tool_call],
            raw_message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "run_sql",
                            "arguments": '{"query": "SELECT 1"}',
                        },
                    }
                ],
            },
        )
        markup_result = LLMResult(
            text=(
                "<｜｜DSML｜｜tool_calls>\n"
                '<｜｜DSML｜｜invoke name="run_sql">\n'
                "</｜｜DSML｜｜invoke>\n"
                "</｜｜DSML｜｜tool_calls>"
            ),
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            raw_message={
                "role": "assistant",
                "content": "<｜｜DSML｜｜tool_calls>",
            },
        )
        retry_result = LLMResult(
            text="By week: **W01 6:04/km**, **W02 5:58/km**.",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )

        seen_messages: list[list[dict]] = []
        seen_kwargs: list[dict] = []

        def fake_call_llm(messages: list[dict], **kwargs: object) -> LLMResult:
            seen_messages.append([dict(message) for message in messages])
            seen_kwargs.append(dict(kwargs))
            return [first_result, markup_result, retry_result][len(seen_messages) - 1]

        with (
            patch("config.MAX_TOOL_ITERATIONS", 1),
            patch(
                "llm_context.load_context", return_value={"prompt": "p", "soul": "s"}
            ),
            patch(
                "llm_context.build_messages",
                return_value=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "context"},
                ],
            ),
            patch(
                "llm_health.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W19"}},
                    "history": [],
                    "week_complete": False,
                },
            ),
            patch("llm_health.render_health_data", return_value="health"),
            patch("baselines.compute_baselines", return_value=None),
            patch("tools.all_chat_tools", return_value=[{"type": "function"}]),
            patch("tools.execute_tool", return_value='[{"week": "2026-W01"}]'),
            patch("llm.call_llm", side_effect=fake_call_llm),
        ):
            result, _edits, rows = daemon._chat._chat_reply(conn)

        assert result.text == "By week: **W01 6:04/km**, **W02 5:58/km**."
        assert rows == [{"week": "2026-W01"}]
        assert seen_kwargs[1]["tools"] is None
        assert seen_kwargs[1]["metadata"] == {"iteration": "final_synthesis"}
        assert seen_kwargs[2]["tools"] is None
        assert seen_kwargs[2]["metadata"] == {
            "iteration": "final_synthesis_tool_markup_retry"
        }

        final_synthesis_messages = seen_messages[1]
        protocol_messages = [
            message
            for message in final_synthesis_messages
            if message.get("role") == "tool"
            or (message.get("role") == "assistant" and message.get("tool_calls"))
        ]
        assert protocol_messages == []
        assert final_synthesis_messages[-1]["role"] == "user"
        assert (
            "tool budget exhausted, answer now"
            in final_synthesis_messages[-1]["content"]
        )
        assert '[{"week": "2026-W01"}]' in final_synthesis_messages[-1]["content"]

    def test_final_markup_fallback_updates_logged_response(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._conversation = MagicMock()
        daemon._chat._conversation.to_messages.return_value = [
            {"role": "user", "content": "What's my running pace trend?"}
        ]
        conn = open_db(daemon.db)
        retry_call_id = log_llm_call(
            conn,
            request_type="chat",
            model="test-model",
            messages=[{"role": "user", "content": "retry"}],
            response_text="<｜｜DSML｜｜tool_calls>",
            metadata={"iteration": "final_synthesis_tool_markup_retry"},
        )

        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="run_sql",
                arguments='{"query": "SELECT 1"}',
            ),
        )
        first_result = LLMResult(
            text="",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            tool_calls=[tool_call],
            raw_message={"role": "assistant", "content": "", "tool_calls": []},
        )
        markup_result = LLMResult(
            text="<｜｜DSML｜｜tool_calls>",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )
        retry_markup_result = LLMResult(
            text="<｜｜DSML｜｜tool_calls>",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            llm_call_id=retry_call_id,
        )

        def fake_call_llm(messages: list[dict], **kwargs: object) -> LLMResult:
            return [first_result, markup_result, retry_markup_result][
                fake_call_llm.call_count
            ]

        fake_call_llm.call_count = 0

        def counted_call_llm(messages: list[dict], **kwargs: object) -> LLMResult:
            result = fake_call_llm(messages, **kwargs)
            fake_call_llm.call_count += 1
            return result

        with (
            patch("config.MAX_TOOL_ITERATIONS", 1),
            patch(
                "llm_context.load_context", return_value={"prompt": "p", "soul": "s"}
            ),
            patch(
                "llm_context.build_messages",
                return_value=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "context"},
                ],
            ),
            patch(
                "llm_health.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W19"}},
                    "history": [],
                    "week_complete": False,
                },
            ),
            patch("llm_health.render_health_data", return_value="health"),
            patch("baselines.compute_baselines", return_value=None),
            patch("tools.all_chat_tools", return_value=[{"type": "function"}]),
            patch("tools.execute_tool", return_value='[{"week": "2026-W01"}]'),
            patch("llm.call_llm", side_effect=counted_call_llm),
        ):
            result, _edits, _rows = daemon._chat._chat_reply(conn)

        assert (
            result.text
            == "I couldn't turn the tool results into a clean Telegram reply. Try again with a narrower question."
        )
        row = conn.execute(
            "SELECT response_text, metadata_json FROM llm_call WHERE id = ?",
            (retry_call_id,),
        ).fetchone()
        assert row["response_text"] == result.text
        metadata = json.loads(row["metadata_json"])
        assert metadata["postprocessed_response_text"] is True
        assert metadata["postprocess_reason"] == "internal_tool_markup_fallback"
        assert metadata["raw_response_text"] == "<｜｜DSML｜｜tool_calls>"

    def test_invalid_context_update_reports_failure_and_allows_retry(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._conversation = MagicMock()
        daemon._chat._conversation.to_messages.return_value = [
            {
                "role": "user",
                "content": ("Weekends are harder than weekdays with family logistics."),
            }
        ]
        conn = open_db(daemon.db)

        invalid_args = json.dumps(
            {
                "file": "log",
                "action": "append",
                "content": "- 2026-05-17 — " + ("weekend logistics " * 12),
                "summary": "Log weekend family constraint",
            }
        )
        valid_args = json.dumps(
            {
                "file": "log",
                "action": "append",
                "content": (
                    "- 2026-05-17 — family logistics make weekends harder; "
                    "prefer key sessions on weekdays"
                ),
                "summary": "Log weekend family constraint",
            }
        )
        invalid_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="update_context", arguments=invalid_args),
        )
        valid_call = SimpleNamespace(
            id="call_2",
            function=SimpleNamespace(name="update_context", arguments=valid_args),
        )
        invalid_result = LLMResult(
            text="",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            tool_calls=[invalid_call],
            raw_message={
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1"}],
            },
        )
        retry_result = LLMResult(
            text="",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            tool_calls=[valid_call],
            raw_message={
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_2"}],
            },
        )
        final_result = LLMResult(
            text="Queued a shorter note for review.",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )

        seen_messages: list[list[dict]] = []

        def fake_call_llm(messages: list[dict], **_kwargs: object) -> LLMResult:
            seen_messages.append([dict(message) for message in messages])
            return [invalid_result, retry_result, final_result][len(seen_messages) - 1]

        with (
            patch("config.MAX_TOOL_ITERATIONS", 8),
            patch(
                "llm_context.load_context", return_value={"prompt": "p", "soul": "s"}
            ),
            patch(
                "llm_context.build_messages",
                return_value=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "context"},
                ],
            ),
            patch(
                "llm_health.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W20"}},
                    "history": [],
                    "week_complete": False,
                },
            ),
            patch("llm_health.render_health_data", return_value="health"),
            patch("baselines.compute_baselines", return_value=None),
            patch("tools.all_chat_tools", return_value=[{"type": "function"}]),
            patch("tools.execute_tool") as execute_tool,
            patch("llm.call_llm", side_effect=fake_call_llm),
        ):
            result, edits, _rows = daemon._chat._chat_reply(conn)

        assert result.text == "Queued a shorter note for review."
        assert len(edits) == 1
        assert edits[0].content == json.loads(valid_args)["content"]
        execute_tool.assert_not_called()

        assert any(
            message.get("role") == "tool"
            and str(message.get("content", "")).startswith("Not proposed:")
            for message in seen_messages[1]
        )
        assert any(
            message.get("role") == "tool"
            and message.get("content") == "Proposed. User will be asked to confirm."
            for message in seen_messages[2]
        )

    def test_repeated_tool_call_forces_clean_final_synthesis(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._conversation = MagicMock()
        daemon._chat._conversation.to_messages.return_value = [
            {
                "role": "user",
                "content": "What's my avg running speed per km by week in 2026?",
            }
        ]
        conn = open_db(daemon.db)

        raw_args = '{"query": "SELECT 1", "limit": 200}'
        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="run_sql", arguments=raw_args),
        )
        repeated_tool_call = SimpleNamespace(
            id="call_2",
            function=SimpleNamespace(
                name="run_sql",
                arguments='{"limit": 200, "query": "SELECT 1"}',
            ),
        )
        first_result = LLMResult(
            text="",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            tool_calls=[tool_call],
            raw_message={
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1"}],
            },
        )
        repeated_result = LLMResult(
            text="",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            tool_calls=[repeated_tool_call],
            raw_message={
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_2"}],
            },
        )
        final_result = LLMResult(
            text="Figure 1 shows the trend.",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )

        seen_messages: list[list[dict]] = []
        seen_kwargs: list[dict] = []

        def fake_call_llm(messages: list[dict], **kwargs: object) -> LLMResult:
            seen_messages.append([dict(message) for message in messages])
            seen_kwargs.append(dict(kwargs))
            return [first_result, repeated_result, final_result][len(seen_messages) - 1]

        with (
            patch("config.MAX_TOOL_ITERATIONS", 8),
            patch(
                "llm_context.load_context", return_value={"prompt": "p", "soul": "s"}
            ),
            patch(
                "llm_context.build_messages",
                return_value=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "context"},
                ],
            ),
            patch(
                "llm_health.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W19"}},
                    "history": [],
                    "week_complete": False,
                },
            ),
            patch("llm_health.render_health_data", return_value="health"),
            patch("baselines.compute_baselines", return_value=None),
            patch("tools.all_chat_tools", return_value=[{"type": "function"}]),
            patch(
                "tools.execute_tool", return_value='[{"week": "2026-W01"}]'
            ) as exec_tool,
            patch("llm.call_llm", side_effect=fake_call_llm),
        ):
            result, _edits, rows = daemon._chat._chat_reply(conn)

        assert result.text == "Figure 1 shows the trend."
        assert rows == [{"week": "2026-W01"}]
        exec_tool.assert_called_once()
        assert len(seen_messages) == 3
        assert seen_kwargs[2]["tools"] is None
        assert seen_kwargs[2]["metadata"] == {"iteration": "final_synthesis"}
        final_messages = seen_messages[2]
        assert not any(message.get("role") == "tool" for message in final_messages)
        assert not any(message.get("tool_calls") for message in final_messages)
        assert '[{"week": "2026-W01"}]' in final_messages[-1]["content"]

    def test_repeated_run_sql_result_forces_clean_final_synthesis(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._conversation = MagicMock()
        daemon._chat._conversation.to_messages.return_value = [
            {"role": "user", "content": "Chart my running pace by week."}
        ]
        conn = open_db(daemon.db)

        first_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="run_sql",
                arguments='{"query": "SELECT week, avg_pace FROM weekly"}',
            ),
        )
        second_call = SimpleNamespace(
            id="call_2",
            function=SimpleNamespace(
                name="run_sql",
                arguments='{"query": "SELECT week, avg_pace FROM weekly ORDER BY week"}',
            ),
        )
        first_result = LLMResult(
            text="",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            tool_calls=[first_call],
            raw_message={"role": "assistant", "content": "", "tool_calls": []},
        )
        second_result = LLMResult(
            text="",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            tool_calls=[second_call],
            raw_message={"role": "assistant", "content": "", "tool_calls": []},
        )
        final_result = LLMResult(
            text="Figure 1 shows the trend.",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )

        seen_kwargs: list[dict] = []

        def fake_call_llm(messages: list[dict], **kwargs: object) -> LLMResult:
            seen_kwargs.append(dict(kwargs))
            return [first_result, second_result, final_result][len(seen_kwargs) - 1]

        with (
            patch("config.MAX_TOOL_ITERATIONS", 8),
            patch(
                "llm_context.load_context", return_value={"prompt": "p", "soul": "s"}
            ),
            patch(
                "llm_context.build_messages",
                return_value=[
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "context"},
                ],
            ),
            patch(
                "llm_health.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W19"}},
                    "history": [],
                    "week_complete": False,
                },
            ),
            patch("llm_health.render_health_data", return_value="health"),
            patch("baselines.compute_baselines", return_value=None),
            patch("tools.all_chat_tools", return_value=[{"type": "function"}]),
            patch(
                "tools.execute_tool",
                return_value='[{"week": "2026-W01", "avg_pace": 6.06}]',
            ) as exec_tool,
            patch("llm.call_llm", side_effect=fake_call_llm),
        ):
            result, _edits, rows = daemon._chat._chat_reply(conn)

        assert result.text == "Figure 1 shows the trend."
        assert rows == [{"week": "2026-W01", "avg_pace": 6.06}]
        assert exec_tool.call_count == 2
        assert len(seen_kwargs) == 3
        assert seen_kwargs[2]["tools"] is None
        assert seen_kwargs[2]["metadata"] == {"iteration": "final_synthesis"}

    def test_failed_chat_chart_uses_rows_fallback(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._chat._conversation = MagicMock()
        daemon._poller.send_reply.return_value = 900
        result = LLMResult(
            text='<chart title="Weekly Pace">\nfig.update_yaxis(ticktext=)\n</chart>\nPace is improving.',
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )
        rows = [
            {"week": "2026-W01", "avg_pace": 6.06},
            {"week": "2026-W02", "avg_pace": 5.97},
            {"week": "2026-W03", "avg_pace": 5.82},
        ]

        with (
            patch.object(daemon._chat, "_chat_reply", return_value=(result, [], rows)),
            patch.object(
                daemon._chat, "_start_placeholder_animation", return_value=(None, None)
            ),
            patch.object(daemon._chat, "_stop_placeholder_animation"),
            patch("charts.render_chart", return_value=None) as render_chart,
            patch(
                "charts.render_rows_chart", return_value=b"\x89PNGfallback"
            ) as render_rows_chart,
        ):
            daemon._handle_telegram_message(
                {
                    "message_id": 1000,
                    "text": "What's my avg running speed trend per km by week in 2026?",
                }
            )

        render_chart.assert_called_once()
        render_rows_chart.assert_called_once_with(rows, title="Weekly Pace")
        daemon._poller.send_photo.assert_called_once_with(
            b"\x89PNGfallback", caption="**Figure 1. Weekly Pace**"
        )
        daemon._poller.edit_message.assert_called_once_with(900, "Pace is improving.")

    def test_chat_with_chart_intent_auto_renders_rows_without_chart_block(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._chat._conversation = MagicMock()
        daemon._poller.send_reply.return_value = 901
        result = LLMResult(
            text="Pace improved from W01 to W18.",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )
        rows = [
            {"week": "2026-W01", "avg_pace_min_km": 6.06},
            {"week": "2026-W18", "avg_pace_min_km": 5.67},
        ]

        with (
            patch.object(daemon._chat, "_chat_reply", return_value=(result, [], rows)),
            patch.object(
                daemon._chat, "_start_placeholder_animation", return_value=(None, None)
            ),
            patch.object(daemon._chat, "_stop_placeholder_animation"),
            patch(
                "charts.render_rows_chart", return_value=b"\x89PNGauto"
            ) as render_rows_chart,
        ):
            daemon._handle_telegram_message(
                {
                    "message_id": 1001,
                    "text": "What's my avg running speed trend per km by week in 2026?",
                }
            )

        render_rows_chart.assert_called_once_with(rows)
        daemon._poller.send_photo.assert_called_once_with(
            b"\x89PNGauto", caption="**Figure 1. Trend**"
        )
        daemon._poller.edit_message.assert_called_once_with(
            901, "Pace improved from W01 to W18."
        )


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
            "cmd_notify_interpreter.interpret_notify_request",
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
    def test_command_usage_records_name_without_arguments(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._chat._conversation = MagicMock()

        daemon._handle_command("/clear private health detail", 40)

        conn = open_db(daemon.db)
        rows = query_events(conn, category="telegram", kind="command")
        assert len(rows) == 1
        assert rows[0]["summary"] == "Telegram command: /clear"
        assert rows[0]["details"] == {"action": "clear"}

    def test_callback_usage_discards_tokens_and_parameters(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        with patch.object(daemon._model_flow, "handle_callback"):
            daemon._handle_telegram_callback(
                {
                    "id": "cb_usage",
                    "data": "model_group:chat",
                    "message": {"message_id": 41},
                }
            )

        conn = open_db(daemon.db)
        rows = query_events(conn, category="telegram", kind="callback")
        assert len(rows) == 1
        assert rows[0]["summary"] == "Telegram callback: model_group"
        assert rows[0]["details"] == {"action": "model_group"}

    def test_events_usage_shows_aggregated_telegram_metrics(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._record_event(
            "telegram",
            "command",
            "Telegram command: /clear",
            {"action": "clear"},
        )
        daemon._record_event(
            "telegram",
            "command",
            "Telegram command: /clear",
            {"action": "clear"},
        )

        daemon._handle_command("/events usage 30", 42)

        daemon._poller.send_reply.assert_called_once()
        sent = daemon._poller.send_reply.call_args.args[0]
        assert "*Telegram usage*" in sent
        assert "`/clear` — 2 use(s)" in sent
        conn = open_db(daemon.db)
        rows = query_telegram_usage(conn)
        assert any(row["action"] == "events" for row in rows)

    def test_review_runs_last_week_insights_flow(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        with (
            patch.object(daemon, "_run_import"),
            patch(
                "cmd_insights.cmd_insights",
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
        assert not hasattr(args, "week")
        assert args.telegram is True

    def test_review_rejects_any_argument(self, tmp_path: Path) -> None:
        """`/review current` used to produce a mid-week report. That report is
        gone, so the old invocation must say so rather than quietly return a
        different week than the user asked for."""
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        with patch.object(daemon, "_run_review") as run_review:
            daemon._handle_command("/review current", 11)

        reply = daemon._poller.send_reply.call_args.args[0]
        assert "takes no arguments" in reply
        assert "always covers last week" in reply
        run_review.assert_not_called()

    def test_status_includes_system_and_data_summary(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._chat._poller.status.return_value = {
            "last_poll_at": "2026-04-05T09:31:00+00:00",
            "last_poll_update_count": 1,
            "last_message_at": "2026-04-05T09:31:02+00:00",
            "last_message_id": "55",
            "last_callback_at": "2026-04-05T09:30:00+00:00",
            "last_callback_data": "add_dt:a1:yest",
        }
        daemon._chat._handler_status.update(
            {
                "active_handlers": 0,
                "last_handler_start_at": "2026-04-05T09:31:02+00:00",
                "last_handler_kind": "message",
                "last_handler_id": "55",
                "last_handler_done_at": "2026-04-05T09:31:05+00:00",
            }
        )
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
        assert "- Nudges today: 2/2" in sent
        assert "- Last report: 2026-04-05 " in sent
        assert "- Last coach run: 2026-04-05 " in sent
        assert "- Telegram: on; last poll: 2026-04-05 " in sent
        assert "- Telegram last message: 55 at 2026-04-05 " in sent
        assert "- Telegram last callback: add_dt:a1:yest at 2026-04-05 " in sent
        assert (
            "- Telegram handler: active 0; last start message 55 at 2026-04-05 " in sent
        )
        assert "- Telegram handler error: never" in sent
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

    def test_advanced_mentions_menu_and_hidden_commands(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        (tmp_path / "me.md").write_text("About me\n", encoding="utf-8")

        daemon._handle_command("/advanced", 55)

        daemon._poller.send_reply.assert_called_once()
        sent = daemon._poller.send_reply.call_args.args[0]
        assert "Menu commands:" in sent
        assert "Advanced commands:" in sent
        assert "/review [current|last] — Run weekly report (default: last)" in sent
        assert "/context [name] — View context files" in sent
        assert "/events [N|usage N] [category] — Recent system events" in sent
        assert "/llm_log [N|id ID|trace ID] — Recent LLM traces" in sent
        assert "/codex — Open Codex panel" in sent
        assert "/claude — Open Claude panel" in sent
        assert "/codex on [prompt]" not in sent
        assert "Available context files:" in sent
        assert "me" in sent

    def test_llm_log_command_shows_recent_calls(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        conn = open_db(daemon.db)
        trace_id = create_llm_trace(conn, "chat")
        log_llm_call(
            conn,
            request_type="chat",
            model="test/model",
            messages=[{"role": "user", "content": "hi"}],
            response_text="hello",
            latency_s=0.4,
            metadata={"iteration": "final_synthesis"},
            trace_id=trace_id,
        )

        daemon._handle_command("/llm_log", 56)

        daemon._poller.send_message_with_keyboard.assert_called_once()
        sent = daemon._poller.send_message_with_keyboard.call_args.args[0]
        buttons = daemon._poller.send_message_with_keyboard.call_args.args[1]
        assert "Recent LLM calls (5)" in sent
        assert "trace" in sent
        assert "chat" in sent
        assert "iter final_synthesis" in sent
        assert buttons[0][0]["text"] == f"Trace {trace_id}"
        assert buttons[0][0]["callback_data"] == f"llmlog:trace:{trace_id}"

    def test_llm_log_command_shows_trace_from_call_id(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        conn = open_db(daemon.db)
        trace_id = create_llm_trace(conn, "nudge")
        first_id = log_llm_call(
            conn,
            request_type="nudge",
            model="test/model",
            messages=[{"role": "user", "content": "draft"}],
            response_text="draft",
            latency_s=0.4,
            metadata={"iteration": 0},
            trace_id=trace_id,
        )
        log_llm_call(
            conn,
            request_type="nudge_verify",
            model="test/verify",
            messages=[{"role": "user", "content": "verify"}],
            response_text="pass",
            latency_s=0.2,
            metadata={"stage": "verify"},
            trace_id=trace_id,
        )

        daemon._handle_command(f"/llm_log id {first_id}", 57)

        daemon._poller.send_message_with_keyboard.assert_called_once()
        sent = daemon._poller.send_message_with_keyboard.call_args.args[0]
        buttons = daemon._poller.send_message_with_keyboard.call_args.args[1]
        assert f"LLM call {first_id}" in sent
        assert "Response preview" in sent
        assert "draft" in sent
        assert buttons[0][0]["callback_data"] == f"llmlog:trace:{trace_id}"

    def test_llm_log_trace_button_edits_to_trace_view(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        conn = open_db(daemon.db)
        trace_id = create_llm_trace(conn, "nudge")
        call_id = log_llm_call(
            conn,
            request_type="nudge",
            model="test/model",
            messages=[{"role": "user", "content": "draft"}],
            response_text="draft",
            latency_s=0.4,
            metadata={"iteration": 0},
            trace_id=trace_id,
        )
        log_llm_call(
            conn,
            request_type="nudge_verify",
            model="test/verify",
            messages=[{"role": "user", "content": "verify"}],
            response_text="pass",
            latency_s=0.2,
            metadata={"stage": "verify"},
            trace_id=trace_id,
        )

        daemon._handle_telegram_callback(
            {
                "id": "cb1",
                "data": f"llmlog:trace:{trace_id}",
                "message": {"message_id": 101},
            }
        )

        daemon._poller.answer_callback_query.assert_called_once_with("cb1")
        daemon._poller.edit_message_with_keyboard.assert_called_once()
        sent = daemon._poller.edit_message_with_keyboard.call_args.args[1]
        buttons = daemon._poller.edit_message_with_keyboard.call_args.args[2]
        assert f"LLM trace {trace_id}" in sent
        assert "nudge_verify" in sent
        assert buttons[0][0]["callback_data"] == f"llmlog:call:{call_id}"


class TestCodexTelegramCommand:
    def test_codex_without_args_shows_button_panel(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        daemon._handle_command("/codex", 53)

        daemon._poller.send_message_with_keyboard.assert_called_once()
        sent = daemon._poller.send_message_with_keyboard.call_args.args[0]
        buttons = daemon._poller.send_message_with_keyboard.call_args.args[1]
        assert sent == "Codex: off"
        labels = [button["text"] for row in buttons for button in row]
        callbacks = [button["callback_data"] for row in buttons for button in row]
        assert labels == ["Turn on", "New session"]
        assert callbacks == ["agent:on:codex", "agent:new:codex"]
        daemon._poller.send_reply.assert_not_called()

    def test_claude_without_args_shows_button_panel(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        daemon._handle_command("/claude", 54)

        daemon._poller.send_message_with_keyboard.assert_called_once()
        sent = daemon._poller.send_message_with_keyboard.call_args.args[0]
        buttons = daemon._poller.send_message_with_keyboard.call_args.args[1]
        assert sent == "Claude: off"
        labels = [button["text"] for row in buttons for button in row]
        callbacks = [button["callback_data"] for row in buttons for button in row]
        assert labels == ["Turn on", "New session"]
        assert callbacks == ["agent:on:claude", "agent:new:claude"]

    def test_agent_panel_shows_active_state(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["agent_mode"] = "codex"
        daemon._state["agent_mode_expires_at"] = "2099-01-01T00:00:00+00:00"

        daemon._handle_command("/codex", 53)

        sent = daemon._poller.send_message_with_keyboard.call_args.args[0]
        buttons = daemon._poller.send_message_with_keyboard.call_args.args[1]
        labels = [button["text"] for row in buttons for button in row]
        assert sent.startswith("Codex: on · ")
        assert sent.endswith(" min left")
        assert labels == ["Turn off", "New session"]

    def test_agent_panel_shows_other_agent_active(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["agent_mode"] = "claude"
        daemon._state["agent_mode_expires_at"] = "2099-01-01T00:00:00+00:00"

        daemon._handle_command("/codex", 53)

        sent = daemon._poller.send_message_with_keyboard.call_args.args[0]
        buttons = daemon._poller.send_message_with_keyboard.call_args.args[1]
        labels = [button["text"] for row in buttons for button in row]
        assert sent == "Codex: off · Claude active"
        assert labels == ["Switch to Codex", "New session"]

    def test_agent_on_callback_switches_mode_and_refreshes_panel(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["agent_mode"] = "codex"
        daemon._state["agent_mode_expires_at"] = "2099-01-01T00:00:00+00:00"

        daemon._handle_telegram_callback(
            {
                "id": "cb1",
                "data": "agent:on:claude",
                "message": {"message_id": 700},
            }
        )

        assert daemon._state["agent_mode"] == "claude"
        daemon._poller.answer_callback_query.assert_called_once_with(
            "cb1", "Claude mode on."
        )
        text = daemon._poller.edit_message_with_keyboard.call_args.args[1]
        assert text.startswith("Claude: on · ")

    def test_agent_off_callback_only_disables_matching_active_agent(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["agent_mode"] = "claude"
        daemon._state["agent_mode_expires_at"] = "2099-01-01T00:00:00+00:00"

        daemon._handle_telegram_callback(
            {
                "id": "cb1",
                "data": "agent:off:codex",
                "message": {"message_id": 700},
            }
        )

        assert daemon._state["agent_mode"] == "claude"
        daemon._poller.answer_callback_query.assert_called_once_with(
            "cb1", "Codex mode was not on."
        )

        daemon._poller.reset_mock()
        daemon._handle_telegram_callback(
            {
                "id": "cb2",
                "data": "agent:off:claude",
                "message": {"message_id": 701},
            }
        )

        assert "agent_mode" not in daemon._state
        assert "agent_mode_expires_at" not in daemon._state
        daemon._poller.answer_callback_query.assert_called_once_with(
            "cb2", "Claude mode off."
        )

    def test_agent_new_callback_clears_only_that_agent_and_turns_it_on(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["codex_session_id"] = "codex-session"
        daemon._state["claude_session_id"] = "claude-session"
        daemon._state["agent_last_message_id"] = 900
        daemon._state["agent_last_message_kind"] = "codex"
        daemon._state["agent_mode"] = "claude"
        daemon._state["agent_mode_expires_at"] = "2099-01-01T00:00:00+00:00"

        daemon._handle_telegram_callback(
            {
                "id": "cb1",
                "data": "agent:new:codex",
                "message": {"message_id": 700},
            }
        )

        assert "codex_session_id" not in daemon._state
        assert daemon._state["claude_session_id"] == "claude-session"
        assert "agent_last_message_id" not in daemon._state
        assert "agent_last_message_kind" not in daemon._state
        assert daemon._state["agent_mode"] == "codex"
        daemon._poller.answer_callback_query.assert_called_once_with(
            "cb1", "New Codex session."
        )

    def test_agent_exit_callback_disables_mode_without_clearing_sessions(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["codex_session_id"] = "codex-session"
        daemon._state["claude_session_id"] = "claude-session"
        daemon._state["agent_mode"] = "codex"
        daemon._state["agent_mode_expires_at"] = "2099-01-01T00:00:00+00:00"

        daemon._handle_telegram_callback(
            {
                "id": "cb1",
                "data": "agent:exit:codex",
                "message": {"message_id": 700},
            }
        )

        assert "agent_mode" not in daemon._state
        assert "agent_mode_expires_at" not in daemon._state
        assert daemon._state["codex_session_id"] == "codex-session"
        assert daemon._state["claude_session_id"] == "claude-session"
        daemon._poller.answer_callback_query.assert_called_once_with(
            "cb1", "Back to chat."
        )
        daemon._poller.edit_message_reply_markup.assert_called_once_with(700, None)

    def test_stale_agent_exit_does_not_disable_other_active_agent(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["agent_mode"] = "claude"
        daemon._state["agent_mode_expires_at"] = "2099-01-01T00:00:00+00:00"

        daemon._handle_telegram_callback(
            {
                "id": "cb1",
                "data": "agent:exit:codex",
                "message": {"message_id": 700},
            }
        )

        assert daemon._state["agent_mode"] == "claude"
        daemon._poller.answer_callback_query.assert_called_once_with(
            "cb1", "Already back in chat."
        )

    def test_codex_command_stores_session_and_edits_placeholder(
        self, tmp_path: Path
    ) -> None:
        from daemon_agent_flow import CodexRunResult

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 900

        with (
            patch.object(
                daemon._chat,
                "_start_placeholder_animation",
                return_value=(None, None),
            ) as start_animation,
            patch.object(
                daemon._chat,
                "_start_agent_stream_status",
                return_value=(None, None, lambda progress: None),
            ) as start_stream_status,
            patch.object(daemon._chat, "_stop_placeholder_animation"),
            patch(
                "daemon_agent_flow.run_codex_workspace",
                return_value=CodexRunResult(
                    text="Workspace answer",
                    session_id="codex-session",
                ),
            ) as run_codex,
        ):
            daemon._handle_command("/codex where is the Telegram router?", 55)

        run_codex.assert_called_once()
        assert run_codex.call_args.kwargs["session_id"] is None
        assert callable(run_codex.call_args.kwargs["progress_callback"])
        start_animation.assert_not_called()
        start_stream_status.assert_called_once_with(900, "Codex")
        daemon._poller.edit_message.assert_called_once()
        text = daemon._poller.edit_message.call_args.args[1]
        assert text.startswith("Workspace answer\n\n_Codex finished in ")
        assert daemon._state["codex_session_id"] == "codex-session"
        assert daemon._state["agent_last_message_id"] == 900
        assert daemon._state["agent_last_message_kind"] == "codex"
        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert state["codex_session_id"] == "codex-session"

    def test_active_agent_reply_includes_back_to_chat_button(
        self, tmp_path: Path
    ) -> None:
        from daemon_agent_flow import CodexRunResult

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 900
        daemon._state["agent_mode"] = "codex"
        daemon._state["agent_mode_expires_at"] = "2099-01-01T00:00:00+00:00"

        with (
            patch.object(
                daemon._chat,
                "_start_placeholder_animation",
                return_value=(None, None),
            ),
            patch.object(
                daemon._chat,
                "_start_agent_stream_status",
                return_value=(None, None, lambda progress: None),
            ),
            patch.object(daemon._chat, "_stop_placeholder_animation"),
            patch(
                "daemon_agent_flow.run_codex_workspace",
                return_value=CodexRunResult(
                    text="Workspace answer",
                    session_id="codex-session",
                ),
            ),
        ):
            daemon._handle_command("/codex where is the Telegram router?", 55)

        daemon._poller.edit_message_with_keyboard.assert_called_once()
        text = daemon._poller.edit_message_with_keyboard.call_args.args[1]
        buttons = daemon._poller.edit_message_with_keyboard.call_args.args[2]
        assert text.startswith("Workspace answer\n\n_Codex finished in ")
        assert buttons == [
            [{"text": "Back to chat", "callback_data": "agent:exit:codex"}]
        ]
        assert daemon._state["agent_last_message_id"] == 900
        assert daemon._state["agent_last_message_kind"] == "codex"

    def test_expired_agent_mode_reply_does_not_include_exit_button(
        self, tmp_path: Path
    ) -> None:
        from daemon_agent_flow import CodexRunResult

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 900
        daemon._state["agent_mode"] = "codex"
        daemon._state["agent_mode_expires_at"] = "2000-01-01T00:00:00+00:00"

        with (
            patch.object(
                daemon._chat,
                "_start_placeholder_animation",
                return_value=(None, None),
            ),
            patch.object(
                daemon._chat,
                "_start_agent_stream_status",
                return_value=(None, None, lambda progress: None),
            ),
            patch.object(daemon._chat, "_stop_placeholder_animation"),
            patch(
                "daemon_agent_flow.run_codex_workspace",
                return_value=CodexRunResult(
                    text="Workspace answer",
                    session_id="codex-session",
                ),
            ),
        ):
            daemon._handle_command("/codex where is the Telegram router?", 55)

        daemon._poller.edit_message.assert_called_once()
        text = daemon._poller.edit_message.call_args.args[1]
        assert text.startswith("Workspace answer\n\n_Codex finished in ")
        daemon._poller.edit_message_with_keyboard.assert_not_called()
        assert "agent_mode" not in daemon._state
        assert "agent_mode_expires_at" not in daemon._state

    def test_active_agent_long_reply_puts_exit_button_after_overflow(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        long_text = "first\n" + ("x" * 4100)
        daemon._poller.send_message_with_keyboard.return_value = 901

        sent_id = daemon._chat._send_agent_result_with_exit(
            long_text,
            kind="codex",
            status_id=900,
            reply_to_message_id=55,
        )

        assert sent_id == 901
        daemon._poller.edit_message.assert_called_once()
        daemon._poller.send_message_with_keyboard.assert_called_once()
        buttons = daemon._poller.send_message_with_keyboard.call_args.args[1]
        assert buttons == [
            [{"text": "Back to chat", "callback_data": "agent:exit:codex"}]
        ]

    def test_codex_progress_events_map_to_friendly_stages(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)

        assert (
            daemon._chat._friendly_agent_stage("agent message: done")
            == "Drafting the reply"
        )
        assert (
            daemon._chat._friendly_agent_stage("exec command: uv run pytest")
            == "Running a repo command"
        )
        assert (
            daemon._chat._friendly_agent_stage("patch file src/a.py")
            == "Inspecting or editing files"
        )
        assert (
            daemon._chat._friendly_agent_stage("turn started")
            == "Working through the request"
        )
        assert daemon._chat._format_elapsed(7) == "7s"
        assert daemon._chat._format_elapsed(67) == "1m 07s"
        assert (
            daemon._chat._append_agent_elapsed("Done\n", "Codex", 67)
            == "Done\n\n_Codex finished in 1m 07s._"
        )

    def test_codex_command_resumes_saved_session(self, tmp_path: Path) -> None:
        from daemon_agent_flow import CodexRunResult

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 901
        daemon._state["codex_session_id"] = "saved-session"

        with (
            patch.object(
                daemon._chat,
                "_start_placeholder_animation",
                return_value=(None, None),
            ),
            patch.object(
                daemon._chat,
                "_start_agent_stream_status",
                return_value=(None, None, lambda progress: None),
            ),
            patch.object(daemon._chat, "_stop_placeholder_animation"),
            patch(
                "daemon_agent_flow.run_codex_workspace",
                return_value=CodexRunResult(
                    text="Follow-up answer",
                    session_id="saved-session",
                ),
            ) as run_codex,
        ):
            daemon._handle_command("/codex next question", 56)

        assert run_codex.call_args.kwargs["session_id"] == "saved-session"
        daemon._poller.edit_message.assert_called_once()
        text = daemon._poller.edit_message.call_args.args[1]
        assert text.startswith("Follow-up answer\n\n_Codex finished in ")

    def test_codex_new_starts_fresh_session(self, tmp_path: Path) -> None:
        from daemon_agent_flow import CodexRunResult

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 902
        daemon._state["codex_session_id"] = "old-session"

        with (
            patch.object(
                daemon._chat,
                "_start_placeholder_animation",
                return_value=(None, None),
            ),
            patch.object(
                daemon._chat,
                "_start_agent_stream_status",
                return_value=(None, None, lambda progress: None),
            ),
            patch.object(daemon._chat, "_stop_placeholder_animation"),
            patch(
                "daemon_agent_flow.run_codex_workspace",
                return_value=CodexRunResult(text="Fresh", session_id="new-session"),
            ) as run_codex,
        ):
            daemon._handle_command("/codex new start over", 57)

        assert run_codex.call_args.kwargs["session_id"] is None
        assert daemon._state["codex_session_id"] == "new-session"

    def test_codex_stop_clears_session(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["codex_session_id"] = "saved-session"
        daemon._state["agent_last_message_id"] = 900
        daemon._state["agent_last_message_kind"] = "codex"
        daemon._state["agent_mode"] = "codex"
        daemon._state["agent_mode_expires_at"] = "2099-01-01T00:00:00+00:00"

        daemon._handle_command("/codex stop", 58)

        assert "codex_session_id" not in daemon._state
        assert "agent_last_message_id" not in daemon._state
        assert "agent_last_message_kind" not in daemon._state
        assert "agent_mode" not in daemon._state
        assert "agent_mode_expires_at" not in daemon._state
        daemon._poller.send_reply.assert_called_once_with(
            "Codex session cleared and mode off.", reply_to_message_id=58
        )

    def test_codex_on_routes_plain_messages_until_off(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        daemon._handle_command("/codex on", 58)

        assert daemon._state["agent_mode"] == "codex"
        assert "agent_mode_expires_at" in daemon._state
        daemon._poller.send_reply.assert_called_once()
        assert "Codex mode on" in daemon._poller.send_reply.call_args.args[0]

        with patch.object(daemon._chat, "_handle_agent_turn") as handle_agent:
            daemon._handle_telegram_message(
                {"message_id": 59, "text": "continue this investigation"}
            )

        handle_agent.assert_called_once_with(
            "continue this investigation",
            59,
            kind="codex",
            new_session=False,
        )

        daemon._poller.reset_mock()
        daemon._handle_command("/codex off", 60)

        assert "agent_mode" not in daemon._state
        assert "agent_mode_expires_at" not in daemon._state
        daemon._poller.send_reply.assert_called_once_with(
            "Codex mode off. Plain messages go back to health chat.",
            reply_to_message_id=60,
        )

    def test_codex_mode_expires_to_health_chat(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._chat._conversation = MagicMock()
        daemon._state["agent_mode"] = "codex"
        daemon._state["agent_mode_expires_at"] = "2000-01-01T00:00:00+00:00"

        with (
            patch.object(daemon._chat, "_handle_agent_turn") as handle_agent,
            patch.object(daemon._chat, "_chat_reply") as chat_reply,
            patch.object(
                daemon._chat,
                "_start_placeholder_animation",
                return_value=(None, None),
            ),
            patch.object(daemon._chat, "_stop_placeholder_animation"),
        ):
            chat_reply.return_value = (CommandResult(text="health reply"), [], [])
            daemon._handle_telegram_message(
                {"message_id": 61, "text": "normal health question"}
            )

        handle_agent.assert_not_called()
        chat_reply.assert_called_once()
        assert "agent_mode" not in daemon._state
        assert "agent_mode_expires_at" not in daemon._state

    def test_codex_reset_clears_session_but_keeps_mode(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["codex_session_id"] = "saved-session"
        daemon._state["agent_last_message_id"] = 900
        daemon._state["agent_last_message_kind"] = "codex"
        daemon._state["agent_mode"] = "codex"
        daemon._state["agent_mode_expires_at"] = "2099-01-01T00:00:00+00:00"

        daemon._handle_command("/codex reset", 62)

        assert "codex_session_id" not in daemon._state
        assert "agent_last_message_id" not in daemon._state
        assert "agent_last_message_kind" not in daemon._state
        assert daemon._state["agent_mode"] == "codex"
        assert "agent_mode_expires_at" in daemon._state
        daemon._poller.send_reply.assert_called_once_with(
            "Codex context cleared.", reply_to_message_id=62
        )

    def test_codex_reset_with_prompt_starts_fresh_session(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["codex_session_id"] = "saved-session"

        with patch.object(daemon._chat, "_handle_agent_turn") as handle_agent:
            daemon._handle_command("/codex reset inspect fresh", 63)

        assert "codex_session_id" not in daemon._state
        handle_agent.assert_called_once_with(
            "inspect fresh",
            63,
            kind="codex",
            new_session=True,
        )

    def test_reply_to_last_codex_message_continues_codex(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["agent_last_message_id"] = 900
        daemon._state["agent_last_message_kind"] = "codex"

        with patch.object(daemon._chat, "_handle_agent_turn") as handle_agent:
            daemon._handle_telegram_message(
                {
                    "message_id": 59,
                    "text": "and where are the tests?",
                    "reply_to_message": {"message_id": 900, "text": "Codex answer"},
                }
            )

        handle_agent.assert_called_once_with(
            "and where are the tests?",
            59,
            kind="codex",
            new_session=False,
        )

    def test_reply_to_last_claude_message_continues_claude(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["agent_last_message_id"] = 901
        daemon._state["agent_last_message_kind"] = "claude"

        with patch.object(daemon._chat, "_handle_agent_turn") as handle_agent:
            daemon._handle_telegram_message(
                {
                    "message_id": 60,
                    "text": "and where is the runner?",
                    "reply_to_message": {"message_id": 901, "text": "Claude answer"},
                }
            )

        handle_agent.assert_called_once_with(
            "and where is the runner?",
            60,
            kind="claude",
            new_session=False,
        )

    def test_codex_off_when_claude_active_does_not_disable(
        self, tmp_path: Path
    ) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["agent_mode"] = "claude"
        daemon._state["agent_mode_expires_at"] = "2099-01-01T00:00:00+00:00"

        daemon._handle_command("/codex off", 70)

        assert daemon._state["agent_mode"] == "claude"
        sent = daemon._poller.send_reply.call_args.args[0]
        assert "Claude mode is" in sent

    def test_claude_on_replaces_codex_mode(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._state["agent_mode"] = "codex"
        daemon._state["agent_mode_expires_at"] = "2099-01-01T00:00:00+00:00"

        daemon._handle_command("/claude on", 71)

        assert daemon._state["agent_mode"] == "claude"
        assert "agent_mode_expires_at" in daemon._state
        sent = daemon._poller.send_reply.call_args.args[0]
        assert "Claude mode on" in sent

    def test_claude_command_stores_session_and_edits_placeholder(
        self, tmp_path: Path
    ) -> None:
        from daemon_claude_flow import ClaudeRunResult

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 950

        with (
            patch.object(
                daemon._chat,
                "_start_placeholder_animation",
                return_value=(None, None),
            ) as start_animation,
            patch.object(
                daemon._chat,
                "_start_agent_stream_status",
                return_value=(None, None, lambda progress: None),
            ) as start_stream_status,
            patch.object(daemon._chat, "_stop_placeholder_animation"),
            patch(
                "daemon_claude_flow.run_claude_workspace",
                return_value=ClaudeRunResult(
                    text="Claude answer",
                    session_id="claude-session",
                ),
            ) as run_claude,
        ):
            daemon._handle_command("/claude where is the Telegram router?", 72)

        run_claude.assert_called_once()
        assert run_claude.call_args.kwargs["session_id"] is None
        assert callable(run_claude.call_args.kwargs["progress_callback"])
        start_animation.assert_not_called()
        start_stream_status.assert_called_once_with(950, "Claude")
        daemon._poller.edit_message.assert_called_once()
        text = daemon._poller.edit_message.call_args.args[1]
        assert text.startswith("Claude answer\n\n_Claude finished in ")
        assert daemon._state["claude_session_id"] == "claude-session"
        assert daemon._state["agent_last_message_id"] == 950
        assert daemon._state["agent_last_message_kind"] == "claude"

    def test_claude_session_independent_from_codex(self, tmp_path: Path) -> None:
        from daemon_claude_flow import ClaudeRunResult

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        daemon._poller.send_reply.return_value = 951
        daemon._state["codex_session_id"] = "codex-session"
        daemon._state["claude_session_id"] = "claude-session"

        with (
            patch.object(
                daemon._chat,
                "_start_placeholder_animation",
                return_value=(None, None),
            ),
            patch.object(
                daemon._chat,
                "_start_agent_stream_status",
                return_value=(None, None, lambda progress: None),
            ),
            patch.object(daemon._chat, "_stop_placeholder_animation"),
            patch(
                "daemon_claude_flow.run_claude_workspace",
                return_value=ClaudeRunResult(text="ok", session_id="claude-session"),
            ) as run_claude,
        ):
            daemon._handle_command("/claude follow up", 73)

        assert run_claude.call_args.kwargs["session_id"] == "claude-session"
        assert daemon._state["codex_session_id"] == "codex-session"


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
        from config import ANTHROPIC_OPUS_MODEL, DEFAULT_CHAT_MODEL
        from model_prefs import resolve_model_route, set_feature_route

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        prefs_path = tmp_path / "model_prefs.json"

        with patch("model_prefs.MODEL_PREFS_PATH", prefs_path):
            set_feature_route("chat", primary=ANTHROPIC_OPUS_MODEL)
            daemon._handle_telegram_callback(
                {
                    "id": "cb_reset_all",
                    "data": "model_reset_all",
                    "message": {"message_id": 902},
                }
            )
            route = resolve_model_route("chat")

        assert route.primary == DEFAULT_CHAT_MODEL

    def test_models_auto_fallback_falls_through_to_profile(
        self, tmp_path: Path
    ) -> None:
        from config import (
            ANTHROPIC_OPUS_MODEL,
            FALLBACK_PRO_MODEL,
            OPENAI_LUNA_MODEL,
        )
        from model_prefs import resolve_model_route, selectable_models

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        prefs_path = tmp_path / "model_prefs.json"
        models = selectable_models()
        primary_idx = models.index(ANTHROPIC_OPUS_MODEL)

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
            # Other feature-level defaults remain independent.
            insights_route = resolve_model_route("insights")

        # "Auto" applies the profile fallback (resolved at read time, not stored).
        assert route.primary == ANTHROPIC_OPUS_MODEL
        assert route.fallback == FALLBACK_PRO_MODEL
        assert route.reasoning_effort == "high"
        assert route.temperature is None
        # Untouched features keep their own defaults; insights is on Luna.
        assert insights_route.primary == OPENAI_LUNA_MODEL

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

    def test_notify_user_failure_sends_verifier_summary_and_buttons(
        self, tmp_path: Path
    ) -> None:
        from llm_verify import VerificationIssue, VerificationSuppression

        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()

        detail = VerificationSuppression(
            kind="insights",
            source_llm_call_id=42,
            verifier_call_id=99,
            trace_id=7,
            verdict="fail",
            confidence="high",
            first_issue=VerificationIssue(
                severity="critical",
                quote="streak",
                problem="False Monday streak claim.",
                correction="Remove the streak.",
                evidence="W18 Monday was a rest day.",
            ),
        )

        daemon._notify_user_failure(
            "Weekly review",
            "Insights verification failed; refusing to save/send report",
            detail=detail,
        )

        sent_text = daemon._poller.send_message_with_keyboard.call_args.args[0]
        buttons = daemon._poller.send_message_with_keyboard.call_args.args[1]
        assert "Weekly review failed" in sent_text
        assert "Verifier blocked it" in sent_text
        assert "False Monday streak claim." in sent_text
        assert "The report was not sent." in sent_text
        assert buttons == [
            [
                {"text": "Details", "callback_data": "faildetail:42"},
                {"text": "Trace 7", "callback_data": "llmlog:trace:7"},
            ]
        ]

    def test_capture_records_verifier_suppression(self, tmp_path: Path) -> None:
        from llm_verify import (
            VerificationIssue,
            VerificationSuppression,
            _notify_suppression,
        )

        snapshot = VerificationSuppression(
            kind="insights",
            source_llm_call_id=11,
            verifier_call_id=22,
            trace_id=33,
            verdict="fail",
            confidence="high",
            first_issue=VerificationIssue(
                severity="critical",
                quote="q",
                problem="problem",
                correction="fix",
            ),
        )

        with daemon_module._capture_last_error() as cap:
            _notify_suppression(snapshot)

        assert cap.last_suppression is snapshot

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
        assert "Check daemon logs" in sent_text

    def test_failure_detail_callback_shows_verifier_issue(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._chat._poller = MagicMock()
        conn = open_db(tmp_path / "test.db")
        trace_id = create_llm_trace(conn, "insights")
        source_id = log_llm_call(
            conn,
            request_type="insights",
            model="draft-model",
            messages=[],
            response_text="draft",
            metadata={
                "insights_verification": {
                    "verdict": "fail",
                    "confidence": "high",
                    "verifier_call_id": 44,
                    "issues": [
                        {
                            "severity": "critical",
                            "problem": "False Monday streak claim.",
                            "correction": "Remove the streak.",
                            "evidence": "W18 Monday was a rest day.",
                        }
                    ],
                }
            },
            trace_id=trace_id,
        )

        daemon._handle_telegram_callback(
            {
                "id": "cb_fail",
                "data": f"faildetail:{source_id}",
                "message": {"message_id": 808},
            }
        )

        daemon._poller.answer_callback_query.assert_called_once_with("cb_fail")
        daemon._poller.edit_message_with_keyboard.assert_called_once()
        text = daemon._poller.edit_message_with_keyboard.call_args.args[1]
        buttons = daemon._poller.edit_message_with_keyboard.call_args.args[2]
        assert "Verifier blocked call" in text
        assert "False Monday streak claim." in text
        assert "Remove the streak." in text
        callbacks = [button["callback_data"] for row in buttons for button in row]
        assert callbacks == [
            f"llmlog:call:{source_id}",
            "llmlog:call:44",
            f"llmlog:trace:{trace_id}",
            "llmlog:recent:5",
        ]

    def test_notify_user_failure_no_op_without_poller(self, tmp_path: Path) -> None:
        """If Telegram isn't configured, _poller is unset — the helper
        must do nothing rather than raising AttributeError."""
        daemon = _make_daemon(tmp_path)
        # Do not set daemon._poller — simulate no Telegram configured.
        # Should not raise.
        daemon._notify_user_failure("Manual review", "some error")


class TestIngestHealthAlerts:
    @pytest.fixture(autouse=True)
    def _no_quiet_hours(self):
        """Neutralise the quiet-hours window for tests not about it.

        With an empty window every delivery decision is time-independent, so
        these tests stay deterministic whatever hour the suite runs.
        """
        with (
            patch.object(
                notification_prefs_module, "DATA_HEALTH_QUIET_START_HHMM", "00:00"
            ),
            patch.object(
                notification_prefs_module, "DATA_HEALTH_QUIET_END_HHMM", "00:00"
            ),
        ):
            yield

    def _runtime(self, tmp_path: Path, health) -> ProfileRuntime:
        """Return a runtime wired to an HTTP profile reporting *health*."""
        from profiles import Profile

        runtime = _make_daemon(tmp_path)
        runtime.profile = Profile(
            name="anna",
            telegram_id=22,
            root=tmp_path / "profiles" / "anna",
            import_source="http",
        )
        runtime._chat._poller = MagicMock()
        return runtime

    def _health(self, status: str):
        from http_ingest import IngestHealth

        return IngestHealth(status=status, detail=f"{status} detail")

    def test_alerts_once_then_stays_quiet_until_the_realert_window(
        self, tmp_path: Path
    ) -> None:
        runtime = self._runtime(tmp_path, self._health("split"))
        prefs = load_notification_prefs(runtime._notification_prefs_path)

        with patch(
            "http_ingest.assess_ingest_health", return_value=self._health("split")
        ):
            runtime._check_ingest_health(prefs)
            runtime._check_ingest_health(prefs)
            runtime._check_ingest_health(prefs)

        sent = runtime._poller.send_reply.call_args_list
        assert len(sent) == 1
        assert "split detail" in sent[0].args[0]
        assert runtime._state["data_health_alert"]["status"] == "split"

    def test_a_changed_condition_alerts_again_immediately(self, tmp_path: Path) -> None:
        runtime = self._runtime(tmp_path, self._health("split"))
        prefs = load_notification_prefs(runtime._notification_prefs_path)

        with patch(
            "http_ingest.assess_ingest_health", return_value=self._health("split")
        ):
            runtime._check_ingest_health(prefs)
        with patch(
            "http_ingest.assess_ingest_health", return_value=self._health("silent")
        ):
            runtime._check_ingest_health(prefs)

        assert runtime._poller.send_reply.call_count == 2

    def test_recovery_clears_state_and_says_so_once(self, tmp_path: Path) -> None:
        runtime = self._runtime(tmp_path, self._health("silent"))
        prefs = load_notification_prefs(runtime._notification_prefs_path)

        with patch(
            "http_ingest.assess_ingest_health", return_value=self._health("silent")
        ):
            runtime._check_ingest_health(prefs)
        with patch("http_ingest.assess_ingest_health", return_value=self._health("ok")):
            runtime._check_ingest_health(prefs)
            runtime._check_ingest_health(prefs)

        messages = [call.args[0] for call in runtime._poller.send_reply.call_args_list]
        assert len(messages) == 2
        assert "working again" in messages[1]
        assert "data_health_alert" not in runtime._state

    def test_a_healthy_profile_is_never_messaged(self, tmp_path: Path) -> None:
        runtime = self._runtime(tmp_path, self._health("ok"))
        prefs = load_notification_prefs(runtime._notification_prefs_path)

        with patch("http_ingest.assess_ingest_health", return_value=self._health("ok")):
            runtime._check_ingest_health(prefs)

        runtime._poller.send_reply.assert_not_called()

    def test_muting_suppresses_the_message_but_still_logs(
        self, tmp_path: Path, caplog
    ) -> None:
        import logging as _logging

        runtime = self._runtime(tmp_path, self._health("split"))
        prefs = load_notification_prefs(runtime._notification_prefs_path)
        prefs["overrides"] = {"data_health": {"enabled": False}}

        with caplog.at_level(_logging.WARNING):
            with patch(
                "http_ingest.assess_ingest_health", return_value=self._health("split")
            ):
                runtime._check_ingest_health(prefs)

        runtime._poller.send_reply.assert_not_called()
        # The operator can still find it in the log even when the user muted it.
        assert "split detail" in caplog.text

    def test_quiet_hours_defer_without_sending_or_recording(
        self, tmp_path: Path
    ) -> None:
        """A 2am fault is held: no message, and no state, so the morning tick
        still delivers it rather than the de-dup guard swallowing it."""
        runtime = self._runtime(tmp_path, self._health("split"))
        prefs = load_notification_prefs(runtime._notification_prefs_path)
        # 02:00 local sits inside the default 22:00-08:00 window. A naive value
        # astimezone()s to local wall time, keeping the check timezone-agnostic.
        night = daemon_module.datetime(2026, 4, 5, 2, 0)
        fake_datetime = MagicMock(wraps=daemon_module.datetime)
        fake_datetime.now.return_value = night

        with (
            patch.object(
                notification_prefs_module, "DATA_HEALTH_QUIET_START_HHMM", "22:00"
            ),
            patch.object(
                notification_prefs_module, "DATA_HEALTH_QUIET_END_HHMM", "08:00"
            ),
            patch.object(daemon_module, "datetime", fake_datetime),
            patch(
                "http_ingest.assess_ingest_health",
                return_value=self._health("split"),
            ),
        ):
            runtime._check_ingest_health(prefs)

        runtime._poller.send_reply.assert_not_called()
        assert "data_health_alert" not in runtime._state

    def test_overnight_recovery_is_held_until_morning(self, tmp_path: Path) -> None:
        """A fault alerted in the evening and cleared at 3am still gets its
        recovery notice: holding the message must not drop the record."""
        runtime = self._runtime(tmp_path, self._health("split"))
        prefs = load_notification_prefs(runtime._notification_prefs_path)
        runtime._state["data_health_alert"] = {
            "status": "split",
            "sent_at": "2026-04-04T20:00:00+00:00",
        }

        def _tick(hour: int) -> None:
            fake_datetime = MagicMock(wraps=daemon_module.datetime)
            fake_datetime.now.return_value = daemon_module.datetime(2026, 4, 5, hour, 0)
            with (
                patch.object(
                    notification_prefs_module, "DATA_HEALTH_QUIET_START_HHMM", "22:00"
                ),
                patch.object(
                    notification_prefs_module, "DATA_HEALTH_QUIET_END_HHMM", "08:00"
                ),
                patch.object(daemon_module, "datetime", fake_datetime),
                patch(
                    "http_ingest.assess_ingest_health",
                    return_value=self._health("ok"),
                ),
            ):
                runtime._check_ingest_health(prefs)

        _tick(3)
        runtime._poller.send_reply.assert_not_called()
        assert "data_health_alert" in runtime._state

        _tick(9)
        assert "working again" in runtime._poller.send_reply.call_args.args[0]
        assert "data_health_alert" not in runtime._state
