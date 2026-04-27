"""Tests for the eval leaderboard recording and rendering."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from evals import leaderboard
from evals import run as eval_run
from evals.framework import AssertionResult, EvalExecution, EvalResult, load_cases


def _eval_result(
    case_id: str,
    *,
    passed: bool,
    latency_s: float = 1.0,
    cost: float | None = 0.01,
) -> EvalResult:
    assertions = [AssertionResult(name="ok", passed=True)]
    if not passed:
        assertions = [AssertionResult(name="failed_assertion", passed=False)]
    return EvalResult(
        case_id=case_id,
        feature="chat",
        case_kind="real_regression",
        model="anthropic/test-model",
        source_feedback_id=1,
        source_llm_call_id=2,
        assertions=assertions,
        execution=EvalExecution(
            text="Done",
            latency_s=latency_s,
            cost=cost,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cache_hits=1,
            cache_misses=0,
        ),
    )


def _build_record(
    *,
    case_ids: list[str],
    results: list[EvalResult],
    model: str,
    reasoning_effort: str | None,
    created_at: str,
    run_id: str,
    feature_filter: str | None = None,
) -> dict:
    return leaderboard.build_run_record(
        results=results,
        case_ids=case_ids,
        model=model,
        reasoning_effort=reasoning_effort,
        max_tool_iterations=5,
        feature_filter=feature_filter,
        repo_context={"git_sha": "abcdef123456", "dirty": False},
        created_at=created_at,
        run_id=run_id,
    )


class TestIdentity:
    def test_case_set_id_ignores_order(self) -> None:
        first = leaderboard.compute_case_set_id(["b", "a"])
        second = leaderboard.compute_case_set_id(["a", "b"])

        assert first == second

    def test_case_set_id_changes_when_cases_change(self) -> None:
        first = leaderboard.compute_case_set_id(["a", "b"])
        second = leaderboard.compute_case_set_id(["a", "b", "c"])

        assert first != second

    def test_run_fingerprint_changes_with_reasoning_effort(self) -> None:
        first = leaderboard.compute_run_fingerprint(
            git_sha="abc",
            case_set_id="case-set",
            model="anthropic/test-model",
            reasoning_effort=None,
            max_tool_iterations=5,
        )
        second = leaderboard.compute_run_fingerprint(
            git_sha="abc",
            case_set_id="case-set",
            model="anthropic/test-model",
            reasoning_effort="high",
            max_tool_iterations=5,
        )

        assert first != second


class TestRecording:
    def test_record_run_skips_duplicate_by_default(self, tmp_path: Path) -> None:
        runs_path = tmp_path / "runs.jsonl"
        markdown_path = tmp_path / "leaderboard.md"
        html_path = tmp_path / "leaderboard.html"
        result = _eval_result("case-1", passed=True)

        first = leaderboard.record_run(
            results=[result],
            case_ids=["case-1"],
            model="anthropic/test-model",
            reasoning_effort="medium",
            max_tool_iterations=5,
            feature_filter="chat",
            runs_path=runs_path,
            markdown_path=markdown_path,
            html_path=html_path,
            repo_context={"git_sha": "abc", "dirty": False},
        )
        second = leaderboard.record_run(
            results=[result],
            case_ids=["case-1"],
            model="anthropic/test-model",
            reasoning_effort="medium",
            max_tool_iterations=5,
            feature_filter="chat",
            runs_path=runs_path,
            markdown_path=markdown_path,
            html_path=html_path,
            repo_context={"git_sha": "abc", "dirty": False},
        )

        lines = [
            line for line in runs_path.read_text(encoding="utf-8").splitlines() if line
        ]
        assert first.recorded is True
        assert second.recorded is False
        assert second.duplicate_of == first.record["run_id"]
        assert len(lines) == 1
        assert html_path.exists()

    def test_record_run_force_duplicate_appends(self, tmp_path: Path) -> None:
        runs_path = tmp_path / "runs.jsonl"
        markdown_path = tmp_path / "leaderboard.md"
        html_path = tmp_path / "leaderboard.html"
        result = _eval_result("case-1", passed=True)

        leaderboard.record_run(
            results=[result],
            case_ids=["case-1"],
            model="anthropic/test-model",
            reasoning_effort="medium",
            max_tool_iterations=5,
            feature_filter="chat",
            runs_path=runs_path,
            markdown_path=markdown_path,
            html_path=html_path,
            repo_context={"git_sha": "abc", "dirty": False},
        )
        forced = leaderboard.record_run(
            results=[result],
            case_ids=["case-1"],
            model="anthropic/test-model",
            reasoning_effort="medium",
            max_tool_iterations=5,
            feature_filter="chat",
            allow_duplicate=True,
            runs_path=runs_path,
            markdown_path=markdown_path,
            html_path=html_path,
            repo_context={"git_sha": "abc", "dirty": False},
        )

        lines = [
            line for line in runs_path.read_text(encoding="utf-8").splitlines() if line
        ]
        assert forced.recorded is True
        assert len(lines) == 2


class TestRendering:
    def test_render_groups_sections_by_case_set_and_orders_them(self) -> None:
        full_suite = _build_record(
            case_ids=["case-a", "case-b"],
            results=[
                _eval_result("case-a", passed=True, latency_s=1.0, cost=0.02),
                _eval_result("case-b", passed=True, latency_s=3.0, cost=0.04),
            ],
            model="anthropic/model-a",
            reasoning_effort="medium",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-full",
            feature_filter="chat",
        )
        subset = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=False, latency_s=2.0, cost=0.03)],
            model="anthropic/model-b",
            reasoning_effort=None,
            created_at="2026-04-12T10:00:00Z",
            run_id="run-subset",
            feature_filter="chat",
        )

        markdown = leaderboard.render_leaderboard_markdown([subset, full_suite])

        assert markdown.index("## 2 cases") < markdown.index("## 1 cases")

    def test_render_keeps_only_latest_run_per_model_and_ranks_rows(self) -> None:
        latest_worse = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=False, latency_s=3.0, cost=0.03)],
            model="anthropic/model-a",
            reasoning_effort="medium",
            created_at="2026-04-12T10:00:00Z",
            run_id="run-a-new",
        )
        older_better = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True, latency_s=1.0, cost=0.01)],
            model="anthropic/model-a",
            reasoning_effort="medium",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-a-old",
        )
        model_b = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True, latency_s=2.0, cost=0.02)],
            model="anthropic/model-b",
            reasoning_effort="medium",
            created_at="2026-04-11T09:00:00Z",
            run_id="run-b",
        )

        markdown = leaderboard.render_leaderboard_markdown(
            [older_better, latest_worse, model_b]
        )

        assert markdown.count("| model-a | medium |") == 1
        assert markdown.index("| model-b | medium | 100.0% |") < markdown.index(
            "| model-a | medium | 0.0% |"
        )

    def test_render_html_contains_filters_and_run_data(self) -> None:
        run = _build_record(
            case_ids=["case-a", "case-b"],
            results=[
                _eval_result("case-a", passed=True, latency_s=1.0, cost=0.02),
                _eval_result("case-b", passed=False, latency_s=3.0, cost=0.04),
            ],
            model="anthropic/model-a",
            reasoning_effort="medium",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-html",
            feature_filter="chat",
        )

        html = leaderboard.render_leaderboard_html([run])

        assert "scope-filter" in html
        assert "model-filter" in html
        assert "reasoning-filter" in html
        assert "latest-only" in html
        assert "failed-only" in html
        assert "leaderboard-data" in html
        assert "model-a" in html
        assert "case-a" in html


class TestCli:
    def test_eval_run_record_writes_jsonl_and_markdown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        case = load_cases()[0]
        result = _eval_result(case.id, passed=True)
        runs_path = tmp_path / "runs.jsonl"
        markdown_path = tmp_path / "leaderboard.md"
        html_path = tmp_path / "leaderboard.html"

        monkeypatch.setattr(leaderboard, "RUNS_PATH", runs_path)
        monkeypatch.setattr(leaderboard, "MARKDOWN_PATH", markdown_path)
        monkeypatch.setattr(leaderboard, "HTML_PATH", html_path)
        monkeypatch.setattr(
            leaderboard,
            "get_repo_context",
            lambda: {"git_sha": "abc", "dirty": False},
        )
        monkeypatch.setattr(eval_run, "load_cases", lambda: [case])
        monkeypatch.setattr(
            eval_run, "_run_selected_cases", lambda *args, **kwargs: [result]
        )
        monkeypatch.setattr(eval_run, "print_results", lambda results: None)
        monkeypatch.setattr(eval_run, "print_result_details", lambda results: None)
        monkeypatch.setattr(
            sys,
            "argv",
            ["evals.run", "--record"],
        )

        eval_run.main()

        out = capsys.readouterr().out
        lines = [
            line for line in runs_path.read_text(encoding="utf-8").splitlines() if line
        ]
        assert len(lines) == 1
        assert "Recorded leaderboard run" in out
        assert markdown_path.exists()
        assert html_path.exists()

    def test_eval_run_duplicate_skip_and_force_append(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        case = load_cases()[0]
        result = _eval_result(case.id, passed=True)
        runs_path = tmp_path / "runs.jsonl"
        markdown_path = tmp_path / "leaderboard.md"
        html_path = tmp_path / "leaderboard.html"

        monkeypatch.setattr(leaderboard, "RUNS_PATH", runs_path)
        monkeypatch.setattr(leaderboard, "MARKDOWN_PATH", markdown_path)
        monkeypatch.setattr(leaderboard, "HTML_PATH", html_path)
        monkeypatch.setattr(
            leaderboard,
            "get_repo_context",
            lambda: {"git_sha": "abc", "dirty": False},
        )
        monkeypatch.setattr(eval_run, "load_cases", lambda: [case])
        monkeypatch.setattr(
            eval_run, "_run_selected_cases", lambda *args, **kwargs: [result]
        )
        monkeypatch.setattr(eval_run, "print_results", lambda results: None)
        monkeypatch.setattr(eval_run, "print_result_details", lambda results: None)

        monkeypatch.setattr(sys, "argv", ["evals.run", "--record"])
        eval_run.main()
        monkeypatch.setattr(sys, "argv", ["evals.run", "--record"])
        eval_run.main()
        second_out = capsys.readouterr().out

        lines = [
            line for line in runs_path.read_text(encoding="utf-8").splitlines() if line
        ]
        assert len(lines) == 1
        assert "already recorded" in second_out

        monkeypatch.setattr(
            sys,
            "argv",
            ["evals.run", "--record", "--record-duplicate"],
        )
        eval_run.main()
        lines = [
            line for line in runs_path.read_text(encoding="utf-8").splitlines() if line
        ]
        assert len(lines) == 2

    def test_leaderboard_render_cli_rebuilds_markdown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        runs_path = tmp_path / "runs.jsonl"
        markdown_path = tmp_path / "leaderboard.md"
        record = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True)],
            model="anthropic/model-a",
            reasoning_effort="medium",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-a",
        )
        runs_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "evals.leaderboard",
                "render",
                "--runs-path",
                str(runs_path),
                "--markdown-path",
                str(markdown_path),
            ],
        )

        leaderboard.main()

        out = capsys.readouterr().out
        assert "Rendered leaderboard with 1 run(s)" in out
        assert "Feedback Eval Leaderboard" in markdown_path.read_text(encoding="utf-8")

    def test_leaderboard_render_html_cli_rebuilds_html(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        runs_path = tmp_path / "runs.jsonl"
        html_path = tmp_path / "leaderboard.html"
        record = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True)],
            model="anthropic/model-a",
            reasoning_effort="medium",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-a",
        )
        runs_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "evals.leaderboard",
                "render-html",
                "--runs-path",
                str(runs_path),
                "--html-path",
                str(html_path),
            ],
        )

        leaderboard.main()

        out = capsys.readouterr().out
        assert "Rendered HTML leaderboard with 1 run(s)" in out
        html = html_path.read_text(encoding="utf-8")
        assert "scope-filter" in html
        assert "Feedback Eval Leaderboard" in html
