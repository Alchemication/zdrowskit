"""Tests for pure functions in src/llm.py."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from llm import (
    FALLBACK_MODEL,
    LLMResult,
    _call_with_retry,
    _is_overloaded,
    _recent_history,
    append_history,
    build_llm_data,
    build_messages,
    call_llm,
    extract_memory,
    load_context,
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
        assert result["current_week"]["days"] == []
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

    @patch("llm.datetime")
    @patch("llm.date")
    def test_today_sleep_is_pending_not_untracked(
        self,
        mock_date: MagicMock,
        mock_datetime: MagicMock,
        in_memory_db: sqlite3.Connection,
        sample_snapshots: list[DailySnapshot],
    ) -> None:
        """Today's null sleep should be 'pending', past null sleep 'not_tracked'."""
        mock_date.today.return_value = date(2026, 3, 11)
        mock_date.fromisoformat = date.fromisoformat
        # After sync cutoff — yesterday's null sleep is genuinely not tracked
        mock_datetime.now.return_value = datetime(2026, 3, 11, 14, 0)
        store_snapshots(in_memory_db, sample_snapshots)
        result = build_llm_data(in_memory_db, months=3)

        days = {d["date"]: d for d in result["current_week"]["days"]}
        # 2026-03-11 is "today" — sleep hasn't happened yet
        assert days["2026-03-11"]["sleep"] == "pending"
        # 2026-03-12 is a past day with no sleep — watch wasn't worn
        assert days["2026-03-12"]["sleep"] == "not_tracked"
        # 2026-03-09 has real sleep data — no marker at all
        assert "sleep" not in days["2026-03-09"]
        assert days["2026-03-09"]["sleep_total_h"] == 7.4

    @patch("llm.datetime")
    @patch("llm.date")
    def test_yesterday_sleep_sync_pending_before_cutoff(
        self,
        mock_date: MagicMock,
        mock_datetime: MagicMock,
        in_memory_db: sqlite3.Connection,
        sample_snapshots: list[DailySnapshot],
    ) -> None:
        """Before 10am, yesterday's null sleep should be 'sync_pending'."""
        mock_date.today.return_value = date(2026, 3, 13)
        mock_date.fromisoformat = date.fromisoformat
        mock_datetime.now.return_value = datetime(2026, 3, 13, 7, 30)
        store_snapshots(in_memory_db, sample_snapshots)
        result = build_llm_data(in_memory_db, months=3)

        days = {d["date"]: d for d in result["current_week"]["days"]}
        # 2026-03-12 is yesterday with no sleep and it's before 10am
        assert days["2026-03-12"]["sleep"] == "sync_pending"
