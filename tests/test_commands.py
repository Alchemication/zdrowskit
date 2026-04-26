"""Tests for coach-specific command behavior."""

from __future__ import annotations

import sqlite3
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cmd_db import cmd_db
from cmd_llm import (
    _apply_verification,
    _query_today_snapshot,
    cmd_coach,
    cmd_insights,
    cmd_nudge,
    interpret_notify_request,
)
from cmd_llm_log import cmd_llm_log
from commands import TELEGRAM_BOT_COMMANDS
from config import MAX_TOKENS_INSIGHTS
from llm import LLMResult
from llm_verify import VerificationResult
from store import log_feedback, log_llm_call, open_db


class TestTelegramBotCommands:
    def test_registered_bot_commands_match_telegram_surface(self) -> None:
        assert TELEGRAM_BOT_COMMANDS == [
            {"command": "review", "description": "Weekly report"},
            {
                "command": "coach",
                "description": "Coaching review (strategy proposals)",
            },
            {"command": "add", "description": "Log a workout or sleep"},
            {"command": "log", "description": "Fast daily log entry via tap-keyboard"},
            {"command": "status", "description": "Bot and data status"},
            {
                "command": "events",
                "description": "Recent system events (nudges, imports, …)",
            },
            {"command": "notify", "description": "Notification settings"},
            {"command": "models", "description": "Model routing settings"},
            {"command": "context", "description": "View context files"},
            {"command": "clear", "description": "Reset chat memory"},
            {"command": "tutorial", "description": "Guided tour of zdrowskit"},
            {"command": "help", "description": "Command list"},
        ]


class TestLogFlowSnapshot:
    def test_uses_previous_night_sleep_in_today_snapshot(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        in_memory_db.execute(
            """
            INSERT INTO daily (date, imported_at, sleep_total_h, sleep_efficiency_pct)
            VALUES (?, ?, ?, ?)
            """,
            ("2026-04-19", "2026-04-20T08:00:00+00:00", 7.5, 94.0),
        )
        snapshot = _query_today_snapshot(in_memory_db, date(2026, 4, 20))
        assert "Last night: 7.50h, 94% efficiency" in snapshot


class TestVerificationGate:
    def test_config_disabled_skips_verifier(
        self,
        in_memory_db: sqlite3.Connection,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("cmd_llm.ENABLE_LLM_VERIFICATION", False)

        def fail_verify(**kwargs):
            raise AssertionError("verifier should not run")

        monkeypatch.setattr("cmd_llm.verify_and_rewrite", fail_verify)

        approved = _apply_verification(
            kind="insights",
            draft="draft",
            evidence={},
            source_messages=[],
            conn=in_memory_db,
            metadata={},
        )

        assert approved == "draft"

    def test_revised_text_is_used_when_enabled(
        self,
        in_memory_db: sqlite3.Connection,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("cmd_llm.ENABLE_LLM_VERIFICATION", True)
        monkeypatch.setattr("cmd_llm.VERIFY_INSIGHTS", True)

        def fake_verify(**kwargs):
            return VerificationResult(
                verdict="revise",
                issues=[],
                revised_text="fixed draft",
            )

        monkeypatch.setattr("cmd_llm.verify_and_rewrite", fake_verify)

        approved = _apply_verification(
            kind="insights",
            draft="draft",
            evidence={},
            source_messages=[],
            conn=in_memory_db,
            metadata={},
        )

        assert approved == "fixed draft"

    def test_fail_returns_none_when_enabled(
        self,
        in_memory_db: sqlite3.Connection,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("cmd_llm.ENABLE_LLM_VERIFICATION", True)
        monkeypatch.setattr("cmd_llm.VERIFY_NUDGE", True)
        monkeypatch.setattr(
            "cmd_llm.verify_and_rewrite",
            lambda **kwargs: VerificationResult(verdict="fail", issues=[]),
        )

        approved = _apply_verification(
            kind="nudge",
            draft="weak nudge",
            evidence={},
            source_messages=[],
            conn=in_memory_db,
            metadata={},
        )

        assert approved is None

    def test_strict_flag_threads_through_to_verify_and_rewrite(
        self,
        in_memory_db: sqlite3.Connection,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr("cmd_llm.ENABLE_LLM_VERIFICATION", True)
        monkeypatch.setattr("cmd_llm.VERIFY_COACH", True)

        captured: dict[str, object] = {}

        def fake_verify(**kwargs):
            captured.update(kwargs)
            return VerificationResult(verdict="fail", issues=[])

        monkeypatch.setattr("cmd_llm.verify_and_rewrite", fake_verify)

        _apply_verification(
            kind="coach",
            draft="bundle",
            evidence={},
            source_messages=[],
            conn=in_memory_db,
            metadata={},
            strict=True,
        )

        assert captured["strict"] is True


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
            context,
            health_data_text,
            baselines=None,
            milestones=None,
            week_complete=True,
        ):
            seen["week_complete"] = week_complete
            seen["review_facts"] = context["review_facts"]
            return [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u"},
            ]

        with (
            patch("cmd_llm.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch("cmd_llm.open_db", return_value=in_memory_db),
            patch("cmd_llm.compute_baselines", return_value="baseline md"),
            patch("cmd_llm._save_baselines"),
            patch(
                "cmd_llm.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W12"}, "days": []},
                    "history": [],
                    "week_complete": False,
                    "week_label": "2026-W12",
                },
            ),
            patch("cmd_llm.build_messages", side_effect=fake_build_messages),
            patch(
                "cmd_llm.call_llm",
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
                    '{"file": "strategy", "action": "replace_section", '
                    '"section": "## Weekly Plan", '
                    '"content": "## Weekly Plan\\n\\nOne lighter week.\\n", '
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
            patch("cmd_llm.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch("cmd_llm.open_db", return_value=in_memory_db),
            patch("cmd_llm.compute_baselines", return_value="baseline md"),
            patch("cmd_llm._save_baselines"),
            patch(
                "cmd_llm.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W12"}, "days": []},
                    "history": [],
                    "week_complete": True,
                    "week_label": "2026-W12",
                },
            ),
            patch(
                "cmd_llm.build_messages",
                return_value=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
            ),
            patch(
                "cmd_llm.call_llm",
                side_effect=[first_result, second_result],
            ),
            patch(
                "cmd_llm.build_edit_preview",
                return_value="--- strategy.md\n+++ strategy.md (proposed)\n",
            ),
        ):
            cmd_result, proposals = cmd_coach(args)

        captured = capsys.readouterr()
        assert "Reduce run volume" in captured.out
        assert len(proposals) == 1
        assert proposals[0].edit.summary == "Lighten next week"
        assert proposals[0].edit.file == "strategy"
        assert proposals[0].edit.section == "## Weekly Plan"
        assert "Reduce run volume" in (cmd_result.text or "")

    def test_verifier_fail_suppresses_coach_bundle(
        self,
        in_memory_db,
        capsys,
        monkeypatch,
    ) -> None:
        args = SimpleNamespace(
            db="ignored.db", model="test-model", week="current", months=3
        )
        result = LLMResult(
            text="Change the plan because this week felt messy.",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            llm_call_id=10,
        )
        monkeypatch.setattr("cmd_llm.ENABLE_LLM_VERIFICATION", True)
        monkeypatch.setattr("cmd_llm.VERIFY_COACH", True)
        monkeypatch.setattr(
            "cmd_llm.verify_and_rewrite",
            lambda **kwargs: VerificationResult(verdict="fail", issues=[]),
        )

        with (
            patch("cmd_llm.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch("cmd_llm.open_db", return_value=in_memory_db),
            patch("cmd_llm.compute_baselines", return_value="baseline md"),
            patch("cmd_llm._save_baselines"),
            patch(
                "cmd_llm.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W14"}, "days": []},
                    "history": [],
                    "week_complete": False,
                    "week_label": "2026-W14",
                },
            ),
            patch(
                "cmd_llm.build_messages",
                return_value=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
            ),
            patch("cmd_llm.call_llm", return_value=result),
        ):
            cmd_result, proposals = cmd_coach(args)

        assert capsys.readouterr().out == ""
        assert cmd_result.text is None
        assert proposals == []


class TestCmdInsights:
    def test_forces_synthesis_when_loop_exits_with_empty_text(
        self,
        in_memory_db,
        capsys,
    ) -> None:
        """If the tool loop exhausts iterations with empty text, a final
        tool-less synthesis call must run so we never ship a blank report."""
        args = SimpleNamespace(
            db="ignored.db",
            model="test-model",
            months=1,
            week="last",
            no_update_baselines=True,
            no_update_history=True,
            explain=False,
            email=False,
            telegram=False,
        )

        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="run_sql",
                arguments='{"query": "SELECT 1"}',
            ),
        )
        # All three iteration slots return empty text + a pending tool call.
        empty_with_tool = LLMResult(
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
        # Final synthesis call returns real text.
        synthesis_result = LLMResult(
            text="W14 Review: solid week, no changes needed.",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )

        seen_kwargs: list[dict] = []

        def fake_call_llm(messages, **kwargs):
            seen_kwargs.append(kwargs)
            # Iterations 0, 1, 2 → empty + tool call. Iteration 3 (final
            # synthesis, called with tools=None) → real text.
            if kwargs.get("tools") is None:
                return synthesis_result
            return empty_with_tool

        with (
            patch("cmd_llm.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch("cmd_llm.open_db", return_value=in_memory_db),
            patch(
                "cmd_llm.build_llm_data",
                return_value={
                    "current_week": {
                        "summary": {"week_label": "2026-W14"},
                        "days": [],
                    },
                    "history": [],
                    "week_complete": True,
                    "week_label": "2026-W14",
                },
            ),
            patch("cmd_llm.build_review_facts", return_value="facts"),
            patch(
                "cmd_llm.build_messages",
                return_value=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
            ),
            patch("cmd_llm.call_llm", side_effect=fake_call_llm),
            patch("tools.run_sql_tool", return_value=[{"type": "function"}]),
            patch("tools.execute_run_sql", return_value="[]"),
            patch("cmd_llm._save_report", return_value=Path("/tmp/r.md")),
        ):
            result = cmd_insights(args)

        captured = capsys.readouterr()
        assert "W14 Review: solid week" in captured.out
        assert "Generated by" not in captured.out
        # MAX_TOOL_ITERATIONS_INSIGHTS in-loop iterations + 1 forced synthesis.
        from config import MAX_TOOL_ITERATIONS_INSIGHTS

        assert len(seen_kwargs) == MAX_TOOL_ITERATIONS_INSIGHTS + 1
        # Final call must have tools disabled.
        assert seen_kwargs[-1].get("tools") is None
        # Final-call metadata flags the synthesis fallback.
        assert seen_kwargs[-1]["metadata"]["iteration"] == "final_synthesis"
        # The returned CommandResult carries the synthesised text, not blank.
        assert "W14 Review" in result.text
        assert "Generated by" not in result.text

    def test_retries_empty_synthesis_without_reasoning(
        self,
        in_memory_db,
        capsys,
    ) -> None:
        """Anthropic extended thinking can return empty content even during
        synthesis. Retry once without reasoning before giving up. The retry
        is only meaningful for models that actually accept reasoning_effort."""
        args = SimpleNamespace(
            db="ignored.db",
            model="anthropic/claude-opus-4-6",
            months=1,
            week="last",
            no_update_baselines=True,
            no_update_history=True,
            explain=False,
            email=False,
            telegram=False,
            reasoning_effort="medium",
        )

        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="run_sql",
                arguments='{"query": "SELECT 1"}',
            ),
        )
        empty_with_tool = LLMResult(
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
        empty_without_tool = LLMResult(
            text="",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )
        synthesis_result = LLMResult(
            text="W14 Review: synthesis worked without reasoning.",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )

        seen_kwargs: list[dict] = []
        loop_calls = 0

        def fake_call_llm(messages, **kwargs):
            nonlocal loop_calls
            seen_kwargs.append(kwargs)
            if kwargs.get("tools") is not None:
                loop_calls += 1
                return empty_with_tool if loop_calls == 1 else empty_without_tool
            if kwargs.get("reasoning_effort") is None:
                return synthesis_result
            return empty_without_tool

        with (
            patch("cmd_llm.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch("cmd_llm.open_db", return_value=in_memory_db),
            patch(
                "cmd_llm.build_llm_data",
                return_value={
                    "current_week": {
                        "summary": {"week_label": "2026-W14"},
                        "days": [],
                    },
                    "history": [],
                    "week_complete": True,
                    "week_label": "2026-W14",
                },
            ),
            patch("cmd_llm.build_review_facts", return_value="facts"),
            patch(
                "cmd_llm.build_messages",
                return_value=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
            ),
            patch("cmd_llm.call_llm", side_effect=fake_call_llm),
            patch("tools.run_sql_tool", return_value=[{"type": "function"}]),
            patch("tools.execute_run_sql", return_value="[]"),
            patch("cmd_llm._save_report", return_value=Path("/tmp/r.md")),
        ):
            result = cmd_insights(args)

        captured = capsys.readouterr()
        assert "synthesis worked without reasoning" in captured.out
        assert result.text == "W14 Review: synthesis worked without reasoning."
        assert seen_kwargs[-2]["metadata"]["iteration"] == "final_synthesis"
        assert seen_kwargs[-2]["reasoning_effort"] == "medium"
        assert (
            seen_kwargs[-1]["metadata"]["iteration"] == "final_synthesis_no_reasoning"
        )
        assert seen_kwargs[-1]["reasoning_effort"] is None

    def test_retries_truncated_report_with_concise_synthesis(
        self,
        in_memory_db,
        capsys,
    ) -> None:
        """If the final report hits max_tokens, retry before saving."""
        args = SimpleNamespace(
            db="ignored.db",
            model="test-model",
            months=1,
            week="last",
            no_update_baselines=True,
            no_update_history=True,
            explain=False,
            email=False,
            telegram=False,
            reasoning_effort="none",
        )

        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="run_sql",
                arguments='{"query": "SELECT 1"}',
            ),
        )
        empty_with_tool = LLMResult(
            text="",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            max_tokens=MAX_TOKENS_INSIGHTS,
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
        truncated_result = LLMResult(
            text="W14 Review: started but cut off",
            model="test-model",
            input_tokens=1,
            output_tokens=MAX_TOKENS_INSIGHTS,
            total_tokens=MAX_TOKENS_INSIGHTS + 1,
            latency_s=0.1,
            max_tokens=MAX_TOKENS_INSIGHTS,
        )
        concise_result = LLMResult(
            text="W14 Review: concise and complete.\n\n<memory>\n- Good week.\n</memory>",
            model="test-model",
            input_tokens=1,
            output_tokens=120,
            total_tokens=121,
            latency_s=0.1,
            max_tokens=MAX_TOKENS_INSIGHTS,
        )

        seen_kwargs: list[dict] = []

        def fake_call_llm(messages, **kwargs):
            seen_kwargs.append(kwargs)
            if len(seen_kwargs) == 1:
                return empty_with_tool
            if len(seen_kwargs) == 2:
                return truncated_result
            return concise_result

        saved_reports: list[str] = []

        def fake_save_report(report: str, week: str) -> Path:
            saved_reports.append(report)
            return Path("/tmp/r.md")

        with (
            patch("cmd_llm.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch("cmd_llm.open_db", return_value=in_memory_db),
            patch(
                "cmd_llm.build_llm_data",
                return_value={
                    "current_week": {
                        "summary": {"week_label": "2026-W14"},
                        "days": [],
                    },
                    "history": [],
                    "week_complete": True,
                    "week_label": "2026-W14",
                },
            ),
            patch("cmd_llm.build_review_facts", return_value="facts"),
            patch(
                "cmd_llm.build_messages",
                return_value=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
            ),
            patch("cmd_llm.call_llm", side_effect=fake_call_llm),
            patch("tools.run_sql_tool", return_value=[{"type": "function"}]),
            patch("tools.execute_run_sql", return_value="[]"),
            patch("cmd_llm._save_report", side_effect=fake_save_report),
        ):
            result = cmd_insights(args)

        captured = capsys.readouterr()
        assert "concise and complete" in captured.out
        assert result.text == "W14 Review: concise and complete."
        assert saved_reports == ["W14 Review: concise and complete."]
        assert seen_kwargs[-1]["metadata"]["iteration"] == "truncation_retry"
        assert seen_kwargs[-1]["reasoning_effort"] is None


class TestCmdCoachEmptyResponseFallback:
    def test_forces_synthesis_when_loop_exits_with_empty_text(
        self,
        in_memory_db,
        capsys,
    ) -> None:
        """Coach loop must force a tool-less synthesis call when iteration cap
        is reached with empty text + pending tool calls."""
        args = SimpleNamespace(
            db="ignored.db", model="test-model", week="last", months=3
        )

        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="run_sql",
                arguments='{"query": "SELECT 1"}',
            ),
        )
        empty_with_tool = LLMResult(
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
        synthesis_result = LLMResult(
            text="No changes — plan is working. HRV stable, runs on target.",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )

        seen_kwargs: list[dict] = []

        def fake_call_llm(messages, **kwargs):
            seen_kwargs.append(kwargs)
            if kwargs.get("tools") is None:
                return synthesis_result
            return empty_with_tool

        with (
            patch("cmd_llm.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch("cmd_llm.open_db", return_value=in_memory_db),
            patch("cmd_llm.compute_baselines", return_value="baseline md"),
            patch("cmd_llm._save_baselines"),
            patch(
                "cmd_llm.build_llm_data",
                return_value={
                    "current_week": {
                        "summary": {"week_label": "2026-W14"},
                        "days": [],
                    },
                    "history": [],
                    "week_complete": True,
                    "week_label": "2026-W14",
                },
            ),
            patch(
                "cmd_llm.build_messages",
                return_value=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
            ),
            patch("cmd_llm.call_llm", side_effect=fake_call_llm),
        ):
            cmd_result, edits = cmd_coach(args)

        captured = capsys.readouterr()
        from config import MAX_TOOL_ITERATIONS_COACH

        assert len(seen_kwargs) == MAX_TOOL_ITERATIONS_COACH + 1
        assert seen_kwargs[-1].get("tools") is None
        assert seen_kwargs[-1]["metadata"]["iteration"] == "final_synthesis"
        assert "No changes" in captured.out
        assert edits == []


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

        with patch("cmd_llm_log.open_db", return_value=in_memory_db):
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

        with patch("cmd_llm_log.open_db", return_value=in_memory_db):
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

        with patch("cmd_llm_log.open_db", return_value=in_memory_db):
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

    def test_detail_json_includes_related_verification_calls(
        self,
        in_memory_db,
        capsys,
    ) -> None:
        source_id = log_llm_call(
            in_memory_db,
            request_type="nudge",
            model="draft-model",
            messages=[{"role": "user", "content": "nudge"}],
            response_text="Weak nudge.",
            metadata={
                "nudge_verification": {
                    "verdict": "fail",
                    "verifier_call_id": None,
                    "issue_count": 1,
                }
            },
        )
        verify_id = log_llm_call(
            in_memory_db,
            request_type="nudge_verify",
            model="verify-model",
            messages=[{"role": "user", "content": "{}"}],
            response_text='{"verdict":"fail","issues":[],"confidence":"high"}',
            metadata={
                "source_llm_call_id": source_id,
                "stage": "verify",
                "verdict": "fail",
                "issue_count": 1,
            },
        )
        rewrite_id = log_llm_call(
            in_memory_db,
            request_type="nudge_rewrite",
            model="rewrite-model",
            messages=[{"role": "user", "content": "{}"}],
            response_text="SKIP",
            metadata={
                "source_llm_call_id": source_id,
                "stage": "rewrite",
                "verdict": "revise",
                "issue_count": 1,
            },
        )
        args = SimpleNamespace(
            db="ignored.db",
            last=10,
            stats=False,
            id=source_id,
            feedback=False,
            json=True,
        )

        with patch("cmd_llm_log.open_db", return_value=in_memory_db):
            cmd_llm_log(args)

        payload = json.loads(capsys.readouterr().out)
        related = payload["related_verification_calls"]
        assert [row["id"] for row in related] == [source_id, verify_id, rewrite_id]
        assert [row["relationship"] for row in related] == [
            "source",
            "verify",
            "rewrite",
        ]
        assert related[1]["metadata"]["source_llm_call_id"] == source_id


class TestCmdNudge:
    def test_verifier_fail_turns_nudge_into_skip(
        self,
        in_memory_db,
        capsys,
        monkeypatch,
    ) -> None:
        args = SimpleNamespace(
            db="ignored.db",
            model="test-model",
            months=1,
            trigger="new_data",
            email=False,
            telegram=True,
        )
        result = LLMResult(
            text="Your run was fine, maybe do something later.",
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            llm_call_id=9,
        )
        monkeypatch.setattr("cmd_llm.ENABLE_LLM_VERIFICATION", True)
        monkeypatch.setattr("cmd_llm.VERIFY_NUDGE", True)
        monkeypatch.setattr(
            "cmd_llm.verify_and_rewrite",
            lambda **kwargs: VerificationResult(verdict="fail", issues=[]),
        )

        with (
            patch("cmd_llm.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch("cmd_llm.open_db", return_value=in_memory_db),
            patch(
                "cmd_llm.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W14"}, "days": []},
                    "history": [],
                    "week_complete": False,
                    "week_label": "2026-W14",
                },
            ),
            patch(
                "cmd_llm.build_messages",
                return_value=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
            ),
            patch("cmd_llm.call_llm", return_value=result),
            patch("tools.run_sql_tool", return_value=[{"type": "function"}]),
            patch("cmd_llm._save_nudge") as save_nudge,
            patch("cmd_llm.send_telegram") as send_telegram,
        ):
            cmd_result = cmd_nudge(args)

        assert capsys.readouterr().out == ""
        assert cmd_result.text is None
        save_nudge.assert_not_called()
        send_telegram.assert_not_called()

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
            patch("cmd_llm.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch("cmd_llm.open_db", return_value=in_memory_db),
            patch(
                "cmd_llm.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W14"}, "days": []},
                    "history": [],
                    "week_complete": False,
                    "week_label": "2026-W14",
                },
            ),
            patch(
                "cmd_llm.build_messages",
                return_value=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
            ),
            patch("cmd_llm.call_llm", side_effect=fake_call_llm),
            patch("tools.run_sql_tool", return_value=[{"type": "function"}]),
            patch("tools.execute_run_sql", return_value="[]"),
            patch("cmd_llm._save_nudge"),
            patch("cmd_llm.send_telegram", return_value=123) as send_telegram,
        ):
            result = cmd_nudge(args)

        captured = capsys.readouterr()
        assert result.telegram_message_id == 123
        assert "Let me check" not in captured.out
        assert "genuinely new data worth a quick response" not in captured.out
        assert "Easy run done." in captured.out
        assert "Generated by" not in captured.out
        assert len(seen_messages) == 3
        assert seen_messages[1][-1]["content"].startswith("Use the tool results above")
        assert seen_messages[2][-1]["content"].startswith("That was internal reasoning")
        sent_text = send_telegram.call_args.args[0]
        assert sent_text.startswith("**📊 Data Sync**")
        assert "Easy run done." in sent_text
        assert "Generated by" not in sent_text


class TestInterpretNotifyRequest:
    def test_validates_and_returns_structured_payload(self, in_memory_db) -> None:
        result = LLMResult(
            text=json.dumps(
                {
                    "status": "proposal",
                    "intent": "set",
                    "changes": [
                        {
                            "action": "set",
                            "path": "nudges.earliest_time",
                            "value": "11:00",
                        }
                    ],
                    "summary": "Move nudges to after 11:00.",
                    "clarification_question": None,
                    "reason": "direct time request",
                }
            ),
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
            llm_call_id=7,
        )

        with (
            patch("cmd_llm.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch(
                "cmd_llm.build_messages",
                return_value=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
            ),
            patch("cmd_llm.open_db", return_value=in_memory_db),
            patch("cmd_llm.call_llm", return_value=result),
        ):
            payload = interpret_notify_request(
                "no nudges before 11am",
                db="ignored.db",
                prefs={"overrides": {}, "temporary_mutes": [], "version": 1},
            )

        assert payload["status"] == "proposal"
        assert payload["changes"][0]["path"] == "nudges.earliest_time"
        assert payload["llm_call_id"] == 7

    def test_requires_clarification_question_when_needed(self, in_memory_db) -> None:
        result = LLMResult(
            text=json.dumps(
                {
                    "status": "needs_clarification",
                    "intent": "set",
                    "changes": [],
                    "summary": "",
                    "clarification_question": "Do you mean weekly insights or the midweek report?",
                    "reason": "report type ambiguous",
                }
            ),
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )

        with (
            patch("cmd_llm.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch(
                "cmd_llm.build_messages",
                return_value=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
            ),
            patch("cmd_llm.open_db", return_value=in_memory_db),
            patch("cmd_llm.call_llm", return_value=result),
        ):
            payload = interpret_notify_request(
                "move reports to Tuesday",
                db="ignored.db",
                prefs={"overrides": {}, "temporary_mutes": [], "version": 1},
            )

        assert payload["status"] == "needs_clarification"
        assert payload["clarification_question"].startswith("Do you mean")

    def test_rejects_out_of_bounds_nudge_cap(self, in_memory_db) -> None:
        result = LLMResult(
            text=json.dumps(
                {
                    "status": "proposal",
                    "intent": "set",
                    "changes": [
                        {
                            "action": "set",
                            "path": "nudges.max_per_day",
                            "value": 0,
                        }
                    ],
                    "summary": "Set max nudges to zero.",
                    "clarification_question": None,
                    "reason": "bad proposal",
                }
            ),
            model="test-model",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_s=0.1,
        )

        with (
            patch("cmd_llm.load_context", return_value={"prompt": "x", "soul": "y"}),
            patch(
                "cmd_llm.build_messages",
                return_value=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
            ),
            patch("cmd_llm.open_db", return_value=in_memory_db),
            patch("cmd_llm.call_llm", return_value=result),
        ):
            try:
                interpret_notify_request(
                    "set max nudges per day to 0",
                    db="ignored.db",
                    prefs={"overrides": {}, "temporary_mutes": [], "version": 1},
                )
            except ValueError as exc:
                assert "between 1 and 6" in str(exc)
            else:
                raise AssertionError("Expected ValueError for invalid nudge cap")


class TestCmdDb:
    def test_status_shows_applied_migrations(self, tmp_path: Path, capsys) -> None:
        db_path = tmp_path / "test.db"
        conn = open_db(db_path)
        conn.close()

        args = SimpleNamespace(db=str(db_path), db_cmd="status")
        cmd_db(args)

        out = capsys.readouterr().out
        assert "Current migration:" in out
        assert "File size:" in out
        assert "Table Stats" in out
        assert "schema_migrations" in out
        assert "20260404_153000__001_initial_schema" in out
        assert "applied" in out

    def test_schema_prints_live_schema(self, tmp_path: Path, capsys) -> None:
        db_path = tmp_path / "test.db"
        conn = open_db(db_path)
        conn.close()

        args = SimpleNamespace(db=str(db_path), db_cmd="schema")
        cmd_db(args)

        out = capsys.readouterr().out
        assert "CREATE TABLE schema_migrations" in out
        assert "CREATE TABLE daily" in out
        assert "CREATE TABLE workout" in out

    def test_migrate_applies_pending_legacy_migrations(
        self, tmp_path: Path, capsys
    ) -> None:
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE daily (
                date                        TEXT PRIMARY KEY,
                steps                       INTEGER,
                distance_km                 REAL,
                active_energy_kj            REAL,
                exercise_min                INTEGER,
                stand_hours                 INTEGER,
                flights_climbed             REAL,
                resting_hr                  INTEGER,
                hrv_ms                      REAL,
                walking_hr_avg              REAL,
                hr_day_min                  INTEGER,
                hr_day_max                  INTEGER,
                vo2max                      REAL,
                walking_speed_kmh           REAL,
                walking_step_length_cm      REAL,
                walking_asymmetry_pct       REAL,
                walking_double_support_pct  REAL,
                stair_speed_up_ms           REAL,
                stair_speed_down_ms         REAL,
                running_stride_length_m     REAL,
                running_power_w             REAL,
                running_speed_kmh           REAL,
                recovery_index              REAL,
                imported_at                 TEXT NOT NULL
            );
            CREATE TABLE workout (
                start_utc                TEXT PRIMARY KEY,
                date                     TEXT NOT NULL,
                type                     TEXT NOT NULL,
                category                 TEXT NOT NULL,
                duration_min             REAL NOT NULL,
                hr_min                   INTEGER,
                hr_avg                   REAL,
                hr_max                   INTEGER,
                active_energy_kj         REAL,
                intensity_kcal_per_hr_kg REAL,
                temperature_c            REAL,
                humidity_pct             INTEGER,
                gpx_distance_km          REAL,
                gpx_elevation_gain_m     REAL,
                gpx_avg_speed_ms         REAL,
                gpx_max_speed_p95_ms     REAL,
                imported_at              TEXT NOT NULL
            );
            CREATE TABLE llm_call (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT NOT NULL,
                request_type    TEXT NOT NULL,
                model           TEXT NOT NULL,
                messages_json   TEXT NOT NULL,
                response_text   TEXT NOT NULL,
                params_json     TEXT,
                input_tokens    INTEGER NOT NULL,
                output_tokens   INTEGER NOT NULL,
                total_tokens    INTEGER NOT NULL,
                latency_s       REAL NOT NULL,
                metadata_json   TEXT
            );
            """
        )
        conn.close()

        args = SimpleNamespace(db=str(db_path), db_cmd="migrate")
        cmd_db(args)

        out = capsys.readouterr().out
        assert "Applied" in out
        assert "20260404_154500__002_add_llm_call_cost" in out
