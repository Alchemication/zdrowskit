"""Tests for the eval leaderboard recording and rendering."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from evals import leaderboard
from evals import matrix as eval_matrix
from evals import run as eval_run
from evals.framework import AssertionResult, EvalCase, EvalExecution, EvalResult
from evals.leaderboard import __main__ as leaderboard_cli
from evals.leaderboard import record as leaderboard_record
from evals.leaderboard.scorecard import build_scorecard


def _eval_result(
    case_id: str,
    *,
    passed: bool,
    feature: str = "chat",
    model: str = "anthropic/test-model",
    route: dict | None = None,
    latency_s: float = 1.0,
    cost: float | None = 0.01,
    error: str | None = None,
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
        assertions=[] if error else assertions,
        execution=None
        if error
        else EvalExecution(
            text="Done",
            latency_s=latency_s,
            cost=cost,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
        ),
        error=error,
    )


def _case(case_id: str, feature: str = "chat") -> EvalCase:
    return EvalCase(
        id=case_id,
        feature=feature,
        case_kind="real_regression",
        source_feedback_id=1,
        source_llm_call_id=2,
        derived_from={"hypothesis": "test"},
        intent="test",
        fixture={},
        assertions=[],
    )


def _build_record(
    *,
    case_ids: list[str],
    results: list[EvalResult],
    requested_model: str | None,
    reasoning_effort: str | None,
    created_at: str,
    run_id: str,
    feature_filter: str | None = None,
    repeat: int = 1,
    git_sha: str = "abcdef123456",
) -> dict:
    return leaderboard.build_run_record(
        results=results,
        case_ids=case_ids,
        requested_model=requested_model,
        reasoning_effort=reasoning_effort,
        max_tool_iterations=5,
        feature_filter=feature_filter,
        repeat=repeat,
        repo_context={"git_sha": git_sha, "dirty": False},
        created_at=created_at,
        run_id=run_id,
    )


class TestIdentity:
    def test_case_set_id_ignores_order(self) -> None:
        assert leaderboard.compute_case_set_id(
            ["b", "a"]
        ) == leaderboard.compute_case_set_id(["a", "b"])

    def test_case_set_id_changes_when_cases_change(self) -> None:
        assert leaderboard.compute_case_set_id(
            ["a", "b"]
        ) != leaderboard.compute_case_set_id(["a", "b", "c"])

    def test_run_fingerprint_changes_with_repeat(self) -> None:
        """A 5-sample run must not be discarded as a duplicate of a 1-sample run."""
        kwargs = {
            "git_sha": "abc",
            "case_set_id": "case-set",
            "requested_model": "anthropic/test-model",
            "reasoning_effort": "high",
            "max_tool_iterations": 5,
            "route_set_id": "route-a",
        }

        single = leaderboard.compute_run_fingerprint(**kwargs, repeat=1)
        repeated = leaderboard.compute_run_fingerprint(**kwargs, repeat=5)

        assert single != repeated

    def test_route_set_id_is_independent_of_repeat_count(self) -> None:
        """Repeat must not leak into route identity, or rows never collapse."""
        single = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True)],
            requested_model="anthropic/model-a",
            reasoning_effort="high",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-single",
        )
        repeated = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True) for _ in range(3)],
            requested_model="anthropic/model-a",
            reasoning_effort="high",
            created_at="2026-04-11T11:00:00Z",
            run_id="run-repeat",
            repeat=3,
        )

        assert single["route_set_id"] == repeated["route_set_id"]


class TestRepeatAggregation:
    def test_repeated_attempts_collapse_to_one_row_per_case(self) -> None:
        record = _build_record(
            case_ids=["case-a"],
            results=[
                _eval_result("case-a", passed=True),
                _eval_result("case-a", passed=False),
                _eval_result("case-a", passed=True),
            ],
            requested_model="anthropic/model-a",
            reasoning_effort="high",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-flaky",
            repeat=3,
        )

        assert record["case_count"] == 1
        assert len(record["per_case"]) == 1
        row = record["per_case"][0]
        assert (row["runs"], row["passes"], row["scored"]) == (3, 2, 3)
        assert row["pass_rate"] == pytest.approx(2 / 3)
        assert row["flaky"] is True
        assert row["outcome"] == "flaky"
        assert len(row["attempts"]) == 3

    def test_strict_accuracy_scores_a_flaky_case_as_unreliable(self) -> None:
        record = _build_record(
            case_ids=["case-a", "case-b"],
            results=[
                _eval_result("case-a", passed=True),
                _eval_result("case-a", passed=True),
                _eval_result("case-b", passed=True),
                _eval_result("case-b", passed=False),
            ],
            requested_model="anthropic/model-a",
            reasoning_effort="high",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-strict",
            repeat=2,
        )

        summary = record["summary"]
        assert summary["accuracy"] == pytest.approx(75.0)
        assert summary["strict_accuracy"] == pytest.approx(50.0)
        assert summary["flaky_count"] == 1
        assert summary["stable_pass_count"] == 1

    def test_stable_failure_is_not_counted_as_flaky(self) -> None:
        record = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=False) for _ in range(3)],
            requested_model="anthropic/model-a",
            reasoning_effort="high",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-stable-fail",
            repeat=3,
        )

        row = record["per_case"][0]
        assert row["flaky"] is False
        assert row["outcome"] == "fail"
        assert record["summary"]["stable_fail_count"] == 1

    def test_cost_per_repeat_normalizes_across_repeat_settings(self) -> None:
        record = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True, cost=0.02) for _ in range(4)],
            requested_model="anthropic/model-a",
            reasoning_effort="high",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-cost",
            repeat=4,
        )

        summary = record["summary"]
        assert summary["total_cost"] == pytest.approx(0.08)
        assert summary["cost_per_repeat"] == pytest.approx(0.02)

    def test_errored_attempts_stay_out_of_the_accuracy_denominator(self) -> None:
        """A provider outage must not read as a quality regression."""
        record = _build_record(
            case_ids=["case-a", "case-b"],
            results=[
                _eval_result("case-a", passed=True),
                _eval_result("case-b", passed=False, error="provider 500"),
            ],
            requested_model="anthropic/model-a",
            reasoning_effort="high",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-errored",
        )

        summary = record["summary"]
        assert summary["accuracy"] == pytest.approx(100.0)
        assert summary["errored"] == 1
        assert summary["errored_case_count"] == 1


class TestFallbackVisibility:
    """A fallback must not be scored under the model the route asked for."""

    def test_record_marks_a_case_answered_by_a_fallback(self) -> None:
        result = _eval_result("case-a", passed=True)
        result.model = "deepseek/deepseek-v4-flash"
        result.route = {
            **result.route,
            "primary": "openai/gpt-5.6-luna",
            "requested_primary": "openai/gpt-5.6-luna",
            "fallback_used": True,
        }

        record = _build_record(
            case_ids=["case-a"],
            results=[result],
            requested_model=None,
            reasoning_effort="production",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-fallback",
        )

        assert record["per_case"][0]["fallback_used"] is True
        assert record["per_case"][0]["model"] == "deepseek/deepseek-v4-flash"

    def test_markdown_says_which_features_a_fallback_answered(self) -> None:
        result = _eval_result("case-a", passed=True, feature="nudge")
        result.model = "deepseek/deepseek-v4-flash"
        result.route = {
            **result.route,
            "primary": "openai/gpt-5.6-luna",
            "fallback_used": True,
        }
        record = _build_record(
            case_ids=["case-a"],
            results=[result],
            requested_model=None,
            reasoning_effort="production",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-fallback-md",
        )

        markdown = leaderboard.render_leaderboard_markdown(
            [record], inventory=[_case("case-a", "nudge")]
        )

        assert "Answered by a fallback model:** `nudge`" in markdown


class TestProductionScorecard:
    def test_production_row_uses_latest_production_run_per_feature(self) -> None:
        old = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=False)],
            requested_model=None,
            reasoning_effort="production",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-old",
        )
        new = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True)],
            requested_model=None,
            reasoning_effort="production",
            created_at="2026-04-12T10:00:00Z",
            run_id="run-new",
        )
        challenger = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True)],
            requested_model="anthropic/challenger",
            reasoning_effort="high",
            created_at="2026-04-13T10:00:00Z",
            run_id="run-challenger",
        )

        scorecard = build_scorecard([old, new, challenger], inventory=[_case("case-a")])

        row = scorecard["production"][0]["row"]
        assert row["run_id"] == "run-new"
        assert row["is_production"] is True

    def test_feature_without_a_production_run_is_reported_as_uncovered(self) -> None:
        run = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True, feature="chat")],
            requested_model=None,
            reasoning_effort="production",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-chat",
        )

        scorecard = build_scorecard(
            [run],
            inventory=[_case("case-a", "chat"), _case("case-n", "nudge")],
        )

        assert scorecard["uncovered_features"] == ["nudge"]
        nudge = next(
            entry for entry in scorecard["production"] if entry["feature"] == "nudge"
        )
        assert nudge["row"] is None
        assert nudge["missing_case_ids"] == ["case-n"]

    def test_cases_added_since_the_run_are_reported_as_missing(self) -> None:
        run = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True)],
            requested_model=None,
            reasoning_effort="production",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-partial",
        )

        scorecard = build_scorecard([run], inventory=[_case("case-a"), _case("case-b")])

        assert scorecard["production"][0]["missing_case_ids"] == ["case-b"]

    def test_rows_are_stale_when_measured_code_has_changed(self) -> None:
        run = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True)],
            requested_model=None,
            reasoning_effort="production",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-stale",
            git_sha="oldsha1234",
        )

        scorecard = build_scorecard(
            [run], inventory=[_case("case-a")], stale_check=lambda sha: True
        )

        assert scorecard["production"][0]["row"]["stale"] is True

    def test_rows_are_not_stale_when_only_the_commit_moved(self) -> None:
        """Recording a run commits its own results and advances HEAD.

        Treating that as staleness would flag every published row forever.
        """
        run = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True)],
            requested_model=None,
            reasoning_effort="production",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-fresh",
            git_sha="oldsha1234",
        )

        scorecard = build_scorecard(
            [run],
            inventory=[_case("case-a")],
            head_sha="newsha5678",
            stale_check=lambda sha: False,
        )

        assert scorecard["production"][0]["row"]["stale"] is False

    def test_unknowable_staleness_does_not_flag_a_row(self) -> None:
        """A shallow CI clone cannot resolve the sha; never flag on a guess."""
        run = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True)],
            requested_model=None,
            reasoning_effort="production",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-unknown",
        )

        scorecard = build_scorecard(
            [run], inventory=[_case("case-a")], stale_check=lambda sha: None
        )

        assert scorecard["production"][0]["row"]["stale"] is False

    def test_variation_rows_are_grouped_by_feature_not_case_set(self) -> None:
        """Adding a case must not orphan a feature's comparison history."""
        two_cases = _build_record(
            case_ids=["case-a", "case-b"],
            results=[
                _eval_result("case-a", passed=True),
                _eval_result("case-b", passed=True),
            ],
            requested_model="anthropic/model-a",
            reasoning_effort="high",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-two",
        )
        one_case = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True)],
            requested_model="anthropic/model-b",
            reasoning_effort="high",
            created_at="2026-04-12T10:00:00Z",
            run_id="run-one",
        )

        scorecard = build_scorecard(
            [two_cases, one_case], inventory=[_case("case-a"), _case("case-b")]
        )

        assert len(scorecard["features"]) == 1
        rows = scorecard["features"][0]["rows"]
        assert {row["run_id"] for row in rows} == {"run-two", "run-one"}

    def test_repeat_settings_are_kept_as_separate_rows(self) -> None:
        single = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True)],
            requested_model="anthropic/model-a",
            reasoning_effort="high",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-single",
        )
        repeated = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True) for _ in range(3)],
            requested_model="anthropic/model-a",
            reasoning_effort="high",
            created_at="2026-04-12T10:00:00Z",
            run_id="run-repeat",
            repeat=3,
        )

        scorecard = build_scorecard([single, repeated], inventory=[_case("case-a")])

        rows = scorecard["features"][0]["rows"]
        assert {row["repeat"] for row in rows} == {1, 3}

    def test_rows_rank_strict_accuracy_above_attempt_accuracy(self) -> None:
        flaky = _build_record(
            case_ids=["case-a", "case-b"],
            results=[
                _eval_result("case-a", passed=True),
                _eval_result("case-a", passed=True),
                _eval_result("case-b", passed=True),
                _eval_result("case-b", passed=False),
            ],
            requested_model="anthropic/flaky",
            reasoning_effort="high",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-flaky",
            repeat=2,
        )
        steady = _build_record(
            case_ids=["case-a", "case-b"],
            results=[
                _eval_result("case-a", passed=True),
                _eval_result("case-a", passed=True),
                _eval_result("case-b", passed=True),
                _eval_result("case-b", passed=True),
            ],
            requested_model="anthropic/steady",
            reasoning_effort="high",
            created_at="2026-04-11T09:00:00Z",
            run_id="run-steady",
            repeat=2,
        )

        scorecard = build_scorecard(
            [flaky, steady], inventory=[_case("case-a"), _case("case-b")]
        )

        rows = scorecard["features"][0]["rows"]
        assert rows[0]["run_id"] == "run-steady"
        assert rows[1]["flaky_count"] == 1


class TestRecording:
    def test_record_run_skips_duplicate_by_default(self, tmp_path: Path) -> None:
        runs_path = tmp_path / "runs.jsonl"
        result = _eval_result("case-1", passed=True)
        kwargs = {
            "results": [result],
            "case_ids": ["case-1"],
            "requested_model": "anthropic/test-model",
            "reasoning_effort": "medium",
            "max_tool_iterations": 5,
            "feature_filter": "chat",
            "runs_path": runs_path,
            "markdown_path": tmp_path / "leaderboard.md",
            "html_path": tmp_path / "leaderboard.html",
            "repo_context": {"git_sha": "abc", "dirty": False},
            "inventory": [_case("case-1")],
        }

        first = leaderboard.record_run(**kwargs)
        second = leaderboard.record_run(**kwargs)

        lines = [
            line for line in runs_path.read_text(encoding="utf-8").splitlines() if line
        ]
        assert first.recorded is True
        assert second.recorded is False
        assert second.duplicate_of == first.record["run_id"]
        assert len(lines) == 1

    def test_record_run_force_duplicate_appends(self, tmp_path: Path) -> None:
        runs_path = tmp_path / "runs.jsonl"
        kwargs = {
            "results": [_eval_result("case-1", passed=True)],
            "case_ids": ["case-1"],
            "requested_model": "anthropic/test-model",
            "reasoning_effort": "medium",
            "max_tool_iterations": 5,
            "feature_filter": "chat",
            "runs_path": runs_path,
            "markdown_path": tmp_path / "leaderboard.md",
            "html_path": tmp_path / "leaderboard.html",
            "repo_context": {"git_sha": "abc", "dirty": False},
            "inventory": [_case("case-1")],
        }

        leaderboard.record_run(**kwargs)
        forced = leaderboard.record_run(**kwargs, allow_duplicate=True)

        lines = [
            line for line in runs_path.read_text(encoding="utf-8").splitlines() if line
        ]
        assert forced.recorded is True
        assert len(lines) == 2


class TestRendering:
    def test_markdown_reports_per_case_pass_rates_for_flaky_cases(self) -> None:
        run = _build_record(
            case_ids=["case-a"],
            results=[
                _eval_result("case-a", passed=True),
                _eval_result("case-a", passed=False),
                _eval_result("case-a", passed=True),
            ],
            requested_model=None,
            reasoning_effort="production",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-flaky",
            repeat=3,
        )

        markdown = leaderboard.render_leaderboard_markdown(
            [run], inventory=[_case("case-a")]
        )

        assert "`case-a` 2/3" in markdown
        assert "FLAKY" in markdown

    def test_markdown_leads_with_production_and_names_uncovered_features(self) -> None:
        run = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True, feature="chat")],
            requested_model=None,
            reasoning_effort="production",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-prod",
        )

        markdown = leaderboard.render_leaderboard_markdown(
            [run], inventory=[_case("case-a", "chat"), _case("case-n", "nudge")]
        )

        assert markdown.index("## Production") < markdown.index("## chat")
        assert "No production run recorded for:** `nudge`" in markdown

    def test_markdown_warns_when_production_is_a_single_sample(self) -> None:
        run = _build_record(
            case_ids=["case-a"],
            results=[_eval_result("case-a", passed=True)],
            requested_model=None,
            reasoning_effort="production",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-single",
        )

        markdown = leaderboard.render_leaderboard_markdown(
            [run], inventory=[_case("case-a")]
        )

        assert "Single sample (repeat=1)" in markdown

    def test_html_contains_scorecard_and_stability_filters(self) -> None:
        run = _build_record(
            case_ids=["case-a", "case-b"],
            results=[
                _eval_result("case-a", passed=True),
                _eval_result("case-a", passed=False),
                _eval_result("case-b", passed=True),
                _eval_result("case-b", passed=True),
            ],
            requested_model=None,
            reasoning_effort="production",
            created_at="2026-04-11T10:00:00Z",
            run_id="run-html",
            repeat=2,
        )

        html = leaderboard.render_leaderboard_html(
            [run], inventory=[_case("case-a"), _case("case-b")]
        )

        assert "feature-filter" in html
        assert "flaky-only" in html
        assert "production-only" in html
        assert "hide-stale" in html
        assert "strict_accuracy" in html
        assert "leaderboard-data" in html
        assert "case-a" in html

    def test_empty_history_renders_guidance(self) -> None:
        markdown = leaderboard.render_leaderboard_markdown([], inventory=[])

        assert "No recorded eval runs yet" in markdown


class TestCli:
    @pytest.fixture
    def recording_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> dict[str, Path]:
        paths = {
            "runs": tmp_path / "runs.jsonl",
            "markdown": tmp_path / "leaderboard.md",
            "html": tmp_path / "leaderboard.html",
        }
        monkeypatch.setattr(leaderboard_record, "RUNS_PATH", paths["runs"])
        monkeypatch.setattr(leaderboard_record, "MARKDOWN_PATH", paths["markdown"])
        monkeypatch.setattr(leaderboard_record, "HTML_PATH", paths["html"])
        monkeypatch.setattr(
            leaderboard_record,
            "get_repo_context",
            lambda *args, **kwargs: {"git_sha": "abc", "dirty": False},
        )
        return paths

    def _stub_run(
        self, monkeypatch: pytest.MonkeyPatch, results: list[EvalResult]
    ) -> None:
        case = _case(results[0].case_id)
        monkeypatch.setattr(eval_run, "load_cases", lambda: [case])
        monkeypatch.setattr(
            eval_run, "_run_selected_cases", lambda *args, **kwargs: results
        )
        monkeypatch.setattr(eval_run, "print_results", lambda results: None)
        monkeypatch.setattr(eval_run, "print_result_details", lambda results: None)

    def test_eval_run_record_writes_jsonl_and_renders(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recording_paths: dict[str, Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._stub_run(monkeypatch, [_eval_result("case-a", passed=True)])
        monkeypatch.setattr(sys, "argv", ["evals.run", "--record"])

        eval_run.main()

        out = capsys.readouterr().out
        lines = [
            line
            for line in recording_paths["runs"].read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert len(lines) == 1
        assert "Recorded leaderboard run" in out
        assert recording_paths["markdown"].exists()
        assert recording_paths["html"].exists()

    def test_eval_run_records_the_repeat_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recording_paths: dict[str, Path],
    ) -> None:
        """Without this, stability is unreadable from the recorded history."""
        results = [_eval_result("case-a", passed=True) for _ in range(3)]
        self._stub_run(monkeypatch, results)
        monkeypatch.setattr(sys, "argv", ["evals.run", "--record", "--repeat", "3"])

        eval_run.main()

        record = json.loads(
            recording_paths["runs"].read_text(encoding="utf-8").splitlines()[0]
        )
        assert record["repeat"] == 3
        assert record["case_count"] == 1
        assert record["per_case"][0]["runs"] == 3

    def test_eval_run_marks_a_run_without_model_as_production(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recording_paths: dict[str, Path],
    ) -> None:
        self._stub_run(monkeypatch, [_eval_result("case-a", passed=True)])
        monkeypatch.setattr(sys, "argv", ["evals.run", "--record"])

        eval_run.main()

        record = json.loads(
            recording_paths["runs"].read_text(encoding="utf-8").splitlines()[0]
        )
        assert record["is_production"] is True
        assert record["requested_model"] is None

    def test_eval_run_duplicate_skip_and_force_append(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recording_paths: dict[str, Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._stub_run(monkeypatch, [_eval_result("case-a", passed=True)])

        monkeypatch.setattr(sys, "argv", ["evals.run", "--record"])
        eval_run.main()
        eval_run.main()
        second_out = capsys.readouterr().out

        lines = [
            line
            for line in recording_paths["runs"].read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert len(lines) == 1
        assert "already recorded" in second_out

        monkeypatch.setattr(
            sys, "argv", ["evals.run", "--record", "--record-duplicate"]
        )
        eval_run.main()
        lines = [
            line
            for line in recording_paths["runs"].read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert len(lines) == 2

    def test_parallel_results_keep_submission_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Out-of-order completion must not shuffle the table or the rollup."""
        import time

        delays = {"case-a": 0.05, "case-b": 0.0}

        def _fake_run_case(case: EvalCase, **kwargs: object) -> EvalResult:
            time.sleep(delays[case.id])
            return _eval_result(case.id, passed=True)

        monkeypatch.setattr(eval_run, "run_case", _fake_run_case)

        results = eval_run._run_selected_cases(
            [_case("case-a"), _case("case-b")],
            model=None,
            max_tool_iterations=5,
            repeat=2,
            concurrency=4,
        )

        assert [result.case_id for result in results] == [
            "case-a",
            "case-a",
            "case-b",
            "case-b",
        ]

    def test_concurrency_defaults_to_repeat(
        self, monkeypatch: pytest.MonkeyPatch, recording_paths: dict[str, Path]
    ) -> None:
        seen: dict[str, object] = {}

        def _capture(cases: object, **kwargs: object) -> list[EvalResult]:
            seen.update(kwargs)
            return [_eval_result("case-a", passed=True)]

        monkeypatch.setattr(eval_run, "load_cases", lambda: [_case("case-a")])
        monkeypatch.setattr(eval_run, "_run_selected_cases", _capture)
        monkeypatch.setattr(eval_run, "print_results", lambda results: None)
        monkeypatch.setattr(eval_run, "print_result_details", lambda results: None)
        monkeypatch.setattr(sys, "argv", ["evals.run", "--repeat", "3"])

        eval_run.main()

        assert seen["concurrency"] == 3

    def test_concurrency_rejects_cache(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The response cache is one SQLite connection, not thread-safe."""
        monkeypatch.setattr(sys, "argv", ["evals.run", "--concurrency", "4", "--cache"])

        with pytest.raises(SystemExit):
            eval_run.main()

        assert "--concurrency cannot be used with --cache" in capsys.readouterr().err

    def test_repeat_rejects_cache(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Replaying one cached sample N times would report false stability."""
        monkeypatch.setattr(sys, "argv", ["evals.run", "--repeat", "3", "--cache"])

        with pytest.raises(SystemExit):
            eval_run.main()

        assert "--repeat cannot be used with --cache" in capsys.readouterr().err

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

        leaderboard_cli.main()

        assert "Rendered leaderboard with 1 run(s)" in capsys.readouterr().out
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

        leaderboard_cli.main()

        assert "Rendered HTML leaderboard with 1 run(s)" in capsys.readouterr().out
        assert "feature-filter" in html_path.read_text(encoding="utf-8")


class TestMatrix:
    def test_matrix_records_the_repeat_count(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured: dict[str, object] = {}

        def _capture(**kwargs: object) -> leaderboard.RecordRunOutcome:
            captured.update(kwargs)
            return leaderboard.RecordRunOutcome(recorded=True, record={"run_id": "x"})

        monkeypatch.setattr(eval_matrix.leaderboard, "record_run", _capture)
        run = eval_matrix.MatrixRun(
            label="cell",
            cases=[_case("case-a")],
            model="anthropic/model-a",
            reasoning_effort="high",
            temperature=None,
        )

        eval_matrix._record_matrix_run(
            [_eval_result("case-a", passed=True)],
            matrix_run=run,
            max_tool_iterations=5,
            feature_filter="chat",
            repeat=4,
            allow_duplicate=False,
        )

        assert captured["repeat"] == 4

    def test_build_matrix_runs_expands_models_and_reasoning(self) -> None:
        cases = [_case("case-a", "chat")]

        runs = eval_matrix.build_matrix_runs(
            cases,
            feature="chat",
            production=False,
            case_ids=None,
            models=["deepseek/a", "deepseek/b"],
            reasoning_efforts=["none", "high"],
            temperature=None,
        )

        assert len(runs) == 4
        assert {run.model for run in runs} == {"deepseek/a", "deepseek/b"}

    def test_production_matrix_uses_production_routes(self) -> None:
        cases = [_case("case-a", "chat")]

        runs = eval_matrix.build_matrix_runs(
            cases,
            feature=None,
            production=True,
            case_ids=None,
            models=[],
            reasoning_efforts=["none"],
            temperature=None,
        )

        assert len(runs) == 1
        assert runs[0].model is None
