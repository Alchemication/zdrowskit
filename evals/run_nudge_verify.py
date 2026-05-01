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
from contextlib import contextmanager
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
import llm_verify  # noqa: E402
from store import connect_db, log_llm_call  # noqa: E402


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
    cache_hits = 0
    cache_misses = 0
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect_db(Path(tmp) / "eval.db", migrate=True)
        try:
            with _cached_verifier_calls(
                cache=cache,
                refresh_cache=refresh_cache,
                counters={"hits": 0, "misses": 0},
            ) as counters:
                result: llm_verify.VerificationResult = llm_verify.verify_and_rewrite(
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
                )
                cache_hits = counters["hits"]
                cache_misses = counters["misses"]
            payload = {
                "verdict": result.verdict,
                "confidence": result.confidence,
                "issues": [asdict(issue) for issue in result.issues],
                "verifier_call_id": result.verifier_call_id,
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            input_tokens, output_tokens, total_tokens, cost = _call_usage(
                conn, result.verifier_call_id
            )
        finally:
            conn.close()

    return (
        EvalExecution(
            text=text,
            tool_calls=[],
            messages=[],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            latency_s=time.perf_counter() - started,
            cost=cost,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
        ),
        _resolved_model_label(),
    )


@contextmanager
def _cached_verifier_calls(
    *,
    cache: Any,
    refresh_cache: bool,
    counters: dict[str, int],
):
    """Temporarily route verifier LLM calls through the eval cache."""
    if cache is None:
        yield counters
        return

    original_call_llm = llm_verify.call_llm

    def cached_call_llm(messages: list[dict[str, Any]], **kwargs: Any):
        request = _cache_request(messages, kwargs)
        if not refresh_cache:
            cached = cache.get(request)
            if cached is not None:
                counters["hits"] += 1
                _log_cached_result(cached, messages, kwargs)
                return cached

        counters["misses"] += 1
        result = original_call_llm(messages, **kwargs)
        cache.put(request, result)
        return result

    llm_verify.call_llm = cached_call_llm
    try:
        yield counters
    finally:
        llm_verify.call_llm = original_call_llm


def _cache_request(
    messages: list[dict[str, Any]],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Build the eval-cache request key for a verifier LLM call."""
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


def _log_cached_result(
    cached: Any,
    messages: list[dict[str, Any]],
    kwargs: dict[str, Any],
) -> None:
    """Mirror call_llm logging for cached verifier results in the temp DB."""
    conn = kwargs.get("conn")
    request_type = kwargs.get("request_type")
    if conn is None or not request_type:
        return
    params = {
        "max_tokens": kwargs.get("max_tokens"),
        "temperature": kwargs.get("temperature"),
    }
    if kwargs.get("response_format") is not None:
        params["response_format"] = kwargs["response_format"]
    if kwargs.get("extra_body") is not None:
        params["extra_body"] = kwargs["extra_body"]
    cached.llm_call_id = log_llm_call(
        conn,
        request_type=str(request_type),
        model=str(getattr(cached, "model", kwargs.get("model", ""))),
        messages=messages,
        response_text=str(getattr(cached, "text", "")),
        params=params,
        input_tokens=int(getattr(cached, "input_tokens", 0) or 0),
        output_tokens=int(getattr(cached, "output_tokens", 0) or 0),
        total_tokens=int(getattr(cached, "total_tokens", 0) or 0),
        latency_s=float(getattr(cached, "latency_s", 0.0) or 0.0),
        cost=getattr(cached, "cost", None),
        metadata=kwargs.get("metadata"),
    )


def _call_usage(conn: Any, call_id: int | None) -> tuple[int, int, int, float | None]:
    """Pull token + cost stats for the verifier call from the temp DB."""
    if call_id is None:
        return 0, 0, 0, None
    row = conn.execute(
        "SELECT input_tokens, output_tokens, total_tokens, cost "
        "FROM llm_call WHERE id = ?",
        (call_id,),
    ).fetchone()
    if row is None:
        return 0, 0, 0, None
    return (
        int(row["input_tokens"] or 0),
        int(row["output_tokens"] or 0),
        int(row["total_tokens"] or 0),
        float(row["cost"]) if row["cost"] is not None else None,
    )
