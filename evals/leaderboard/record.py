"""Recording eval runs to the leaderboard history.

One recorded run is one invocation of `evals.run` or one `evals.matrix` cell.
Because `--repeat N` runs every case N times, a run holds N attempts per case;
the record aggregates those attempts into one row per case and keeps the raw
attempts nested, so stability is recoverable from the history rather than
collapsed into a single average.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.framework import EvalResult, score_counts

LEADERBOARD_DIR = Path(__file__).resolve().parent
RUNS_PATH = LEADERBOARD_DIR / "runs.jsonl"
MARKDOWN_PATH = LEADERBOARD_DIR.parent / "leaderboard.md"
HTML_PATH = LEADERBOARD_DIR.parent / "leaderboard.html"
_ROOT = LEADERBOARD_DIR.parent.parent


@dataclass(frozen=True)
class RecordRunOutcome:
    """Result of attempting to persist an eval leaderboard run."""

    recorded: bool
    record: dict[str, Any]
    duplicate_of: str | None = None


def compute_case_set_id(case_ids: list[str]) -> str:
    """Return a stable fingerprint for a sorted set of case ids."""
    return _stable_hash(sorted(case_ids))


def compute_run_fingerprint(
    *,
    git_sha: str,
    case_set_id: str,
    requested_model: str | None,
    reasoning_effort: str | None,
    max_tool_iterations: int,
    route_set_id: str,
    repeat: int,
) -> str:
    """Return a stable fingerprint for one comparable eval run.

    Repeat count is part of the identity: a 5-sample run and a 1-sample run of
    the same commit and route are different measurements, and the duplicate
    guard must not discard the more informative one as an already-seen rerun.
    """
    return _stable_hash(
        {
            "git_sha": git_sha,
            "case_set_id": case_set_id,
            "requested_model": requested_model,
            "reasoning_effort": reasoning_effort,
            "max_tool_iterations": max_tool_iterations,
            "route_set_id": route_set_id,
            "repeat": repeat,
        }
    )


def get_repo_context(repo_root: Path = _ROOT) -> dict[str, Any]:
    """Return the current git sha and dirty state for the repository."""
    git_sha = _git_output(["git", "rev-parse", "HEAD"], cwd=repo_root) or "unknown"
    dirty = bool(_git_output(["git", "status", "--porcelain"], cwd=repo_root))
    return {"git_sha": git_sha, "dirty": dirty}


def build_run_record(
    *,
    results: list[EvalResult],
    case_ids: list[str],
    requested_model: str | None,
    reasoning_effort: str | None,
    max_tool_iterations: int,
    feature_filter: str | None,
    repeat: int = 1,
    repo_context: dict[str, Any] | None = None,
    created_at: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build a normalized leaderboard record from eval results.

    Args:
        results: Every attempt from the run, `repeat` per case.
        case_ids: The distinct cases selected for the run.
        requested_model: Explicit `--model`, or None for production routes.
        reasoning_effort: Reasoning effort asked of the run.
        max_tool_iterations: Tool loop cap used.
        feature_filter: `--feature` value, or None for a mixed run.
        repeat: Attempts per case.
        repo_context: Git sha/dirty override, for tests.
        created_at: Timestamp override, for tests.
        run_id: Run id override, for tests.

    Returns:
        The record as persisted to the JSONL history.
    """
    sorted_case_ids = sorted(case_ids)
    context = repo_context or get_repo_context()
    per_case = _aggregate_per_case(results)
    record = {
        "run_id": run_id or uuid.uuid4().hex,
        "created_at": created_at or _utc_now_iso(),
        "requested_model": requested_model,
        "is_production": requested_model is None,
        "reasoning_effort": reasoning_effort,
        "max_tool_iterations": max_tool_iterations,
        "repeat": repeat,
        "case_ids": sorted_case_ids,
        "case_count": len(sorted_case_ids),
        "feature_filter": feature_filter,
        "case_set_id": compute_case_set_id(sorted_case_ids),
        "route_set_id": _compute_route_set_id(per_case),
        "git_sha": str(context.get("git_sha", "unknown")),
        "dirty": bool(context.get("dirty", False)),
        "summary": build_summary_metrics(results, per_case, repeat=repeat),
        "feature_summary": build_feature_summary(results, per_case, repeat=repeat),
        "per_case": per_case,
    }
    record["run_fingerprint"] = compute_run_fingerprint(
        git_sha=record["git_sha"],
        case_set_id=record["case_set_id"],
        requested_model=requested_model,
        reasoning_effort=reasoning_effort,
        max_tool_iterations=max_tool_iterations,
        route_set_id=record["route_set_id"],
        repeat=repeat,
    )
    return record


def load_run_records(runs_path: Path | None = None) -> list[dict[str, Any]]:
    """Load all persisted leaderboard run records from JSONL."""
    path = runs_path or RUNS_PATH
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        records.append(json.loads(stripped))
    return records


def record_run(
    *,
    results: list[EvalResult],
    case_ids: list[str],
    requested_model: str | None,
    reasoning_effort: str | None,
    max_tool_iterations: int,
    feature_filter: str | None,
    repeat: int = 1,
    allow_duplicate: bool = False,
    runs_path: Path | None = None,
    markdown_path: Path | None = None,
    html_path: Path | None = None,
    repo_context: dict[str, Any] | None = None,
    inventory: list[Any] | None = None,
) -> RecordRunOutcome:
    """Persist one eval run and regenerate the Markdown and HTML leaderboards."""
    from evals.leaderboard.html import write_leaderboard_html
    from evals.leaderboard.markdown import write_leaderboard_markdown

    path = runs_path or RUNS_PATH
    markdown = markdown_path or MARKDOWN_PATH
    html_output = html_path or HTML_PATH
    context = repo_context or get_repo_context()
    head_sha = str(context.get("git_sha", "unknown"))
    runs = load_run_records(path)
    record = build_run_record(
        results=results,
        case_ids=case_ids,
        requested_model=requested_model,
        reasoning_effort=reasoning_effort,
        max_tool_iterations=max_tool_iterations,
        feature_filter=feature_filter,
        repeat=repeat,
        repo_context=context,
    )
    duplicate = next(
        (
            existing
            for existing in runs
            if existing.get("run_fingerprint") == record["run_fingerprint"]
        ),
        None,
    )
    if duplicate is not None and not allow_duplicate:
        write_leaderboard_markdown(
            runs, markdown, inventory=inventory, head_sha=head_sha
        )
        write_leaderboard_html(
            runs, html_output, inventory=inventory, head_sha=head_sha
        )
        return RecordRunOutcome(
            recorded=False,
            record=duplicate,
            duplicate_of=str(duplicate.get("run_id")),
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    runs.append(record)
    write_leaderboard_markdown(runs, markdown, inventory=inventory, head_sha=head_sha)
    write_leaderboard_html(runs, html_output, inventory=inventory, head_sha=head_sha)
    return RecordRunOutcome(recorded=True, record=record)


def _aggregate_per_case(results: list[EvalResult]) -> list[dict[str, Any]]:
    """Collapse repeated attempts into one row per case.

    A run with `--repeat N` produces N results sharing a case id. Keeping them
    as N sibling rows makes `case_count` disagree with the row count and hides
    the only thing repetition measures: whether a case passes consistently.
    """
    grouped: dict[str, list[EvalResult]] = {}
    for result in results:
        grouped.setdefault(result.case_id, []).append(result)

    rows: list[dict[str, Any]] = []
    for case_id in sorted(grouped):
        attempts = grouped[case_id]
        first = attempts[0]
        scored = [attempt for attempt in attempts if not attempt.errored]
        passes = sum(1 for attempt in scored if attempt.passed)
        rows.append(
            {
                "case_id": case_id,
                "feature": first.feature,
                "case_kind": first.case_kind,
                "model": first.model,
                "route": first.route,
                "runs": len(attempts),
                "scored": len(scored),
                "errored": len(attempts) - len(scored),
                "passes": passes,
                "pass_rate": (passes / len(scored)) if scored else None,
                "flaky": 0 < passes < len(scored),
                "outcome": _case_outcome(passes, len(scored)),
                "failure_names": sorted(
                    {
                        failure.name
                        for attempt in attempts
                        for failure in attempt.failures
                    }
                ),
                "attempts": [_build_attempt(attempt) for attempt in attempts],
            }
        )
    return rows


def _case_outcome(passes: int, scored: int) -> str:
    """Classify one case's repeated attempts."""
    if scored == 0:
        return "errored"
    if passes == scored:
        return "pass"
    if passes == 0:
        return "fail"
    return "flaky"


def _build_attempt(result: EvalResult) -> dict[str, Any]:
    """Build one attempt row inside an aggregated case."""
    execution = result.execution
    return {
        "passed": result.passed,
        "errored": result.errored,
        "error": result.error,
        "failure_names": [failure.name for failure in result.failures],
        "assertions": [
            {"name": assertion.name, "passed": assertion.passed, "kind": assertion.kind}
            for assertion in result.assertions
        ],
        "latency_s": execution.latency_s if execution is not None else None,
        "cost": execution.cost if execution is not None else None,
    }


def build_summary_metrics(
    results: list[EvalResult],
    per_case: list[dict[str, Any]],
    *,
    repeat: int = 1,
) -> dict[str, Any]:
    """Build summary metrics for a batch of attempts and their case rollups.

    Two accuracies are stored because they answer different questions.
    `accuracy` is attempt-weighted and estimates what a single run would score.
    `strict_accuracy` counts only cases that passed every attempt, so a flaky
    case scores as the unreliable result it is rather than as a fraction.
    """
    executions = [
        result.execution for result in results if result.execution is not None
    ]
    passed, failed, errored, accuracy = score_counts(results)
    latencies = [execution.latency_s for execution in executions]
    costs = [execution.cost for execution in executions if execution.cost is not None]
    total_cost = sum(costs) if costs else None
    scored_cases = [row for row in per_case if row["scored"]]
    stable_pass = sum(1 for row in scored_cases if row["outcome"] == "pass")
    return {
        "accuracy": accuracy,
        "strict_accuracy": (
            (stable_pass / len(scored_cases) * 100.0) if scored_cases else 0.0
        ),
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "case_count": len(per_case),
        "flaky_count": sum(1 for row in per_case if row["flaky"]),
        "stable_pass_count": stable_pass,
        "stable_fail_count": sum(1 for row in scored_cases if row["outcome"] == "fail"),
        "errored_case_count": sum(1 for row in per_case if row["outcome"] == "errored"),
        "avg_latency_s": sum(latencies) / len(latencies) if latencies else None,
        "p95_latency_s": _percentile_nearest_rank(latencies, 0.95)
        if latencies
        else None,
        "total_cost": total_cost,
        # Cost of covering the case set once. Without it, a repeat=5 row is
        # ranked on cost against a repeat=1 row at five times the price.
        "cost_per_repeat": (total_cost / repeat) if total_cost is not None else None,
        "avg_cost": (sum(costs) / len(costs)) if costs else None,
        "input_tokens": sum(execution.input_tokens for execution in executions),
        "output_tokens": sum(execution.output_tokens for execution in executions),
        "total_tokens": sum(execution.total_tokens for execution in executions),
        "cache_hits": sum(execution.cache_hits for execution in executions),
        "cache_misses": sum(execution.cache_misses for execution in executions),
    }


def build_feature_summary(
    results: list[EvalResult],
    per_case: list[dict[str, Any]],
    *,
    repeat: int = 1,
) -> dict[str, Any]:
    """Build per-feature rollups for a run."""
    features = sorted({row["feature"] for row in per_case})
    summary: dict[str, Any] = {}
    for feature in features:
        feature_results = [result for result in results if result.feature == feature]
        feature_cases = [row for row in per_case if row["feature"] == feature]
        metrics = build_summary_metrics(feature_results, feature_cases, repeat=repeat)
        summary[feature] = {
            "case_count": len(feature_cases),
            "case_ids": [str(row["case_id"]) for row in feature_cases],
            "accuracy": metrics["accuracy"],
            "strict_accuracy": metrics["strict_accuracy"],
            "passed": metrics["passed"],
            "failed": metrics["failed"],
            "errored": metrics["errored"],
            "flaky_count": metrics["flaky_count"],
            "avg_latency_s": metrics["avg_latency_s"],
            "cost_per_repeat": metrics["cost_per_repeat"],
            "routes": sorted(
                {
                    route_label(row["route"] or {"primary": row["model"]})
                    for row in feature_cases
                }
            ),
        }
    return summary


def _compute_route_set_id(per_case: list[dict[str, Any]]) -> str:
    """Return a stable fingerprint for the actual per-case model routes.

    Built from aggregated cases, so the fingerprint describes routes only.
    Hashing one entry per attempt made repeat count leak into route identity,
    and two runs over identical routes then never collapsed into one row.
    """
    route_payload = [
        {
            "case_id": row["case_id"],
            "feature": row["feature"],
            "model": row["model"],
            "route": row["route"],
        }
        for row in per_case
    ]
    return _stable_hash(route_payload)


EVAL_SOURCE_PATHS = (
    "src",
    "main.py",
    "evals/framework.py",
    "evals/cases",
    "evals/run.py",
    "evals/matrix.py",
)
"""Paths whose changes can move an eval score.

Chosen as the code an eval actually exercises: the app under test, the harness
that drives it, and the cases themselves. Deliberately excludes
`evals/leaderboard/` — recording a run commits its own results, so counting
that as a change would mark every run stale the moment it was published.
"""


def code_changed_since(sha: str | None, repo_root: Path = _ROOT) -> bool | None:
    """Return whether eval-relevant source changed between `sha` and HEAD.

    Staleness cannot be `sha != HEAD`. Recording a run and committing its
    results advances HEAD by construction, so that test marks every published
    row stale forever and the warning stops meaning anything.

    Args:
        sha: The commit a run was recorded at.
        repo_root: Repository to inspect.

    Returns:
        True or False, or None when the answer is unknowable — an unknown sha,
        a shallow clone missing the commit, or no git at all. Callers treat
        None as "not stale" so a row is never flagged on a guess.
    """
    if not sha or sha == "unknown":
        return None
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only", f"{sha}..HEAD", "--", *EVAL_SOURCE_PATHS],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    return bool(completed.stdout.strip())


def route_label(route: dict[str, Any]) -> str:
    """Return a compact route label for reports."""
    primary = str(route["primary"])
    effort = route.get("reasoning_effort")
    fallback_models = route.get("fallback_models") or []
    label = primary.split("/")[-1]
    if effort:
        label += f" ({effort})"
    if fallback_models:
        fallback = str(fallback_models[0]).split("/")[-1]
        extra = "" if len(fallback_models) == 1 else f"+{len(fallback_models) - 1}"
        label += f" -> {fallback}{extra}"
    return label


def _git_output(command: list[str], cwd: Path) -> str | None:
    """Return stripped stdout for a git command, or None on failure."""
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    return completed.stdout.strip() or None


def _percentile_nearest_rank(values: list[float], percentile: float) -> float:
    """Return the nearest-rank percentile for a non-empty numeric list."""
    sorted_values = sorted(values)
    rank = max(1, int((percentile * len(sorted_values)) + 0.999999999))
    return sorted_values[rank - 1]


def _stable_hash(payload: Any) -> str:
    """Return a stable sha256 hash for a JSON-serializable payload."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc_now_iso() -> str:
    """Return an ISO timestamp in UTC suitable for persisted run records."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
