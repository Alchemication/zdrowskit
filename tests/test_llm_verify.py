"""Tests for LLM verification helpers."""

from __future__ import annotations

import json
import sqlite3

from llm import LLMResult
from llm_verify import (
    VerificationIssue,
    deterministic_verification_issues,
    parse_verification_result,
    verify_and_rewrite,
)
from store import log_llm_call


class TestParseVerificationResult:
    def test_pass_payload(self) -> None:
        result = parse_verification_result(
            '{"verdict":"pass","issues":[],"confidence":"high"}'
        )

        assert result.verdict == "pass"
        assert result.issues == []
        assert result.confidence == "high"

    def test_pass_with_issues_becomes_revise(self) -> None:
        result = parse_verification_result(
            json.dumps(
                {
                    "verdict": "pass",
                    "confidence": "medium",
                    "issues": [
                        {
                            "severity": "minor",
                            "quote": "x",
                            "problem": "unsupported",
                            "correction": "remove it",
                            "evidence": "no matching workout",
                        }
                    ],
                }
            )
        )

        assert result.verdict == "revise"
        assert result.issues[0].evidence == "no matching workout"


class TestDeterministicVerificationIssues:
    def test_catches_markdown_table(self) -> None:
        issues = deterministic_verification_issues(
            "insights",
            "| Day | Run |\n| --- | --- |\n| Mon | 5 km |",
        )

        assert any(issue.severity == "major" for issue in issues)

    def test_empty_output_is_critical(self) -> None:
        issues = deterministic_verification_issues("nudge", "  ")

        assert issues == [
            VerificationIssue(
                severity="critical",
                quote="",
                problem="The draft is empty.",
                correction="Regenerate a non-empty final output or SKIP for nudge.",
            )
        ]


class TestVerifyAndRewrite:
    def test_source_call_metadata_gets_verification_summary(
        self,
        in_memory_db: sqlite3.Connection,
        monkeypatch,
    ) -> None:
        source_id = log_llm_call(
            in_memory_db,
            request_type="nudge",
            model="draft-model",
            messages=[{"role": "user", "content": "draft"}],
            response_text="Weak nudge.",
        )

        def fake_call_llm(messages, **kwargs):
            text = '{"verdict":"fail","issues":[],"confidence":"high"}'
            row_id = log_llm_call(
                kwargs["conn"],
                request_type=kwargs["request_type"],
                model=kwargs["model"],
                messages=messages,
                response_text=text,
                metadata=kwargs["metadata"],
            )
            return LLMResult(
                text=text,
                model=kwargs["model"],
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                latency_s=0.1,
                llm_call_id=row_id,
            )

        monkeypatch.setattr("llm_verify.call_llm", fake_call_llm)

        result = verify_and_rewrite(
            kind="nudge",
            draft="Weak nudge.",
            evidence={},
            source_messages=[],
            conn=in_memory_db,
            metadata={"source_llm_call_id": source_id},
            model="verify-model",
            rewrite_model="rewrite-model",
            max_revisions=1,
        )

        assert result.verdict == "fail"
        source = in_memory_db.execute(
            "SELECT metadata_json FROM llm_call WHERE id = ?",
            (source_id,),
        ).fetchone()
        metadata = json.loads(source["metadata_json"])
        assert metadata["nudge_verification"]["verdict"] == "fail"
        assert metadata["nudge_verification"]["verifier_call_id"] == source_id + 1
        assert metadata["nudge_verification"]["issue_count"] == 0

    def test_revise_calls_rewriter_and_updates_verify_metadata(
        self,
        in_memory_db: sqlite3.Connection,
        monkeypatch,
    ) -> None:
        outputs = [
            json.dumps(
                {
                    "verdict": "revise",
                    "confidence": "high",
                    "issues": [
                        {
                            "severity": "major",
                            "quote": "rest day",
                            "problem": "There was a workout.",
                            "correction": "Say the workout happened.",
                            "evidence": "Monday workout: run 5 km",
                        }
                    ],
                }
            ),
            "Monday had a 5 km run.",
        ]

        def fake_call_llm(messages, **kwargs):
            text = outputs.pop(0)
            row_id = log_llm_call(
                kwargs["conn"],
                request_type=kwargs["request_type"],
                model=kwargs["model"],
                messages=messages,
                response_text=text,
                metadata=kwargs["metadata"],
            )
            return LLMResult(
                text=text,
                model=kwargs["model"],
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                latency_s=0.1,
                llm_call_id=row_id,
            )

        monkeypatch.setattr("llm_verify.call_llm", fake_call_llm)

        result = verify_and_rewrite(
            kind="insights",
            draft="Monday was a rest day.",
            evidence={"workouts": ["Monday run 5 km"]},
            source_messages=[{"role": "user", "content": "draft"}],
            conn=in_memory_db,
            metadata={"source_llm_call_id": 123},
            model="verify-model",
            rewrite_model="rewrite-model",
            max_revisions=1,
        )

        assert result.verdict == "revise"
        assert result.revised_text == "Monday had a 5 km run."

        rows = in_memory_db.execute(
            "SELECT request_type, metadata_json FROM llm_call ORDER BY id"
        ).fetchall()
        assert [row["request_type"] for row in rows] == [
            "insights_verify",
            "insights_rewrite",
        ]
        verify_metadata = json.loads(rows[0]["metadata_json"])
        assert verify_metadata["source_llm_call_id"] == 123
        assert verify_metadata["verdict"] == "revise"
        assert verify_metadata["issue_count"] == 1
        assert verify_metadata["major_count"] == 1

    def test_malformed_verifier_json_fails_safely(
        self,
        in_memory_db: sqlite3.Connection,
        monkeypatch,
    ) -> None:
        def fake_call_llm(messages, **kwargs):
            return LLMResult(
                text="not json",
                model=kwargs["model"],
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                latency_s=0.1,
            )

        monkeypatch.setattr("llm_verify.call_llm", fake_call_llm)

        result = verify_and_rewrite(
            kind="nudge",
            draft="Nice work today.",
            evidence={},
            source_messages=[],
            conn=in_memory_db,
            metadata={},
            model="verify-model",
            rewrite_model="rewrite-model",
            max_revisions=1,
        )

        assert result.verdict == "fail"
        assert result.issues[0].severity == "critical"
