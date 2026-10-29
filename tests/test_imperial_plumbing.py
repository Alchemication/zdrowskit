"""Units must travel from a Profile to every renderer a person reads."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import main as main_module
from cmd_targets import _provenance_line
from daemon import ProfileRuntime
from goal_check import build_goal_check
from llm_health import build_review_facts
from models import DailySnapshot, WorkoutSnapshot
from profiles import Profile
from store import store_snapshots
from telegram_progress import handle_targets
from units import KM_PER_MILE, UnitSystem
from weekly_progress import (
    targets_completed_by,
    weekly_progress_nudge_line,
)
from weekly_targets import (
    SPEC_BY_KEY,
    StoredTarget,
    extract_goal_text,
    goals_digest,
    save_targets,
)

M = UnitSystem.METRIC
IMP = UnitSystem.IMPERIAL
TEN_MILES = 10 * KM_PER_MILE


def _distance_target(km: float) -> StoredTarget:
    return StoredTarget(
        spec=SPEC_BY_KEY["distance_km_week"],
        category="run",
        target=km,
        threshold=None,
        goal_text="run 10 miles a week",
        strategy_hash="h",
        llm_call_id=None,
    )


def _run(day: str, km: float, hour: int = 7) -> DailySnapshot:
    return DailySnapshot(
        date=day,
        workouts=[
            WorkoutSnapshot(
                type="Outdoor Run",
                category="run",
                start_utc=f"{day}T{hour:02d}:00:00Z",
                duration_min=40.0,
                gpx_distance_km=km,
            )
        ],
    )


class TestGoalCheckUnits:
    WEEKS = ["2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21"]
    TODAY = date(2026, 9, 28)

    def _seed(self, conn: sqlite3.Connection) -> None:
        for week in self.WEEKS:
            save_targets(conn, week, [_distance_target(TEN_MILES)])
            store_snapshots(conn, [_run(week, 5.0)])

    def test_metric_and_imperial_text(self, in_memory_db: sqlite3.Connection) -> None:
        self._seed(in_memory_db)
        metric = build_goal_check(in_memory_db, None, today=self.TODAY).text
        imperial = build_goal_check(
            in_memory_db, None, today=self.TODAY, units=IMP
        ).text
        assert "Run km (16.1 km/week)" in metric
        assert "Run mi (10 mi/week)" in imperial
        assert "W36 3.1/10" in imperial
        assert "km" not in imperial

    def test_met_is_decided_on_stored_values(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        self._seed(in_memory_db)
        a = build_goal_check(in_memory_db, None, today=self.TODAY)
        b = build_goal_check(in_memory_db, None, today=self.TODAY, units=IMP)
        assert [t.key for t in a.triggers] == [t.key for t in b.triggers]
        assert "met 0 of 4" in b.text


class TestProgressLinesUnits:
    WEDNESDAY = date(2026, 9, 2)
    WEEK = "2026-08-31"
    STRATEGY = "## Goals\n- run 10 miles a week\n"

    @pytest.fixture(autouse=True)
    def _seed(
        self, in_memory_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "weekly_targets.derive_targets",
            lambda *a, **k: pytest.fail("cached targets must not re-derive"),
        )
        save_targets(in_memory_db, self.WEEK, [_distance_target(TEN_MILES)])
        digest = goals_digest(extract_goal_text(self.STRATEGY))
        in_memory_db.execute("UPDATE weekly_target SET strategy_hash = ?", (digest,))
        in_memory_db.commit()
        store_snapshots(
            in_memory_db,
            [_run("2026-09-01", TEN_MILES / 2), _run("2026-09-02", TEN_MILES / 2)],
        )

    def test_targets_completed_by_in_miles(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        ids = {"2026-09-02T07:00:00Z"}
        kwargs = dict(strategy_md=self.STRATEGY, workout_ids=ids, today=self.WEDNESDAY)
        assert targets_completed_by(in_memory_db, **kwargs) == (
            f"- Run km: {TEN_MILES:.1f}/{TEN_MILES:.1f} km — completed by this sync."
        )
        assert targets_completed_by(in_memory_db, units=IMP, **kwargs) == (
            "- Run mi: 10/10 mi — completed by this sync."
        )

    def test_nudge_line_in_miles(self, in_memory_db: sqlite3.Connection) -> None:
        shown = weekly_progress_nudge_line(
            in_memory_db,
            strategy_md=self.STRATEGY,
            today=self.WEDNESDAY,
            units=IMP,
        )
        assert shown is not None
        line, _ = shown
        assert "Run mi" in line
        assert "km" not in line


class TestReviewFactsUnits:
    HEALTH = {
        "week_label": "W38",
        "current_week": {
            "summary": {"week_label": "W38", "run_count": 2, "total_run_km": 16.09344}
        },
        "history": [{"summary": {"total_run_km": 32.18688}}],
    }

    def test_metric(self) -> None:
        text = build_review_facts(self.HEALTH, week_complete=True)
        assert "**16.1 km** run volume" in text
        assert "run volume -16.1 km" in text

    def test_imperial(self) -> None:
        text = build_review_facts(self.HEALTH, week_complete=True, units=IMP)
        assert "**10.0 mi** run volume" in text
        assert "run volume -10.0 mi" in text


class TestProvenanceLine:
    def test_units(self) -> None:
        assert _provenance_line(_distance_target(TEN_MILES), M).startswith(
            "  distance_km_week/run: 16.0934 km"
        )
        assert "distance_km_week/run: 10 mi" in _provenance_line(
            _distance_target(TEN_MILES), IMP
        )


class TestTelegramTargetsUnits:
    def _chat(self, tmp_path: Path, units: UnitSystem) -> SimpleNamespace:
        context = tmp_path / "context"
        context.mkdir()
        daemon = SimpleNamespace(
            db=tmp_path / "health.db",
            context_dir=context,
            model_prefs_path=None,
            units=units,
        )
        return SimpleNamespace(_daemon=daemon, _poller=MagicMock())

    @pytest.mark.parametrize(
        ("units", "label", "example"),
        [(M, "Run km", "20 km"), (IMP, "Run mi", "12 mi")],
    )
    def test_reply(self, tmp_path: Path, units, label: str, example: str) -> None:
        chat = self._chat(tmp_path, units)
        with patch(
            "telegram_progress.ensure_weekly_targets",
            return_value=[_distance_target(TEN_MILES)],
        ):
            handle_targets(chat, "", 5)
        text = chat._poller.send_reply.call_args.args[0]
        assert label in text
        assert example in text
        if units is IMP:
            assert "km" not in text


class TestUnitsPlumbing:
    def test_runtime_takes_units_from_profile(self, tmp_path: Path) -> None:
        profile = Profile(
            name="me",
            telegram_id=1,
            root=tmp_path / "p",
            operator=True,
            units=IMP,
        )
        runtime = ProfileRuntime(None, tmp_path / "h.db", tmp_path, profile=profile)
        assert runtime.units is IMP
        assert ProfileRuntime(None, tmp_path / "h.db", tmp_path).units is M

    def test_main_sets_args_units_from_resolved_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = Profile(
            name="me", telegram_id=1, root=tmp_path / "p", operator=True, units=IMP
        )
        seen: dict[str, object] = {}

        def fake_report(args) -> None:
            seen["units"] = args.units

        db = tmp_path / "h.db"
        monkeypatch.setattr(
            main_module, "resolve_cli_profile", lambda *a, **k: (profile, db)
        )
        monkeypatch.setattr(main_module, "cmd_report", fake_report)
        monkeypatch.setattr("sys.argv", ["main.py", "report"])
        main_module.main()
        assert seen["units"] is IMP

    def test_every_runner_namespace_carries_runtime_units(self, tmp_path: Path) -> None:
        profile = Profile(
            name="me", telegram_id=1, root=tmp_path / "p", operator=True, units=IMP
        )
        runtime = ProfileRuntime(
            None,
            tmp_path / "h.db",
            tmp_path,
            profile=profile,
            state_path=tmp_path / "state.json",
        )
        captured: dict[str, object] = {}

        def spy(name: str):
            def inner(args, *a, **k):
                captured[name] = args.units
                raise RuntimeError("stop after capture")

            return inner

        patches = [
            patch("commands.cmd_import", side_effect=spy("import")),
            patch("cmd_insights.cmd_insights", side_effect=spy("insights")),
            patch("cmd_nudge.cmd_nudge", side_effect=spy("nudge")),
            patch("cmd_coach.cmd_coach", side_effect=spy("coach")),
            # The nudge runner checks quiet hours, the earliest send time and
            # the rate limit against the wall clock before it reaches
            # cmd_nudge; pin them open so the capture doesn't depend on when
            # the suite runs.
            patch(
                "notification_prefs.evaluate_nudge_delivery",
                return_value={"status": "allowed"},
            ),
            patch.object(
                runtime._runners,
                "_check_nudge_rate_limit",
                return_value=(True, None, None),
            ),
        ]
        for p in patches:
            p.start()
        weekly_patches = (
            patch.object(runtime, "_load_notification_prefs", return_value={}),
            patch(
                "notification_prefs.evaluate_report_delivery",
                return_value={"status": "allowed"},
            ),
            patch.object(runtime, "_run_import"),
            patch.object(runtime._runners, "_can_send_report", return_value=True),
            patch.object(runtime._runners, "_missing_week_end", return_value=None),
        )
        calls = [
            lambda: runtime._runners._run_import(),
            lambda: runtime._runners._run_review(skip_import=True),
            lambda: runtime._runners._run_nudge("new_data"),
            lambda: runtime._runners._run_coach(skip_import=True, force=True),
        ]
        try:
            for call in calls:
                try:
                    call()
                except BaseException:  # noqa: BLE001 - only the captured args matter
                    pass
            captured["review"] = captured.pop("insights", None)
            for p in weekly_patches:
                p.start()
            try:
                runtime._runners._run_weekly_report()
            except BaseException:  # noqa: BLE001
                pass
            weekly_units = captured.pop("insights", None)
        finally:
            patch.stopall()
        captured["weekly_report"] = weekly_units
        assert captured == {
            "import": IMP,
            "review": IMP,
            "nudge": IMP,
            "coach": IMP,
            "weekly_report": IMP,
        }
