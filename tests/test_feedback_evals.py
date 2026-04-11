"""Tests for the feedback-derived eval framework."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sys
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import MagicMock

import llm
import pytest

from evals.framework import (
    CapturedToolCall,
    EvalCache,
    AssertionResult,
    EvalExecution,
    EvalResult,
    load_cases,
    print_result_details,
    print_results,
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

    def test_load_cases_reads_pace_format_feedback_case(self) -> None:
        cases = {case.id: case for case in load_cases()}

        case = cases["chat_running_speed_trend_pace_format"]

        assert case.case_kind == "real_regression"
        assert case.source_feedback_id == 9
        assert case.source_llm_call_id == 272
        assert case.derived_from["feedback_id"] == 9
        assert case.fixture["today"] == "2026-04-08"
        assert "workout_all" in case.fixture["db_seed"]["tables"]

    def test_load_cases_reads_chart_text_independence_feedback_case(self) -> None:
        cases = {case.id: case for case in load_cases()}

        case = cases["chat_running_speed_trend_chart_text_independent"]

        assert case.case_kind == "real_regression"
        assert case.source_feedback_id == 9
        assert case.source_llm_call_id == 272
        assert case.derived_from["feedback_id"] == 9
        assert case.fixture["today"] == "2026-04-08"
        assert "workout_all" in case.fixture["db_seed"]["tables"]

    def test_load_cases_reads_strategy_update_feedback_case(self) -> None:
        cases = {case.id: case for case in load_cases()}

        case = cases["chat_strategy_change_updates_weekly_plan"]

        assert case.case_kind == "real_regression"
        assert case.source_feedback_id == 12
        assert case.source_llm_call_id == 304
        assert case.derived_from["feedback_id"] == 12
        assert case.fixture["today"] == "2026-04-10"
        assert "thumbs-down feedback #12" in case.notes


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

    def test_chat_case_normalizes_replace_section_heading_from_raw_tool_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        case = next(
            case
            for case in load_cases()
            if case.id == "chat_strategy_change_updates_weekly_plan"
        )
        raw_tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "update_context",
                "arguments": json.dumps(
                    {
                        "file": "strategy",
                        "action": "replace_section",
                        "section": "Weekly Plan",
                        "content": (
                            "## Weekly Plan\n\n"
                            "**Week runs Monday -> Sunday.** Target: 4 runs + 2 "
                            "strength sessions per week (~20 km total running).\n"
                        ),
                        "summary": "Updated weekly plan.",
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
                _llm_result("Done. Plan updated.", tool_calls=[]),
            ]
        )
        monkeypatch.setattr(llm, "call_llm", mock_call)

        result = run_case(case, model="test-model")

        assert result.passed
        assert result.execution is not None
        assert result.execution.tool_calls[0].arguments["section"] == "## Weekly Plan"

    def test_chat_case_does_not_log_normalization_warning_for_invalid_action(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        case = next(
            case
            for case in load_cases()
            if case.id == "chat_strategy_change_updates_weekly_plan"
        )
        raw_tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "update_context",
                "arguments": json.dumps(
                    {
                        "file": "strategy",
                        "section": "Weekly Plan",
                        "content": "## Weekly Plan\n\n- 4 runs\n",
                        "summary": "Updated weekly plan.",
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
                _llm_result("Done. Plan updated.", tool_calls=[]),
            ]
        )
        monkeypatch.setattr(llm, "call_llm", mock_call)

        with caplog.at_level(logging.WARNING):
            result = run_case(case, model="test-model")

        assert not result.passed
        assert "Unknown context edit action in tool call" not in caplog.text

    def test_chat_case_captures_strategy_update_without_mutating(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        case = next(
            case
            for case in load_cases()
            if case.id == "chat_strategy_change_updates_weekly_plan"
        )
        tool_call = _tool_call(
            "update_context",
            {
                "file": "strategy",
                "action": "replace_section",
                "section": "## Weekly Plan",
                "content": (
                    "## Weekly Plan\n\n"
                    "**Week runs Monday -> Sunday.** Target: 4 runs + 2 strength "
                    "sessions per week (~20 km total running).\n\n"
                    "- Run days: 4-5 km each\n"
                    "- 1 tempo session/week when recovery supports it\n"
                    "- Remaining runs at easy pace (5:30-6:00/km)\n"
                    "- Strength A: Push + core\n"
                    "- Strength B: Pull + core\n"
                    "- One easy run may share a day with strength when needed\n"
                ),
                "summary": "Updated Weekly Plan target to 4 runs per week.",
            },
        )
        mock_call = MagicMock(
            side_effect=[
                _llm_result("", tool_calls=[tool_call]),
                _llm_result(
                    "Changed it to **4 runs/week**. Keep the extra run easy so recovery stays manageable.",
                    tool_calls=[],
                ),
            ]
        )
        monkeypatch.setattr(llm, "call_llm", mock_call)

        result = run_case(case, model="test-model")

        assert result.passed
        assert result.execution is not None
        assert result.execution.tool_calls[0].name == "update_context"

    def test_chat_case_reuses_cached_llm_responses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
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
        cache = EvalCache(tmp_path / "eval-cache.sqlite")

        first = run_case(case, model="test-model", cache=cache)
        second = run_case(case, model="test-model", cache=cache)

        assert first.execution is not None
        assert second.execution is not None
        assert second.execution.text == first.execution.text
        assert mock_call.call_count == 2

    def test_chat_case_refresh_cache_forces_fresh_llm_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        case = next(
            case for case in load_cases() if case.id == "chat_explicit_add_to_log"
        )
        cached_result = _llm_result("Cached response", tool_calls=[])
        refreshed_result = _llm_result("Fresh response", tool_calls=[])
        mock_call = MagicMock(side_effect=[cached_result, refreshed_result])
        monkeypatch.setattr(llm, "call_llm", mock_call)
        cache = EvalCache(tmp_path / "eval-cache.sqlite")

        first = run_case(case, model="test-model", cache=cache)
        second = run_case(
            case,
            model="test-model",
            cache=cache,
            refresh_cache=True,
        )

        assert first.execution is not None
        assert second.execution is not None
        assert first.execution.text == "Cached response"
        assert second.execution.text == "Fresh response"
        assert mock_call.call_count == 2

    def test_chat_case_passes_reasoning_effort_into_cacheable_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        case = next(
            case for case in load_cases() if case.id == "chat_explicit_add_to_log"
        )
        mock_call = MagicMock(return_value=_llm_result("Fresh response", tool_calls=[]))
        monkeypatch.setattr(llm, "call_llm", mock_call)
        cache = EvalCache(tmp_path / "eval-cache.sqlite")

        run_case(
            case,
            model="test-model",
            reasoning_effort="high",
            cache=cache,
        )

        assert mock_call.call_args.kwargs["reasoning_effort"] == "high"

        second = run_case(
            case,
            model="test-model",
            reasoning_effort="high",
            cache=cache,
        )
        assert second.execution is not None
        assert second.execution.text == "Fresh response"
        assert mock_call.call_count == 1

    def test_cached_execution_preserves_latency_and_cost(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        case = next(
            case for case in load_cases() if case.id == "chat_explicit_add_to_log"
        )
        mock_call = MagicMock(return_value=_llm_result("Fresh response", tool_calls=[]))
        monkeypatch.setattr(llm, "call_llm", mock_call)
        cache = EvalCache(tmp_path / "eval-cache.sqlite")

        first = run_case(case, model="test-model", cache=cache)
        second = run_case(case, model="test-model", cache=cache)

        assert first.execution is not None
        assert second.execution is not None
        assert first.execution.latency_s == pytest.approx(0.01)
        assert second.execution.latency_s == pytest.approx(0.01)
        assert first.execution.cost == pytest.approx(0.001)
        assert second.execution.cost == pytest.approx(0.001)
        assert first.execution.cache_hits == 0
        assert first.execution.cache_misses == 1
        assert second.execution.cache_hits == 1
        assert second.execution.cache_misses == 0
        assert mock_call.call_count == 1


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

    def test_text_absent_fails_on_invalid_pace_seconds(self) -> None:
        case = next(
            case
            for case in load_cases()
            if case.id == "chat_running_speed_trend_pace_format"
        )

        results = run_assertions(
            case.assertions,
            EvalExecution(
                text="Easy pace improving too: ~6:00-6:20/km to ~5:40-5:70/km.",
                tool_calls=[
                    CapturedToolCall(
                        name="run_sql",
                        arguments={
                            "query": "SELECT date FROM workout_all LIMIT 1",
                        },
                        tool_call_id="call_1",
                    )
                ],
            ),
        )

        assert not all(result.passed for result in results)
        assert any(
            result.name == "does_not_emit_invalid_pace_seconds" and not result.passed
            for result in results
        )

    def test_text_without_chart_absent_fails_on_chart_handoff_scaffolding(self) -> None:
        case = next(
            case
            for case in load_cases()
            if case.id == "chat_running_speed_trend_chart_text_independent"
        )

        results = run_assertions(
            case.assertions,
            EvalExecution(
                text=(
                    "Clear downward trend. Here's the picture:\n\n"
                    '<chart title="Running Pace Trend">\n'
                    "fig = None\n"
                    "</chart>\n\n"
                    "The trend is real."
                ),
                tool_calls=[
                    CapturedToolCall(
                        name="run_sql",
                        arguments={
                            "query": "SELECT date FROM workout_all LIMIT 1",
                        },
                        tool_call_id="call_1",
                    )
                ],
            ),
        )

        assert not all(result.passed for result in results)
        assert any(
            result.name == "telegram_text_does_not_depend_on_inline_chart"
            and not result.passed
            for result in results
        )


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

    def test_normalize_reasoning_effort_maps_none_sentinel(self) -> None:
        assert eval_run._normalize_reasoning_effort("none") is None
        assert eval_run._normalize_reasoning_effort("medium") == "medium"


class _FakeTable:
    instances: list["_FakeTable"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.title = kwargs.get("title")
        self.columns: list[str] = []
        self.rows: list[tuple[str, ...]] = []
        _FakeTable.instances.append(self)

    def add_column(self, label: str, **kwargs) -> None:
        self.columns.append(label)

    def add_row(self, *values: str) -> None:
        self.rows.append(tuple(values))


class _FakeConsole:
    instances: list["_FakeConsole"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.printed: list[object] = []
        _FakeConsole.instances.append(self)

    def print(self, obj: object) -> None:
        self.printed.append(obj)


class _FakePanel:
    def __init__(self, renderable: str, title: str, border_style: str) -> None:
        self.renderable = renderable
        self.title = title
        self.border_style = border_style


class _FakeSyntax:
    def __init__(self, code: str, lexer: str, theme: str) -> None:
        self.code = code
        self.lexer = lexer
        self.theme = theme


class _FakeText:
    def __init__(self, plain: str, style: str | None = None) -> None:
        self.plain = plain
        self.style = style


class TestPrinting:
    def test_print_results_includes_latency_and_cost_columns(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _FakeConsole.instances.clear()
        _FakeTable.instances.clear()
        monkeypatch.setitem(sys.modules, "rich.console", ModuleType("rich.console"))
        monkeypatch.setitem(sys.modules, "rich.table", ModuleType("rich.table"))
        monkeypatch.setitem(sys.modules, "rich.text", ModuleType("rich.text"))
        sys.modules["rich.console"].Console = _FakeConsole
        sys.modules["rich.table"].Table = _FakeTable
        sys.modules["rich.text"].Text = _FakeText
        result = EvalResult(
            case_id="case-1",
            feature="chat",
            case_kind="real_regression",
            model="anthropic/test-model",
            source_feedback_id=1,
            source_llm_call_id=2,
            assertions=[AssertionResult(name="ok", passed=True)],
            execution=EvalExecution(
                text="Done",
                latency_s=1.234,
                cost=0.0567,
            ),
        )

        print_results([result])

        table = _FakeTable.instances[0]
        summary = _FakeTable.instances[1]
        assert "Latency" in table.columns
        assert "Cost" in table.columns
        assert table.rows[0][5] == "1.23s"
        assert table.rows[0][6] == "$0.0567"
        assert summary.title == "Run Summary"
        accuracy_row = next(row for row in summary.rows if row[0] == "Accuracy")
        assert isinstance(accuracy_row[1], _FakeText)
        assert accuracy_row[1].plain == "100.0%"
        assert accuracy_row[1].style == "green"
        assert ("Passed", "1") in summary.rows
        assert ("Failed", "0") in summary.rows
        assert len(_FakeConsole.instances[0].printed) == 2

    def test_print_results_multi_case_includes_summary_metrics(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _FakeConsole.instances.clear()
        _FakeTable.instances.clear()
        monkeypatch.setitem(sys.modules, "rich.console", ModuleType("rich.console"))
        monkeypatch.setitem(sys.modules, "rich.table", ModuleType("rich.table"))
        monkeypatch.setitem(sys.modules, "rich.text", ModuleType("rich.text"))
        sys.modules["rich.console"].Console = _FakeConsole
        sys.modules["rich.table"].Table = _FakeTable
        sys.modules["rich.text"].Text = _FakeText
        results = [
            EvalResult(
                case_id="case-1",
                feature="chat",
                case_kind="real_regression",
                model="anthropic/test-model",
                source_feedback_id=1,
                source_llm_call_id=2,
                assertions=[AssertionResult(name="ok", passed=True)],
                execution=EvalExecution(
                    text="Done",
                    latency_s=1.0,
                    cost=0.0100,
                    cache_hits=1,
                    cache_misses=0,
                ),
            ),
            EvalResult(
                case_id="case-2",
                feature="chat",
                case_kind="real_regression",
                model="anthropic/test-model",
                source_feedback_id=1,
                source_llm_call_id=3,
                assertions=[AssertionResult(name="bad", passed=False)],
                execution=EvalExecution(
                    text="Done",
                    latency_s=3.0,
                    cost=0.0200,
                    cache_hits=0,
                    cache_misses=1,
                ),
            ),
        ]

        print_results(results)

        console = _FakeConsole.instances[0]
        summary = _FakeTable.instances[1]
        assert console.printed[1] is summary
        assert summary.title == "Run Summary"
        accuracy_row = next(row for row in summary.rows if row[0] == "Accuracy")
        assert isinstance(accuracy_row[1], _FakeText)
        assert accuracy_row[1].plain == "50.0%"
        assert accuracy_row[1].style == "yellow"
        assert ("Passed", "1") in summary.rows
        assert ("Failed", "1") in summary.rows
        assert ("Failed Cases", "case-2") in summary.rows
        assert ("Latency Avg", "2.00s") in summary.rows
        assert ("Latency p95", "3.00s") in summary.rows
        assert ("Estimated Cost", "$0.0300") in summary.rows
        assert ("Avg Cost", "$0.0150") in summary.rows
        assert ("Cache", "1 hits, 1 misses") in summary.rows

    def test_print_results_low_accuracy_uses_red(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _FakeConsole.instances.clear()
        _FakeTable.instances.clear()
        monkeypatch.setitem(sys.modules, "rich.console", ModuleType("rich.console"))
        monkeypatch.setitem(sys.modules, "rich.table", ModuleType("rich.table"))
        monkeypatch.setitem(sys.modules, "rich.text", ModuleType("rich.text"))
        sys.modules["rich.console"].Console = _FakeConsole
        sys.modules["rich.table"].Table = _FakeTable
        sys.modules["rich.text"].Text = _FakeText
        results = [
            EvalResult(
                case_id="case-1",
                feature="chat",
                case_kind="real_regression",
                model="anthropic/test-model",
                source_feedback_id=1,
                source_llm_call_id=2,
                assertions=[AssertionResult(name="bad", passed=False)],
                execution=EvalExecution(text="Done"),
            ),
            EvalResult(
                case_id="case-2",
                feature="chat",
                case_kind="real_regression",
                model="anthropic/test-model",
                source_feedback_id=1,
                source_llm_call_id=3,
                assertions=[AssertionResult(name="bad", passed=False)],
                execution=EvalExecution(text="Done"),
            ),
        ]

        print_results(results)

        summary = _FakeTable.instances[1]
        accuracy_row = next(row for row in summary.rows if row[0] == "Accuracy")
        assert isinstance(accuracy_row[1], _FakeText)
        assert accuracy_row[1].plain == "0.0%"
        assert accuracy_row[1].style == "red"

    def test_print_result_details_includes_latency_and_cost(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _FakeConsole.instances.clear()
        monkeypatch.setitem(sys.modules, "rich.console", ModuleType("rich.console"))
        monkeypatch.setitem(sys.modules, "rich.panel", ModuleType("rich.panel"))
        monkeypatch.setitem(sys.modules, "rich.syntax", ModuleType("rich.syntax"))
        sys.modules["rich.console"].Console = _FakeConsole
        sys.modules["rich.panel"].Panel = _FakePanel
        sys.modules["rich.syntax"].Syntax = _FakeSyntax
        result = EvalResult(
            case_id="case-1",
            feature="chat",
            case_kind="real_regression",
            model="anthropic/test-model",
            source_feedback_id=1,
            source_llm_call_id=2,
            assertions=[AssertionResult(name="bad", passed=False)],
            execution=EvalExecution(
                text="Done",
                latency_s=1.234,
                cost=0.0567,
            ),
        )

        print_result_details([result])

        panel = _FakeConsole.instances[0].printed[0]
        assert isinstance(panel, _FakePanel)
        assert "Latency: 1.23s | Cost: $0.0567" in panel.renderable
