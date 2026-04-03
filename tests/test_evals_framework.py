"""Tests for the eval harness and case dataset."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import evals.cache as eval_cache
import evals.run as eval_run
import llm
import pytest

from evals.cache import build_cache_key, load_cache_entry
from evals.framework import (
    EvalResult,
    _assert_answer_uses_expected_value,
    _assert_chart_count,
    _assert_contains_memory_block,
    _assert_no_markdown_table,
    _assert_pace_format_valid,
    _assert_sql_valid,
    _assert_tool_arg_matches,
    _build_cache_payload,
    _run_chat_eval,
    load_blueprints,
    load_cases,
    run_case,
)


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
    return SimpleNamespace(
        text=text,
        tool_calls=tool_calls or [],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost=0.01,
        raw_message={"role": "assistant", "content": text, "tool_calls": []},
    )


class TestCaseDataset:
    def test_load_cases_smoke(self) -> None:
        cases = load_cases()
        assert len(cases) >= 20
        assert any(case["suite"] == "core" for case in cases)
        assert any(case["suite"] == "benchmark" for case in cases)
        assert any("turns" in case for case in cases)

    def test_named_blueprint_uses_overrides_and_fallbacks(self) -> None:
        context, health_data, metadata, db_seed = load_blueprints(
            prompt_file="chat_prompt",
            blueprint="sparse_week",
        )
        assert "## Sleep" in context["plan"]
        assert "# Profile" in context["me"]
        assert metadata["week_complete"] is False
        assert db_seed is None
        assert health_data["current_week"]["days"][-1]["sleep_status"] == "pending"


class TestChatEvalLoop:
    def test_run_chat_eval_executes_tool_loop(self, monkeypatch) -> None:
        first = _llm_result(
            text="",
            tool_calls=[
                _tool_call(
                    "run_sql",
                    {
                        "query": (
                            "SELECT date, resting_hr FROM daily "
                            "WHERE date = '2026-03-19'"
                        )
                    },
                )
            ],
        )
        second = _llm_result(text="Lowest was **50** bpm on 2026-03-19.")
        mock_call = MagicMock(side_effect=[first, second])
        monkeypatch.setattr(llm, "call_llm", mock_call)

        execution = _run_chat_eval(
            messages=[{"role": "user", "content": "What was the lowest resting HR?"}],
            model="test-model",
            max_tokens=256,
            reasoning_effort=None,
            health_data={"current_week": {"days": []}, "history": []},
            db_seed={
                "days": [
                    {
                        "date": "2026-03-19",
                        "resting_hr": 50,
                        "workouts": [],
                    }
                ]
            },
            max_tool_iterations=3,
        )

        assert execution.text == "Lowest was **50** bpm on 2026-03-19."
        assert len(execution.tool_calls) == 1
        assert execution.query_rows == [{"date": "2026-03-19", "resting_hr": 50}]
        assert execution.input_tokens == 20
        assert execution.output_tokens == 10
        assert mock_call.call_count == 2


class TestEvalCache:
    def test_run_case_uses_cache_on_second_run(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(eval_cache, "_CACHE_DB", tmp_path / "eval-cache.sqlite")

        case = next(c for c in load_cases() if c["id"] == "nudge_rest_day_skip")
        response = SimpleNamespace(
            text="SKIP",
            tool_calls=[],
            input_tokens=12,
            output_tokens=1,
            latency_s=0.42,
            cost=0.002,
        )
        mock_call = MagicMock(return_value=response)
        monkeypatch.setattr(llm, "call_llm", mock_call)

        first = run_case(case=case, model="test-model", use_cache=True)
        second = run_case(case=case, model="test-model", use_cache=True)

        assert first.passed
        assert second.passed
        assert mock_call.call_count == 1
        assert eval_cache.cache_db_path().exists()

    def test_cache_entry_stores_strong_key_payload(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(eval_cache, "_CACHE_DB", tmp_path / "eval-cache.sqlite")

        case = next(c for c in load_cases() if c["id"] == "nudge_rest_day_skip")
        response = SimpleNamespace(
            text="SKIP",
            tool_calls=[],
            input_tokens=12,
            output_tokens=1,
            latency_s=0.42,
            cost=0.002,
        )
        mock_call = MagicMock(return_value=response)
        monkeypatch.setattr(llm, "call_llm", mock_call)

        run_case(case=case, model="test-model", use_cache=True)

        conn = eval_cache._open_cache_db()
        try:
            row = conn.execute("SELECT key_hash FROM eval_cache").fetchone()
        finally:
            conn.close()

        assert row is not None
        entry = load_cache_entry(row["key_hash"])
        assert entry is not None
        key_payload = json.loads(entry.key_json)
        assert key_payload["case_id"] == "nudge_rest_day_skip"
        assert key_payload["model"] == "test-model"
        assert "messages" in key_payload

    def test_cache_key_changes_when_messages_change(self) -> None:
        case = next(c for c in load_cases() if c["id"] == "nudge_rest_day_skip")

        first_payload = _build_cache_payload(
            case=case,
            model="test-model",
            reasoning_effort=None,
            messages=[{"role": "user", "content": "First prompt"}],
            tools=None,
            max_tokens=256,
            temperature=0.0,
            max_tool_iterations=5,
        )
        second_payload = _build_cache_payload(
            case=case,
            model="test-model",
            reasoning_effort=None,
            messages=[{"role": "user", "content": "Second prompt"}],
            tools=None,
            max_tokens=256,
            temperature=0.0,
            max_tool_iterations=5,
        )

        first_hash, _ = build_cache_key(first_payload)
        second_hash, _ = build_cache_key(second_payload)

        assert first_hash != second_hash

    def test_cache_key_changes_when_tool_schema_changes(self) -> None:
        case = next(c for c in load_cases() if c["id"] == "sleep_last_night_chat")
        messages = [{"role": "user", "content": "How did I sleep?"}]

        first_payload = _build_cache_payload(
            case=case,
            model="test-model",
            reasoning_effort=None,
            messages=messages,
            tools=[{"type": "function", "function": {"name": "run_sql"}}],
            max_tokens=256,
            temperature=0.0,
            max_tool_iterations=5,
        )
        second_payload = _build_cache_payload(
            case=case,
            model="test-model",
            reasoning_effort=None,
            messages=messages,
            tools=[
                {"type": "function", "function": {"name": "run_sql"}},
                {"type": "function", "function": {"name": "update_context"}},
            ],
            max_tokens=256,
            temperature=0.0,
            max_tool_iterations=5,
        )

        first_hash, _ = build_cache_key(first_payload)
        second_hash, _ = build_cache_key(second_payload)

        assert first_hash != second_hash


class TestTypedAssertions:
    def test_tool_arg_matches_checks_expected_fields(self) -> None:
        result = _assert_tool_arg_matches(
            [_tool_call("update_context", {"file": "goals", "action": "append"})],
            {
                "tool": "update_context",
                "matches": {"file": ["goals", "plan"], "action": "append"},
            },
        )

        assert result.passed is True

    def test_contains_memory_block_requires_bullets(self) -> None:
        response = """
        ## Summary

        <memory>
        - First memory
        - Second memory
        </memory>
        """

        result = _assert_contains_memory_block(response, {"min_bullets": 2})

        assert result.passed is True
        assert result.detail == "2 bullet(s)"

    def test_pace_format_valid_rejects_decimal_min_per_km(self) -> None:
        result = _assert_pace_format_valid("Your easy pace was 5.5 / km today.")

        assert result.passed is False

    def test_no_markdown_table_rejects_pipe_rows(self) -> None:
        result = _assert_no_markdown_table("| Day | Sleep |\n| --- | --- |")

        assert result.passed is False

    def test_chart_count_counts_embedded_chart_blocks(self) -> None:
        response = """
        Intro text

        <chart title="Trend">
        fig = px.line(rows, x="date", y="value")
        </chart>
        """

        result = _assert_chart_count(response, {"count": 1})

        assert result.passed is True

    def test_sql_valid_passes_for_good_queries(self) -> None:
        tool_calls = [
            _tool_call("run_sql", {"query": "SELECT date, resting_hr FROM daily"}),
        ]
        result = _assert_sql_valid(tool_calls)
        assert result.passed is True

    def test_sql_valid_fails_for_unknown_columns(self) -> None:
        tool_calls = [
            _tool_call("run_sql", {"query": "SELECT fake_col FROM daily"}),
        ]
        result = _assert_sql_valid(tool_calls)
        assert result.passed is False
        assert "fake_col" in result.detail

    def test_sql_valid_passes_when_no_sql_calls(self) -> None:
        result = _assert_sql_valid([])
        assert result.passed is True

    def test_no_markdown_table_allows_pipes_in_prose(self) -> None:
        result = _assert_no_markdown_table("Your pace range is 5:30|5:45/km")
        assert result.passed is True

    def test_answer_uses_expected_value_allows_tolerance(self) -> None:
        result = _assert_answer_uses_expected_value(
            "Average resting HR was 51.2 bpm.",
            {"expected": 51, "tolerance": 0.3},
        )

        assert result.passed is True


class TestEvalRunCli:
    def test_main_filters_suite_and_disables_cache(self, monkeypatch) -> None:
        cases = [
            {
                "id": "core_case",
                "suite": "core",
                "category": "chat_data",
                "scenario_fn": "baseline",
                "config": {},
                "assertions": [{"type": "response_is_not_skip"}],
            },
            {
                "id": "benchmark_case",
                "suite": "benchmark",
                "category": "chat_data",
                "scenario_fn": "baseline",
                "config": {},
                "assertions": [{"type": "response_is_not_skip"}],
            },
        ]
        seen_calls: list[dict] = []

        def fake_run_case(**kwargs) -> EvalResult:
            seen_calls.append(kwargs)
            return EvalResult(
                eval_name=kwargs["case"]["id"],
                suite=kwargs["case"]["suite"],
                category=kwargs["case"]["category"],
                model=kwargs["model"],
                assertions=[],
            )

        monkeypatch.setattr(eval_run, "load_cases", lambda: cases)
        monkeypatch.setattr(eval_run, "run_case", fake_run_case)
        monkeypatch.setattr(eval_run, "print_results", lambda results: None)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "evals.run",
                "--suite",
                "core",
                "--no-cache",
                "--model",
                "test-model",
            ],
        )

        eval_run.main()

        assert len(seen_calls) == 1
        assert seen_calls[0]["case"]["id"] == "core_case"
        assert seen_calls[0]["use_cache"] is False

    def test_main_exits_when_suite_filter_matches_no_cases(self, monkeypatch) -> None:
        cases = [
            {
                "id": "benchmark_case",
                "suite": "benchmark",
                "category": "chat_data",
                "scenario_fn": "baseline",
                "config": {},
                "assertions": [{"type": "response_is_not_skip"}],
            }
        ]

        monkeypatch.setattr(eval_run, "load_cases", lambda: cases)
        monkeypatch.setattr(
            sys,
            "argv",
            ["evals.run", "--suite", "core", "--model", "test-model"],
        )

        with pytest.raises(SystemExit) as exc:
            eval_run.main()

        assert exc.value.code == 1
