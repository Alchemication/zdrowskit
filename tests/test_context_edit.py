"""Tests for src/context_edit.py."""

from __future__ import annotations

import json
from pathlib import Path

from context_edit import (
    EditPreviewError,
    ContextEdit,
    PendingEdits,
    apply_edit,
    build_edit_preview,
    append_coach_feedback,
    extract_all_context_updates,
    extract_context_update,
    new_feedback_entry,
    strip_all_context_updates,
    strip_context_update,
    update_coach_feedback_reason,
)


# ---------------------------------------------------------------------------
# extract_context_update
# ---------------------------------------------------------------------------


class TestExtractContextUpdate:
    def test_valid_append(self) -> None:
        response = (
            "Sure, I noted that.\n"
            "<context_update>"
            '{"file": "log", "action": "append", '
            '"content": "## 2026-W12\\n\\nEasy 8k felt great.\\n", '
            '"summary": "Added W12 log entry"}'
            "</context_update>"
        )
        edit = extract_context_update(response)
        assert edit is not None
        assert edit.file == "log"
        assert edit.action == "append"
        assert "Easy 8k" in edit.content
        assert edit.summary == "Added W12 log entry"
        assert edit.section is None

    def test_valid_replace_section(self) -> None:
        response = (
            "Updated.\n"
            "<context_update>"
            '{"file": "goals", "action": "replace_section", '
            '"section": "## Strength Goals", '
            '"content": "## Strength Goals\\n- Bench 100kg by June\\n", '
            '"summary": "Updated bench target"}'
            "</context_update>"
        )
        edit = extract_context_update(response)
        assert edit is not None
        assert edit.action == "replace_section"
        assert edit.section == "## Strength Goals"

    def test_no_block_returns_none(self) -> None:
        assert extract_context_update("Just a normal reply.") is None

    def test_invalid_json_returns_none(self) -> None:
        response = "<context_update>not valid json</context_update>"
        assert extract_context_update(response) is None

    def test_disallowed_file_returns_none(self) -> None:
        response = (
            "<context_update>"
            '{"file": "soul", "action": "append", '
            '"content": "new soul", "summary": "change soul"}'
            "</context_update>"
        )
        assert extract_context_update(response) is None

    def test_unknown_action_returns_none(self) -> None:
        response = (
            "<context_update>"
            '{"file": "log", "action": "delete", '
            '"content": "x", "summary": "y"}'
            "</context_update>"
        )
        assert extract_context_update(response) is None

    def test_missing_content_returns_none(self) -> None:
        response = (
            "<context_update>"
            '{"file": "log", "action": "append", '
            '"content": "", "summary": "empty"}'
            "</context_update>"
        )
        assert extract_context_update(response) is None

    def test_replace_section_without_section_returns_none(self) -> None:
        response = (
            "<context_update>"
            '{"file": "goals", "action": "replace_section", '
            '"content": "x", "summary": "y"}'
            "</context_update>"
        )
        assert extract_context_update(response) is None

    def test_multiline_json(self) -> None:
        block = json.dumps(
            {
                "file": "log",
                "action": "append",
                "content": "Line 1\nLine 2\n",
                "summary": "Multi-line entry",
            },
            indent=2,
        )
        response = f"Reply text.\n<context_update>\n{block}\n</context_update>"
        edit = extract_context_update(response)
        assert edit is not None
        assert "Line 1" in edit.content


# ---------------------------------------------------------------------------
# strip_context_update
# ---------------------------------------------------------------------------


class TestStripContextUpdate:
    def test_strips_block(self) -> None:
        response = (
            "Visible reply.\n"
            '<context_update>{"file":"log","action":"append",'
            '"content":"x","summary":"y"}</context_update>'
        )
        assert strip_context_update(response) == "Visible reply."

    def test_no_block_returns_original(self) -> None:
        text = "Just a reply."
        assert strip_context_update(text) == text

    def test_block_at_start(self) -> None:
        response = '<context_update>{"a":"b"}</context_update>\nVisible part.'
        assert strip_context_update(response) == "Visible part."

    def test_preserves_surrounding_text(self) -> None:
        response = "Before.\n<context_update>{}</context_update>\nAfter."
        result = strip_context_update(response)
        assert "Before." in result
        assert "After." in result


# ---------------------------------------------------------------------------
# apply_edit
# ---------------------------------------------------------------------------


class TestApplyEdit:
    def test_append_to_existing_file(self, tmp_path: Path) -> None:
        md = tmp_path / "log.md"
        md.write_text("# Weekly Log\n\n## 2026-W11\n\nOld entry.\n")
        edit = ContextEdit(
            file="log",
            action="append",
            content="## 2026-W12\n\nNew entry.",
            summary="Added W12",
        )
        apply_edit(tmp_path, edit)
        result = md.read_text()
        assert "Old entry." in result
        assert "## 2026-W12" in result
        assert "New entry." in result

    def test_append_to_empty_file(self, tmp_path: Path) -> None:
        md = tmp_path / "log.md"
        md.write_text("")
        edit = ContextEdit(
            file="log", action="append", content="First entry.", summary="Init"
        )
        apply_edit(tmp_path, edit)
        assert "First entry." in md.read_text()

    def test_append_creates_file(self, tmp_path: Path) -> None:
        edit = ContextEdit(
            file="log", action="append", content="Brand new.", summary="Create"
        )
        apply_edit(tmp_path, edit)
        assert (tmp_path / "log.md").read_text().strip() == "Brand new."

    def test_replace_section_found(self, tmp_path: Path) -> None:
        md = tmp_path / "goals.md"
        md.write_text(
            "# Goals\n\n## Running\n\nSub-50 10K\n\n## Strength\n\nBench 80kg\n"
        )
        edit = ContextEdit(
            file="goals",
            action="replace_section",
            section="## Strength",
            content="## Strength\n\nBench 100kg by June\n",
            summary="Updated bench target",
        )
        apply_edit(tmp_path, edit)
        result = md.read_text()
        assert "Bench 100kg by June" in result
        assert "Bench 80kg" not in result
        assert "Sub-50 10K" in result

    def test_replace_section_not_found_appends(self, tmp_path: Path) -> None:
        md = tmp_path / "goals.md"
        md.write_text("# Goals\n\n## Running\n\nSub-50 10K\n")
        edit = ContextEdit(
            file="goals",
            action="replace_section",
            section="## Mobility",
            content="## Mobility\n\nDaily stretching\n",
            summary="Added mobility goal",
        )
        apply_edit(tmp_path, edit)
        result = md.read_text()
        assert "Sub-50 10K" in result
        assert "## Mobility" in result
        assert "Daily stretching" in result

    def test_replace_section_not_found_strict_raises(self, tmp_path: Path) -> None:
        md = tmp_path / "goals.md"
        md.write_text("# Goals\n\n## Running\n\nSub-50 10K\n")
        edit = ContextEdit(
            file="goals",
            action="replace_section",
            section="## Mobility",
            content="## Mobility\n\nDaily stretching\n",
            summary="Added mobility goal",
        )
        try:
            apply_edit(tmp_path, edit, strict=True)
        except EditPreviewError as exc:
            assert "Section not found" in str(exc)
        else:
            raise AssertionError("Expected EditPreviewError")

    def test_atomic_write(self, tmp_path: Path) -> None:
        """Verify no .tmp file remains after a successful write."""
        md = tmp_path / "log.md"
        md.write_text("existing\n")
        edit = ContextEdit(file="log", action="append", content="new", summary="test")
        apply_edit(tmp_path, edit)
        assert not (tmp_path / "log.md.tmp").exists()
        assert md.exists()


class TestBuildEditPreview:
    def test_append_preview_contains_unified_diff(self, tmp_path: Path) -> None:
        (tmp_path / "log.md").write_text(
            "## 2026-03-20\n\nOld note\n", encoding="utf-8"
        )
        edit = ContextEdit(
            file="log",
            action="append",
            content="## 2026-03-21\n\nNew note\n",
            summary="Add next log entry",
        )

        preview = build_edit_preview(tmp_path, edit)

        assert "--- log.md" in preview
        assert "+++ log.md (proposed)" in preview
        assert "+## 2026-03-21" in preview

    def test_strict_preview_rejects_missing_section(self, tmp_path: Path) -> None:
        (tmp_path / "plan.md").write_text("## Weekly Structure\n\nEasy week\n")
        edit = ContextEdit(
            file="plan",
            action="replace_section",
            section="## Sleep",
            content="## Sleep\n\n8 hours\n",
            summary="Raise sleep target",
        )

        try:
            build_edit_preview(tmp_path, edit, strict=True)
        except EditPreviewError as exc:
            assert "Section not found" in str(exc)
        else:
            raise AssertionError("Expected EditPreviewError")


# ---------------------------------------------------------------------------
# PendingEdits
# ---------------------------------------------------------------------------


class TestPendingEdits:
    def test_store_and_pop(self) -> None:
        pe = PendingEdits()
        edit = ContextEdit(file="log", action="append", content="x", summary="y")
        edit_id = pe.store(edit, source="chat", preview="preview")
        assert edit_id.startswith("ce_")
        result = pe.pop(edit_id)
        assert result is not None
        assert result.edit is edit
        assert result.source == "chat"
        assert result.preview == "preview"

    def test_pop_unknown_returns_none(self) -> None:
        pe = PendingEdits()
        assert pe.pop("ce_999") is None

    def test_pop_twice_returns_none(self) -> None:
        pe = PendingEdits()
        edit = ContextEdit(file="log", action="append", content="x", summary="y")
        edit_id = pe.store(edit, source="chat", preview="preview")
        pe.pop(edit_id)
        assert pe.pop(edit_id) is None

    def test_expiry(self) -> None:
        pe = PendingEdits()
        edit = ContextEdit(file="log", action="append", content="x", summary="y")
        edit_id = pe.store(edit, source="chat", preview="preview")
        # Manually expire the entry.
        with pe._lock:
            ts = pe._edits[edit_id][1]
            pe._edits[edit_id] = (pe._edits[edit_id][0], ts - 700)
        assert pe.pop(edit_id) is None

    def test_sequential_ids(self) -> None:
        pe = PendingEdits()
        edit = ContextEdit(file="log", action="append", content="x", summary="y")
        id1 = pe.store(edit, source="chat", preview="preview")
        id2 = pe.store(edit, source="coach", preview="preview2")
        assert id1 != id2


# ---------------------------------------------------------------------------
# extract_all_context_updates
# ---------------------------------------------------------------------------


class TestExtractAllContextUpdates:
    def test_no_blocks(self) -> None:
        assert extract_all_context_updates("Just a normal reply.") == []

    def test_single_block(self) -> None:
        response = (
            "Some reasoning.\n"
            "<context_update>"
            '{"file": "plan", "action": "append", '
            '"content": "New rule.", "summary": "Added rule"}'
            "</context_update>"
        )
        edits = extract_all_context_updates(response)
        assert len(edits) == 1
        assert edits[0].file == "plan"
        assert edits[0].summary == "Added rule"

    def test_two_blocks(self) -> None:
        response = (
            "Reasoning for plan change.\n"
            "<context_update>"
            '{"file": "plan", "action": "replace_section", '
            '"section": "## Sleep", '
            '"content": "## Sleep\\n\\nTarget: 7 hours.\\n", '
            '"summary": "Increased sleep target to 7h"}'
            "</context_update>\n"
            "Reasoning for goal change.\n"
            "<context_update>"
            '{"file": "goals", "action": "replace_section", '
            '"section": "## Goals", '
            '"content": "## Goals\\n\\n1. Sub-26 5K\\n", '
            '"summary": "Adjusted 5K target to sub-26"}'
            "</context_update>"
        )
        edits = extract_all_context_updates(response)
        assert len(edits) == 2
        assert edits[0].file == "plan"
        assert edits[1].file == "goals"

    def test_mixed_valid_and_invalid(self) -> None:
        response = (
            "<context_update>"
            '{"file": "plan", "action": "append", '
            '"content": "Valid.", "summary": "Good edit"}'
            "</context_update>\n"
            "<context_update>not valid json</context_update>\n"
            "<context_update>"
            '{"file": "goals", "action": "append", '
            '"content": "Also valid.", "summary": "Another edit"}'
            "</context_update>"
        )
        edits = extract_all_context_updates(response)
        assert len(edits) == 2
        assert edits[0].summary == "Good edit"
        assert edits[1].summary == "Another edit"

    def test_disallowed_file_skipped(self) -> None:
        response = (
            "<context_update>"
            '{"file": "soul", "action": "append", '
            '"content": "Nope.", "summary": "Bad"}'
            "</context_update>\n"
            "<context_update>"
            '{"file": "plan", "action": "append", '
            '"content": "Yes.", "summary": "Good"}'
            "</context_update>"
        )
        edits = extract_all_context_updates(response)
        assert len(edits) == 1
        assert edits[0].file == "plan"


# ---------------------------------------------------------------------------
# strip_all_context_updates
# ---------------------------------------------------------------------------


class TestStripAllContextUpdates:
    def test_strips_multiple_blocks(self) -> None:
        response = (
            "Before.\n"
            '<context_update>{"a":"b"}</context_update>\n'
            "Middle.\n"
            '<context_update>{"c":"d"}</context_update>\n'
            "After."
        )
        result = strip_all_context_updates(response)
        assert "<context_update>" not in result
        assert "Before." in result
        assert "Middle." in result
        assert "After." in result

    def test_no_blocks(self) -> None:
        text = "Just text."
        assert strip_all_context_updates(text) == text


class TestCoachFeedbackEntries:
    def test_append_and_update_reason(self, tmp_path: Path) -> None:
        pending = PendingEdits().pop("missing")
        assert pending is None

        edit = ContextEdit(
            file="plan",
            action="replace_section",
            section="## Weekly Structure",
            content="## Weekly Structure\n\nLighter week\n",
            summary="Reduce run volume next week",
        )
        from context_edit import PendingContextEdit

        pending_edit = PendingContextEdit(edit=edit, source="coach", preview="diff")
        entry = new_feedback_entry(pending_edit, "rejected")

        append_coach_feedback(tmp_path, entry)
        content = (tmp_path / "coach_feedback.md").read_text(encoding="utf-8")
        assert entry.feedback_id in content
        assert "Decision: rejected" in content

        assert update_coach_feedback_reason(
            tmp_path,
            entry.feedback_id,
            "Too aggressive after travel week",
        )
        updated = (tmp_path / "coach_feedback.md").read_text(encoding="utf-8")
        assert "Reason: Too aggressive after travel week" in updated
