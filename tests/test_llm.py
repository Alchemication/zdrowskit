"""Tests for pure functions in src/llm.py."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from config import (
    ANTHROPIC_HAIKU_MODEL,
    ANTHROPIC_OPUS_4_7_MODEL,
    ANTHROPIC_OPUS_MODEL,
    DEEPSEEK_EXTRA_BODY,
    DEEPSEEK_FLASH_MODEL,
    DEEPSEEK_PRO_MODEL,
    DEFAULT_ADD_CLONE_MODEL,
    DEFAULT_CHAT_MODEL,
    DEFAULT_COACH_MODEL,
    DEFAULT_INSIGHTS_MODEL,
    DEFAULT_LOG_FLOW_MODEL,
    DEFAULT_MODEL,
    DEFAULT_NOTIFY_MODEL,
    DEFAULT_NUDGE_MODEL,
    FALLBACK_FLASH_MODEL,
    FALLBACK_MODEL,
    FALLBACK_PRO_MODEL,
    PRIMARY_FLASH_MODEL,
    PRIMARY_PRO_MODEL,
    PROMPTS_DIR,
)
from charts import chart_figure_caption
from llm import (
    LLMResult,
    _call_with_retry,
    _completion_kwargs_for_model,
    _deepseek_v4_cost,
    _fallback_chain,
    _is_overloaded,
    call_llm,
    extract_memory,
)
from llm_context import _recent_history, append_history, build_messages, load_context
from llm_health import (
    build_llm_data,
    build_review_facts,
    format_recent_nudges,
    render_health_data,
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


class TestFeatureDefaultModels:
    def test_high_judgment_surfaces_default_to_deepseek_pro(self) -> None:
        assert DEFAULT_MODEL == PRIMARY_PRO_MODEL == DEEPSEEK_PRO_MODEL
        assert FALLBACK_MODEL == FALLBACK_PRO_MODEL == ANTHROPIC_OPUS_MODEL
        assert DEFAULT_INSIGHTS_MODEL == PRIMARY_PRO_MODEL
        assert DEFAULT_COACH_MODEL == PRIMARY_PRO_MODEL
        assert DEFAULT_NUDGE_MODEL == PRIMARY_PRO_MODEL
        assert DEFAULT_CHAT_MODEL == PRIMARY_PRO_MODEL

    def test_lightweight_utility_surfaces_default_models(self) -> None:
        assert PRIMARY_FLASH_MODEL == DEEPSEEK_FLASH_MODEL
        assert FALLBACK_FLASH_MODEL == ANTHROPIC_HAIKU_MODEL
        assert DEFAULT_NOTIFY_MODEL == PRIMARY_FLASH_MODEL
        assert DEFAULT_LOG_FLOW_MODEL == ANTHROPIC_HAIKU_MODEL
        assert DEFAULT_ADD_CLONE_MODEL == PRIMARY_FLASH_MODEL


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
        (tmp_path / "insights_prompt.md").write_text("Hello {me}")
        (tmp_path / "soul.md").write_text("Be direct.")
        (tmp_path / "me.md").write_text("Runner, 30y")
        ctx = load_context(tmp_path, prompts_dir=tmp_path)
        assert ctx["prompt"] == "Hello {me}"
        assert ctx["soul"] == "Be direct."
        assert ctx["me"] == "Runner, 30y"

    def test_missing_prompt_raises(self, tmp_path: Path) -> None:
        (tmp_path / "soul.md").write_text("Be direct.")
        with pytest.raises(FileNotFoundError, match="insights_prompt.md"):
            load_context(tmp_path, prompts_dir=tmp_path)

    def test_optional_files_default(self, tmp_path: Path) -> None:
        (tmp_path / "insights_prompt.md").write_text("template")
        ctx = load_context(tmp_path, prompts_dir=tmp_path)
        assert ctx["strategy"] == "(not provided)"
        assert ctx["log"] == "(not provided)"

    def test_history_trimmed(self, tmp_path: Path) -> None:
        (tmp_path / "insights_prompt.md").write_text("template")
        entries = "\n\n".join(f"## 2026-03-{i:02d}\n\nEntry {i}" for i in range(1, 20))
        (tmp_path / "history.md").write_text(entries)
        ctx = load_context(tmp_path, prompts_dir=tmp_path)
        # MAX_HISTORY_ENTRIES = 8, so only last 8 should remain
        assert "## 2026-03-19" in ctx["history"]
        assert "## 2026-03-12" in ctx["history"]
        assert "## 2026-03-01" not in ctx["history"]

    def test_log_trimmed(self, tmp_path: Path) -> None:
        (tmp_path / "insights_prompt.md").write_text("template")
        entries = "\n\n".join(f"## 2026-03-{i:02d}\n\nLog {i}" for i in range(1, 12))
        (tmp_path / "log.md").write_text(entries)
        ctx = load_context(tmp_path, prompts_dir=tmp_path)
        # MAX_LOG_ENTRIES = 5, so only last 5 should remain
        assert "## 2026-03-11" in ctx["log"]
        assert "## 2026-03-07" in ctx["log"]
        assert "## 2026-03-06" not in ctx["log"]

    def test_max_log_zero_disables_trimming(self, tmp_path: Path) -> None:
        (tmp_path / "insights_prompt.md").write_text("template")
        entries = "\n\n".join(f"## 2026-03-{i:02d}\n\nLog {i}" for i in range(1, 12))
        (tmp_path / "log.md").write_text(entries)
        ctx = load_context(tmp_path, prompts_dir=tmp_path, max_log=0)
        assert "## 2026-03-11" in ctx["log"]
        assert "## 2026-03-01" in ctx["log"]

    def test_coach_feedback_trimmed(self, tmp_path: Path) -> None:
        (tmp_path / "insights_prompt.md").write_text("template")
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
    def test_support_prompts_live_in_prompts_dir(self) -> None:
        names = [
            "default_soul.md",
            "schema_reference.md",
            "week_status_full.md",
            "week_status_partial.md",
            "nudge_tool_followup.md",
            "nudge_nonfinal_retry.md",
            "nudge_empty_retry.md",
            "insights_truncation_retry.md",
            "verify_rewrite_prompt.md",
            "tool_budget_synthesize.md",
            "tool_budget_nudge.md",
            "tool_budget_chat.md",
        ]

        for name in names:
            assert (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()

    def test_soul_prompt_leaves_formatting_to_task_prompts(self) -> None:
        soul = (PROMPTS_DIR / "soul.md").read_text(encoding="utf-8")
        assert "Follow the task-specific instructions exactly" in soul
        assert "markdown headers" not in soul

    def test_soul_prompt_carries_voice_basics(self) -> None:
        """Cross-cutting voice rules belong in soul.md so every task inherits them."""
        soul = (PROMPTS_DIR / "soul.md").read_text(encoding="utf-8")
        assert "Never open with" in soul
        assert "Wait" in soul
        assert "Do not narrate your own reasoning" in soul
        assert "mm:ss/km" in soul

    def test_nudge_prompt_states_event_driven_purpose_and_boundaries(self) -> None:
        prompt = (PROMPTS_DIR / "nudge_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "It is not a summary of the latest sync." in normalized
        assert "does not revise the user's strategy" in normalized
        # The redundancy check now lives in the ordered SKIP checklist.
        assert "Recent Nudges Sent section already" in normalized
        assert "## Recent Nudges Sent" in prompt
        assert "## Latest Coach Session" in prompt
        assert "## Recent User Notes" in prompt
        assert "## Recent Coaching History" in prompt
        assert "compact markdown rendering" in normalized
        assert "{schema_reference}" in prompt
        # Trigger context placeholder must be present so the daemon's
        # delta description actually reaches the LLM.
        assert "{trigger_context}" in prompt
        # Output-rules block must lead the prompt — recency bias matters.
        assert prompt.index("Output rules") < prompt.index("Instructions")
        assert "If you need `run_sql`, call it directly" in prompt
        assert "output only the final nudge or `SKIP`" in prompt
        assert "Figure 1" in prompt
        assert "rendered as a separate figure before the nudge text" in normalized

    def test_nudge_prompt_has_ordered_skip_checklist(self) -> None:
        """The SKIP/write decision must be a single ordered checklist, not
        scattered overlapping rules that bias toward over-skipping."""
        prompt = (PROMPTS_DIR / "nudge_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "Decide whether to SKIP or write (ordered checklist)" in normalized
        # Ordered checklist items in expected order.
        carveout_pos = normalized.index("Carve-out check")
        redundancy_pos = normalized.index("Redundancy check")
        coach_pos = normalized.index("Coach overlap check")
        trigger_pos = normalized.index("Trigger-specific skip rules")
        materiality_pos = normalized.index("Materiality check")
        assert (
            carveout_pos < redundancy_pos < coach_pos < trigger_pos < materiality_pos
        ), "SKIP checklist must run carve-out first, materiality last"

    def test_coach_prompt_states_strategy_review_role(self) -> None:
        prompt = (PROMPTS_DIR / "coach_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "weekly review of whether the user's current strategy" in normalized
        assert "not a short reactive notification" in normalized
        assert "Do not propose edits to any other files." in normalized
        assert "## Recent Coaching History" in prompt

    def test_coach_prompt_has_skip_branch_for_no_change_weeks(self) -> None:
        """Coach must SKIP (silent) when no plan/goal changes are warranted."""
        prompt = (PROMPTS_DIR / "coach_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        # SKIP is the documented no-change output.
        assert "SKIP" in prompt
        assert "SKIP is the common case" in normalized
        # The structured review is gated on having a concrete change to propose.
        assert "structured review only when" in normalized
        # And every concrete change must be backed by an update_context call.
        assert "update_context" in prompt
        assert "Correct flow:" in prompt
        assert "Wrong flow:" in prompt
        assert "Tool calls are not visible to the user" in prompt

    def test_chat_prompt_states_conversational_purpose_and_boundaries(self) -> None:
        prompt = (PROMPTS_DIR / "chat_prompt.md").read_text(encoding="utf-8")
        assert "Purpose: answer the user's current question or message" in prompt
        assert "Stay focused on the current conversation turn." in prompt
        assert "## Recent User Notes" in prompt
        assert "## Recent Coaching History" in prompt
        assert "## Latest Coach Session" in prompt
        assert "{schema_reference}" in prompt

    def test_chat_prompt_defines_status_first_week_recap_shape(self) -> None:
        prompt = (PROMPTS_DIR / "chat_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "Simple Current-Week Status Questions" in prompt
        assert "status-first" in normalized
        assert "chronological order" in normalized
        assert "Do **not** use target fractions" in prompt
        assert "not a full strength workout" in normalized

    def test_chat_prompt_resolves_tool_turn_conflict_and_chart_scaffolding(
        self,
    ) -> None:
        """Chat should require tool-only turns but non-empty final replies."""
        prompt = (PROMPTS_DIR / "chat_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        soul = (PROMPTS_DIR / "soul.md").read_text(encoding="utf-8")

        assert "tool-call turn itself should be tool-only" in normalized
        assert (
            "final reply after the tool result must still answer the user" in normalized
        )
        assert "here's the chart" in normalized
        assert "here's the picture" in normalized
        assert "Figure 1" in prompt
        assert (
            "rendered as a separate image attachment before your text reply"
            in normalized
        )
        assert (
            "Refer to it explicitly when it materially supports your answer" in prompt
        )
        assert "Correct flow:" in prompt
        assert "Wrong flow:" in prompt
        assert "I'll add that to your log" in prompt
        assert "Respect the task-specific tool-turn protocol" in soul


class TestCharts:
    def test_chart_figure_caption_includes_index_and_title(self) -> None:
        assert (
            chart_figure_caption(1, "Running Pace Trend")
            == "**Figure 1. Running Pace Trend**"
        )

    def test_chart_figure_caption_handles_missing_title(self) -> None:
        assert chart_figure_caption(2, "") == "**Figure 2**"

    def test_chat_prompt_routes_run_questions_to_workout_all(self) -> None:
        """Run/session queries should prefer workout_all over daily metrics."""
        prompt = (PROMPTS_DIR / "chat_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())

        assert "Query routing:" in prompt
        assert "Use `workout_all` for workout/session questions" in prompt
        assert "Use `workout_split` joined on `start_utc`" in prompt
        assert "running speed" in normalized
        assert "prefer `workout_all`, not `daily.running_speed_kmh`" in prompt

    def test_other_tool_prompts_share_run_query_routing_note(self) -> None:
        """Report/coach/nudge should reinforce the shared workout-vs-daily split."""
        for prompt_name in ("insights_prompt.md", "coach_prompt.md", "nudge_prompt.md"):
            prompt = (PROMPTS_DIR / prompt_name).read_text(encoding="utf-8")
            assert "Query routing:" in prompt
            assert "Use `workout_all` for workout/session questions" in prompt
            assert "Use `workout_split` joined on `start_utc`" in prompt
            assert "Use `daily` for day-level health questions" in prompt
            assert "prefer `workout_all`, not `daily.running_speed_kmh`" in prompt

    def test_report_prompt_sets_non_inline_chart_contract(self) -> None:
        prompt = (PROMPTS_DIR / "insights_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())

        assert "Figure 1" in prompt
        assert "rendered as separate figures rather than inline" in normalized
        assert "Do **not** use positional language like `below`, `above`" in prompt

    def test_chat_prompt_shows_plan_from_context_not_sql(self) -> None:
        """Asking 'what is my plan' should be answered from injected context."""
        prompt = (PROMPTS_DIR / "chat_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "show me my goals" in normalized or "what is my plan" in normalized
        assert "Do NOT run SQL" in normalized
        # Forbidden self-correction openings must be called out explicitly.
        assert "Never begin a reply with" in normalized
        assert "Wait" in normalized
        assert "Looking at" in normalized

    def test_chat_prompt_treats_direct_strategy_commands_as_explicit_updates(
        self,
    ) -> None:
        """Direct durable edit commands should trigger update_context, not a bounce-back."""
        prompt = (PROMPTS_DIR / "chat_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "Direct change requests: update, don't negotiate" in prompt
        assert "direct language to change durable context" in normalized
        assert "illustrative, not exhaustive" in normalized
        assert "explicit permission to call `update_context`" in prompt
        assert "Want me to lock this in?" in prompt
        assert "replace `## Weekly Plan`" in prompt

    def test_nudge_prompt_has_scheduled_session_carveout(self) -> None:
        """Nudge must restate today's scheduled session even when SKIP would otherwise apply."""
        prompt = (PROMPTS_DIR / "nudge_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "Scheduled-session carve-out" in normalized
        assert "session scheduled for today" in normalized
        assert "MUST restate today's session" in normalized
        assert "(`new_data`)" in normalized
        assert "missed_session" not in normalized

    def test_nudge_prompt_strategy_updated_blocks_cheerleader_acknowledgment(
        self,
    ) -> None:
        """The strategy_updated branch must SKIP on positive-only acks.

        Regression guard: previously the prompt told the model to "confirm
        it looks solid", which led to a celebratory follow-up after every
        coach accept. Manual edits should get the same silent treatment as
        accept-side coach edits unless there's concrete tension or a
        next-action correction to surface.
        """
        prompt = (PROMPTS_DIR / "nudge_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "**strategy_updated**" in normalized
        assert "not** to congratulate the change" in normalized
        assert '"looks solid"' in normalized
        assert "If the only thing you would say is positive" in normalized

    def test_notify_prompt_keeps_behavioral_rules_and_examples(self) -> None:
        prompt = (PROMPTS_DIR / "notify_prompt.md").read_text(encoding="utf-8")
        # Schema is now provided via the Pydantic NotifyResponse class; the
        # prompt no longer hand-codes it. Behavioral rules and example payloads
        # stay because they teach the LLM the supported paths/values.
        assert "Return JSON only." not in prompt
        assert '`status = "needs_clarification"`' in prompt
        assert '"action":"mute_until"' in prompt

    def test_notify_prompt_renders_with_build_messages(self) -> None:
        ctx = {
            "prompt": (PROMPTS_DIR / "notify_prompt.md").read_text(encoding="utf-8"),
            "soul": "Be strict.",
            "current_settings": "{}",
            "default_settings": "{}",
            "active_mutes": "[]",
            "notify_request": "set all as default",
            "clarification_answer": "(none)",
            "timezone": "Europe/Dublin",
        }

        msgs = build_messages(
            ctx,
            health_data_text="{}",
            week_complete=False,
            today=date(2026, 4, 4),
        )

        assert len(msgs) == 2
        assert "set all as default" in msgs[1]["content"]

    def test_weekly_report_prompt_states_report_role_and_boundaries(self) -> None:
        prompt = (PROMPTS_DIR / "insights_prompt.md").read_text(encoding="utf-8")
        assert (
            "Purpose: this is a weekly report that interprets what happened" in prompt
        )
        assert "help the user understand what happened and what to do next." in prompt
        assert "## Recent User Notes" in prompt
        assert "## Recent Coaching History" in prompt

    def test_weekly_report_prompt_requires_run_sql_before_training_review(self) -> None:
        """The Training Review template needs per-workout fields the summary
        layer does not contain. The prompt must say so explicitly."""
        prompt = (PROMPTS_DIR / "insights_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "MUST call `run_sql` before drafting the Training Review" in normalized
        assert "compact summary view" in normalized
        assert "{schema_reference}" in prompt
        assert "Correct flow:" in prompt
        assert "Wrong flow:" in prompt
        assert "Tool calls are not visible to the user" in prompt

    def test_coach_prompt_uses_recent_coaching_feedback(self) -> None:
        """Coach must read the Recent Coaching Feedback section before
        producing a review, so prior thumbs-down items inform the next one."""
        prompt = (PROMPTS_DIR / "coach_prompt.md").read_text(encoding="utf-8")
        normalized = " ".join(prompt.split())
        assert "Read Recent Coaching Feedback first" in normalized
        assert "thumbs-down" in normalized
        assert "Do not mention the feedback in your response" in normalized
        assert "## Recent Coaching Feedback" in prompt


class TestBuildMessages:
    def test_basic_structure(self) -> None:
        ctx = {
            "soul": "Be a coach.",
            "prompt": "Report for {me} on {today}. Strategy: {strategy}",
            "me": "Adam",
            "strategy": "Run more",
        }
        msgs = build_messages(ctx, health_data_text='{"data": 1}')
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "Be a coach."
        assert msgs[1]["role"] == "user"
        assert "Adam" in msgs[1]["content"]
        assert "Run more" in msgs[1]["content"]

    def test_soul_not_provided_uses_default(self) -> None:
        ctx = {"soul": "(not provided)", "prompt": "Hello"}
        msgs = build_messages(ctx, health_data_text="{}")
        assert "no-nonsense" in msgs[0]["content"]

    def test_missing_soul_uses_default(self) -> None:
        ctx = {"prompt": "Hello"}
        msgs = build_messages(ctx, health_data_text="{}")
        assert "no-nonsense" in msgs[0]["content"]

    def test_unknown_placeholder_defaults(self) -> None:
        ctx = {"prompt": "Data: {health_data}, Unknown: {unknown_key}"}
        msgs = build_messages(ctx, health_data_text='{"x":1}')
        assert "(not provided)" in msgs[1]["content"]

    def test_baselines_injected(self) -> None:
        ctx = {"prompt": "Baselines: {baselines}"}
        msgs = build_messages(ctx, health_data_text="{}", baselines="## HR: 52bpm")
        assert "## HR: 52bpm" in msgs[1]["content"]

    def test_baselines_none_shows_not_computed(self) -> None:
        ctx = {"prompt": "Baselines: {baselines}"}
        msgs = build_messages(ctx, health_data_text="{}", baselines=None)
        assert "(not computed)" in msgs[1]["content"]

    def test_milestones_injected(self) -> None:
        ctx = {"prompt": "Milestones: {milestones}"}
        msgs = build_messages(
            ctx,
            health_data_text="{}",
            milestones="## Milestones\n- 5 km PR",
        )
        assert "5 km PR" in msgs[1]["content"]

    def test_explicit_today_override_is_used(self) -> None:
        ctx = {
            "prompt": "Today: {today}; Weekday: {weekday}; Status: {week_status}",
        }
        msgs = build_messages(
            ctx,
            health_data_text="{}",
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
            health_data_text="{}",
        )
        assert "Facts: (not provided)" in msgs[1]["content"]


class TestPromptRenderers:
    def _health_data(self) -> dict:
        return {
            "current_week": {
                "summary": {
                    "week_label": "2026-W15 (2026-04-06 – 2026-04-08)",
                    "run_count": 1,
                    "lift_count": 0,
                    "walk_count": 0,
                    "total_run_km": 5.27,
                    "best_pace_min_per_km": 5.66,
                    "avg_run_hr": 152.2,
                    "avg_elevation_gain_m": 12.4,
                    "avg_steps": 5754,
                    "avg_active_energy_kj": 2101.1,
                    "avg_exercise_min": 25.5,
                    "avg_resting_hr": 53,
                    "avg_hrv_ms": 44.3,
                    "avg_recovery_index": 0.837,
                    "hrv_trend": None,
                    "avg_sleep_total_h": 6.47,
                    "avg_sleep_efficiency_pct": 77.9,
                    "avg_sleep_deep_h": 0.51,
                    "avg_sleep_core_h": 4.47,
                    "avg_sleep_rem_h": 1.49,
                    "avg_sleep_awake_h": 1.83,
                    "sleep_nights_tracked": 2,
                    "sleep_nights_total": 2,
                },
                "days": [
                    {
                        "date": "2026-04-06",
                        "steps": 9295,
                        "exercise_min": 46,
                        "hrv_ms": 45.7,
                        "resting_hr": 52,
                        "recovery_index": 0.879,
                        "sleep_status": "tracked",
                        "sleep_total_h": 8.1,
                        "sleep_efficiency_pct": 95.6,
                        "workouts": [
                            {
                                "type": "Outdoor Run",
                                "category": "run",
                                "duration_min": 29.8,
                                "gpx_distance_km": 5.27,
                                "gpx_elevation_gain_m": 12.4,
                                "hr_avg": 152.2,
                            }
                        ],
                    },
                    {
                        "date": "2026-04-07",
                        "steps": 2213,
                        "exercise_min": 5,
                        "hrv_ms": 42.91133476457976,
                        "resting_hr": 55,
                        "recovery_index": 0.7802,
                        "sleep_status": "tracked",
                        "sleep_total_h": 6.47,
                        "sleep_efficiency_pct": 77.9,
                        "workouts": [],
                    },
                    {
                        "date": "2026-04-08",
                        "steps": 1200,
                        "exercise_min": 0,
                        "hrv_ms": None,
                        "resting_hr": None,
                        "recovery_index": None,
                        "sleep_status": "pending",
                        "workouts": [],
                    },
                ],
            },
            "history": [
                {
                    "summary": {
                        "week_label": "2026-W14 (2026-03-30 – 2026-04-05)",
                        "run_count": 2,
                        "lift_count": 3,
                        "walk_count": 0,
                        "total_run_km": 11.4,
                        "avg_hrv_ms": 48.4,
                        "avg_resting_hr": 53.0,
                        "avg_sleep_total_h": None,
                    }
                }
            ],
        }

    def test_render_health_data_for_nudge_uses_compact_markdown(self) -> None:
        rendered = render_health_data(
            self._health_data(),
            prompt_kind="nudge",
            today=date(2026, 4, 8),
        )

        assert "### Today" in rendered
        assert "### Recent Days" in rendered
        assert "### Previous Weeks" in rendered
        assert "2026-W14" in rendered
        assert "42.9 ms" in rendered
        assert "0.78" in rendered
        assert "pending sync" in rendered
        assert "null" not in rendered
        assert "```json" not in rendered

    def test_render_health_data_for_chat_is_chronological_and_hides_targets(
        self,
    ) -> None:
        rendered = render_health_data(
            self._health_data(),
            prompt_kind="chat",
            today=date(2026, 4, 8),
        )

        assert "### This Week So Far" in rendered
        assert "### This Week Days (Mon to today)" in rendered
        assert "### Today" not in rendered
        assert "### Recent Days" not in rendered
        assert "/2 runs" not in rendered
        assert "/2 lifts" not in rendered
        monday_idx = rendered.index("#### Monday 6 Apr")
        tuesday_idx = rendered.index("#### Tuesday 7 Apr")
        wednesday_idx = rendered.index("#### Wednesday 8 Apr")
        assert monday_idx < tuesday_idx < wednesday_idx

    def test_render_health_data_for_report_hides_target_fractions(self) -> None:
        rendered = render_health_data(
            self._health_data(),
            prompt_kind="report",
            week="current",
            today=date(2026, 4, 8),
        )

        assert "### Target Week Summary" in rendered
        assert "/2 runs" not in rendered
        assert "/2 lifts" not in rendered
        assert "- Logged so far: 1 run, 0 lifts, 0 walks." in rendered

    def test_render_health_data_for_report_last_renders_full_target_week(self) -> None:
        rendered = render_health_data(
            self._health_data(),
            prompt_kind="report",
            week="last",
            today=date(2026, 4, 8),
        )

        assert "### Target Week Days (Mon to Sun)" in rendered
        assert "#### Monday 6 Apr" in rendered
        assert "#### Tuesday 7 Apr" in rendered
        assert "#### Wednesday 8 Apr" in rendered

    def test_render_health_data_inlines_current_week_run_splits(self) -> None:
        data = self._health_data()
        monday = data["current_week"]["days"][0]
        monday["workouts"][0]["splits"] = [
            {"km_index": 1, "pace_min_km": 5.2},
            {"km_index": 2, "pace_min_km": 5.1333},
            {"km_index": 3, "pace_min_km": 5.0833},
            {"km_index": 4, "pace_min_km": 5.3},
            {"km_index": 5, "pace_min_km": 5.4},
        ]

        rendered = render_health_data(
            data,
            prompt_kind="report",
            week="current",
            today=date(2026, 4, 8),
        )

        assert "splits 5:12/5:08/5:05/5:18/5:24" in rendered

    def test_format_recent_nudges_strips_saved_nudge_chrome(self) -> None:
        rendered = format_recent_nudges(
            [
                {
                    "ts": "2026-04-07T10:25:00",
                    "trigger": "new_data",
                    "text": (
                        "**📊 Data Sync**\n\n"
                        "Easy 5 km tomorrow.\n\n"
                        "---\n"
                        "_Generated by anthropic/claude-opus-4-6_"
                    ),
                }
            ]
        )

        assert "Data Sync" not in rendered
        assert "_Generated by" not in rendered
        assert "---" not in rendered
        assert "Easy 5 km tomorrow." in rendered


class TestBuildReviewFacts:
    def test_includes_shared_signals_and_feedback_hint(self) -> None:
        health_data = {
            "week_label": "2026-W12",
            "current_week": {
                "summary": {
                    "week_label": "2026-W12",
                    "run_count": 2,
                    "lift_count": 1,
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
        assert "Training snapshot" in result
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
        assert result.model == DEFAULT_MODEL
        assert result.cost == pytest.approx(0.000087)

    @patch("llm.litellm")
    def test_uses_provider_reported_cost_when_litellm_missing(
        self, mock_litellm: MagicMock
    ) -> None:
        response = self._mock_response()
        response.usage.cost = 0.0123
        mock_litellm.completion.return_value = response
        mock_litellm.completion_cost.side_effect = Exception("no pricing")

        result = call_llm(
            [{"role": "user", "content": "test"}],
            model="openrouter/deepseek/deepseek-v4-flash",
        )

        assert result.cost == 0.0123

    @patch("llm.litellm")
    def test_direct_deepseek_v4_cost_fallback(self, mock_litellm: MagicMock) -> None:
        response = self._mock_response(prompt_tokens=30_000, completion_tokens=5_000)
        response.usage.prompt_cache_hit_tokens = 10_000
        response.usage.prompt_cache_miss_tokens = 20_000
        mock_litellm.completion.return_value = response
        mock_litellm.completion_cost.side_effect = Exception("no pricing")

        result = call_llm(
            [{"role": "user", "content": "test"}],
            model="deepseek/deepseek-v4-flash",
        )

        assert result.cost == pytest.approx(0.004228)

    def test_direct_deepseek_v4_pro_pricing_window(self) -> None:
        response = self._mock_response(
            prompt_tokens=2_000_000, completion_tokens=1_000_000
        )
        response.usage.prompt_cache_hit_tokens = 1_000_000
        response.usage.prompt_cache_miss_tokens = 1_000_000

        discounted = _deepseek_v4_cost(
            response,
            "deepseek/deepseek-v4-pro",
            at=datetime(2026, 5, 31, 15, 58, tzinfo=UTC),
        )
        list_price = _deepseek_v4_cost(
            response,
            "deepseek/deepseek-v4-pro",
            at=datetime(2026, 5, 31, 15, 59, tzinfo=UTC),
        )

        assert discounted == pytest.approx(1.308625)
        assert list_price == pytest.approx(5.2345)

    @patch("llm.litellm")
    def test_raw_message_preserves_reasoning_content(
        self, mock_litellm: MagicMock
    ) -> None:
        """DeepSeek requires reasoning_content to be replayed after tool calls."""
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "run_sql", "arguments": '{"sql":"select 1"}'},
        }
        response = self._mock_response(text="")
        response.choices[0].message.content = ""
        response.choices[0].message.tool_calls = [tool_call]
        response.choices[0].message.model_dump.return_value = {
            "role": "assistant",
            "content": "",
            "reasoning_content": "I should query the database.",
            "tool_calls": [tool_call],
        }
        mock_litellm.completion.return_value = response
        mock_litellm.completion_cost.return_value = None

        result = call_llm([{"role": "user", "content": "test"}], tools=[])

        assert result.raw_message == {
            "role": "assistant",
            "content": "",
            "reasoning_content": "I should query the database.",
            "tool_calls": [tool_call],
        }

    @patch("llm.litellm")
    def test_reasoning_effort_omitted_for_deepseek_default(
        self, mock_litellm: MagicMock
    ) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = None

        call_llm(
            [{"role": "user", "content": "test"}],
            reasoning_effort="high",
        )
        kwargs = mock_litellm.completion.call_args[1]
        assert kwargs["model"] == DEFAULT_MODEL
        assert "reasoning_effort" not in kwargs

    @patch("llm.litellm")
    def test_reasoning_effort_passed_for_anthropic(
        self, mock_litellm: MagicMock
    ) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = None

        call_llm(
            [{"role": "user", "content": "test"}],
            model=ANTHROPIC_OPUS_MODEL,
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
    def test_deepseek_default_extra_body_applied(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = None

        call_llm(
            [{"role": "user", "content": "test"}],
            model=DEEPSEEK_FLASH_MODEL,
        )

        kwargs = mock_litellm.completion.call_args[1]
        assert kwargs["extra_body"] == DEEPSEEK_EXTRA_BODY

    @patch("llm.litellm")
    def test_explicit_extra_body_overrides_deepseek_default(
        self, mock_litellm: MagicMock
    ) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = None
        explicit = {"thinking": {"type": "enabled"}}

        call_llm(
            [{"role": "user", "content": "test"}],
            model=DEEPSEEK_FLASH_MODEL,
            extra_body=explicit,
        )

        kwargs = mock_litellm.completion.call_args[1]
        assert kwargs["extra_body"] == explicit

    @patch("llm.litellm")
    def test_deepseek_extra_body_omitted_for_anthropic_primary(
        self, mock_litellm: MagicMock
    ) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = None

        call_llm(
            [{"role": "user", "content": "test"}],
            model=ANTHROPIC_OPUS_MODEL,
        )

        kwargs = mock_litellm.completion.call_args[1]
        assert "extra_body" not in kwargs

    @patch("llm.litellm")
    def test_reasoning_effort_forces_temperature_to_one(
        self, mock_litellm: MagicMock
    ) -> None:
        """Anthropic extended thinking rejects any temperature != 1, so the
        caller's temperature must be overridden when reasoning is enabled."""
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = None

        call_llm(
            [{"role": "user", "content": "test"}],
            model=ANTHROPIC_OPUS_MODEL,
            temperature=0.7,
            reasoning_effort="medium",
        )
        kwargs = mock_litellm.completion.call_args[1]
        assert kwargs["temperature"] == 1.0
        assert kwargs["reasoning_effort"] == "medium"

    @patch("llm.litellm")
    def test_temperature_preserved_without_reasoning(
        self, mock_litellm: MagicMock
    ) -> None:
        """When reasoning is off, the caller's temperature must pass through
        unchanged — the override only kicks in for extended thinking."""
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = None

        call_llm(
            [{"role": "user", "content": "test"}],
            temperature=0.7,
        )
        kwargs = mock_litellm.completion.call_args[1]
        assert kwargs["temperature"] == 0.7

    @patch("llm.litellm")
    def test_temperature_omitted_when_none(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = None

        call_llm(
            [{"role": "user", "content": "test"}],
            model=ANTHROPIC_OPUS_4_7_MODEL,
            temperature=None,
        )

        kwargs = mock_litellm.completion.call_args[1]
        assert "temperature" not in kwargs

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
        params = json.loads(row["params_json"])
        assert params["extra_body"] == DEEPSEEK_EXTRA_BODY

    @patch("llm.litellm")
    def test_anthropic_logging_omits_implicit_deepseek_extra_body(
        self, mock_litellm: MagicMock, in_memory_db: sqlite3.Connection
    ) -> None:
        mock_litellm.completion.return_value = self._mock_response()
        mock_litellm.completion_cost.return_value = 0.02

        call_llm(
            [{"role": "user", "content": "test"}],
            model=ANTHROPIC_OPUS_MODEL,
            conn=in_memory_db,
            request_type="insights",
        )

        row = in_memory_db.execute("SELECT * FROM llm_call").fetchone()
        params = json.loads(row["params_json"])
        assert "extra_body" not in params
        assert "requested_extra_body" not in params

    @patch("llm.litellm")
    def test_logs_deepseek_injected_schema_messages(
        self, mock_litellm: MagicMock, in_memory_db: sqlite3.Connection
    ) -> None:
        class TestSchema(BaseModel):
            value: str

        original_messages = [{"role": "user", "content": "test"}]
        mock_litellm.completion.return_value = self._mock_response(
            '{"value":"ok"}'
        )
        mock_litellm.completion_cost.return_value = 0.02

        call_llm(
            original_messages,
            model=DEEPSEEK_PRO_MODEL,
            response_format=TestSchema,
            conn=in_memory_db,
            request_type="insights",
        )

        row = in_memory_db.execute("SELECT * FROM llm_call").fetchone()
        logged_messages = json.loads(row["messages_json"])
        params = json.loads(row["params_json"])

        assert original_messages == [{"role": "user", "content": "test"}]
        assert logged_messages[0]["role"] == "system"
        assert "TestSchema" in logged_messages[0]["content"]
        assert "value" in logged_messages[0]["content"]
        assert logged_messages[1] == {"role": "user", "content": "test"}
        assert params["response_format"] == {"type": "json_object"}
        assert params["pydantic_schema_injected"]["name"] == "TestSchema"

    @patch("llm.litellm")
    def test_logs_requested_model_when_fallback_used(
        self, mock_litellm: MagicMock, in_memory_db: sqlite3.Connection
    ) -> None:
        mock_litellm.completion.side_effect = [
            Exception("anthropic unavailable"),
            self._mock_response(),
        ]
        mock_litellm.completion_cost.return_value = 0.02

        call_llm(
            [{"role": "user", "content": "test"}],
            model=ANTHROPIC_OPUS_MODEL,
            conn=in_memory_db,
            request_type="insights",
            metadata={"week": "last"},
        )
        row = in_memory_db.execute("SELECT * FROM llm_call").fetchone()
        assert row["model"] == DEEPSEEK_PRO_MODEL

        params = json.loads(row["params_json"])
        metadata = json.loads(row["metadata_json"])
        assert params["extra_body"] == DEEPSEEK_EXTRA_BODY
        assert params["requested_model"] == ANTHROPIC_OPUS_MODEL
        assert params["fallback_used"] is True
        assert metadata["requested_model"] == ANTHROPIC_OPUS_MODEL
        assert metadata["effective_model"] == DEEPSEEK_PRO_MODEL
        assert metadata["fallback_used"] is True
        assert metadata["week"] == "last"

    @patch("llm.litellm")
    def test_explicit_fallback_chain_is_used(self, mock_litellm: MagicMock) -> None:
        mock_litellm.completion.side_effect = [
            Exception("primary unavailable"),
            self._mock_response("fallback ok"),
        ]
        mock_litellm.completion_cost.return_value = None

        result = call_llm(
            [{"role": "user", "content": "test"}],
            model=ANTHROPIC_OPUS_4_7_MODEL,
            fallback_models=[DEEPSEEK_PRO_MODEL],
        )

        seen_models = [
            call.kwargs["model"] for call in mock_litellm.completion.call_args_list
        ]
        assert seen_models == [ANTHROPIC_OPUS_4_7_MODEL, DEEPSEEK_PRO_MODEL]
        assert "extra_body" not in mock_litellm.completion.call_args_list[0].kwargs
        assert (
            mock_litellm.completion.call_args_list[1].kwargs["extra_body"]
            == DEEPSEEK_EXTRA_BODY
        )
        assert result.model == DEEPSEEK_PRO_MODEL

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
    def test_deepseek_pro_falls_back_to_opus_after_all_retries(
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
        resp, model = _call_with_retry(
            {"model": DEEPSEEK_PRO_MODEL}, DEEPSEEK_PRO_MODEL
        )
        assert model == FALLBACK_MODEL
        assert model == ANTHROPIC_OPUS_MODEL
        # 3 delays for primary retries.
        assert mock_sleep.call_count == 3

    @patch("llm.time.sleep")
    @patch("llm.litellm")
    def test_non_overloaded_error_uses_cross_provider_fallback(
        self, mock_litellm, mock_sleep
    ) -> None:
        mock_litellm.completion.side_effect = [
            Exception("authentication failed"),
            self._mock_response("fallback ok"),
        ]
        resp, model = _call_with_retry(
            {"model": ANTHROPIC_OPUS_MODEL}, ANTHROPIC_OPUS_MODEL
        )
        assert resp.choices[0].message.content == "fallback ok"
        assert model == DEEPSEEK_PRO_MODEL
        mock_sleep.assert_not_called()

    @patch("llm.time.sleep")
    @patch("llm.litellm")
    def test_raises_after_all_providers_fail(self, mock_litellm, mock_sleep) -> None:
        mock_litellm.completion.side_effect = Exception("authentication failed")
        with pytest.raises(Exception, match="authentication failed"):
            _call_with_retry({"model": ANTHROPIC_OPUS_MODEL}, ANTHROPIC_OPUS_MODEL)
        mock_sleep.assert_not_called()

    @patch("llm.time.sleep")
    @patch("llm.litellm")
    def test_raises_if_fallback_exhausted(self, mock_litellm, mock_sleep) -> None:
        # All calls overloaded — should eventually raise.
        mock_litellm.completion.side_effect = self._overloaded_error()
        with pytest.raises(Exception, match="overloaded_error"):
            _call_with_retry({"model": "m"}, "m")

    def test_fallback_chain_pairs_budget_models(self) -> None:
        assert _fallback_chain(PRIMARY_FLASH_MODEL) == [
            PRIMARY_FLASH_MODEL,
            FALLBACK_FLASH_MODEL,
        ]
        assert _fallback_chain(FALLBACK_FLASH_MODEL) == [
            FALLBACK_FLASH_MODEL,
            PRIMARY_FLASH_MODEL,
        ]

    def test_deepseek_attempt_omits_reasoning_effort(self) -> None:
        kwargs = _completion_kwargs_for_model(
            {
                "model": DEEPSEEK_PRO_MODEL,
                "messages": [],
                "max_tokens": 10,
                "temperature": 0.7,
                "reasoning_effort": "medium",
            },
            DEEPSEEK_PRO_MODEL,
        )

        assert kwargs["model"] == DEEPSEEK_PRO_MODEL
        assert kwargs["temperature"] == 0.7
        assert "reasoning_effort" not in kwargs

    def test_deepseek_attempt_keeps_response_format(self) -> None:
        response_format = {"type": "json_object"}

        kwargs = _completion_kwargs_for_model(
            {
                "model": DEEPSEEK_PRO_MODEL,
                "messages": [],
                "max_tokens": 10,
                "response_format": response_format,
            },
            DEEPSEEK_PRO_MODEL,
        )

        assert kwargs["response_format"] == response_format

    def test_deepseek_attempt_keeps_extra_body(self) -> None:
        extra_body = {"thinking": {"type": "disabled"}}

        kwargs = _completion_kwargs_for_model(
            {
                "model": DEEPSEEK_PRO_MODEL,
                "messages": [],
                "max_tokens": 10,
                "extra_body": extra_body,
            },
            DEEPSEEK_PRO_MODEL,
        )

        assert kwargs["extra_body"] == extra_body

    def test_anthropic_attempt_keeps_response_format(self) -> None:
        kwargs = _completion_kwargs_for_model(
            {
                "model": DEEPSEEK_PRO_MODEL,
                "messages": [],
                "max_tokens": 10,
                "response_format": {"type": "json_object"},
            },
            ANTHROPIC_OPUS_MODEL,
        )

        assert kwargs["response_format"] == {"type": "json_object"}

    def test_anthropic_attempt_keeps_pydantic_response_format(self) -> None:
        class TestSchema(BaseModel):
            value: str

        kwargs = _completion_kwargs_for_model(
            {
                "model": DEEPSEEK_PRO_MODEL,
                "messages": [],
                "max_tokens": 10,
                "response_format": TestSchema,
            },
            ANTHROPIC_OPUS_MODEL,
        )

        assert kwargs["response_format"] is TestSchema

    def test_deepseek_attempt_downgrades_pydantic_to_json_object_with_hint(
        self,
    ) -> None:
        class VerdictSchema(BaseModel):
            verdict: str

        original_messages = [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Is sky blue?"},
        ]

        kwargs = _completion_kwargs_for_model(
            {
                "model": DEEPSEEK_PRO_MODEL,
                "messages": original_messages,
                "max_tokens": 10,
                "response_format": VerdictSchema,
            },
            DEEPSEEK_PRO_MODEL,
        )

        assert kwargs["response_format"] == {"type": "json_object"}
        assert "VerdictSchema" in kwargs["messages"][0]["content"]
        assert "verdict" in kwargs["messages"][0]["content"]
        # Original messages list must not be mutated.
        assert original_messages[0] == {"role": "system", "content": "Be concise."}
        assert kwargs["messages"][1] == {"role": "user", "content": "Is sky blue?"}

    def test_deepseek_attempt_inserts_system_when_missing(self) -> None:
        class S(BaseModel):
            x: int

        kwargs = _completion_kwargs_for_model(
            {
                "model": DEEPSEEK_PRO_MODEL,
                "messages": [{"role": "user", "content": "What?"}],
                "max_tokens": 10,
                "response_format": S,
            },
            DEEPSEEK_PRO_MODEL,
        )

        assert kwargs["messages"][0]["role"] == "system"
        assert "x" in kwargs["messages"][0]["content"]
        assert kwargs["messages"][1] == {"role": "user", "content": "What?"}

    def test_anthropic_attempt_omits_extra_body(self) -> None:
        kwargs = _completion_kwargs_for_model(
            {
                "model": DEEPSEEK_PRO_MODEL,
                "messages": [],
                "max_tokens": 10,
                "extra_body": {"thinking": {"type": "disabled"}},
            },
            ANTHROPIC_OPUS_MODEL,
        )

        assert "extra_body" not in kwargs

    def test_anthropic_attempt_keeps_reasoning_effort_and_temperature_one(self) -> None:
        kwargs = _completion_kwargs_for_model(
            {
                "model": DEFAULT_MODEL,
                "messages": [],
                "max_tokens": 10,
                "temperature": 0.7,
                "reasoning_effort": "medium",
            },
            ANTHROPIC_OPUS_MODEL,
        )

        assert kwargs["model"] == ANTHROPIC_OPUS_MODEL
        assert kwargs["temperature"] == 1.0
        assert kwargs["reasoning_effort"] == "medium"


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

    @patch("llm_health.date")
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

    @patch("llm_health.date")
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

    @patch("llm_health.date")
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

    @patch("llm_health.datetime")
    @patch("llm_health.date")
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

    @patch("llm_health.datetime")
    @patch("llm_health.date")
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

    @patch("llm_health.datetime")
    @patch("llm_health.date")
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

    @patch("llm_health.datetime")
    @patch("llm_health.date")
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

    @patch("llm_health.datetime")
    @patch("llm_health.date")
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

    @patch("llm_health.datetime")
    @patch("llm_health.date")
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
        run_workout = today["workouts"][0]
        assert "counts_as_lift" in run_workout
        # Run-identifying fields needed for the LLM to recognise tempo efforts.
        assert run_workout["distance_km"] == 5.2
        assert run_workout["pace_min_per_km"] == round(35.0 / 5.2, 2)
        assert run_workout["avg_hr"] == 155.0
        assert run_workout["elevation_gain_m"] == 45.0
