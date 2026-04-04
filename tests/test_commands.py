"""Tests for coach-specific command behavior."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from commands import cmd_coach, cmd_llm_log, cmd_nudge
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
        assert payload["messages"] == [{"role": "user", "content": "hello"}]
        assert payload["transcript"][-1]["role"] == "assistant_final"
        assert payload["transcript"][-1]["content"] == "response"
        assert payload["nearby_calls"][0]["id"] == call_id
        assert payload["nearby_calls"][0]["selected"] is True

    def test_detail_json_normalizes_tool_calls_and_nearby_calls(
        self,
        in_memory_db,
        capsys,
    ) -> None:
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "show me the latest nudge"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "run_sql",
                            "arguments": '{"query": "SELECT 1"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"rows": [{"value": 1}]}',
            },
        ]
        earlier_id = log_llm_call(
            in_memory_db,
            request_type="chat",
            model="test-model",
            messages=[{"role": "user", "content": "earlier"}],
            response_text="earlier response",
        )
        target_id = log_llm_call(
            in_memory_db,
            request_type="chat",
            model="test-model",
            messages=messages,
            response_text="Here is the answer.",
        )
        other_type_id = log_llm_call(
            in_memory_db,
            request_type="nudge",
            model="test-model",
            messages=[{"role": "user", "content": "different type"}],
            response_text="skip",
        )
        far_id = log_llm_call(
            in_memory_db,
            request_type="chat",
            model="test-model",
            messages=[{"role": "user", "content": "far away"}],
            response_text="later response",
        )
        in_memory_db.execute(
            "UPDATE llm_call SET timestamp = ? WHERE id = ?",
            ("2026-03-15T10:00:30+00:00", earlier_id),
        )
        in_memory_db.execute(
            "UPDATE llm_call SET timestamp = ? WHERE id = ?",
            ("2026-03-15T10:01:00+00:00", target_id),
        )
        in_memory_db.execute(
            "UPDATE llm_call SET timestamp = ? WHERE id = ?",
            ("2026-03-15T10:01:30+00:00", other_type_id),
        )
        in_memory_db.execute(
            "UPDATE llm_call SET timestamp = ? WHERE id = ?",
            ("2026-03-15T10:04:30+00:00", far_id),
        )
        in_memory_db.commit()
        args = SimpleNamespace(
            db="ignored.db",
            last=10,
            stats=False,
            id=target_id,
            feedback=False,
            json=True,
        )

        with patch("commands.open_db", return_value=in_memory_db):
            cmd_llm_log(args)

        payload = json.loads(capsys.readouterr().out)
        assistant_tool_entry = payload["transcript"][2]
        tool_result_entry = payload["transcript"][3]
        nearby_ids = [row["id"] for row in payload["nearby_calls"]]

        assert assistant_tool_entry["role"] == "assistant"
        assert assistant_tool_entry["tool_calls"][0]["name"] == "run_sql"
        assert (
            assistant_tool_entry["tool_calls"][0]["arguments"]
            == '{"query": "SELECT 1"}'
        )
        assert tool_result_entry["role"] == "tool"
        assert tool_result_entry["tool_call_id"] == "call_1"
        assert payload["transcript"][-1]["role"] == "assistant_final"
        assert nearby_ids == [earlier_id, target_id]


class TestCmdNudge:
    def test_retries_when_model_returns_meta_text_instead_of_final_nudge(
        self,
        in_memory_db,
        capsys,
    ) -> None:
        args = SimpleNamespace(
            db="ignored.db",
            model="test-model",
            months=1,
            trigger="new_data",
            email=False,
            telegram=False,
        )
        seen_messages: list[list[dict[str, str]]] = []

        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="run_sql",
                arguments='{"query": "SELECT 1"}',
            ),
        )
        first_result = LLMResult(
            text="Let me check what's actually new since the last notification.",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            tool_calls=[tool_call],
            raw_message={
                "role": "assistant",
                "content": "Let me check what's actually new since the last notification.",
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
        second_result = LLMResult(
            text=(
                "The 9:02 AM notification prescribed today's easy run. "
                "Now the run is done, so that's genuinely new data worth a quick response."
            ),
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            raw_message={
                "role": "assistant",
                "content": (
                    "The 9:02 AM notification prescribed today's easy run. "
                    "Now the run is done, so that's genuinely new data worth a quick response."
                ),
            },
        )
        third_result = LLMResult(
            text=(
                "Easy run done. **5.3 km at 5:42/km, HR 155** on a flat route "
                "was exactly right. Don't add more tonight. Tomorrow is "
                "**tempo only if HRV clears 48 ms**; otherwise easy 5 km."
            ),
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            raw_message={
                "role": "assistant",
                "content": (
                    "Easy run done. **5.3 km at 5:42/km, HR 155** on a flat route "
                    "was exactly right. Don't add more tonight. Tomorrow is "
                    "**tempo only if HRV clears 48 ms**; otherwise easy 5 km."
                ),
            },
        )

        def fake_call_llm(messages, **kwargs):
            seen_messages.append(messages.copy())
            return [first_result, second_result, third_result][len(seen_messages) - 1]

        with (
            patch("commands.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch("commands.open_db", return_value=in_memory_db),
            patch(
                "commands.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W14"}, "days": []},
                    "history": [],
                    "week_complete": False,
                    "week_label": "2026-W14",
                },
            ),
            patch(
                "commands.build_messages",
                return_value=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
            ),
            patch("commands.call_llm", side_effect=fake_call_llm),
            patch("tools.run_sql_tool", return_value=[{"type": "function"}]),
            patch("tools.execute_run_sql", return_value="[]"),
            patch("commands._save_nudge"),
            patch("commands.send_telegram", return_value=123) as send_telegram,
        ):
            result = cmd_nudge(args)

        captured = capsys.readouterr()
        assert result.telegram_message_id == 123
        assert "Let me check" not in captured.out
        assert "genuinely new data worth a quick response" not in captured.out
        assert "Easy run done." in captured.out
        assert len(seen_messages) == 3
        assert seen_messages[1][-1]["content"].startswith("Use the tool results above")
        assert seen_messages[2][-1]["content"].startswith("That was internal reasoning")
        sent_text = send_telegram.call_args.args[0]
        assert sent_text.startswith("**📊 Data Sync**")
        assert "Easy run done." in sent_text
