"""Runner for ``nudge_verify`` eval cases.

Exercises the production verifier path (``verify_and_rewrite`` with the
rewriter disabled) against a fixture captured from a real verifier call.
Models, fallback, reasoning, temperature, and the structured response schema
are resolved through the same model route used by production. Change the
verifier route's ``reasoning_effort`` via ``main.py models`` to A/B DeepSeek
thinking behavior.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from llm import LLMResult, call_llm  # noqa: E402
from llm_verify import VerificationResult, verify_and_rewrite  # noqa: E402
from model_prefs import resolve_model_route  # noqa: E402
from store import connect_db  # noqa: E402


def _resolved_model_label() -> str:
    """Render the verifier model for result reporting."""
    return resolve_model_route("verification").primary


def run_nudge_verify_case(
    case: Any,
    *,
    cache: Any = None,
    refresh_cache: bool = False,
) -> tuple[Any, str]:
    """Run one ``nudge_verify`` case and return its execution + model label."""
    from evals.framework import EvalExecution  # local import to avoid cycle

    fixture = case.fixture
    kind = str(fixture.get("kind", "nudge"))
    draft = str(fixture["draft"])
    evidence = dict(fixture["evidence"])
    source_messages = list(fixture["source_messages"])
    metadata = dict(fixture.get("metadata", {}))
    # Drop source_llm_call_id: the temp DB has no source row, so any FK-bound
    # event/metadata write that references it will fail. The verifier itself
    # does not need it to produce a verdict.
    metadata.pop("source_llm_call_id", None)
    metadata.setdefault("eval_case_id", case.id)
    metadata.setdefault("source_feedback_id", case.source_feedback_id)

    started = time.perf_counter()
    wrapper = _CachingCallLLM(cache=cache, refresh_cache=refresh_cache)
    verifier_route = resolve_model_route("verification")
    rewrite_route = resolve_model_route("verification_rewrite")
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect_db(Path(tmp) / "eval.db", migrate=True)
        try:
            result: VerificationResult = verify_and_rewrite(
                kind=kind,
                draft=draft,
                evidence=evidence,
                source_messages=source_messages,
                conn=conn,
                metadata=metadata,
                model=verifier_route.primary,
                rewrite_model=rewrite_route.primary,
                fallback_models=(
                    [verifier_route.fallback] if verifier_route.fallback else None
                ),
                temperature=verifier_route.temperature,
                reasoning_effort=verifier_route.reasoning_effort,
                rewrite_temperature=rewrite_route.temperature,
                rewrite_reasoning_effort=rewrite_route.reasoning_effort,
                max_revisions=0,
                strict=False,
                _call_llm=wrapper,
            )
            payload = {
                "verdict": result.verdict,
                "confidence": result.confidence,
                "issues": [issue.model_dump() for issue in result.issues],
                "verifier_call_id": result.verifier_call_id,
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        finally:
            conn.close()

    return (
        EvalExecution(
            text=text,
            tool_calls=[],
            messages=[],
            input_tokens=wrapper.input_tokens,
            output_tokens=wrapper.output_tokens,
            total_tokens=wrapper.total_tokens,
            latency_s=time.perf_counter() - started,
            cost=wrapper.cost,
            cache_hits=wrapper.hits,
            cache_misses=wrapper.misses,
        ),
        _resolved_model_label(),
    )


class _CachingCallLLM:
    """``call_llm`` wrapper that consults an eval cache and accumulates stats.

    Used as a dependency-injected seam (``verify_and_rewrite(_call_llm=...)``)
    so eval runs can reuse verifier responses across runs without monkey-
    patching the production ``call_llm`` import.
    """

    def __init__(self, *, cache: Any, refresh_cache: bool) -> None:
        self._cache = cache
        self._refresh_cache = refresh_cache
        self.hits = 0
        self.misses = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.cost: float | None = None

    def __call__(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResult:
        if self._cache is None:
            result = call_llm(messages, **kwargs)
            self._record(result)
            return result

        request = self._cache_request(messages, kwargs)
        if not self._refresh_cache:
            cached = self._cache.get(request)
            if cached is not None:
                self.hits += 1
                self._record(cached)
                return cached

        self.misses += 1
        result = call_llm(messages, **kwargs)
        self._cache.put(request, result)
        self._record(result)
        return result

    def _record(self, result: LLMResult) -> None:
        self.input_tokens += int(getattr(result, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(result, "output_tokens", 0) or 0)
        self.total_tokens += int(getattr(result, "total_tokens", 0) or 0)
        result_cost = getattr(result, "cost", None)
        if result_cost is not None:
            self.cost = (self.cost or 0.0) + float(result_cost)

    @staticmethod
    def _cache_request(
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        from evals.framework import (
            EVAL_CACHE_SCHEMA_VERSION,
            _response_format_cache_key,
        )

        return {
            "cache_schema_version": EVAL_CACHE_SCHEMA_VERSION,
            "runner": "nudge_verify",
            "model": kwargs.get("model"),
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens"),
            "temperature": kwargs.get("temperature"),
            "response_format": _response_format_cache_key(
                kwargs.get("response_format")
            ),
            "reasoning_effort": kwargs.get("reasoning_effort"),
            "fallback_models": kwargs.get("fallback_models"),
        }
