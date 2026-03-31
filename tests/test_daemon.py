"""Tests for scheduled coach behavior and Telegram feedback flow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from context_edit import (
    ContextEdit,
    PendingContextEdit,
    PendingEdits,
    append_coach_feedback,
    new_feedback_entry,
)
from daemon import ZdrowskitDaemon


class TestWeeklyReportScheduling:
    def test_weekly_report_runs_coach_after_insights(self, tmp_path: Path) -> None:
        daemon = ZdrowskitDaemon("test-model", tmp_path / "test.db", tmp_path)
        events: list[str] = []

        with (
            patch.object(daemon, "_run_import"),
            patch.object(
                daemon, "_record_report", side_effect=lambda _: events.append("record")
            ),
            patch(
                "commands.cmd_insights",
                side_effect=lambda args: events.append("insights"),
            ),
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
        daemon = ZdrowskitDaemon("test-model", tmp_path / "test.db", tmp_path)
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

    def test_reason_reply_updates_matching_feedback_entry(self, tmp_path: Path) -> None:
        daemon = ZdrowskitDaemon("test-model", tmp_path / "test.db", tmp_path)
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
        daemon = ZdrowskitDaemon("test-model", tmp_path / "test.db", tmp_path)
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
