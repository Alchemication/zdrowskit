"""Imperial display variants of every renderer; metric stays the default."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from baselines import compute_baselines
from challenges import (
    coerce_proposal,
    describe_active,
    describe_challenge,
    propose,
    propose_challenge_tool,
    accept,
)
from config import STANDOUT_MIN_POPULATION, STANDOUT_VOLUME_STEP_MI
from daemon_data import format_data_delta
from llm_context import build_messages
from llm_health import _format_workout_line, render_health_data
from milestones import compute_milestones
from models import DailySnapshot, WorkoutSnapshot
from report import print_daily, print_summary
from run_comparison import describe_run_comparisons
from standouts import find_candidates
from store import open_db, store_snapshots
from units import KM_PER_MILE, UnitSystem
from weekly_progress import display_ring, render_progress_block
from weekly_targets import (
    SPEC_BY_KEY,
    StoredTarget,
    goals_digest,
    parse_targets_response,
)
from aggregator import summarise

M = UnitSystem.METRIC
IMP = UnitSystem.IMPERIAL
TODAY = date(2026, 9, 23)


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _run_snapshots(first_offset: int, count: int, km: float = 5.0) -> list:
    out = []
    for i in range(count):
        d = _days_ago(first_offset + i)
        out.append(
            DailySnapshot(
                date=d,
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc=f"{d}T07:00:00Z",
                        duration_min=30.0,
                        gpx_distance_km=km,
                        hr_avg=150.0 + i,
                    )
                ],
            )
        )
    return out


class TestHealthDataRendering:
    WORKOUT = {
        "type": "Outdoor Run",
        "category": "run",
        "gpx_distance_km": 10.0,
        "duration_min": 50.0,
        "gpx_elevation_gain_m": 100.0,
        "splits": [{"pace_min_km": 5.0}, {"pace_min_km": 5.0}],
    }

    def test_metric_workout_line_is_unchanged(self) -> None:
        line = _format_workout_line(self.WORKOUT)
        assert "10.0 km" in line
        assert "5:00/km" in line
        assert "elev +100 m" in line
        assert "splits 5:00/5:00" in line

    def test_imperial_workout_line(self) -> None:
        line = _format_workout_line(self.WORKOUT, units=IMP)
        assert "6.2 mi" in line
        assert "8:03/mi" in line
        assert "elev +328 ft" in line
        assert "km splits (pace per mi) 8:03/8:03" in line
        assert "km;" not in line

    def test_summary_and_day_blocks(self) -> None:
        health = {
            "current_week": {
                "summary": {
                    "week_label": "2026-W38",
                    "run_count": 1,
                    "total_run_km": 10.0,
                    "best_pace_min_per_km": 5.0,
                    "avg_elevation_gain_m": 100.0,
                    "avg_active_energy_kj": 2092.0,
                },
                "days": [
                    {
                        "date": "2026-09-21",
                        "distance_km": 10.0,
                        "workouts": [self.WORKOUT],
                    }
                ],
            },
            "history": [{"summary": {"week_label": "W37", "total_run_km": 20.0}}],
        }
        metric = render_health_data(health, prompt_kind="chat", today=TODAY)
        imperial = render_health_data(
            health, prompt_kind="chat", today=TODAY, units=IMP
        )
        assert "10.0 km total" in metric
        assert "2092 kJ/day" in metric
        assert "6.2 mi total" in imperial
        assert "best pace 8:03/mi" in imperial
        assert "avg elev 328 ft" in imperial
        assert "500 kcal/day" in imperial
        assert "Activity: 6.2 mi" in imperial
        assert "12.4 mi" in imperial  # history week
        assert " km" not in imperial.replace("km splits", "")


class TestBaselinesAndMilestones:
    def test_best_recent_pace_in_miles(self, in_memory_db: sqlite3.Connection) -> None:
        store_snapshots(in_memory_db, _run_snapshots(5, 5))
        metric = compute_baselines(in_memory_db)
        imperial = compute_baselines(in_memory_db, units=IMP)
        assert "6:00/km" in metric
        assert "runs of 3 km or more" in metric
        assert "9:39/mi" in imperial
        assert "runs of 1.9 mi or more" in imperial

    def test_pace_curve_label_keeps_race_distance(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        store_snapshots(in_memory_db, _run_snapshots(5, 5))
        assert "/km" not in compute_baselines(in_memory_db, units=IMP)

    def test_lifetime_volume_and_pr_pace(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        store_snapshots(in_memory_db, _run_snapshots(5, 4, km=10.0))
        in_memory_db.execute(
            "INSERT INTO workout_split (start_utc, km_index, pace_min_km) "
            "SELECT start_utc, 1, 5.0 FROM workout LIMIT 1"
        )
        in_memory_db.commit()
        assert "**40.0 km**" in compute_milestones(in_memory_db)
        imperial = compute_milestones(in_memory_db, units=IMP)
        assert "**24.9 mi**" in imperial
        assert "1 km PR: **8:03/mi**" in imperial


class TestRunComparison:
    def test_imperial_head_and_band(self, in_memory_db: sqlite3.Connection) -> None:
        today = date(2026, 9, 23)
        snaps = [
            DailySnapshot(
                date=(today - timedelta(days=2 + i)).isoformat(),
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc=f"{(today - timedelta(days=2 + i)).isoformat()}T07:00:00Z",
                        duration_min=29.0,
                        gpx_distance_km=5.0,
                        hr_avg=150.0 + i,
                    )
                ],
            )
            for i in range(5)
        ]
        iso = today.isoformat()
        snaps.append(
            DailySnapshot(
                date=iso,
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc=f"{iso}T07:00:00Z",
                        duration_min=29.0,
                        gpx_distance_km=5.0,
                        hr_avg=160.0,
                    )
                ],
            )
        )
        store_snapshots(in_memory_db, snaps)
        ids = {f"{iso}T07:00:00Z"}
        assert "5.0 km at 5:48/km" in describe_run_comparisons(in_memory_db, ids)
        text = describe_run_comparisons(in_memory_db, ids, units=IMP)
        assert "3.1 mi at 9:20/mi" in text
        assert " mi," in text and "/mi)" in text
        assert "/km" not in text


class TestReport:
    def _summary(self):
        snaps = _run_snapshots(3, 2, km=10.0)
        return snaps, summarise(snaps)

    def test_metric_default_unchanged(self, capsys: pytest.CaptureFixture) -> None:
        snaps, summary = self._summary()
        print_summary(snaps, summary)
        print_daily(snaps)
        out = capsys.readouterr().out
        assert "Total run km:      20.0 km" in out
        assert "10.00 km" in out

    def test_imperial_output(self, capsys: pytest.CaptureFixture) -> None:
        snaps, summary = self._summary()
        print_summary(snaps, summary, units=IMP)
        print_daily(snaps, units=IMP)
        out = capsys.readouterr().out
        assert "Total run mi:      12.4 mi" in out
        assert "6.21 mi" in out
        assert "min/mi" in out
        assert " km" not in out

    def test_imperial_energy_is_kcal_only(self, capsys: pytest.CaptureFixture) -> None:
        snaps, summary = self._summary()
        summary.avg_active_energy_kj = 4184.0
        print_summary(snaps, summary, units=IMP)
        out = capsys.readouterr().out
        assert "1000.0 kcal" in out
        assert "kJ" not in out


class TestDataDelta:
    def test_distance_in_display_units(self, tmp_path: Path) -> None:
        db = tmp_path / "health.db"
        conn = open_db(db)
        store_snapshots(conn, _run_snapshots(1, 1, km=10.0))
        conn.close()
        after = {"workout_all_max": "x"}
        assert "10.00 km" in format_data_delta(db, {}, after)
        assert "6.21 mi" in format_data_delta(db, {}, after, units=IMP)


class TestStandoutVolume:
    def _fill(self, conn: sqlite3.Connection, total_km: float, today: date) -> None:
        population = STANDOUT_MIN_POPULATION + 5
        for i in range(population):
            d = (date(2022, 1, 1) + timedelta(days=i * 3)).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO daily (date, imported_at) VALUES (?, 'x')", (d,)
            )
            conn.execute(
                "INSERT INTO workout (start_utc, date, type, category, duration_min, "
                "gpx_distance_km, imported_at, counts_as_lift) "
                "VALUES (?, ?, 'Run', 'run', 40, ?, 'x', 0)",
                (d + "T09:00:00Z", d, total_km / population),
            )
        conn.commit()

    def test_threshold_counted_in_miles(self, in_memory_db: sqlite3.Connection) -> None:
        step = STANDOUT_VOLUME_STEP_MI
        before_km = (step - 5) * KM_PER_MILE
        self._fill(in_memory_db, before_km, TODAY)
        in_memory_db.execute(
            "INSERT INTO daily (date, imported_at) VALUES (?, 'x')",
            (TODAY.isoformat(),),
        )
        in_memory_db.execute(
            "INSERT INTO workout (start_utc, date, type, category, duration_min, "
            "gpx_distance_km, imported_at, counts_as_lift) "
            "VALUES (?, ?, 'Run', 'run', 40, 20, 'x', 0)",
            (TODAY.isoformat() + "T09:00:00Z", TODAY.isoformat()),
        )
        in_memory_db.commit()
        imperial = [
            c
            for c in find_candidates(in_memory_db, today=TODAY, units=IMP)
            if c.kind == "volume_run"
        ]
        assert len(imperial) == 1
        assert f"{step:,} mi" in imperial[0].headline
        assert imperial[0].key == f"volume_run|{step}mi"
        metric = [
            c
            for c in find_candidates(in_memory_db, today=TODAY)
            if c.kind == "volume_run"
        ]
        assert all("mi" not in c.key for c in metric)


class TestDisplayRings:
    def _ring(self, actual: float, target: float = 15 * KM_PER_MILE):
        from weekly_progress import RingProgress

        stored = StoredTarget(
            spec=SPEC_BY_KEY["distance_km_week"],
            category="run",
            target=target,
            threshold=None,
            goal_text=None,
            strategy_hash="h",
            llm_call_id=None,
        )
        return RingProgress(stored, actual, "2026-09-03", 4)

    def test_display_copy_is_in_miles(self) -> None:
        ring = self._ring(7.5 * KM_PER_MILE)
        shown = display_ring(ring, IMP)
        assert shown.target.target == 15
        assert shown.actual == 7.5
        assert shown.target.spec.unit == "mi"
        assert shown.target.label == "Run mi"
        # The stored ring is untouched.
        assert ring.target.target == pytest.approx(15 * KM_PER_MILE)
        assert ring.target.spec.unit == "km"

    def test_status_is_unit_invariant(self) -> None:
        for actual in (0.0, 5.0, 15 * KM_PER_MILE, 30.0):
            ring = self._ring(actual)
            assert display_ring(ring, IMP).status == ring.status
            assert display_ring(ring, IMP).fraction == pytest.approx(ring.fraction)

    def test_metric_returns_same_object(self) -> None:
        ring = self._ring(3.0)
        assert display_ring(ring, M) is ring

    def test_block_renders_miles(self) -> None:
        shown = display_ring(self._ring(7.5 * KM_PER_MILE), IMP)
        block = render_progress_block([shown], week_start="2026-08-31")
        assert "Run mi" in block
        assert "7.5/15" in block


class TestGoalMiles:
    def _parse(self, unit: str | None, target: float = 15, category: str = "run"):
        unit_part = f', "unit": "{unit}"' if unit is not None else ""
        raw = (
            '{"targets": [{"metric": "distance_km_week", '
            f'"category": "{category}", "target": {target}{unit_part}}}]}}'
        )
        return parse_targets_response(raw, "h")

    def test_miles_are_stored_as_km(self) -> None:
        targets = self._parse("mi")
        assert targets[0].target == pytest.approx(15 * KM_PER_MILE)
        assert targets[0].for_display(IMP).target == 15
        assert targets[0].for_display(IMP).label == "Run mi"

    def test_km_and_missing_unit_are_unchanged(self) -> None:
        assert self._parse("km")[0].target == 15
        assert self._parse(None)[0].target == 15

    def test_unknown_unit_is_dropped(self) -> None:
        assert self._parse("yards") == []

    def test_range_check_runs_on_km(self) -> None:
        assert self._parse("mi", target=200) == []  # 322 km > 300 km run cap
        assert self._parse("mi", target=150) != []  # 241 km

    def test_metric_label_unchanged(self) -> None:
        assert self._parse("km")[0].label == "Run km"

    def test_vocabulary_version_is_in_the_digest(self, monkeypatch) -> None:
        import weekly_targets

        before = goals_digest("run 15 miles")
        monkeypatch.setattr(weekly_targets, "_VOCABULARY_VERSION", 99)
        assert goals_digest("run 15 miles") != before

    def test_non_distance_targets_ignore_display(self) -> None:
        targets = parse_targets_response(
            '{"targets": [{"metric": "sessions_week", "category": "run",'
            ' "target": 3}]}',
            "h",
        )
        assert targets[0].for_display(IMP) is targets[0]


class TestChallengeUnits:
    RAW = {
        "title": "Fifteen miles",
        "goal": "run 15 miles a week",
        "rationale": "steady",
        "metric": "distance_km_week",
        "category": "run",
        "target": 15,
        "unit": "mi",
        "weeks": 2,
    }

    def test_tool_schema_has_unit(self) -> None:
        props = propose_challenge_tool()[0]["function"]["parameters"]["properties"]
        assert props["unit"]["enum"] == ["km", "mi"]

    def test_mile_proposal_is_stored_in_km(self) -> None:
        proposal = coerce_proposal(self.RAW, frozenset())
        assert proposal.target.target == pytest.approx(15 * KM_PER_MILE)
        assert describe_challenge(proposal) == (
            f"Run km: {proposal.target.target:g} km a week for 2 weeks"
        )
        assert describe_challenge(proposal, units=IMP) == (
            "Run mi: 15 mi a week for 2 weeks"
        )

    def test_describe_active_in_miles(self, in_memory_db: sqlite3.Connection) -> None:
        monday = date(2026, 9, 28)
        cid = propose(
            in_memory_db, coerce_proposal(self.RAW, frozenset()), llm_call_id=1
        )
        accept(in_memory_db, cid, today=monday - timedelta(days=3))
        text = describe_active(
            in_memory_db, today=monday + timedelta(days=1), units=IMP
        )
        assert "15 mi a week" in text
        assert "/15 mi" in text


class TestBuildMessagesUnits:
    CTX = {"soul": "Be a coach.", "prompt": "Hi {today}", "me": "A"}

    def test_metric_system_message_has_no_units_block(self) -> None:
        msgs = build_messages(self.CTX, health_data_text="{}")
        assert "## Display units: imperial" not in msgs[0]["content"]

    def test_imperial_appends_block_after_conduct(self) -> None:
        metric = build_messages(self.CTX, health_data_text="{}")
        imperial = build_messages(self.CTX, health_data_text="{}", units=IMP)
        assert imperial[0]["content"].startswith(metric[0]["content"].rstrip())
        assert "## Display units: imperial" in imperial[0]["content"]
        assert "run_sql" in imperial[0]["content"]
        assert "1.609344" in imperial[0]["content"]
        assert imperial[1] == metric[1]


class TestUnitsFromProfile:
    def test_args_carry_units_into_helpers(self) -> None:
        from units import units_from_args

        assert units_from_args(SimpleNamespace(units=IMP)) is IMP


class TestBaselineTablesImperial:
    """Run distance in every baselines table must be converted exactly once."""

    def _seed(self, conn: sqlite3.Connection, km: float = 10.0) -> None:
        snaps = []
        for i in range(120):
            d = _days_ago(i)
            workouts = []
            if i % 3 == 0:
                workouts = [
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc=f"{d}T07:00:00Z",
                        duration_min=60.0,
                        gpx_distance_km=km,
                    )
                ]
            snaps.append(
                DailySnapshot(date=d, walking_speed_kmh=5.0, workouts=workouts)
            )
        # A prior-year run so the seasonal and same-season tables appear.
        older = (date.today() - timedelta(days=400)).isoformat()
        snaps.append(DailySnapshot(date=older, walking_speed_kmh=4.0))
        snaps.append(
            DailySnapshot(
                date=older,
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc=f"{older}T07:00:00Z",
                        duration_min=60.0,
                        gpx_distance_km=1.0,
                    )
                ],
            )
        )
        old = (date.today() - timedelta(days=360)).isoformat()
        snaps.append(
            DailySnapshot(
                date=old,
                walking_speed_kmh=4.0,
                workouts=[
                    WorkoutSnapshot(
                        type="Outdoor Run",
                        category="run",
                        start_utc=f"{old}T07:00:00Z",
                        duration_min=60.0,
                        gpx_distance_km=16.09344,
                    )
                ],
            )
        )
        store_snapshots(conn, snaps)

    def test_training_volume_is_converted_once(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        self._seed(in_memory_db)
        metric = compute_baselines(in_memory_db)
        imperial = compute_baselines(in_memory_db, units=IMP)
        row = next(x for x in metric.splitlines() if x.startswith("| Run distance |"))
        km_per_week = float(row.split("|")[2].split()[0])
        assert "km/week" in row
        irow = next(
            x for x in imperial.splitlines() if x.startswith("| Run distance |")
        )
        assert "mi/week" in irow
        assert float(irow.split("|")[2].split()[0]) == pytest.approx(
            km_per_week / KM_PER_MILE, abs=0.06
        )

    def test_daily_metric_walking_speed(self, in_memory_db: sqlite3.Connection) -> None:
        self._seed(in_memory_db)
        metric = compute_baselines(in_memory_db)
        imperial = compute_baselines(in_memory_db, units=IMP)
        assert "| Walking Speed | 5.0 | 5.0 | km/h |" in metric
        assert "| Walking Speed | 3.1 | 3.1 | mph |" in imperial

    def test_seasonal_table(self, in_memory_db: sqlite3.Connection) -> None:
        self._seed(in_memory_db)
        metric = compute_baselines(in_memory_db)
        imperial = compute_baselines(in_memory_db, units=IMP)
        mrow = next(
            x for x in metric.splitlines() if "Run distance" in x and " km |" in x
        )
        irow = next(
            x for x in imperial.splitlines() if "Run distance" in x and " mi |" in x
        )
        current_km = float(mrow.split("|")[2].split()[0])
        assert float(irow.split("|")[2].split()[0]) == pytest.approx(
            current_km / KM_PER_MILE, abs=0.06
        )


class TestChallengeImperialText:
    """Converted figures are rounded to the metric's own precision."""

    RAW = {
        "title": "Eleven miles",
        "goal": "run 11 miles a week",
        "metric": "distance_km_week",
        "category": "run",
        "target": 11,
        "unit": "mi",
        "weeks": 2,
    }

    def _closed(self, conn: sqlite3.Connection):
        from challenges import close_finished

        monday = date(2026, 9, 28)
        cid = propose(conn, coerce_proposal(self.RAW, frozenset()), llm_call_id=1)
        accept(conn, cid, today=monday - timedelta(days=3))
        store_snapshots(
            conn,
            [
                DailySnapshot(
                    date=(monday + timedelta(days=i * 7)).isoformat(),
                    workouts=[
                        WorkoutSnapshot(
                            type="Outdoor Run",
                            category="run",
                            start_utc=f"{(monday + timedelta(days=i * 7)).isoformat()}T07:00:00Z",
                            duration_min=60.0,
                            gpx_distance_km=km,
                        )
                    ],
                )
                for i, km in enumerate((38.4, 10.0))
            ],
        )
        return close_finished(conn, today=date(2026, 10, 12))[0]

    def test_outcome_message_has_no_float_noise(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        from challenges import outcome_message

        closed = self._closed(in_memory_db)
        text = outcome_message(closed, units=IMP)
        assert "week 1 23.9/11" in text
        assert "week 2 6.2/11 mi" in text
        assert "17.7028" not in text
        assert "week 1 38.4/17.7028" in outcome_message(closed)

    def test_history_and_description(self, in_memory_db: sqlite3.Connection) -> None:
        from challenges import describe_history

        closed = self._closed(in_memory_db)
        assert "Run mi: 11 mi a week for 2 weeks" in describe_history(
            in_memory_db, units=IMP
        )
        assert describe_challenge(closed, units=IMP) == (
            "Run mi: 11 mi a week for 2 weeks"
        )


class TestChallengeUnitRequired:
    BASE = {
        "title": "t",
        "goal": "g",
        "metric": "distance_km_week",
        "category": "run",
        "target": 20,
        "weeks": 2,
    }

    def test_missing_unit_asks_the_model_to_supply_one(self) -> None:
        from challenges import ChallengeError

        with pytest.raises(ChallengeError, match="set `unit` to `km` or `mi`"):
            coerce_proposal(dict(self.BASE), frozenset())

    def test_invalid_unit_is_rejected(self) -> None:
        from challenges import ChallengeError

        with pytest.raises(ChallengeError, match="yards"):
            coerce_proposal({**self.BASE, "unit": "yards"}, frozenset())

    @pytest.mark.parametrize(("unit", "km"), [("km", 20.0), ("mi", 20 * KM_PER_MILE)])
    def test_valid_units(self, unit: str, km: float) -> None:
        proposal = coerce_proposal({**self.BASE, "unit": unit}, frozenset())
        assert proposal.target.target == pytest.approx(km)

    def test_count_metrics_need_no_unit(self) -> None:
        raw = {**self.BASE, "metric": "sessions_week", "target": 3}
        assert coerce_proposal(raw, frozenset()).target.target == 3

    def test_tool_description_names_the_unit(self) -> None:
        desc = propose_challenge_tool()[0]["function"]["description"]
        assert "target in `unit` (km or mi)" in desc
