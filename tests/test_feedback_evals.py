"""Tests for the feedback-derived eval framework."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import llm
import pytest

from evals.framework import (
    EvalExecution,
    CapturedToolCall,
    load_cases,
    run_assertions,
    run_case,
)
from evals import run as eval_run
from evals.run import select_cases


def _tool_call(name: str, arguments: dict, tool_id: str = "call_1") -> SimpleNamespace:
    return SimpleNamespace(
        id=tool_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _llm_result(
    text: str,
    tool_calls: list | None = None,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> SimpleNamespace:
    raw_message = {"role": "assistant", "content": text}
    if tool_calls:
        raw_message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in tool_calls
        ]
    return SimpleNamespace(
        text=text,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        latency_s=0.01,
        cost=0.001,
        raw_message=raw_message,
    )


class TestCaseLoading:
    def test_load_cases_reads_real_feedback_case(self) -> None:
        cases = load_cases()

        case = next(case for case in cases if case.id == "chat_log_life_disruption")

        assert case.feature == "chat"
        assert case.case_kind == "real_regression"
        assert case.source_feedback_id == 10
        assert case.source_llm_call_id == 288
        assert case.derived_from["feedback_id"] == 10
        assert "thumbs-down feedback #10" in case.notes
        assert case.fixture["today"] == "2026-04-09"

    def test_synthetic_cases_keep_feedback_provenance(self) -> None:
        cases = {case.id: case for case in load_cases()}

        positive = cases["chat_explicit_add_to_log"]
        negative = cases["chat_plan_lookup_no_log"]

        assert positive.case_kind == "synthetic_positive"
        assert negative.case_kind == "synthetic_negative"
        assert positive.source_feedback_id == 10
        assert negative.source_feedback_id == 10
        assert positive.derived_from["llm_call_id"] == 288
        assert negative.derived_from["llm_call_id"] == 288
        assert "hypothesis" in positive.derived_from
        assert "hypothesis" in negative.derived_from


class TestChatRunner:
    def test_chat_case_captures_context_update_without_mutating(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        case = next(
            case for case in load_cases() if case.id == "chat_log_life_disruption"
        )
        tool_call = _tool_call(
            "update_context",
            {
                "file": "log",
                "action": "append",
                "content": (
                    "## 2026-04-09\n\n"
                    "- Small one is sick and did not go to creche; strength "
                    "session may be hard today."
                ),
                "summary": "Logged child sickness disrupting strength session.",
            },
        )
        mock_call = MagicMock(
            side_effect=[
                _llm_result("", tool_calls=[tool_call]),
                _llm_result(
                    "Logged that. If strength gets squeezed, shift it to Friday.",
                    tool_calls=[],
                ),
            ]
        )
        monkeypatch.setattr(llm, "call_llm", mock_call)

        result = run_case(case, model="test-model")

        assert result.passed
        assert result.execution is not None
        assert result.execution.tool_calls[0].name == "update_context"
        assert mock_call.call_count == 2
        second_messages = mock_call.call_args_list[1].args[0]
        assert second_messages[-1]["role"] == "tool"
        assert (
            second_messages[-1]["content"] == "Proposed. User will be asked to confirm."
        )

    def test_chat_case_detects_tool_calls_from_raw_message_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        case = next(
            case for case in load_cases() if case.id == "chat_explicit_add_to_log"
        )
        raw_tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "update_context",
                "arguments": json.dumps(
                    {
                        "file": "log",
                        "action": "append",
                        "content": (
                            "## 2026-04-09\n\n"
                            "- Small one is sick and did not go to creche; "
                            "strength may not happen."
                        ),
                        "summary": "Logged child sickness disrupting strength.",
                    }
                ),
            },
        }
        mock_call = MagicMock(
            side_effect=[
                SimpleNamespace(
                    text="",
                    tool_calls=None,
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                    latency_s=0.01,
                    cost=0.001,
                    raw_message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [raw_tool_call],
                    },
                ),
                _llm_result("Logged it.", tool_calls=[]),
            ]
        )
        monkeypatch.setattr(llm, "call_llm", mock_call)

        result = run_case(case, model="test-model")

        assert result.passed
        assert result.execution is not None
        assert result.execution.tool_calls[0].name == "update_context"


class TestAssertions:
    def test_tool_arg_match_reports_missing_content(self) -> None:
        case = next(
            case for case in load_cases() if case.id == "chat_log_life_disruption"
        )
        execution = EvalExecution(
            text="Logged.",
            tool_calls=[
                CapturedToolCall(
                    name="update_context",
                    arguments={
                        "file": "log",
                        "action": "append",
                        "content": "## 2026-04-09\n\n- Something else.",
                    },
                    tool_call_id="call_1",
                )
            ],
        )

        results = run_assertions(case.assertions, execution)

        assert not all(result.passed for result in results)
        assert any(
            result.name == "log_append_captures_disruption" and not result.passed
            for result in results
        )

    def test_forbidden_opening_fails_on_wait(self) -> None:
        results = run_assertions(
            [{"type": "forbidden_opening", "patterns": ["Wait"]}],
            EvalExecution(text="Wait - I see the issue."),
        )

        assert not results[0].passed
        assert "Wait" in results[0].detail


class TestCliSelection:
    def test_select_cases_filters_by_id_and_feature(self) -> None:
        cases = load_cases()

        selected = select_cases(
            cases,
            case_ids=["chat_log_life_disruption"],
            feature="chat",
        )

        assert [case.id for case in selected] == ["chat_log_life_disruption"]

    def test_select_cases_rejects_unknown_id(self) -> None:
        with pytest.raises(ValueError, match="Unknown case"):
            select_cases(load_cases(), case_ids=["missing"])

    def test_multi_case_runner_uses_progress(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cases = load_cases()[:2]
        mock_run_case = MagicMock(side_effect=lambda case, **_: case.id)
        mock_progress = MagicMock()
        mock_progress.add_task.return_value = "task-1"
        mock_progress.__enter__.return_value = mock_progress
        mock_progress.__exit__.return_value = None
        mock_progress_cls = MagicMock(return_value=mock_progress)
        monkeypatch.setattr(eval_run, "run_case", mock_run_case)
        monkeypatch.setattr(eval_run, "Progress", mock_progress_cls)

        results = eval_run._run_selected_cases(
            cases,
            model="test-model",
            max_tool_iterations=2,
        )

        assert results == [case.id for case in cases]
        assert mock_progress.add_task.call_count == 1
        assert mock_progress.advance.call_count == 2
