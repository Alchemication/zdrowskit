"""Runner for direct verifier-judgement eval cases.

Exercises the verifier prompt/schema only. It intentionally does not call the
full ``verify_and_rewrite`` pipeline, resolve ``model_prefs``, write DB rows, or
invoke the bounded rewriter. Model comparisons therefore behave like chat evals:
the requested eval model is the model under test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import MAX_TOKENS_VERIFICATION  # noqa: E402
from llm_verify import (  # noqa: E402
    VerifierPayload,
    messages_for_verifier,
    parse_verification_result,
)


def run_verification_judge_case(
    case: Any,
    *,
    model: str,
    reasoning_effort: str | None,
    temperature: float | None,
    cache: Any = None,
    refresh_cache: bool = False,
) -> tuple[Any, str, dict[str, Any]]:
    """Run one verifier judgement case and return execution, model, and route."""
    from evals.framework import (  # local import to avoid cycle
        EvalExecution,
        _call_llm_for_eval,
    )

    fixture = case.fixture
    kind = str(fixture.get("kind", "nudge"))
    draft = str(fixture["draft"])
    evidence = dict(fixture["evidence"])
    source_messages = list(fixture["source_messages"])
    metadata = dict(fixture.get("metadata", {}))
    metadata.setdefault("eval_case_id", case.id)
    metadata.setdefault("source_feedback_id", case.source_feedback_id)

    messages = messages_for_verifier(
        kind=kind,
        draft=draft,
        evidence=evidence,
        source_messages=source_messages,
        metadata=metadata,
    )
    llm_result, cache_hit = _call_llm_for_eval(
        messages=messages,
        model=model,
        max_tokens=MAX_TOKENS_VERIFICATION,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        tools=None,
        response_format=VerifierPayload,
        fallback_models=[],
        metadata={
            "eval_case_id": case.id,
            "source_feedback_id": case.source_feedback_id,
            "stage": "verification_judge",
            "kind": kind,
        },
        cache=cache,
        refresh_cache=refresh_cache,
    )
    parsed = parse_verification_result(llm_result.text)
    text = json.dumps(
        {
            "verdict": parsed.verdict,
            "confidence": parsed.confidence,
            "issues": [issue.model_dump() for issue in parsed.issues],
        },
        ensure_ascii=False,
        indent=2,
    )
    return (
        EvalExecution(
            text=text,
            tool_calls=[],
            messages=messages,
            input_tokens=int(getattr(llm_result, "input_tokens", 0) or 0),
            output_tokens=int(getattr(llm_result, "output_tokens", 0) or 0),
            total_tokens=int(getattr(llm_result, "total_tokens", 0) or 0),
            latency_s=float(getattr(llm_result, "latency_s", 0.0) or 0.0),
            cost=getattr(llm_result, "cost", None),
            cache_hits=1 if cache_hit else 0,
            cache_misses=0 if cache_hit else 1,
            effective_models=(
                [str(llm_result.model)] if getattr(llm_result, "model", None) else []
            ),
        ),
        model,
        {
            "feature": str(case.feature),
            "kind": kind,
            "primary": model,
            "fallback_models": [],
            "reasoning_effort": reasoning_effort,
            "temperature": temperature,
            "source": "eval_cli",
        },
    )
