"""Tests for the eval leaderboard recording and rendering."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from evals import leaderboard
from evals import matrix as eval_matrix
from evals import run as eval_run
from evals.framework import AssertionResult, EvalExecution, EvalResult, load_cases


def _eval_result(
    case_id: str,
    *,
    passed: bool,
    feature: str = "chat",
    model: str = "anthropic/test-model",
    route: dict | None = None,
    latency_s: float = 1.0,
    cost: float | None = 0.01,
) -> EvalResult:
    assertions = [AssertionResult(name="ok", passed=True)]
    if not passed:
        assertions = [AssertionResult(name="failed_assertion", passed=False)]
    return EvalResult(
        case_id=case_id,
        feature=feature,
        case_kind="real_regression",
        model=model,
        source_feedback_id=1,
        source_llm_call_id=2,
        route=route
        or {
            "feature": feature,
            "primary": model,
            "fallback_models": [],
            "reasoning_effort": "medium",
            "temperature": None,
            "source": "test",
        },
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
    requested_model: str,
    reasoning_effort: str | None,
    created_at: str,
    run_id: str,
    feature_filter: str | None = None,
) -> dict:
    return leaderboard.build_run_record(
        results=results,
        case_ids=case_ids,
        requested_model=requested_model,
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
            requested_model="anthropic/test-model",
            reasoning_effort=None,
            max_tool_iterations=5,
            route_set_id="route-a",
        )
        second = leaderboard.compute_run_fingerprint(
            git_sha="abc",
            case_set_id="case-set",
            requested_model="anthropic/test-model",
            reasoning_effort="high",
            max_tool_iterations=5,
            route_set_id="route-a",
        )

        assert first != second

    def test_run_fingerprint_changes_with_route_set(self) -> None:
        first = leaderboard.compute_run_fingerprint(
            git_sha="abc",
            case_set_id="case-set",
            requested_model="anthropic/test-model",
            reasoning_effort=None,
            max_tool_iterations=5,
            route_set_id="route-a",
        )
        second = leaderboard.compute_run_fingerprint(
            git_sha="abc",
            case_set_id="case-set",
            requested_model="anthropic/test-model",
            reasoning_effort=None,
            max_tool_iterations=5,
            route_set_id="route-b",
        )

        assert first != second


class TestRecording:
    def test_build_record_includes_actual_case_routes_and_feature_summary(
        self,
    ) -> None:
        chat = _eval_result(
            "case-chat",
            passed=True,
            feature="chat",
            model="deepseek/chat",
        )
        verifier = _eval_result(
            "case-verify",
            passed=False,
            feature="verification_judge",
            model="deepseek/verifier",
            route={
                "feature": "verification_judge",
                "kind": "nudge",
                "primary": "deepseek/verifier",
                "fallback_models": [],
                "reasoning_effort": "high",
                "temperature": None,
                "source": "eval_cli",
            },
        )

        record = _build_record(
            case_ids=["case-chat", "case-verify"],
            results=[chat, verifier],
            requested_model="deepseek/chat",
            reasoning_effort="high",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-routes",
        )

        assert record["requested_model"] == "deepseek/chat"
        assert record["route_set_id"]
        assert record["feature_summary"]["chat"]["passed"] == 1
        assert record["feature_summary"]["verification_judge"]["failed"] == 1
        assert record["per_case"][1]["route"]["primary"] == "deepseek/verifier"

    def test_record_run_skips_duplicate_by_default(self, tmp_path: Path) -> None:
        runs_path = tmp_path / "runs.jsonl"
        markdown_path = tmp_path / "leaderboard.md"
        html_path = tmp_path / "leaderboard.html"
        result = _eval_result("case-1", passed=True)

        first = leaderboard.record_run(
            results=[result],
            case_ids=["case-1"],
            requested_model="anthropic/test-model",
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
            requested_model="anthropic/test-model",
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
            requested_model="anthropic/test-model",
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
            requested_model="anthropic/test-model",
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
            requested_model="anthropic/model-a",
            reasoning_effort="medium",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-full",
            feature_filter="chat",
        )
        subset = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=False, latency_s=2.0, cost=0.03)],
            requested_model="anthropic/model-b",
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
            requested_model="anthropic/model-a",
            reasoning_effort="medium",
            created_at="2026-04-12T10:00:00Z",
            run_id="run-a-new",
        )
        older_better = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True, latency_s=1.0, cost=0.01)],
            requested_model="anthropic/model-a",
            reasoning_effort="medium",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-a-old",
        )
        model_b = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True, latency_s=2.0, cost=0.02)],
            requested_model="anthropic/model-b",
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

    def test_render_keeps_distinct_routes_for_same_requested_model(self) -> None:
        route_a = _build_record(
            case_ids=["case-a"],
            results=[
                _eval_result(
                    "case-a",
                    passed=True,
                    model="anthropic/requested",
                    route={"primary": "provider/route-a"},
                )
            ],
            requested_model="anthropic/requested",
            reasoning_effort=None,
            created_at="2026-04-11T10:00:00Z",
            run_id="run-route-a",
        )
        route_b = _build_record(
            case_ids=["case-a"],
            results=[
                _eval_result(
                    "case-a",
                    passed=False,
                    model="anthropic/requested",
                    route={"primary": "provider/route-b"},
                )
            ],
            requested_model="anthropic/requested",
            reasoning_effort=None,
            created_at="2026-04-12T10:00:00Z",
            run_id="run-route-b",
        )

        markdown = leaderboard.render_leaderboard_markdown([route_a, route_b])

        assert markdown.count("| requested | none |") == 2
        assert "route-a" in markdown
        assert "route-b" in markdown

    def test_render_html_contains_filters_and_run_data(self) -> None:
        run = _build_record(
            case_ids=["case-a", "case-b"],
            results=[
                _eval_result("case-a", passed=True, latency_s=1.0, cost=0.02),
                _eval_result("case-b", passed=False, latency_s=3.0, cost=0.04),
            ],
            requested_model="anthropic/model-a",
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
        assert "Params" in html
        assert "Checks" in html
        assert "route_params" in html
        assert "reasoning=medium" in html
        assert "temp=omit" in html
        assert "assertion_summary" in html
        assert "deterministic passed" in html
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
            requested_model="anthropic/model-a",
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
            requested_model="anthropic/model-a",
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


class TestMatrix:
    def test_build_matrix_runs_expands_chat_models_and_reasoning(self) -> None:
        cases = load_cases()

        runs = eval_matrix.build_matrix_runs(
            cases,
            feature="chat",
            production=False,
            case_ids=["chat_explicit_add_to_log"],
            models=["model-a", "model-b"],
            reasoning_efforts=["none", "high"],
            temperature=None,
        )

        assert len(runs) == 4
        assert {run.model for run in runs} == {"model-a", "model-b"}
        assert {run.reasoning_effort for run in runs} == {None, "high"}
        assert all(
            [case.id for case in run.cases] == ["chat_explicit_add_to_log"]
            for run in runs
        )

    def test_build_matrix_expands_verification_judge_models(self) -> None:
        runs = eval_matrix.build_matrix_runs(
            load_cases(),
            feature="verification_judge",
            production=False,
            case_ids=["verification_judge_nudge_hrv_direction_reversal"],
            models=["model-a", "model-b"],
            reasoning_efforts=["none", "high"],
            temperature=None,
        )

        assert len(runs) == 4
        assert {run.model for run in runs} == {"model-a", "model-b"}
        assert {run.reasoning_effort for run in runs} == {None, "high"}
        assert all(
            [case.id for case in run.cases]
            == ["verification_judge_nudge_hrv_direction_reversal"]
            for run in runs
        )

    def test_build_matrix_expands_insights_models(self) -> None:
        runs = eval_matrix.build_matrix_runs(
            load_cases(),
            feature="insights",
            production=False,
            case_ids=["insights_fits_a_phone_notification_w31"],
            models=["model-a", "model-b"],
            reasoning_efforts=["none", "high"],
            temperature=None,
        )

        assert len(runs) == 4
        assert {run.model for run in runs} == {"model-a", "model-b"}
        assert {run.reasoning_effort for run in runs} == {None, "high"}
        assert all(
            [case.id for case in run.cases]
            == ["insights_fits_a_phone_notification_w31"]
            for run in runs
        )
