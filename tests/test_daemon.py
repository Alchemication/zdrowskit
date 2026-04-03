"""Tests for scheduled coach behavior and Telegram feedback flow."""

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
from store import log_llm_call, open_db


def _make_daemon(tmp_path: Path) -> ZdrowskitDaemon:
    daemon_module.STATE_FILE = tmp_path / "state.json"
    return ZdrowskitDaemon("test-model", tmp_path / "test.db", tmp_path)


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


class TestCoachFeedbackFlow:
    def test_reject_records_feedback_and_prompts_for_reason(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "plan.md").write_text(
            "## Weekly Structure\n\nKeep volume steady\n", encoding="utf-8"
        )
        daemon = _make_daemon(tmp_path)
        daemon._poller = MagicMock()
        daemon._poller.send_reply.return_value = 321
        daemon._pending_edits = PendingEdits()

        edit = ContextEdit(
            file="plan",
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
            file="plan",
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
        (tmp_path / "plan.md").write_text(
            "## Weekly Structure\n\nKeep volume steady\n", encoding="utf-8"
        )
        daemon = _make_daemon(tmp_path)
        daemon._poller = MagicMock()
        daemon._pending_edits = PendingEdits()

        edit = ContextEdit(
            file="plan",
            action="replace_section",
            section="## Weekly Structure",
            content="## Weekly Structure\n\nAdd a recovery day\n",
            summary="Add extra recovery day",
        )

        daemon._propose_context_edit(edit, source="chat")

        stored = next(iter(daemon._pending_edits._edits.values()))[0]
        assert stored.source == "chat"
        assert "+++ plan.md (proposed)" in stored.preview


class TestTelegramFeedbackFlow:
    def test_fb_neg_swaps_to_category_keyboard(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._poller = MagicMock()

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
        daemon._poller = MagicMock()
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
        daemon._poller = MagicMock()
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
        daemon._poller = MagicMock()

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

    def test_insights_feedback_uses_footer_message(self, tmp_path: Path) -> None:
        daemon = _make_daemon(tmp_path)
        daemon._poller = MagicMock()

        daemon._attach_feedback_button(
            CommandResult(text="report", llm_call_id=12, telegram_message_id=44),
            "insights",
        )

        daemon._poller.send_message_with_keyboard.assert_called_once()
        daemon._poller.edit_message_reply_markup.assert_not_called()
