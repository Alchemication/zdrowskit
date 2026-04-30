"""LLM verification and bounded rewrite helpers.

The verifier is deliberately audit-shaped: it checks a generated draft against
surface-specific criteria and a compact evidence packet, then returns strict
JSON. Rewrites are separate so the audit trail stays readable in ``llm-log``.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from config import (
    MAX_TOKENS_VERIFICATION,
    MAX_TOKENS_VERIFICATION_REWRITE,
    MAX_VERIFICATION_REVISIONS,
    VERIFICATION_EXTRA_BODY,
    VERIFICATION_RESPONSE_FORMAT,
    VERIFICATION_MODEL,
    VERIFICATION_REWRITE_MODEL,
)
from events import record_event
from llm import call_llm
from llm_context import load_prompt_text

logger = logging.getLogger(__name__)

VerificationKind = Literal["insights", "coach", "nudge"]
Verdict = Literal["pass", "revise", "fail"]

_EVENT_CATEGORY: dict[VerificationKind, str] = {
    "insights": "insights",
    "coach": "coach",
    "nudge": "nudge",
}

_PROMPT_BY_KIND: dict[VerificationKind, str] = {
    "insights": "verify_insights_prompt.md",
    "coach": "verify_coach_prompt.md",
    "nudge": "verify_nudge_prompt.md",
}


@dataclass
class VerificationIssue:
    """One concrete verifier finding."""

    severity: Literal["critical", "major", "minor"]
    quote: str
    problem: str
    correction: str
    evidence: str | None = None


@dataclass
class VerificationResult:
    """Parsed verifier result and optional bounded rewrite."""

    verdict: Verdict
    issues: list[VerificationIssue]
    confidence: str = "unknown"
    revised_text: str | None = None
    verifier_call_id: int | None = None
    rewrite_call_id: int | None = None


def extract_tool_evidence(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair tool calls with their results as a flat evidence list.

    Walks an assistant/tool conversation and pulls the data the writer
    actually saw — `run_sql` queries with their results, `update_context`
    proposals, etc. Used to seed the verifier's evidence packet without
    forcing it to parse the full chat transcript.

    Args:
        messages: Full conversation including ``assistant`` messages with
            ``tool_calls`` and ``tool`` messages with results.

    Returns:
        A list of {"tool", "arguments", "result"} dicts in call order.
    """
    pairs: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                tc_id = tc.get("id")
                if tc_id is None:
                    continue
                pending[tc_id] = {
                    "tool": fn.get("name"),
                    "arguments": fn.get("arguments"),
                }
        elif role == "tool":
            tc_id = msg.get("tool_call_id")
            call = pending.pop(tc_id, {"tool": "unknown", "arguments": ""})
            pairs.append({**call, "result": msg.get("content", "")})
    return pairs


def slim_source_messages(
    messages: list[dict[str, Any]],
    final_text: str,
) -> list[dict[str, str]]:
    """Return a focused source_messages payload: system + user + final draft.

    Tool turns are not included — they're surfaced separately via
    :func:`extract_tool_evidence` so the verifier sees data, not the
    assistant/tool back-and-forth that triples its prompt size.
    """
    slim: list[dict[str, str]] = []
    system = next((m for m in messages if m.get("role") == "system"), None)
    initial_user = next((m for m in messages if m.get("role") == "user"), None)
    if system is not None:
        slim.append({"role": "system", "content": str(system.get("content", ""))})
    if initial_user is not None:
        slim.append({"role": "user", "content": str(initial_user.get("content", ""))})
    slim.append({"role": "assistant", "content": final_text})
    return slim


def _loads_json_object(text: str) -> dict[str, Any]:
    """Parse a strict JSON object, tolerating only a single fenced block."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError("verifier returned JSON, but not an object")
    return data


def _coerce_issue(raw: object) -> VerificationIssue:
    """Validate and coerce one raw issue object."""
    if not isinstance(raw, dict):
        raise ValueError("verification issue must be an object")
    severity = raw.get("severity", "major")
    if severity not in {"critical", "major", "minor"}:
        raise ValueError(f"unsupported verification severity: {severity!r}")
    return VerificationIssue(
        severity=severity,
        quote=str(raw.get("quote", "")).strip(),
        problem=str(raw.get("problem", "")).strip(),
        correction=str(raw.get("correction", "")).strip(),
        evidence=(
            str(raw["evidence"]).strip()
            if raw.get("evidence") not in {None, ""}
            else None
        ),
    )


def parse_verification_result(text: str) -> VerificationResult:
    """Parse verifier JSON into a ``VerificationResult``.

    Args:
        text: Raw verifier model output.

    Returns:
        Parsed verification result.

    Raises:
        ValueError: If the payload is malformed or violates the contract.
    """
    data = _loads_json_object(text)
    verdict = data.get("verdict")
    if verdict not in {"pass", "revise", "fail"}:
        raise ValueError(f"unsupported verification verdict: {verdict!r}")
    raw_issues = data.get("issues", [])
    if not isinstance(raw_issues, list):
        raise ValueError("verification issues must be a list")
    issues = [_coerce_issue(raw) for raw in raw_issues]
    if verdict == "pass" and issues:
        verdict = "revise"
    return VerificationResult(
        verdict=verdict,
        issues=issues,
        confidence=str(data.get("confidence", "unknown")).strip() or "unknown",
    )


def _issue_counts(issues: list[VerificationIssue]) -> dict[str, int]:
    """Return severity counts for metadata logging."""
    return {
        "issue_count": len(issues),
        "critical_count": sum(1 for issue in issues if issue.severity == "critical"),
        "major_count": sum(1 for issue in issues if issue.severity == "major"),
        "minor_count": sum(1 for issue in issues if issue.severity == "minor"),
    }


def _issues_for_metadata(
    issues: list[VerificationIssue],
) -> list[dict[str, str | None]]:
    """Serialize verifier issues for explainability metadata."""
    return [
        {
            "severity": issue.severity,
            "quote": issue.quote,
            "problem": issue.problem,
            "correction": issue.correction,
            "evidence": issue.evidence,
        }
        for issue in issues
    ]


def _update_call_metadata(
    conn: sqlite3.Connection,
    call_id: int | None,
    metadata: dict[str, Any],
) -> None:
    """Merge metadata into an already-logged LLM call."""
    if call_id is None:
        return
    row = conn.execute(
        "SELECT metadata_json FROM llm_call WHERE id = ?",
        (call_id,),
    ).fetchone()
    existing: dict[str, Any] = {}
    if row and row["metadata_json"]:
        try:
            loaded = json.loads(row["metadata_json"])
            if isinstance(loaded, dict):
                existing = loaded
        except (TypeError, json.JSONDecodeError):
            existing = {}
    existing.update(metadata)
    conn.execute(
        "UPDATE llm_call SET metadata_json = ? WHERE id = ?",
        (json.dumps(existing), call_id),
    )
    conn.commit()


def _source_call_id(metadata: dict[str, Any]) -> int | None:
    """Return the source LLM call id from metadata when available."""
    raw_id = metadata.get("source_llm_call_id")
    if isinstance(raw_id, bool) or raw_id is None:
        return None
    try:
        return int(raw_id)
    except (TypeError, ValueError):
        return None


def _verification_summary_metadata(
    result: VerificationResult,
) -> dict[str, Any]:
    """Return compact verification summary metadata for logs."""
    return {
        "verdict": result.verdict,
        "confidence": result.confidence,
        "verifier_call_id": result.verifier_call_id,
        "rewrite_call_id": result.rewrite_call_id,
        **_issue_counts(result.issues),
        "issues": _issues_for_metadata(result.issues),
    }


def _record_source_verification(
    conn: sqlite3.Connection,
    *,
    kind: VerificationKind,
    source_llm_call_id: int | None,
    result: VerificationResult,
) -> None:
    """Attach the verification summary to the original source LLM call."""
    if source_llm_call_id is None:
        return
    _update_call_metadata(
        conn,
        source_llm_call_id,
        {f"{kind}_verification": _verification_summary_metadata(result)},
    )


def _finalize_failed_verification(
    conn: sqlite3.Connection,
    *,
    kind: VerificationKind,
    source_llm_call_id: int | None,
    result: VerificationResult,
    strict: bool,
) -> VerificationResult:
    """Persist a verifier failure consistently and return it."""
    _update_call_metadata(
        conn,
        result.verifier_call_id,
        _verification_summary_metadata(result),
    )
    _record_source_verification(
        conn,
        kind=kind,
        source_llm_call_id=source_llm_call_id,
        result=result,
    )
    _emit_verification_event(
        conn,
        kind=kind,
        result=result,
        source_llm_call_id=source_llm_call_id,
        strict=strict,
    )
    return result


def _empty_verifier_result(
    *,
    verifier_result: Any,
    max_tokens: int,
) -> VerificationResult:
    """Build a precise failure result for empty verifier responses."""
    output_tokens = getattr(verifier_result, "output_tokens", 0)
    result_max_tokens = getattr(verifier_result, "max_tokens", None) or max_tokens
    hit_limit = output_tokens >= result_max_tokens
    if hit_limit:
        problem = (
            "Verifier returned an empty response after hitting "
            f"max_tokens={result_max_tokens}; it likely exhausted its output "
            "budget before emitting strict JSON."
        )
    else:
        problem = "Verifier returned an empty response instead of strict JSON."
    return VerificationResult(
        verdict="fail",
        verifier_call_id=getattr(verifier_result, "llm_call_id", None),
        issues=[
            VerificationIssue(
                severity="critical",
                quote="",
                problem=problem,
                correction=(
                    "Increase ZDROWSKIT_MAX_TOKENS_VERIFICATION or route "
                    "verification to a model that reliably emits strict JSON."
                ),
                evidence=(
                    f"output_tokens={output_tokens}, max_tokens={result_max_tokens}"
                ),
            )
        ],
    )


def _has_markdown_table(text: str) -> bool:
    """Return True when text contains a markdown table separator row."""
    return bool(
        re.search(r"(?m)^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", text)
    )


def deterministic_verification_issues(
    kind: VerificationKind,
    draft: str,
) -> list[VerificationIssue]:
    """Run cheap contract checks before any LLM verifier call."""
    issues: list[VerificationIssue] = []
    if not draft.strip():
        issues.append(
            VerificationIssue(
                severity="critical",
                quote="",
                problem="The draft is empty.",
                correction="Regenerate a non-empty final output or SKIP for nudge.",
            )
        )
    if _has_markdown_table(draft):
        issues.append(
            VerificationIssue(
                severity="major",
                quote="markdown table",
                problem="The draft includes a markdown table, which does not render reliably.",
                correction="Rewrite the same content as bullets or short paragraphs.",
            )
        )
    if kind == "nudge" and len(draft.split()) > 95 and draft.strip().upper() != "SKIP":
        issues.append(
            VerificationIssue(
                severity="major",
                quote=draft[:120],
                problem="The nudge is too long for a short notification.",
                correction="Cut to one compact observation or action under 80 words.",
            )
        )
    return issues


def _messages_for_verifier(
    *,
    kind: VerificationKind,
    draft: str,
    evidence: dict[str, Any],
    source_messages: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> list[dict[str, str]]:
    """Build verifier messages from the surface prompt and evidence packet."""
    system = load_prompt_text(_PROMPT_BY_KIND[kind])
    user_payload = {
        "draft": draft,
        "evidence": evidence,
        "metadata": metadata,
        "source_messages": source_messages,
    }
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, default=str),
        },
    ]


def _messages_for_rewriter(
    *,
    kind: VerificationKind,
    draft: str,
    issues: list[VerificationIssue],
    evidence: dict[str, Any],
    metadata: dict[str, Any],
) -> list[dict[str, str]]:
    """Build bounded rewrite messages."""
    payload = {
        "kind": kind,
        "original_draft": draft,
        "issues": [issue.__dict__ for issue in issues],
        "evidence": evidence,
        "metadata": metadata,
    }
    return [
        {
            "role": "system",
            "content": load_prompt_text("verify_rewrite_prompt"),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, default=str),
        },
    ]


def _emit_verification_event(
    conn: sqlite3.Connection,
    *,
    kind: VerificationKind,
    result: VerificationResult,
    source_llm_call_id: int | None,
    strict: bool,
) -> None:
    """Record a verifier outcome to the events log.

    Surfaces verifier suppressions in `events`, alongside other daemon
    decisions, so the user can see how often verification killed or
    rewrote an output without grepping `llm-log`.
    """
    category = _EVENT_CATEGORY[kind]
    counts = _issue_counts(result.issues)
    if result.verdict == "pass":
        return
    if result.verdict == "fail":
        event_kind = "verifier_suppressed"
        summary = f"{kind} suppressed by verifier ({counts['issue_count']} issue(s))"
    elif strict:
        event_kind = "verifier_suppressed"
        summary = (
            f"{kind} suppressed (strict): verifier asked for revisions "
            f"({counts['issue_count']} issue(s))"
        )
    elif result.revised_text is not None:
        event_kind = "verifier_revised"
        summary = f"{kind} rewritten by verifier ({counts['issue_count']} issue(s))"
    else:
        event_kind = "verifier_revise_skipped"
        summary = (
            f"{kind} kept original; verifier asked for revisions but "
            f"rewriter was disabled ({counts['issue_count']} issue(s))"
        )
    record_event(
        conn,
        category,
        event_kind,
        summary,
        details={
            "verdict": result.verdict,
            "confidence": result.confidence,
            "strict": strict,
            **counts,
            "verifier_call_id": result.verifier_call_id,
            "rewrite_call_id": result.rewrite_call_id,
        },
        llm_call_id=source_llm_call_id,
    )


def verify_and_rewrite(
    *,
    kind: VerificationKind,
    draft: str,
    evidence: dict[str, Any],
    source_messages: list[dict[str, Any]],
    conn: sqlite3.Connection,
    metadata: dict[str, Any],
    model: str = VERIFICATION_MODEL,
    rewrite_model: str = VERIFICATION_REWRITE_MODEL,
    max_revisions: int = MAX_VERIFICATION_REVISIONS,
    strict: bool = False,
) -> VerificationResult:
    """Verify a draft and optionally perform one bounded rewrite.

    Args:
        kind: Output surface being verified.
        draft: Generated text to audit.
        evidence: Compact facts the verifier may use.
        source_messages: Slim source payload (system + user + final draft).
        conn: Open DB connection for LLM logging.
        metadata: Product metadata to store with verifier/rewrite calls.
        model: Verifier model.
        rewrite_model: Bounded rewriter model.
        max_revisions: Maximum rewrite attempts; normally 0 or 1. The rewriter
            is also bypassed entirely when *strict* is True.
        strict: When True, treat any non-pass verdict as fail and skip the
            rewriter. Used by surfaces where a partial rewrite could ship
            content (e.g. coach proposal diffs) the verifier never approved.

    Returns:
        VerificationResult. Malformed verifier JSON fails closed.
    """
    source_llm_call_id = _source_call_id(metadata)
    guard_issues = deterministic_verification_issues(kind, draft)
    if any(issue.severity == "critical" for issue in guard_issues):
        result = VerificationResult(verdict="fail", issues=guard_issues)
        _record_source_verification(
            conn,
            kind=kind,
            source_llm_call_id=source_llm_call_id,
            result=result,
        )
        _emit_verification_event(
            conn,
            kind=kind,
            result=result,
            source_llm_call_id=source_llm_call_id,
            strict=strict,
        )
        return result

    verifier_messages = _messages_for_verifier(
        kind=kind,
        draft=draft,
        evidence=evidence,
        source_messages=source_messages,
        metadata=metadata,
    )
    verifier_call_id: int | None = None
    try:
        verifier_result = call_llm(
            verifier_messages,
            model=model,
            max_tokens=MAX_TOKENS_VERIFICATION,
            temperature=0,
            response_format=VERIFICATION_RESPONSE_FORMAT,
            extra_body=VERIFICATION_EXTRA_BODY,
            conn=conn,
            request_type=f"{kind}_verify",
            metadata={**metadata, "stage": "verify"},
        )
        verifier_call_id = verifier_result.llm_call_id
        if not verifier_result.text.strip():
            logger.warning(
                "%s verifier returned empty response (output_tokens=%d, max_tokens=%d)",
                kind,
                verifier_result.output_tokens,
                verifier_result.max_tokens or MAX_TOKENS_VERIFICATION,
            )
            result = _empty_verifier_result(
                verifier_result=verifier_result,
                max_tokens=MAX_TOKENS_VERIFICATION,
            )
            return _finalize_failed_verification(
                conn,
                kind=kind,
                source_llm_call_id=source_llm_call_id,
                result=result,
                strict=strict,
            )
        parsed = parse_verification_result(verifier_result.text)
        parsed.verifier_call_id = verifier_call_id
    except Exception as exc:
        logger.warning("%s verifier failed closed: %s", kind, exc)
        result = VerificationResult(
            verdict="fail",
            verifier_call_id=verifier_call_id,
            issues=[
                VerificationIssue(
                    severity="critical",
                    quote="",
                    problem=f"Verifier failed or returned malformed JSON: {exc}",
                    correction="Inspect the trace and regenerate before sending.",
                )
            ],
        )
        return _finalize_failed_verification(
            conn,
            kind=kind,
            source_llm_call_id=source_llm_call_id,
            result=result,
            strict=strict,
        )

    if guard_issues:
        parsed.issues = [*guard_issues, *parsed.issues]
        if parsed.verdict == "pass":
            parsed.verdict = "revise"

    if parsed.verdict == "pass" and parsed.confidence.lower() == "low":
        logger.warning(
            "%s verifier returned pass with low confidence; "
            "treating as soft signal, output will still ship",
            kind,
        )

    _update_call_metadata(
        conn,
        parsed.verifier_call_id,
        {
            "verdict": parsed.verdict,
            "confidence": parsed.confidence,
            **_issue_counts(parsed.issues),
            "issues": _issues_for_metadata(parsed.issues),
        },
    )

    if strict and parsed.verdict != "pass":
        parsed.verdict = "fail"
        _record_source_verification(
            conn,
            kind=kind,
            source_llm_call_id=source_llm_call_id,
            result=parsed,
        )
        _emit_verification_event(
            conn,
            kind=kind,
            result=parsed,
            source_llm_call_id=source_llm_call_id,
            strict=strict,
        )
        return parsed

    if parsed.verdict != "revise" or max_revisions <= 0:
        _record_source_verification(
            conn,
            kind=kind,
            source_llm_call_id=source_llm_call_id,
            result=parsed,
        )
        _emit_verification_event(
            conn,
            kind=kind,
            result=parsed,
            source_llm_call_id=source_llm_call_id,
            strict=strict,
        )
        return parsed

    rewrite_messages = _messages_for_rewriter(
        kind=kind,
        draft=draft,
        issues=parsed.issues,
        evidence=evidence,
        metadata=metadata,
    )
    try:
        rewrite_result = call_llm(
            rewrite_messages,
            model=rewrite_model,
            max_tokens=MAX_TOKENS_VERIFICATION_REWRITE,
            temperature=0,
            conn=conn,
            request_type=f"{kind}_rewrite",
            metadata={
                **metadata,
                "stage": "rewrite",
                "verdict": parsed.verdict,
                **_issue_counts(parsed.issues),
                "issues": _issues_for_metadata(parsed.issues),
            },
        )
    except Exception as exc:
        logger.warning("%s rewrite failed closed: %s", kind, exc)
        parsed.verdict = "fail"
        parsed.issues.append(
            VerificationIssue(
                severity="critical",
                quote="",
                problem=f"Rewrite failed: {exc}",
                correction="Inspect the trace and regenerate before sending.",
            )
        )
        _record_source_verification(
            conn,
            kind=kind,
            source_llm_call_id=source_llm_call_id,
            result=parsed,
        )
        _emit_verification_event(
            conn,
            kind=kind,
            result=parsed,
            source_llm_call_id=source_llm_call_id,
            strict=strict,
        )
        return parsed

    parsed.revised_text = rewrite_result.text.strip()
    parsed.rewrite_call_id = rewrite_result.llm_call_id
    rewrite_guard_issues = deterministic_verification_issues(kind, parsed.revised_text)
    if any(issue.severity == "critical" for issue in rewrite_guard_issues):
        parsed.verdict = "fail"
        parsed.issues.extend(rewrite_guard_issues)
    _record_source_verification(
        conn,
        kind=kind,
        source_llm_call_id=source_llm_call_id,
        result=parsed,
    )
    _emit_verification_event(
        conn,
        kind=kind,
        result=parsed,
        source_llm_call_id=source_llm_call_id,
        strict=strict,
    )
    return parsed
