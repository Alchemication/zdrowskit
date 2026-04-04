"""Tests for pure functions in src/llm.py."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config import PROMPTS_DIR
from llm import (
    FALLBACK_MODEL,
    LLMResult,
    _call_with_retry,
    _is_overloaded,
    _recent_history,
    append_history,
    build_llm_data,
    build_messages,
    build_review_facts,
    call_llm,
    extract_memory,
    load_context,
    slim_for_prompt,
)
from models import DailySnapshot
from store import store_snapshots


class TestExtractMemory:
    def test_present(self) -> None:
        text = "Some report text.\n<memory>Key insight about training.</memory>\nMore text."
        assert extract_memory(text) == "Key insight about training."

    def test_absent(self) -> None:
        assert extract_memory("No memory block here.") is None

    def test_multiline(self) -> None:
        text = "<memory>\nLine 1\nLine 2\nLine 3\n</memory>"
        result = extract_memory(text)
        assert "Line 1" in result
        assert "Line 3" in result

    def test_strips_whitespace(self) -> None:
        text = "<memory>  padded content  </memory>"
        assert extract_memory(text) == "padded content"


class TestRecentHistory:
    def test_trims_to_n(self) -> None:
        entries = "\n\n".join(f"## 2026-03-{i:02d}\n\nEntry {i}" for i in range(1, 11))
        result = _recent_history(entries, 3)
        assert "## 2026-03-10" in result
        assert "## 2026-03-09" in result
        assert "## 2026-03-08" in result
        assert "## 2026-03-01" not in result

    def test_fewer_than_n_returns_original(self) -> None:
        content = "## 2026-03-01\n\nEntry 1\n\n## 2026-03-02\n\nEntry 2"
        result = _recent_history(content, 5)
        assert result == content

    def test_empty_string(self) -> None:
        assert _recent_history("", 5) == ""


class TestLoadContext:
    def test_loads_all_files(self, tmp_path: Path) -> None:
        (tmp_path / "prompt.md").write_text("Hello {me}")
        (tmp_path / "soul.md").write_text("Be direct.")
        (tmp_path / "me.md").write_text("Runner, 30y")
        ctx = load_context(tmp_path, prompts_dir=tmp_path)
        assert ctx["prompt"] == "Hello {me}"
        assert ctx["soul"] == "Be direct."
        assert ctx["me"] == "Runner, 30y"

    def test_missing_prompt_raises(self, tmp_path: Path) -> None:
        (tmp_path / "soul.md").write_text("Be direct.")
        with pytest.raises(FileNotFoundError, match="prompt.md"):
            load_context(tmp_path, prompts_dir=tmp_path)

    def test_optional_files_default(self, tmp_path: Path) -> None:
        (tmp_path / "prompt.md").write_text("template")
        ctx = load_context(tmp_path, prompts_dir=tmp_path)
        assert ctx["goals"] == "(not provided)"
        assert ctx["log"] == "(not provided)"

    def test_history_trimmed(self, tmp_path: Path) -> None:
        (tmp_path / "prompt.md").write_text("template")
        entries = "\n\n".join(f"## 2026-03-{i:02d}\n\nEntry {i}" for i in range(1, 20))
        (tmp_path / "history.md").write_text(entries)
        ctx = load_context(tmp_path, prompts_dir=tmp_path)
        # MAX_HISTORY_ENTRIES = 8, so only last 8 should remain
        assert "## 2026-03-19" in ctx["history"]
        assert "## 2026-03-12" in ctx["history"]
        assert "## 2026-03-01" not in ctx["history"]

    def test_log_trimmed(self, tmp_path: Path) -> None:
        (tmp_path / "prompt.md").write_text("template")
        entries = "\n\n".join(f"## 2026-03-{i:02d}\n\nLog {i}" for i in range(1, 12))
        (tmp_path / "log.md").write_text(entries)
        ctx = load_context(tmp_path, prompts_dir=tmp_path)
        # MAX_LOG_ENTRIES = 5, so only last 5 should remain
        assert "## 2026-03-11" in ctx["log"]
        assert "## 2026-03-07" in ctx["log"]
        assert "## 2026-03-06" not in ctx["log"]

    def test_coach_feedback_trimmed(self, tmp_path: Path) -> None:
        (tmp_path / "prompt.md").write_text("template")
        entries = "\n\n".join(
            f"## 2026-03-{i:02d}\n\nFeedback ID: cf_{i}\nDecision: rejected"
            for i in range(1, 12)
        )
        (tmp_path / "coach_feedback.md").write_text(entries)
        ctx = load_context(tmp_path, prompts_dir=tmp_path)
        assert "Feedback ID: cf_11" in ctx["coach_feedback"]
        assert "Feedback ID: cf_4" in ctx["coach_feedback"]
        assert "Feedback ID: cf_3" not in ctx["coach_feedback"]


class TestRepoPrompts:
    def test_soul_prompt_leaves_formatting_to_task_prompts(self) -> None:
        soul = (PROMPTS_DIR / "soul.md").read_text(encoding="utf-8")
        assert "Follow the task-specific instructions exactly" in soul
        assert "markdown headers" not in soul

    def test_nudge_prompt_states_event_driven_purpose_and_boundaries(self) -> None:
        prompt = (PROMPTS_DIR / "nudge_prompt.md").read_text(encoding="utf-8")
        assert "A nudge is not a summary of the latest sync." in prompt
        assert "If the trigger does not materially change the next action" in prompt
        assert "does not revise long-term goals or the training plan" in prompt
        assert "## Recent Nudges Already Sent" in prompt
        assert "## Recent Coach Recommendation" in prompt
        assert "## Recent User Notes" in prompt
        assert "## Recent Durable Coaching Context" in prompt

    def test_coach_prompt_states_plan_goal_review_role(self) -> None:
        prompt = (PROMPTS_DIR / "coach_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "weekly review of whether the user's current training plan and" in prompt
        assert "not a short reactive notification" in normalized
        assert "Review adherence, recovery, constraints, and trajectory." in prompt
        assert "Do not propose edits to any other files." in prompt
        assert "## Recent Coaching History" in prompt

    def test_chat_prompt_states_conversational_purpose_and_boundaries(self) -> None:
        prompt = (PROMPTS_DIR / "chat_prompt.md").read_text(encoding="utf-8")
        assert "Purpose: answer the user's current question or message" in prompt
        assert "This is not a proactive nudge and not a weekly plan/goals review." in prompt
        assert "## Recent User Notes" in prompt
        assert "## Recent Durable Coaching Context" in prompt
        assert "## Recent Coach Recommendation" in prompt
        assert "Do not turn a simple answer into a weekly review" in prompt

    def test_weekly_report_prompt_states_report_role_and_boundaries(self) -> None:
        prompt = (PROMPTS_DIR / "prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "Purpose: this is a weekly report that interprets what happened" in prompt
        assert "It is not a reactive nudge and not a plan/goals editing workflow." in normalized
        assert "## Recent User Notes This Week" in prompt
        assert "## Recent Durable Coaching Context" in prompt
        assert "Do not write this like a quick chat reply or a reactive nudge." in prompt


class TestBuildMessages:
    def test_basic_structure(self) -> None:
        ctx = {
            "soul": "Be a coach.",
            "prompt": "Report for {me} on {today}. Goals: {goals}",
            "me": "Adam",
            "goals": "Run more",
        }
        msgs = build_messages(ctx, health_data_json='{"data": 1}')
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "Be a coach."
        assert msgs[1]["role"] == "user"
        assert "Adam" in msgs[1]["content"]
        assert "Run more" in msgs[1]["content"]

    def test_soul_not_provided_uses_default(self) -> None:
        ctx = {"soul": "(not provided)", "prompt": "Hello"}
        msgs = build_messages(ctx, health_data_json="{}")
        assert "no-nonsense" in msgs[0]["content"]

    def test_missing_soul_uses_default(self) -> None:
        ctx = {"prompt": "Hello"}
        msgs = build_messages(ctx, health_data_json="{}")
        assert "no-nonsense" in msgs[0]["content"]

    def test_unknown_placeholder_defaults(self) -> None:
        ctx = {"prompt": "Data: {health_data}, Unknown: {unknown_key}"}
        msgs = build_messages(ctx, health_data_json='{"x":1}')
        assert "(not provided)" in msgs[1]["content"]

    def test_baselines_injected(self) -> None:
        ctx = {"prompt": "Baselines: {baselines}"}
        msgs = build_messages(ctx, health_data_json="{}", baselines="## HR: 52bpm")
        assert "## HR: 52bpm" in msgs[1]["content"]

    def test_baselines_none_shows_not_computed(self) -> None:
        ctx = {"prompt": "Baselines: {baselines}"}
        msgs = build_messages(ctx, health_data_json="{}", baselines=None)
        assert "(not computed)" in msgs[1]["content"]

    def test_explicit_today_override_is_used(self) -> None:
        ctx = {
            "prompt": "Today: {today}; Weekday: {weekday}; Status: {week_status}",
        }
        msgs = build_messages(
            ctx,
            health_data_json="{}",
            week_complete=False,
            today=date(2026, 3, 25),
        )
        assert "Today: 2026-03-25" in msgs[1]["content"]
        assert "Weekday: Wednesday" in msgs[1]["content"]
        assert "Mon–Wednesday" in msgs[1]["content"]

    def test_review_facts_placeholder_is_injected(self) -> None:
        ctx = {"prompt": "Facts: {review_facts}"}
        msgs = build_messages(
            ctx,
            health_data_json="{}",
        )
        assert "Facts: (not provided)" in msgs[1]["content"]


class TestBuildReviewFacts:
    def test_includes_shared_signals_and_feedback_hint(self) -> None:
        health_data = {
            "week_label": "2026-W12",
            "current_week": {
                "summary": {
                    "week_label": "2026-W12",
                    "run_count": 2,
                    "lift_count": 1,
                    "run_consistency_pct": 100.0,
                    "lift_consistency_pct": 50.0,
                    "total_run_km": 18.4,
                    "avg_hrv_ms": 54.2,
                    "avg_resting_hr": 51.1,
                    "avg_sleep_total_h": 7.2,
                    "avg_sleep_efficiency_pct": 91.5,
                    "avg_recovery_index": 1.06,
                    "hrv_trend": "stable",
                }
            },
            "history": [
                {
                    "summary": {
                        "total_run_km": 15.0,
                        "avg_hrv_ms": 50.0,
                        "avg_resting_hr": 53.0,
                    }
                }
            ],
        }
        context = {
            "log": "## 2026-03-24\n\nTravel week",
            "coach_feedback": "## 2026-03-24\n\nFeedback ID: cf_1",
        }

        result = build_review_facts(health_data, context, week_complete=True)

        assert "Shared Review Facts" in result
        assert "2026-W12" in result
        assert "Training adherence" in result
        assert "Recovery verdict" in result
        assert "coach_feedback.md" in result


class TestCallLlm:
    def _mock_response(
        self,
        text: str = "Report text",
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
    ) -> MagicMock:
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = text
        response.usage.prompt_tokens = prompt_tokens
        response.usage.completion_tokens = completion_tokens
        response.usage.total_tokens = prompt_tokens + completion_tokens
        return response

    @patch("llm.litellm")
    def test_returns_llm_result(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = 0.05

        msgs = [{"role": "user", "content": "test"}]
        result = call_llm(msgs, model="test-model")

        assert isinstance(result, LLMResult)
        assert result.text == "Report text"
        assert result.model == "test-model"
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.total_tokens == 150
        assert result.cost == 0.05
        assert result.latency_s >= 0

    @patch("llm.litellm")
    def test_cost_fallback_on_exception(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.side_effect = Exception("no pricing")

        result = call_llm([{"role": "user", "content": "test"}])
        assert result.cost is None

    @patch("llm.litellm")
    def test_reasoning_effort_passed(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = None

        call_llm(
            [{"role": "user", "content": "test"}],
            reasoning_effort="high",
        )
        kwargs = mock_litellm.completion.call_args[1]
        assert kwargs["reasoning_effort"] == "high"

    @patch("llm.litellm")
    def test_reasoning_effort_omitted_by_default(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = None

        call_llm([{"role": "user", "content": "test"}])
        kwargs = mock_litellm.completion.call_args[1]
        assert "reasoning_effort" not in kwargs

    @patch("llm.litellm")
    def test_logs_to_db(
        self, mock_litellm: MagicMock, in_memory_db: sqlite3.Connection
    ) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = 0.02

        call_llm(
            [{"role": "user", "content": "test"}],
            conn=in_memory_db,
            request_type="insights",
        )
        row = in_memory_db.execute("SELECT * FROM llm_call").fetchone()
        assert row is not None
        assert row["request_type"] == "insights"

    @patch("llm.litellm")
    def test_db_logging_failure_swallowed(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = None

        # Pass a closed connection to trigger a logging error
        conn = sqlite3.connect(":memory:")
        conn.close()

        result = call_llm(
            [{"role": "user", "content": "test"}],
            conn=conn,
            request_type="insights",
        )
        # Should still return the result despite DB error
        assert result.text == "Report text"

    @patch("llm.litellm")
    def test_no_logging_without_conn(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = None

        result = call_llm([{"role": "user", "content": "test"}])
        assert result.text == "Report text"


class TestIsOverloaded:
    def test_overloaded_error_string(self) -> None:
        assert _is_overloaded(Exception("overloaded_error occurred"))

    def test_overloaded_string(self) -> None:
        assert _is_overloaded(Exception("Overloaded. See docs"))

    def test_other_error(self) -> None:
        assert not _is_overloaded(Exception("authentication failed"))

    def test_rate_limit_not_overloaded(self) -> None:
        assert not _is_overloaded(Exception("rate_limit_error"))


class TestCallWithRetry:
    def _mock_response(self, text: str = "ok") -> MagicMock:
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = text
        resp.usage.prompt_tokens = 10
        resp.usage.completion_tokens = 5
        resp.usage.total_tokens = 15
        return resp

    def _overloaded_error(self) -> Exception:
        return Exception("overloaded_error: service is Overloaded")

    @patch("llm.time.sleep")
    @patch("llm.litellm")
    def test_succeeds_on_first_attempt(self, mock_litellm, mock_sleep) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        resp, model = _call_with_retry({"model": "m"}, "m")
        assert model == "m"
        mock_sleep.assert_not_called()

    @patch("llm.time.sleep")
    @patch("llm.litellm")
    def test_retries_then_succeeds(self, mock_litellm, mock_sleep) -> None:
        mock_litellm.completion.side_effect = [
            self._overloaded_error(),
            self._mock_response("after retry"),
        ]
        resp, model = _call_with_retry({"model": "m"}, "m")
        assert model == "m"
        assert mock_litellm.completion.call_count == 2
        mock_sleep.assert_called_once_with(10)  # first delay

    @patch("llm.time.sleep")
    @patch("llm.litellm")
    def test_falls_back_to_sonnet_after_all_retries(
        self, mock_litellm, mock_sleep
    ) -> None:
        # Primary model always overloaded; fallback succeeds.
        mock_litellm.completion.side_effect = [
            self._overloaded_error(),  # primary attempt 1
            self._overloaded_error(),  # primary attempt 2
            self._overloaded_error(),  # primary attempt 3
            self._overloaded_error(),  # primary attempt 4 (no delay after)
            self._mock_response("fallback ok"),  # fallback succeeds
        ]
        resp, model = _call_with_retry({"model": "primary"}, "primary")
        assert model == FALLBACK_MODEL
        # 3 delays for primary retries.
        assert mock_sleep.call_count == 3

    @patch("llm.time.sleep")
    @patch("llm.litellm")
    def test_raises_on_non_overloaded_error(self, mock_litellm, mock_sleep) -> None:
        mock_litellm.completion.side_effect = Exception("authentication failed")
        with pytest.raises(Exception, match="authentication failed"):
            _call_with_retry({"model": "m"}, "m")
        mock_sleep.assert_not_called()

    @patch("llm.time.sleep")
    @patch("llm.litellm")
    def test_raises_if_fallback_exhausted(self, mock_litellm, mock_sleep) -> None:
        # All calls overloaded — should eventually raise.
        mock_litellm.completion.side_effect = self._overloaded_error()
        with pytest.raises(Exception, match="overloaded_error"):
            _call_with_retry({"model": "m"}, "m")


class TestAppendHistory:
    def test_creates_new_file(self, tmp_path: Path) -> None:
        append_history(tmp_path, "First memory block")
        content = (tmp_path / "history.md").read_text()
        assert "First memory block" in content
        assert content.startswith("## ")

    def test_appends_to_existing(self, tmp_path: Path) -> None:
        (tmp_path / "history.md").write_text("## 2026-03-01\n\nOld entry\n")
        append_history(tmp_path, "New memory block")
        content = (tmp_path / "history.md").read_text()
        assert "Old entry" in content
        assert "New memory block" in content

    def test_replaces_same_week_entry(self, tmp_path: Path) -> None:
        append_history(tmp_path, "Entry one", week_label="2026-W12")
        append_history(tmp_path, "Entry two", week_label="2026-W12")
        content = (tmp_path / "history.md").read_text()
        # Second call for the same week replaces, not appends
        assert content.count("## ") == 1
        assert "Entry one" not in content
        assert "Entry two" in content

    def test_appends_different_week_entry(self, tmp_path: Path) -> None:
        append_history(tmp_path, "W11 notes", week_label="2026-W11")
        append_history(tmp_path, "W12 notes", week_label="2026-W12")
        content = (tmp_path / "history.md").read_text()
        assert content.count("## ") == 2
        assert "W11 notes" in content
        assert "W12 notes" in content

    def test_different_weeks_not_clobbered(self, tmp_path: Path) -> None:
        """Running --week last and --week current on same day keeps both entries."""
        append_history(tmp_path, "Last week review", week_label="2026-W12")
        append_history(tmp_path, "Current week progress", week_label="2026-W13")
        content = (tmp_path / "history.md").read_text()
        assert content.count("## ") == 2
        assert "Last week review" in content
        assert "Current week progress" in content

    def test_grows_unbounded_without_trimming(self, tmp_path: Path) -> None:
        """BUG: append_history does not trim despite its docstring claiming it does.

        The file grows without bound — trimming only happens at read time
        via _recent_history() in load_context(). This test documents the
        current behavior; if trimming is added to append_history, update
        this test to assert heading_count == MAX_HISTORY_ENTRIES.
        """
        from config import MAX_HISTORY_ENTRIES

        entries = "\n\n".join(
            f"## 2026-03-{i:02d}\n\nOld entry {i}"
            for i in range(1, MAX_HISTORY_ENTRIES + 1)
        )
        (tmp_path / "history.md").write_text(entries)

        append_history(tmp_path, "Brand new entry")
        content = (tmp_path / "history.md").read_text()
        heading_count = content.count("## ")
        # Currently does NOT trim — grows to MAX + 1
        assert heading_count == MAX_HISTORY_ENTRIES + 1
        assert "Brand new entry" in content
        # Oldest entry is still present (not trimmed)
        assert "Old entry 1" in content


class TestBuildLlmData:
    def test_empty_db(self, in_memory_db: sqlite3.Connection) -> None:
        result = build_llm_data(in_memory_db, months=3)
        assert result["current_week"]["summary"] is None
        assert result["history"] == []

    @patch("llm.date")
    def test_with_data(
        self,
        mock_date: MagicMock,
        in_memory_db: sqlite3.Connection,
        sample_snapshots: list[DailySnapshot],
    ) -> None:
        mock_date.today.return_value = date(2026, 3, 11)
        mock_date.fromisoformat = date.fromisoformat
        store_snapshots(in_memory_db, sample_snapshots)
        result = build_llm_data(in_memory_db, months=3)
        assert "current_week" in result
        assert "history" in result
        # Should have some days in the result
        assert isinstance(result["current_week"]["days"], list)

    @patch("llm.date")
    def test_last_week_mode(
        self,
        mock_date: MagicMock,
        in_memory_db: sqlite3.Connection,
        sample_snapshots: list[DailySnapshot],
    ) -> None:
        mock_date.today.return_value = date(2026, 3, 16)
        mock_date.fromisoformat = date.fromisoformat
        store_snapshots(in_memory_db, sample_snapshots)
        result = build_llm_data(in_memory_db, months=3, week="last")
        assert "current_week" in result
        assert "history" in result

    @patch("llm.date")
    def test_structure_has_expected_fields(
        self,
        mock_date: MagicMock,
        in_memory_db: sqlite3.Connection,
        sample_snapshots: list[DailySnapshot],
    ) -> None:
        """Verify the nested structure contains actual WeeklySummary and DailySnapshot fields."""
        mock_date.today.return_value = date(2026, 3, 11)
        mock_date.fromisoformat = date.fromisoformat
        store_snapshots(in_memory_db, sample_snapshots)
        result = build_llm_data(in_memory_db, months=3)

        summary = result["current_week"]["summary"]
        assert summary is not None
        # WeeklySummary fields
        assert "week_label" in summary
        assert "run_count" in summary
        assert "avg_resting_hr" in summary
        assert "hrv_trend" in summary

        days = result["current_week"]["days"]
        assert len(days) > 0
        day = days[0]
        # DailySnapshot fields
        assert "date" in day
        assert "steps" in day
        assert "workouts" in day
        assert "recovery_index" in day
        assert "counts_as_lift" in day["workouts"][0]

    @patch("llm.datetime")
    @patch("llm.date")
    def test_sleep_shifted_forward_by_one_day(
        self,
        mock_date: MagicMock,
        mock_datetime: MagicMock,
        in_memory_db: sqlite3.Connection,
        sample_snapshots: list[DailySnapshot],
    ) -> None:
        """Each day's sleep should be the previous day's sleep (night before).

        Fixture: Mon Mar 9 has sleep (7.4h), Tue Mar 10 has sleep (7.0h),
        Wed Mar 11 onward has no sleep.  After the shift:
        - Mon Mar 9: no sleep (no pre-week Sunday data)
        - Tue Mar 10: Mon's sleep (7.4h)
        - Wed Mar 11: Tue's sleep (7.0h)
        - Thu Mar 12 onward: not_tracked
        """
        # Today = Wed Mar 11, week = Mon Mar 9 – Sun Mar 15.
        mock_date.today.return_value = date(2026, 3, 11)
        mock_date.fromisoformat = date.fromisoformat
        mock_datetime.now.return_value = datetime(2026, 3, 11, 14, 0)
        store_snapshots(in_memory_db, sample_snapshots)
        result = build_llm_data(in_memory_db, months=3)

        days = {d["date"]: d for d in result["current_week"]["days"]}
        # Mon: no pre-week data to shift in, today's sync heuristic doesn't
        # apply (it's Wed) — but no sleep was shifted in, so not_tracked
        assert days["2026-03-09"]["sleep_status"] == "not_tracked"
        # Tue: gets Mon night's sleep (7.4h)
        assert days["2026-03-10"]["sleep_total_h"] == 7.4
        assert days["2026-03-10"]["sleep_status"] == "tracked"
        # Wed (today): gets Tue night's sleep (7.0h)
        assert days["2026-03-11"]["sleep_total_h"] == 7.0
        assert days["2026-03-11"]["sleep_status"] == "tracked"
        # Thu: no sleep shifted in — not_tracked
        assert days["2026-03-12"]["sleep_status"] == "not_tracked"
        # Metrics unchanged
        assert days["2026-03-09"]["steps"] == 9500
        assert days["2026-03-10"]["steps"] == 12000

    @patch("llm.datetime")
    @patch("llm.date")
    def test_sleep_shift_with_pre_week_day(
        self,
        mock_date: MagicMock,
        mock_datetime: MagicMock,
        in_memory_db: sqlite3.Connection,
        sample_snapshots: list[DailySnapshot],
    ) -> None:
        """Monday gets Sunday's sleep when pre-week Sunday has data."""
        mock_date.today.return_value = date(2026, 3, 19)  # Wednesday
        mock_date.fromisoformat = date.fromisoformat
        mock_datetime.now.return_value = datetime(2026, 3, 19, 10, 0)

        store_snapshots(in_memory_db, sample_snapshots)
        store_snapshots(
            in_memory_db,
            [
                DailySnapshot(
                    date="2026-03-15",
                    steps=5000,
                    distance_km=3.0,
                    active_energy_kj=1000.0,
                    exercise_min=10,
                    stand_hours=8,
                    resting_hr=52,
                    hrv_ms=55.0,
                    sleep_total_h=7.5,
                    sleep_in_bed_h=8.0,
                    sleep_efficiency_pct=93.8,
                    sleep_deep_h=0.9,
                    sleep_core_h=4.5,
                    sleep_rem_h=2.1,
                    sleep_awake_h=0.5,
                    recovery_index=55.0 / 52,
                ),
                DailySnapshot(
                    date="2026-03-16",
                    steps=9000,
                    distance_km=6.0,
                    active_energy_kj=1700.0,
                    exercise_min=30,
                    stand_hours=10,
                    resting_hr=53,
                    hrv_ms=50.0,
                    recovery_index=50.0 / 53,
                ),
                DailySnapshot(
                    date="2026-03-17",
                    steps=8000,
                    distance_km=5.5,
                    active_energy_kj=1500.0,
                    exercise_min=20,
                    stand_hours=9,
                    resting_hr=51,
                    hrv_ms=58.0,
                    recovery_index=58.0 / 51,
                ),
            ],
        )

        result = build_llm_data(in_memory_db, months=3, week="current")
        days = {d["date"]: d for d in result["current_week"]["days"]}

        # Monday gets Sunday night's sleep (shifted from Mar 15)
        assert days["2026-03-16"]["sleep_total_h"] == 7.5
        assert days["2026-03-16"]["sleep_efficiency_pct"] == 93.8
        # Sunday (Mar 15) is NOT in the output — it's the pre-week day
        assert "2026-03-15" not in days
        # Monday's own metrics are still there
        assert days["2026-03-16"]["steps"] == 9000

    @patch("llm.datetime")
    @patch("llm.date")
    def test_today_unsynced_sleep_not_flagged(
        self,
        mock_date: MagicMock,
        mock_datetime: MagicMock,
        in_memory_db: sqlite3.Connection,
        sample_snapshots: list[DailySnapshot],
    ) -> None:
        """Today's null sleep (after shift) is omitted, not marked not_tracked."""
        # Today = Thu Mar 12 before sync cutoff.  After shift, Thu would get
        # Wed's sleep — but Wed (Mar 11) has no sleep, so Thu has nothing.
        # Since it's today, it should be omitted (not synced yet), not flagged.
        mock_date.today.return_value = date(2026, 3, 12)
        mock_date.fromisoformat = date.fromisoformat
        mock_datetime.now.return_value = datetime(2026, 3, 12, 7, 30)
        store_snapshots(in_memory_db, sample_snapshots)
        result = build_llm_data(in_memory_db, months=3)

        days = {d["date"]: d for d in result["current_week"]["days"]}
        # Today — sleep_status is "pending", not "not_tracked"
        assert days["2026-03-12"]["sleep_status"] == "pending"

    @patch("llm.datetime")
    @patch("llm.date")
    def test_last_week_sunday_sleep_shifted_off(
        self,
        mock_date: MagicMock,
        mock_datetime: MagicMock,
        in_memory_db: sqlite3.Connection,
        sample_snapshots: list[DailySnapshot],
    ) -> None:
        """In --week last, Sunday's original sleep shifts to next week (dropped)."""
        mock_date.today.return_value = date(2026, 3, 16)  # Monday
        mock_date.fromisoformat = date.fromisoformat
        # After sync cutoff — yesterday heuristic doesn't apply
        mock_datetime.now.return_value = datetime(2026, 3, 16, 12, 0)
        store_snapshots(in_memory_db, sample_snapshots)
        result = build_llm_data(in_memory_db, months=3, week="last")

        days = {d["date"]: d for d in result["current_week"]["days"]}
        # Sunday (Mar 15) — its sleep was shifted forward (off the end),
        # no sleep remains → not_tracked
        assert days["2026-03-15"]["sleep_status"] == "not_tracked"
        # The week still has 7 days
        assert len(result["current_week"]["days"]) == 7

    @patch("llm.datetime")
    @patch("llm.date")
    def test_sleep_compliance_fields(
        self,
        mock_date: MagicMock,
        mock_datetime: MagicMock,
        in_memory_db: sqlite3.Connection,
        sample_snapshots: list[DailySnapshot],
    ) -> None:
        """Summary includes pre-computed sleep compliance stats."""
        # Wed Mar 11.  Fixture has Mon-Sun.  After shift:
        # Mon=not_tracked, Tue=tracked(7.4h), Wed(today)=tracked(7.0h from shift),
        # Thu-Sun=not_tracked (no sleep in fixture).
        mock_date.today.return_value = date(2026, 3, 11)
        mock_date.fromisoformat = date.fromisoformat
        mock_datetime.now.return_value = datetime(2026, 3, 11, 14, 0)
        store_snapshots(in_memory_db, sample_snapshots)
        result = build_llm_data(in_memory_db, months=3)

        summary = result["current_week"]["summary"]
        assert summary["sleep_nights_tracked"] == 2  # Tue, Wed (shifted sleep)
        assert summary["sleep_nights_total"] == 7  # all 7 days eligible
        assert "2026-03-09" in summary["sleep_not_tracked_dates"]
        assert "2026-03-12" in summary["sleep_not_tracked_dates"]
        assert len(summary["sleep_not_tracked_dates"]) == 5
        assert summary["run_target"] == 2
        assert summary["lift_target"] == 2

    @patch("llm.datetime")
    @patch("llm.date")
    def test_today_snapshot_in_summary(
        self,
        mock_date: MagicMock,
        mock_datetime: MagicMock,
        in_memory_db: sqlite3.Connection,
        sample_snapshots: list[DailySnapshot],
    ) -> None:
        """Summary includes a today snapshot with key vitals."""
        mock_date.today.return_value = date(2026, 3, 10)
        mock_date.fromisoformat = date.fromisoformat
        mock_datetime.now.return_value = datetime(2026, 3, 10, 14, 0)
        store_snapshots(in_memory_db, sample_snapshots)
        result = build_llm_data(in_memory_db, months=3)

        summary = result["current_week"]["summary"]
        today = summary["today"]
        assert today["date"] == "2026-03-10"
        assert today["hrv_ms"] is not None
        assert today["steps"] is not None
        assert today["sleep_status"] in ("tracked", "pending", "not_tracked")
        assert "counts_as_lift" in today["workouts"][0]

    @patch("llm.datetime")
    @patch("llm.date")
    def test_slim_for_prompt_strips_days(
        self,
        mock_date: MagicMock,
        mock_datetime: MagicMock,
        in_memory_db: sqlite3.Connection,
        sample_snapshots: list[DailySnapshot],
    ) -> None:
        """slim_for_prompt removes per-day arrays but keeps summary."""
        mock_date.today.return_value = date(2026, 3, 11)
        mock_date.fromisoformat = date.fromisoformat
        mock_datetime.now.return_value = datetime(2026, 3, 11, 14, 0)
        store_snapshots(in_memory_db, sample_snapshots)
        result = build_llm_data(in_memory_db, months=3)

        # Full result has days
        assert "days" in result["current_week"]

        # Slim version does not
        slim = slim_for_prompt(result)
        assert "days" not in slim["current_week"]
        assert slim["current_week"]["summary"] is not None

        # Original is not mutated
        assert "days" in result["current_week"]
