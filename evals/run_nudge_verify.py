"""Runner for ``nudge_verify`` eval cases.

Exercises the production verifier path (``verify_and_rewrite`` with the
rewriter disabled) against a fixture captured from a real verifier call.
Models and provider extras (deepseek thinking flag, JSON mode, etc.)
are resolved by ``src/config.py`` at runtime, so swapping them via env
vars is the supported way to A/B verifier behaviour.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import (  # noqa: E402
    VERIFICATION_EXTRA_BODY,
    VERIFICATION_MODEL,
    VERIFICATION_REWRITE_MODEL,
)
from llm import LLMResult, call_llm  # noqa: E402
from llm_verify import VerificationResult, verify_and_rewrite  # noqa: E402
from store import connect_db  # noqa: E402


def _resolved_model_label() -> str:
    """Render the verifier model + thinking flag for result reporting."""
    thinking = "inherit"
    if isinstance(VERIFICATION_EXTRA_BODY, dict):
        thinking_block = VERIFICATION_EXTRA_BODY.get("thinking")
        if isinstance(thinking_block, dict):
            thinking = str(thinking_block.get("type", thinking))
    return f"{VERIFICATION_MODEL} (thinking={thinking})"


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
                model=VERIFICATION_MODEL,
                rewrite_model=VERIFICATION_REWRITE_MODEL,
                max_revisions=0,
                strict=False,
                _call_llm=wrapper,
            )
            payload = {
                "verdict": result.verdict,
                "confidence": result.confidence,
                "issues": [asdict(issue) for issue in result.issues],
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
        from evals.framework import EVAL_CACHE_SCHEMA_VERSION

        return {
            "cache_schema_version": EVAL_CACHE_SCHEMA_VERSION,
            "runner": "nudge_verify",
            "model": kwargs.get("model"),
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens"),
            "temperature": kwargs.get("temperature"),
            "response_format": kwargs.get("response_format"),
            "extra_body": kwargs.get("extra_body"),
            "fallback_models": kwargs.get("fallback_models"),
        }
