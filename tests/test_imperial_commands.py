"""Commands and chat must hand the profile's units to every renderer they call.

Each test wraps the real renderers with spies, runs the command with an
imperial profile, and checks every recorded call carried imperial units. The
spies also have to have been called, so a renamed callee cannot pass vacuously.
"""

from __future__ import annotations

import sqlite3
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import baselines
import challenges
import llm_context
import llm_health
from cmd_coach import cmd_coach
from cmd_insights import cmd_insights
from cmd_nudge import cmd_nudge
from daemon import ProfileRuntime
from llm import LLMResult
from units import UnitSystem

IMP = UnitSystem.IMPERIAL

_HEALTH = {
    "current_week": {"summary": {"week_label": "2026-W14"}, "days": []},
    "history": [],
    "week_complete": True,
    "week_label": "2026-W14",
}


class _Stop(Exception):
    """Raised by the stubbed LLM to end a run once messages are built."""


def _result(text: str) -> LLMResult:
    return LLMResult(
        text=text,
        model="test-model",
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        latency_s=0.1,
        llm_call_id=1,
    )


def _spy(stack: ExitStack, target: str) -> MagicMock:
    """Wrap a real callable so it still runs but its calls are recorded."""
    module_name, _, attr = target.rpartition(".")
    module = __import__(module_name)
    for part in module_name.split(".")[1:]:
        module = getattr(module, part)
    return stack.enter_context(patch(target, wraps=getattr(module, attr)))


def _assert_imperial(spies: dict[str, MagicMock]) -> None:
    for name, spy in spies.items():
        assert spy.call_count >= 1, f"{name} was never called"
        for call in spy.call_args_list:
            passed = call.kwargs.get("units") is IMP or IMP in call.args
            assert passed, f"{name} did not receive imperial units: {call}"


def _spies(stack: ExitStack, targets: list[str]) -> dict[str, MagicMock]:
    return {target: _spy(stack, target) for target in targets}


class TestCmdNudgeUnits:
    def test_every_renderer_gets_the_profile_units(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        args = SimpleNamespace(
            db="ignored.db",
            model=None,
            months=1,
            trigger="new_data",
            telegram=False,
            units=IMP,
            standout_workout_ids=["2026-09-01T07:00:00Z"],
        )
        with ExitStack() as stack:
            spies = _spies(
                stack,
                [
                    "cmd_nudge.render_health_data",
                    "cmd_nudge.find_standout",
                    "cmd_nudge.describe_run_comparisons",
                    "cmd_nudge.targets_completed_by",
                    "cmd_nudge.nudge_challenge_text",
                    "cmd_nudge.build_messages",
                    "cmd_nudge.weekly_progress_nudge_line",
                ],
            )
            stack.enter_context(
                patch(
                    "cmd_nudge.load_context", return_value={"prompt": "x", "soul": "y"}
                )
            )
            stack.enter_context(patch("cmd_nudge.open_db", return_value=in_memory_db))
            stack.enter_context(patch("cmd_nudge.build_llm_data", return_value=_HEALTH))
            stack.enter_context(
                patch("cmd_nudge.call_llm", return_value=_result("**Rest.** Recover."))
            )
            stack.enter_context(patch("tools.run_sql_tool", return_value=[]))
            stack.enter_context(patch("cmd_nudge._save_nudge"))
            stack.enter_context(patch("cmd_nudge.send_telegram", return_value=1))
            cmd_nudge(args)
        _assert_imperial(spies)


class TestCmdCoachUnits:
    def test_every_renderer_gets_the_profile_units(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        args = SimpleNamespace(
            db="ignored.db", model="test-model", week="current", months=3, units=IMP
        )
        with ExitStack() as stack:
            spies = _spies(
                stack,
                [
                    "cmd_coach.compute_baselines",
                    "cmd_coach.compute_milestones",
                    "cmd_coach.build_review_facts",
                    "cmd_coach.build_goal_check",
                    "cmd_coach.challenge_status_text",
                    "cmd_coach.describe_history",
                    "cmd_coach.render_health_data",
                    "cmd_coach.build_messages",
                    "cmd_coach._format_coach_bundle",
                    "cmd_coach._challenge_evidence",
                ],
            )
            stack.enter_context(
                patch(
                    "cmd_coach.load_context", return_value={"prompt": "x", "soul": "y"}
                )
            )
            stack.enter_context(patch("cmd_coach.open_db", return_value=in_memory_db))
            stack.enter_context(patch("cmd_coach.save_baselines"))
            stack.enter_context(patch("cmd_coach.build_llm_data", return_value=_HEALTH))
            stack.enter_context(
                patch(
                    "cmd_coach.call_llm", return_value=_result("## W14 Review\n\nFine.")
                )
            )
            stack.enter_context(patch("tools.run_sql_tool", return_value=[]))
            cmd_coach(args)
        _assert_imperial(spies)


class TestCmdInsightsUnits:
    def test_every_renderer_gets_the_profile_units(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        args = SimpleNamespace(
            db="ignored.db",
            model="test-model",
            months=1,
            week="last",
            no_update_baselines=False,
            no_update_history=True,
            explain=False,
            telegram=False,
            units=IMP,
        )
        with ExitStack() as stack:
            spies = _spies(
                stack,
                [
                    "cmd_insights.compute_baselines",
                    "cmd_insights.compute_milestones",
                    "cmd_insights.build_review_facts",
                    "cmd_insights.challenge_status_text",
                    "cmd_insights.render_health_data",
                    "cmd_insights.weekly_progress_block",
                    "cmd_insights._week_run_comparisons",
                    "cmd_insights.build_messages",
                ],
            )
            stack.enter_context(
                patch(
                    "cmd_insights.load_context",
                    return_value={"prompt": "x", "soul": "y"},
                )
            )
            stack.enter_context(
                patch("cmd_insights.open_db", return_value=in_memory_db)
            )
            stack.enter_context(patch("cmd_insights.save_baselines"))
            stack.enter_context(
                patch(
                    "cmd_insights.build_llm_data",
                    return_value={
                        **_HEALTH,
                        "week_start": "2026-09-28",
                        "week_end": "2026-10-04",
                    },
                )
            )
            stack.enter_context(
                patch(
                    "cmd_insights.call_llm", return_value=_result("W14 Review: fine.")
                )
            )
            stack.enter_context(patch("tools.run_sql_tool", return_value=[]))
            stack.enter_context(
                patch("cmd_insights._save_report", return_value=Path("/tmp/r.md"))
            )
            cmd_insights(args)
        _assert_imperial(spies)


class TestChatReplyUnits:
    def test_every_renderer_gets_the_runtime_units(self, tmp_path: Path) -> None:
        daemon = ProfileRuntime(
            "test-model",
            tmp_path / "test.db",
            tmp_path,
            state_path=tmp_path / "state.json",
        )
        daemon.units = IMP
        daemon.model_prefs_path = tmp_path / "model_prefs.json"
        daemon._chat._conversation = MagicMock()
        daemon._chat._conversation.to_messages.return_value = []
        from store import open_db

        conn = open_db(daemon.db)
        with ExitStack() as stack:
            spies = {
                "compute_baselines": stack.enter_context(
                    patch(
                        "baselines.compute_baselines", wraps=baselines.compute_baselines
                    )
                ),
                "render_health_data": stack.enter_context(
                    patch(
                        "llm_health.render_health_data",
                        wraps=llm_health.render_health_data,
                    )
                ),
                "build_messages": stack.enter_context(
                    patch(
                        "llm_context.build_messages", wraps=llm_context.build_messages
                    )
                ),
                "challenge_status_text": stack.enter_context(
                    patch(
                        "challenges.challenge_status_text",
                        wraps=challenges.challenge_status_text,
                    )
                ),
            }
            stack.enter_context(patch("llm.call_llm", side_effect=_Stop))
            with pytest.raises(_Stop):
                daemon._chat._chat_reply(conn)
        conn.close()
        _assert_imperial(spies)
