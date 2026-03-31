"""Tests for coach-specific command behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from commands import cmd_coach
from llm import LLMResult


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

    def test_strips_context_updates_and_returns_edits(
        self, in_memory_db, capsys
    ) -> None:
        args = SimpleNamespace(
            db="ignored.db", model="test-model", week="last", months=3
        )
        llm_text = (
            "Reduce run volume for one week.\n"
            "<context_update>"
            '{"file": "plan", "action": "replace_section", '
            '"section": "## Weekly Structure", '
            '"content": "## Weekly Structure\\n\\nOne lighter week.\\n", '
            '"summary": "Lighten next week"}'
            "</context_update>"
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
                return_value=LLMResult(
                    text=llm_text,
                    model="test-model",
                    input_tokens=1,
                    output_tokens=1,
                    total_tokens=2,
                    latency_s=0.1,
                ),
            ),
        ):
            visible_text, edits = cmd_coach(args)

        captured = capsys.readouterr()
        assert "<context_update>" not in visible_text
        assert "<context_update>" not in captured.out
        assert len(edits) == 1
        assert edits[0].summary == "Lighten next week"
