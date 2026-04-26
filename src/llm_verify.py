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
    MAX_VERIFICATION_REVISIONS,
    PROMPTS_DIR,
    VERIFICATION_MODEL,
    VERIFICATION_REWRITE_MODEL,
)
from llm import call_llm

logger = logging.getLogger(__name__)

VerificationKind = Literal["insights", "coach", "nudge"]
Verdict = Literal["pass", "revise", "fail"]

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
    system = (PROMPTS_DIR / _PROMPT_BY_KIND[kind]).read_text(encoding="utf-8")
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
            "content": (
                "You rewrite generated health-coaching text after an audit. "
                "Preserve the original structure, tone, and length. Apply only "
                "the listed corrections. Do not add new claims. Do not include "
                "explanations, audit notes, or JSON. For nudges, output either "
                "the final nudge text or exactly SKIP."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, default=str),
        },
    ]


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
) -> VerificationResult:
    """Verify a draft and optionally perform one bounded rewrite.

    Args:
        kind: Output surface being verified.
        draft: Generated text to audit.
        evidence: Compact facts the verifier may use.
        source_messages: Source conversation that created the draft.
        conn: Open DB connection for LLM logging.
        metadata: Product metadata to store with verifier/rewrite calls.
        model: Verifier model.
        rewrite_model: Bounded rewriter model.
        max_revisions: Maximum rewrite attempts; normally 0 or 1.

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
            max_tokens=2048,
            temperature=0,
            conn=conn,
            request_type=f"{kind}_verify",
            metadata={**metadata, "stage": "verify"},
        )
        verifier_call_id = verifier_result.llm_call_id
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
        _record_source_verification(
            conn,
            kind=kind,
            source_llm_call_id=source_llm_call_id,
            result=result,
        )
        return result

    if guard_issues:
        parsed.issues = [*guard_issues, *parsed.issues]
        if parsed.verdict == "pass":
            parsed.verdict = "revise"

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

    if parsed.verdict != "revise" or max_revisions <= 0:
        _record_source_verification(
            conn,
            kind=kind,
            source_llm_call_id=source_llm_call_id,
            result=parsed,
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
            max_tokens=max(1024, min(4096, len(draft) // 2 + 512)),
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
    return parsed
