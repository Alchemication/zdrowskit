"""Small feedback-driven eval framework.

The framework is intentionally narrow: cases are curated from real thumbs-down
feedback, and each case runs through the current production prompt path. The
runner captures tool calls, but never writes context files.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import llm  # noqa: E402
from charts import strip_charts  # noqa: E402
from config import PROMPTS_DIR  # noqa: E402
from tools import all_chat_tools  # noqa: E402

CASES_DIR = Path(__file__).resolve().parent / "cases"
DEFAULT_MODEL = llm.DEFAULT_MODEL


@dataclass(frozen=True)
class EvalCase:
    """A curated eval case derived from one real feedback item."""

    id: str
    feature: str
    case_kind: str
    source_feedback_id: int
    source_llm_call_id: int
    derived_from: dict[str, Any]
    intent: str
    fixture: dict[str, Any]
    assertions: list[dict[str, Any]]
    notes: str = ""


@dataclass(frozen=True)
class CapturedToolCall:
    """A parsed tool call emitted during an eval run."""

    name: str
    arguments: dict[str, Any]
    tool_call_id: str


@dataclass(frozen=True)
class AssertionResult:
    """Result of one deterministic assertion."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class EvalExecution:
    """Captured output from a case run before assertions."""

    text: str
    tool_calls: list[CapturedToolCall] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_s: float = 0.0
    cost: float | None = None


@dataclass
class EvalResult:
    """Final eval outcome for one case and model."""

    case_id: str
    feature: str
    case_kind: str
    model: str
    source_feedback_id: int
    source_llm_call_id: int
    assertions: list[AssertionResult] = field(default_factory=list)
    execution: EvalExecution | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        """Whether every assertion passed and no runner error occurred."""
        return self.error is None and all(
            assertion.passed for assertion in self.assertions
        )

    @property
    def failures(self) -> list[AssertionResult]:
        """Failed assertion results."""
        return [assertion for assertion in self.assertions if not assertion.passed]


def load_cases(cases_dir: Path = CASES_DIR) -> list[EvalCase]:
    """Load all curated feedback eval cases.

    Args:
        cases_dir: Directory containing one JSON file per case.

    Returns:
        Cases sorted by id.
    """
    cases: list[EvalCase] = []
    for path in sorted(cases_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        cases.append(_case_from_dict(raw, path))
    if not cases:
        raise ValueError(f"No eval cases found in {cases_dir}")
    return sorted(cases, key=lambda case: case.id)


def run_case(
    case: EvalCase,
    *,
    model: str = DEFAULT_MODEL,
    max_tool_iterations: int = 5,
) -> EvalResult:
    """Run one eval case and evaluate its deterministic assertions."""
    result = EvalResult(
        case_id=case.id,
        feature=case.feature,
        case_kind=case.case_kind,
        model=model,
        source_feedback_id=case.source_feedback_id,
        source_llm_call_id=case.source_llm_call_id,
    )
    try:
        if case.feature != "chat":
            raise ValueError(f"Unsupported eval feature: {case.feature}")
        execution = _run_chat_case(
            case,
            model=model,
            max_tool_iterations=max_tool_iterations,
        )
        result.execution = execution
        result.assertions = run_assertions(case.assertions, execution)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def run_assertions(
    assertions: list[dict[str, Any]],
    execution: EvalExecution,
) -> list[AssertionResult]:
    """Evaluate deterministic assertions against a captured execution."""
    return [_evaluate_assertion(assertion, execution) for assertion in assertions]


def print_results(results: list[EvalResult]) -> None:
    """Print a compact human-readable result table."""
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        for result in results:
            status = "PASS" if result.passed else "FAIL"
            print(f"{status} {result.case_id}")
        return

    console = Console()
    table = Table(title="Feedback Eval Results", show_lines=False)
    table.add_column("Case", style="bold")
    table.add_column("Feature")
    table.add_column("Kind")
    table.add_column("Model", style="dim")
    table.add_column("Source")
    table.add_column("Pass", justify="center")
    table.add_column("Failures")

    for result in results:
        if result.error:
            failures = result.error
        else:
            failures = "; ".join(
                f"{failure.name}: {failure.detail}" if failure.detail else failure.name
                for failure in result.failures
            )
        table.add_row(
            result.case_id,
            result.feature,
            result.case_kind,
            result.model.split("/")[-1],
            f"fb#{result.source_feedback_id}/call#{result.source_llm_call_id}",
            "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]",
            failures or "-",
        )
    console.print(table)
    passed = sum(1 for result in results if result.passed)
    console.print(f"{passed}/{len(results)} passed")


def print_result_details(results: list[EvalResult]) -> None:
    """Print captured response/tool details for debugging failed evals."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.syntax import Syntax
    except ImportError:
        for result in results:
            execution = result.execution
            print(f"\n{result.case_id}")
            print("error:", result.error or "-")
            print("text:", execution.text if execution else "(no execution)")
            print("tools:", execution.tool_calls if execution else [])
        return

    console = Console()
    for result in results:
        execution = result.execution
        if result.passed and not result.error:
            continue
        parts = [f"Error: {result.error or '-'}"]
        if execution is not None:
            parts.append(f"Final text:\n{execution.text or '(empty)'}")
            tools = [
                {"name": call.name, "arguments": call.arguments}
                for call in execution.tool_calls
            ]
            parts.append("Captured tools:")
            parts.append(json.dumps(tools, indent=2))
        console.print(
            Panel(
                "\n\n".join(parts),
                title=f"Details: {result.case_id}",
                border_style="yellow",
            )
        )
        if execution is not None and execution.tool_calls:
            console.print(
                Syntax(
                    json.dumps(
                        [
                            {"name": call.name, "arguments": call.arguments}
                            for call in execution.tool_calls
                        ],
                        indent=2,
                    ),
                    "json",
                    theme="ansi_dark",
                )
            )


def _case_from_dict(raw: dict[str, Any], path: Path) -> EvalCase:
    required = {
        "id",
        "feature",
        "case_kind",
        "source_feedback_id",
        "source_llm_call_id",
        "derived_from",
        "intent",
        "fixture",
        "assertions",
    }
    missing = sorted(required - set(raw))
    if missing:
        raise ValueError(f"{path} missing required fields: {missing}")
    if not isinstance(raw["assertions"], list) or not raw["assertions"]:
        raise ValueError(f"{path} must define at least one assertion")
    fixture = raw["fixture"]
    if not isinstance(fixture, dict):
        raise ValueError(f"{path} fixture must be an object")
    if "today" not in fixture or "context" not in fixture or "turns" not in fixture:
        raise ValueError(f"{path} fixture must include today, context, and turns")
    return EvalCase(
        id=str(raw["id"]),
        feature=str(raw["feature"]),
        case_kind=str(raw["case_kind"]),
        source_feedback_id=int(raw["source_feedback_id"]),
        source_llm_call_id=int(raw["source_llm_call_id"]),
        derived_from=dict(raw["derived_from"]),
        intent=str(raw["intent"]),
        fixture=fixture,
        assertions=raw["assertions"],
        notes=str(raw.get("notes", "")),
    )


def _run_chat_case(
    case: EvalCase,
    *,
    model: str,
    max_tool_iterations: int,
) -> EvalExecution:
    fixture = case.fixture
    today = date.fromisoformat(str(fixture["today"]))
    context = _build_context(fixture)
    health_data_text = llm.render_health_data(
        fixture.get("health_data", {}),
        prompt_kind="chat",
        today=today,
    )
    messages: list[dict[str, Any]] = llm.build_messages(
        context,
        health_data_text=health_data_text,
        baselines=fixture.get("baselines"),
        today=today,
    )
    messages.extend(_fixture_turns(fixture))

    tools = all_chat_tools()
    captured: list[CapturedToolCall] = []
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    latency_s = 0.0
    cost = 0.0

    last_result: Any = None
    for iteration in range(max_tool_iterations):
        last_result = llm.call_llm(
            messages,
            model=model,
            max_tokens=int(fixture.get("max_tokens", 1024)),
            tools=tools,
            request_type="",
            metadata={
                "eval_case_id": case.id,
                "source_feedback_id": case.source_feedback_id,
                "iteration": iteration,
            },
        )
        input_tokens += int(getattr(last_result, "input_tokens", 0) or 0)
        output_tokens += int(getattr(last_result, "output_tokens", 0) or 0)
        total_tokens += int(getattr(last_result, "total_tokens", 0) or 0)
        latency_s += float(getattr(last_result, "latency_s", 0.0) or 0.0)
        if getattr(last_result, "cost", None) is not None:
            cost += float(last_result.cost)

        tool_calls = _result_tool_calls(last_result)
        if not tool_calls:
            return EvalExecution(
                text=str(getattr(last_result, "text", "") or ""),
                tool_calls=captured,
                messages=messages,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                latency_s=latency_s,
                cost=cost or None,
            )

        messages.append(_assistant_message(last_result))
        for raw_tool_call in tool_calls:
            tool_call = _capture_tool_call(raw_tool_call)
            captured.append(tool_call)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.tool_call_id,
                    "content": _eval_tool_result(tool_call, fixture),
                }
            )

    if last_result is not None and _result_tool_calls(last_result):
        last_result = llm.call_llm(
            messages,
            model=model,
            max_tokens=int(fixture.get("max_tokens", 1024)),
            tools=None,
            request_type="",
            metadata={
                "eval_case_id": case.id,
                "source_feedback_id": case.source_feedback_id,
                "iteration": "final_synthesis",
            },
        )
        input_tokens += int(getattr(last_result, "input_tokens", 0) or 0)
        output_tokens += int(getattr(last_result, "output_tokens", 0) or 0)
        total_tokens += int(getattr(last_result, "total_tokens", 0) or 0)
        latency_s += float(getattr(last_result, "latency_s", 0.0) or 0.0)
        if getattr(last_result, "cost", None) is not None:
            cost += float(last_result.cost)

    return EvalExecution(
        text=str(getattr(last_result, "text", "") if last_result is not None else ""),
        tool_calls=captured,
        messages=messages,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        latency_s=latency_s,
        cost=cost or None,
    )


def _build_context(fixture: dict[str, Any]) -> dict[str, str]:
    context = {key: str(value) for key, value in fixture["context"].items()}
    context["prompt"] = (PROMPTS_DIR / "chat_prompt.md").read_text(encoding="utf-8")
    soul_path = PROMPTS_DIR / "soul.md"
    context["soul"] = (
        soul_path.read_text(encoding="utf-8")
        if soul_path.exists()
        else llm.DEFAULT_SOUL
    )
    return context


def _fixture_turns(fixture: dict[str, Any]) -> list[dict[str, str]]:
    turns = fixture.get("turns", [])
    if not isinstance(turns, list) or not turns:
        raise ValueError("fixture.turns must be a non-empty list")
    cleaned: list[dict[str, str]] = []
    for turn in turns:
        role = turn.get("role")
        content = turn.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            raise ValueError(f"Invalid fixture turn: {turn}")
        cleaned.append({"role": role, "content": content})
    return cleaned


def _assistant_message(result: Any) -> dict[str, Any]:
    raw = getattr(result, "raw_message", None)
    if isinstance(raw, dict):
        return raw
    message: dict[str, Any] = {
        "role": "assistant",
        "content": str(getattr(result, "text", "") or ""),
    }
    tool_calls = _result_tool_calls(result)
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": _tool_call_id(tool_call),
                "type": "function",
                "function": {
                    "name": _tool_name(tool_call),
                    "arguments": json.dumps(_tool_arguments(tool_call)),
                },
            }
            for tool_call in tool_calls
        ]
    return message


def _result_tool_calls(result: Any) -> list[Any]:
    tool_calls = list(getattr(result, "tool_calls", None) or [])
    if tool_calls:
        return tool_calls
    raw = getattr(result, "raw_message", None)
    if isinstance(raw, dict):
        return list(raw.get("tool_calls", []) or [])
    return []


def _capture_tool_call(raw_tool_call: Any) -> CapturedToolCall:
    return CapturedToolCall(
        name=_tool_name(raw_tool_call),
        arguments=_tool_arguments(raw_tool_call),
        tool_call_id=_tool_call_id(raw_tool_call),
    )


def _tool_name(raw_tool_call: Any) -> str:
    function = _tool_function(raw_tool_call)
    if isinstance(function, dict):
        return str(function.get("name", ""))
    return str(getattr(function, "name", ""))


def _tool_arguments(raw_tool_call: Any) -> dict[str, Any]:
    function = _tool_function(raw_tool_call)
    raw_args = (
        function.get("arguments", "{}")
        if isinstance(function, dict)
        else getattr(function, "arguments", "{}")
    )
    if isinstance(raw_args, dict):
        return raw_args
    try:
        parsed = json.loads(str(raw_args or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_call_id(raw_tool_call: Any) -> str:
    if isinstance(raw_tool_call, dict):
        return str(raw_tool_call.get("id", "call_unknown"))
    return str(getattr(raw_tool_call, "id", "call_unknown"))


def _tool_function(raw_tool_call: Any) -> Any:
    if isinstance(raw_tool_call, dict):
        return raw_tool_call.get("function", {})
    return getattr(raw_tool_call, "function", {})


def _eval_tool_result(tool_call: CapturedToolCall, fixture: dict[str, Any]) -> str:
    if tool_call.name == "update_context":
        return "Proposed. User will be asked to confirm."
    if tool_call.name == "run_sql":
        return _execute_seed_sql(tool_call.arguments, fixture.get("db_seed"))
    return json.dumps({"error": f"Unknown tool: {tool_call.name}"})


def _execute_seed_sql(arguments: dict[str, Any], db_seed: Any) -> str:
    if not db_seed:
        return json.dumps({"error": "This eval fixture does not define db_seed."})
    query = str(arguments.get("query", "")).strip()
    if not query:
        return json.dumps({"error": "Empty query."})
    if query.lstrip("( \t\n").split()[0].upper() != "SELECT":
        return json.dumps({"error": "Only SELECT queries are allowed."})
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _load_seed_tables(conn, db_seed)
        rows = [dict(row) for row in conn.execute(query).fetchall()]
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    finally:
        conn.close()
    return json.dumps(rows, default=str)


def _load_seed_tables(conn: sqlite3.Connection, db_seed: dict[str, Any]) -> None:
    tables = db_seed.get("tables", {})
    if not isinstance(tables, dict):
        raise ValueError("db_seed.tables must be an object")
    for table, rows in tables.items():
        if not isinstance(rows, list) or not rows:
            continue
        columns = sorted({key for row in rows if isinstance(row, dict) for key in row})
        if not columns:
            continue
        col_defs = ", ".join(f"{column} TEXT" for column in columns)
        conn.execute(f"CREATE TABLE {table} ({col_defs})")
        placeholders = ", ".join(["?"] * len(columns))
        col_names = ", ".join(columns)
        for row in rows:
            conn.execute(
                f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})",
                [row.get(column) for column in columns],
            )
    conn.commit()


def _evaluate_assertion(
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    atype = assertion.get("type")
    name = str(assertion.get("name", atype))
    if atype == "tool_called":
        return _assert_tool_called(name, assertion, execution)
    if atype == "tool_not_called":
        return _assert_tool_not_called(name, assertion, execution)
    if atype == "tool_arg_matches":
        return _assert_tool_arg_matches(name, assertion, execution)
    if atype == "text_contains":
        return _assert_text_contains(name, assertion, execution)
    if atype == "text_absent":
        return _assert_text_absent(name, assertion, execution)
    if atype == "text_without_chart_absent":
        return _assert_text_without_chart_absent(name, assertion, execution)
    if atype == "word_count_max":
        return _assert_word_count_max(name, assertion, execution)
    if atype == "forbidden_opening":
        return _assert_forbidden_opening(name, assertion, execution)
    return AssertionResult(
        name=name, passed=False, detail=f"Unknown assertion type: {atype}"
    )


def _assert_tool_called(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    tool = str(assertion["tool"])
    calls = [call for call in execution.tool_calls if call.name == tool]
    expected = assertion.get("count")
    if expected is not None:
        passed = len(calls) == int(expected)
        detail = f"{tool}: {len(calls)} call(s), expected {expected}"
    else:
        min_count = int(assertion.get("min_count", 1))
        max_count = assertion.get("max_count")
        passed = len(calls) >= min_count and (
            max_count is None or len(calls) <= int(max_count)
        )
        detail = f"{tool}: {len(calls)} call(s)"
    return AssertionResult(name=name, passed=passed, detail="" if passed else detail)


def _assert_tool_not_called(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    tool = str(assertion["tool"])
    calls = [call for call in execution.tool_calls if call.name == tool]
    return AssertionResult(
        name=name,
        passed=not calls,
        detail="" if not calls else f"{tool}: {len(calls)} unexpected call(s)",
    )


def _assert_tool_arg_matches(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    tool = str(assertion["tool"])
    matches = assertion.get("matches", {})
    calls = [call for call in execution.tool_calls if call.name == tool]
    for call in calls:
        if all(
            _value_matches(call.arguments.get(key), expected)
            for key, expected in matches.items()
        ):
            return AssertionResult(name=name, passed=True)
    return AssertionResult(
        name=name,
        passed=False,
        detail=f"No {tool} call matched {matches}; got {[call.arguments for call in calls]}",
    )


def _assert_text_contains(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    missing = [
        pattern
        for pattern in assertion.get("patterns", [])
        if not _text_matches(execution.text, str(pattern))
    ]
    return AssertionResult(
        name=name,
        passed=not missing,
        detail="" if not missing else f"Missing: {missing}",
    )


def _assert_text_absent(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    present = [
        pattern
        for pattern in assertion.get("patterns", [])
        if _text_matches(execution.text, str(pattern))
    ]
    return AssertionResult(
        name=name,
        passed=not present,
        detail="" if not present else f"Present: {present}",
    )


def _assert_word_count_max(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    max_words = int(assertion["max_words"])
    count = len(re.findall(r"\S+", execution.text.strip()))
    return AssertionResult(
        name=name,
        passed=count <= max_words,
        detail=f"{count} words, max {max_words}",
    )


def _assert_text_without_chart_absent(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    visible_text = strip_charts(execution.text)
    present = [
        pattern
        for pattern in assertion.get("patterns", [])
        if _text_matches(visible_text, str(pattern))
    ]
    return AssertionResult(
        name=name,
        passed=not present,
        detail="" if not present else f"Present after chart stripping: {present}",
    )


def _assert_forbidden_opening(
    name: str,
    assertion: dict[str, Any],
    execution: EvalExecution,
) -> AssertionResult:
    text = execution.text.lstrip()
    for pattern in assertion.get("patterns", []):
        pattern_text = str(pattern)
        if text.lower().startswith(pattern_text.lower()):
            return AssertionResult(
                name=name,
                passed=False,
                detail=f"Started with forbidden opening: {pattern_text}",
            )
    return AssertionResult(name=name, passed=True)


def _value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        text = str(actual or "")
        if "equals" in expected and actual != expected["equals"]:
            return False
        contains = expected.get("contains", [])
        if contains and any(str(item).lower() not in text.lower() for item in contains):
            return False
        regex = expected.get("regex")
        if regex and re.search(str(regex), text, re.IGNORECASE) is None:
            return False
        return True
    if isinstance(expected, str) and expected.startswith("re:"):
        return re.search(expected[3:], str(actual or ""), re.IGNORECASE) is not None
    return actual == expected


def _text_matches(text: str, pattern: str) -> bool:
    if pattern.startswith("re:"):
        return re.search(pattern[3:], text, re.IGNORECASE) is not None
    return pattern.lower() in text.lower()
