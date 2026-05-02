"""Tests for LLM verification helpers."""

from __future__ import annotations

import json
import sqlite3

from config import (
    MAX_TOKENS_VERIFICATION,
    MAX_TOKENS_VERIFICATION_REWRITE,
    VERIFICATION_EXTRA_BODY,
)
from events import query_events
from llm import LLMResult
from llm_verify import (
    VerificationIssue,
    _VerifierPayload,
    deterministic_verification_issues,
    extract_tool_evidence,
    parse_verification_result,
    slim_source_messages,
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
        seen_max_tokens = []
        seen_response_formats = []
        seen_extra_bodies = []
        seen_temperatures = []
        seen_reasoning = []

        def fake_call_llm(messages, **kwargs):
            seen_max_tokens.append(kwargs["max_tokens"])
            seen_response_formats.append(kwargs.get("response_format"))
            seen_extra_bodies.append(kwargs.get("extra_body"))
            seen_temperatures.append(kwargs.get("temperature"))
            seen_reasoning.append(kwargs.get("reasoning_effort"))
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
            reasoning_effort="high",
            temperature=None,
            rewrite_reasoning_effort="high",
            rewrite_temperature=None,
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
        assert seen_max_tokens == [
            MAX_TOKENS_VERIFICATION,
            MAX_TOKENS_VERIFICATION_REWRITE,
        ]
        assert seen_response_formats == [_VerifierPayload, None]
        assert seen_extra_bodies == [VERIFICATION_EXTRA_BODY, None]
        assert seen_temperatures == [None, None]
        assert seen_reasoning == ["high", "high"]
        verify_metadata = json.loads(rows[0]["metadata_json"])
        assert verify_metadata["source_llm_call_id"] == 123
        assert verify_metadata["verdict"] == "revise"
        assert verify_metadata["issue_count"] == 1
        assert verify_metadata["major_count"] == 1

    def test_verifier_reasoning_effort_is_passed_to_llm(
        self,
        in_memory_db: sqlite3.Connection,
        monkeypatch,
    ) -> None:
        seen_reasoning: list[str | None] = []
        seen_temperature: list[float | None] = []
        seen_fallback_models: list[list[str] | None] = []

        def fake_call_llm(_messages, **kwargs):
            seen_reasoning.append(kwargs.get("reasoning_effort"))
            seen_temperature.append(kwargs.get("temperature"))
            seen_fallback_models.append(kwargs.get("fallback_models"))
            return LLMResult(
                text='{"verdict":"pass","issues":[],"confidence":"high"}',
                model=kwargs["model"],
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                latency_s=0.1,
            )

        monkeypatch.setattr("llm_verify.call_llm", fake_call_llm)

        result = verify_and_rewrite(
            kind="nudge",
            draft="Short nudge.",
            evidence={},
            source_messages=[],
            conn=in_memory_db,
            metadata={},
            model="verify-model",
            rewrite_model="rewrite-model",
            fallback_models=["fallback-model"],
            temperature=None,
            reasoning_effort="high",
            max_revisions=1,
        )

        assert result.verdict == "pass"
        assert seen_reasoning == ["high"]
        assert seen_temperature == [None]
        assert seen_fallback_models == [["fallback-model"]]

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

    def test_empty_verifier_response_reports_token_budget(
        self,
        in_memory_db: sqlite3.Connection,
        monkeypatch,
    ) -> None:
        source_id = log_llm_call(
            in_memory_db,
            request_type="nudge",
            model="source-model",
            messages=[],
            response_text="Draft nudge.",
            metadata={},
        )

        def fake_call_llm(messages, **kwargs):
            row_id = log_llm_call(
                kwargs["conn"],
                request_type=kwargs["request_type"],
                model=kwargs["model"],
                messages=messages,
                response_text="",
                metadata=kwargs["metadata"],
            )
            return LLMResult(
                text="",
                model=kwargs["model"],
                input_tokens=10,
                output_tokens=kwargs["max_tokens"],
                total_tokens=10 + kwargs["max_tokens"],
                latency_s=0.1,
                max_tokens=kwargs["max_tokens"],
                llm_call_id=row_id,
            )

        monkeypatch.setattr("llm_verify.call_llm", fake_call_llm)

        result = verify_and_rewrite(
            kind="nudge",
            draft="Draft nudge.",
            evidence={},
            source_messages=[],
            conn=in_memory_db,
            metadata={"source_llm_call_id": source_id},
            model="verify-model",
            rewrite_model="rewrite-model",
            max_revisions=1,
        )

        assert result.verdict == "fail"
        assert result.issues[0].severity == "critical"
        assert "empty response" in result.issues[0].problem
        assert f"max_tokens={MAX_TOKENS_VERIFICATION}" in result.issues[0].problem
        assert "ZDROWSKIT_MAX_TOKENS_VERIFICATION" in result.issues[0].correction

        source = in_memory_db.execute(
            "SELECT metadata_json FROM llm_call WHERE id = ?",
            (source_id,),
        ).fetchone()
        source_metadata = json.loads(source["metadata_json"])
        issue = source_metadata["nudge_verification"]["issues"][0]
        assert "empty response" in issue["problem"]
        assert issue["evidence"] == (
            f"output_tokens={MAX_TOKENS_VERIFICATION}, "
            f"max_tokens={MAX_TOKENS_VERIFICATION}"
        )

        verifier = in_memory_db.execute(
            "SELECT metadata_json FROM llm_call WHERE id = ?",
            (result.verifier_call_id,),
        ).fetchone()
        verifier_metadata = json.loads(verifier["metadata_json"])
        assert verifier_metadata["verdict"] == "fail"
        assert verifier_metadata["critical_count"] == 1


class TestExtractToolEvidence:
    def test_pairs_assistant_tool_calls_with_results(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "ask"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "run_sql",
                            "arguments": '{"query": "SELECT 1"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "[[1]]"},
            {"role": "assistant", "content": "final"},
        ]

        pairs = extract_tool_evidence(messages)

        assert pairs == [
            {
                "tool": "run_sql",
                "arguments": '{"query": "SELECT 1"}',
                "result": "[[1]]",
            }
        ]

    def test_handles_orphan_tool_message(self) -> None:
        # A tool result with no matching call (defensive — shouldn't happen
        # in practice, but the helper must not crash).
        messages = [{"role": "tool", "tool_call_id": "ghost", "content": "x"}]

        pairs = extract_tool_evidence(messages)

        assert pairs == [{"tool": "unknown", "arguments": "", "result": "x"}]

    def test_skips_assistant_with_no_tool_calls(self) -> None:
        messages = [
            {"role": "assistant", "content": "thinking"},
            {"role": "assistant", "content": "answer"},
        ]

        assert extract_tool_evidence(messages) == []


class TestSlimSourceMessages:
    def test_strips_tool_turns(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "ask"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
            {"role": "tool", "tool_call_id": "x", "content": "result"},
            {"role": "assistant", "content": "intermediate"},
        ]

        slim = slim_source_messages(messages, "final draft")

        assert slim == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "ask"},
            {"role": "assistant", "content": "final draft"},
        ]

    def test_handles_missing_system(self) -> None:
        slim = slim_source_messages(
            [{"role": "user", "content": "ask"}],
            "final",
        )
        assert slim == [
            {"role": "user", "content": "ask"},
            {"role": "assistant", "content": "final"},
        ]


class TestStrictMode:
    def test_strict_revise_becomes_fail_and_skips_rewriter(
        self,
        in_memory_db: sqlite3.Connection,
        monkeypatch,
    ) -> None:
        calls: list[str] = []

        def fake_call_llm(messages, **kwargs):
            calls.append(kwargs["request_type"])
            text = json.dumps(
                {
                    "verdict": "revise",
                    "confidence": "high",
                    "issues": [
                        {
                            "severity": "major",
                            "quote": "x",
                            "problem": "wording",
                            "correction": "rewrite it",
                        }
                    ],
                }
            )
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
            kind="coach",
            draft="draft text",
            evidence={},
            source_messages=[],
            conn=in_memory_db,
            metadata={"source_llm_call_id": 1},
            model="verify-model",
            rewrite_model="rewrite-model",
            max_revisions=1,
            strict=True,
        )

        assert result.verdict == "fail"
        assert result.revised_text is None
        assert calls == ["coach_verify"]


class TestVerificationEvents:
    def _fake_call_llm(self, response_text: str):
        def fake(messages, **kwargs):
            row_id = log_llm_call(
                kwargs["conn"],
                request_type=kwargs["request_type"],
                model=kwargs["model"],
                messages=messages,
                response_text=response_text,
                metadata=kwargs["metadata"],
            )
            return LLMResult(
                text=response_text,
                model=kwargs["model"],
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                latency_s=0.1,
                llm_call_id=row_id,
            )

        return fake

    def test_fail_emits_suppressed_event(
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
        monkeypatch.setattr(
            "llm_verify.call_llm",
            self._fake_call_llm('{"verdict":"fail","issues":[],"confidence":"high"}'),
        )

        verify_and_rewrite(
            kind="nudge",
            draft="weak nudge",
            evidence={},
            source_messages=[],
            conn=in_memory_db,
            metadata={"source_llm_call_id": source_id},
            model="verify-model",
            rewrite_model="rewrite-model",
            max_revisions=1,
        )

        events = query_events(in_memory_db, category="nudge")
        kinds = [e["kind"] for e in events]
        assert "verifier_suppressed" in kinds

    def test_pass_emits_no_event(
        self,
        in_memory_db: sqlite3.Connection,
        monkeypatch,
    ) -> None:
        source_id = log_llm_call(
            in_memory_db,
            request_type="insights",
            model="draft-model",
            messages=[{"role": "user", "content": "draft"}],
            response_text="Clean report.",
        )
        monkeypatch.setattr(
            "llm_verify.call_llm",
            self._fake_call_llm('{"verdict":"pass","issues":[],"confidence":"high"}'),
        )

        verify_and_rewrite(
            kind="insights",
            draft="report text",
            evidence={},
            source_messages=[],
            conn=in_memory_db,
            metadata={"source_llm_call_id": source_id},
            model="verify-model",
            rewrite_model="rewrite-model",
            max_revisions=1,
        )

        events = query_events(in_memory_db, category="insights")
        assert events == []
