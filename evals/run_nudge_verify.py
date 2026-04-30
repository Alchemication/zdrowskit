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


def run_nudge_verify_case(case: Any) -> tuple[Any, str]:
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
            )
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
        ),
        _resolved_model_label(),
    )


def _call_usage(
    conn: Any, call_id: int | None
) -> tuple[int, int, int, float | None]:
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
