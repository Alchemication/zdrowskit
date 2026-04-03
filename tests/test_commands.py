"""Tests for coach-specific command behavior."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from commands import cmd_coach, cmd_llm_log
from llm import LLMResult
from store import log_feedback, log_llm_call


class TestCmdCoach:
    def test_preserves_week_complete_when_building_messages(
        self,
        in_memory_db,
        capsys,
    ) -> None:
        args = SimpleNamespace(
            db="ignored.db", model="test-model", week="current", months=3
        )
        seen: dict[str, object] = {}

        def fake_build_messages(
            context, health_data_json, baselines=None, week_complete=True
        ):
            seen["week_complete"] = week_complete
            seen["review_facts"] = context["review_facts"]
            return [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u"},
            ]

        with (
            patch("commands.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch("commands.open_db", return_value=in_memory_db),
            patch("commands.compute_baselines", return_value="baseline md"),
            patch("commands._save_baselines"),
            patch(
                "commands.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W12"}, "days": []},
                    "history": [],
                    "week_complete": False,
                    "week_label": "2026-W12",
                },
            ),
            patch("commands.build_messages", side_effect=fake_build_messages),
            patch(
                "commands.call_llm",
                return_value=LLMResult(
                    text="Plan looks fine.",
                    model="test-model",
                    input_tokens=1,
                    output_tokens=1,
                    total_tokens=2,
                    latency_s=0.1,
                ),
            ),
        ):
            cmd_coach(args)

        captured = capsys.readouterr()
        assert "Plan looks fine." in captured.out
        assert seen["week_complete"] is False
        assert "Shared Review Facts" in str(seen["review_facts"])

    def test_extracts_edits_from_tool_calls(self, in_memory_db, capsys) -> None:
        args = SimpleNamespace(
            db="ignored.db", model="test-model", week="last", months=3
        )

        # First call: LLM returns a tool call (no visible text yet).
        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="update_context",
                arguments=(
                    '{"file": "plan", "action": "replace_section", '
                    '"section": "## Weekly Structure", '
                    '"content": "## Weekly Structure\\n\\nOne lighter week.\\n", '
                    '"summary": "Lighten next week"}'
                ),
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
                    {"id": "call_1", "function": {"name": "update_context"}},
                ],
            },
        )
        # Second call: LLM returns text, no more tool calls.
        second_result = LLMResult(
            text="Reduce run volume for one week.",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )

        with (
            patch("commands.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch("commands.open_db", return_value=in_memory_db),
            patch("commands.compute_baselines", return_value="baseline md"),
            patch("commands._save_baselines"),
            patch(
                "commands.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W12"}, "days": []},
                    "history": [],
                    "week_complete": True,
                    "week_label": "2026-W12",
                },
            ),
            patch(
                "commands.build_messages",
                return_value=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
            ),
            patch(
                "commands.call_llm",
                side_effect=[first_result, second_result],
            ),
        ):
            visible_text, edits = cmd_coach(args)

        captured = capsys.readouterr()
        assert "Reduce run volume" in captured.out
        assert len(edits) == 1
        assert edits[0].summary == "Lighten next week"
        assert edits[0].file == "plan"
        assert edits[0].section == "## Weekly Structure"


class TestCmdLlmLog:
    def test_feedback_json_view(self, in_memory_db, capsys) -> None:
        call_id = log_llm_call(
            in_memory_db,
            request_type="insights",
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_text="response",
        )
        log_feedback(
            in_memory_db,
            llm_call_id=call_id,
            category="wrong_tone",
            message_type="insights",
            reason="Too harsh for a weekly review.",
        )
        args = SimpleNamespace(
            db="ignored.db",
            last=10,
            stats=False,
            id=None,
            feedback=True,
            json=True,
        )

        with patch("commands.open_db", return_value=in_memory_db):
            cmd_llm_log(args)

        payload = json.loads(capsys.readouterr().out)
        assert len(payload) == 1
        assert payload[0]["category"] == "wrong_tone"
        assert payload[0]["request_type"] == "insights"
        assert payload[0]["reason"] == "Too harsh for a weekly review."

    def test_detail_json_still_returns_call_row(self, in_memory_db, capsys) -> None:
        call_id = log_llm_call(
            in_memory_db,
            request_type="chat",
            model="test-model",
            messages=[{"role": "user", "content": "hello"}],
            response_text="response",
        )
        args = SimpleNamespace(
            db="ignored.db",
            last=10,
            stats=False,
            id=call_id,
            feedback=False,
            json=True,
        )

        with patch("commands.open_db", return_value=in_memory_db):
            cmd_llm_log(args)

        payload = json.loads(capsys.readouterr().out)
        assert payload["id"] == call_id
        assert payload["request_type"] == "chat"
