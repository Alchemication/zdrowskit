"""LLM runner methods and rate-limiting logic for the zdrowskit daemon.

Owns:
    - Report runners: weekly insights, mid-week progress, manual ``/review``
    - Nudge runner, deferred-queue drain
    - Coaching review runner and bundled-proposal delivery
    - Rate limiting: nudge caps, report once-per-day guards, report-proximity
      suppression
    - Data-snapshot helpers that describe what changed across an import

Extracted from ``daemon.py`` so that module can focus on the event loop,
file-watching, and scheduling glue.
"""

from __future__ import annotations

import logging
import sqlite3
import types
from datetime import date, datetime
from typing import TYPE_CHECKING

from config import (
    COACH_SUPPRESSION_S,
    MIN_NUDGE_INTERVAL_S,
)

if TYPE_CHECKING:
    from cmd_coach import CoachProposal
    from cmd_llm_common import CommandResult
    from daemon import ZdrowskitDaemon

logger = logging.getLogger(__name__)


class DaemonRunnerHandler:
    """Rate-limited LLM runners for reports, nudges, and coaching reviews.

    Borrows from the daemon (via ``self._d``):
        ``db``, ``model``, ``context_dir``, ``_state``, ``_poller``,
        ``_pending_edits``, ``_load_notification_prefs``,
        ``_attach_feedback_button``, ``_notify_user_failure``,
        ``_propose_context_edit``, ``_queue_nudge_trigger``.
    """

    def __init__(self, daemon: "ZdrowskitDaemon") -> None:
        self._d = daemon

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _is_report_imminent(self) -> bool:
        """Check if a scheduled report will fire within COACH_SUPPRESSION_S."""
        from notification_prefs import effective_notification_prefs

        now = datetime.now().astimezone()
        prefs = self._d._load_notification_prefs(now=now)
        effective = effective_notification_prefs(prefs)

        for report_type in ("weekly_insights", "midweek_report"):
            report = effective[report_type]
            if now.strftime("%A").lower() != report["weekday"]:
                continue
            hour_str, minute_str = report["time"].split(":")
            report_time = now.replace(
                hour=int(hour_str),
                minute=int(minute_str),
                second=0,
                microsecond=0,
            )
            delta = (report_time - now).total_seconds()
            if 0 < delta < COACH_SUPPRESSION_S:
                return True

        return False

    def _can_send_nudge(self) -> bool:
        """Check whether a nudge is allowed under the rate limits.

        Returns:
            True if a nudge may be sent; False if suppressed.
        """
        allowed, _, _ = self._check_nudge_rate_limit()
        return allowed

    def _check_nudge_rate_limit(self) -> tuple[bool, str | None, dict | None]:
        """Evaluate nudge rate limits and return allowed + reason + details.

        Returns:
            A tuple ``(allowed, reason, details)``. When allowed is False,
            ``reason`` is a short machine-readable tag (e.g. "daily_cap",
            "min_interval", "report_recent", "report_imminent") and
            ``details`` is a JSON-serialisable dict with the numbers behind
            the decision. Both are None when allowed is True.
        """
        prefs = self._d._load_notification_prefs(now=datetime.now().astimezone())
        max_nudges_per_day = (
            prefs.get("overrides", {}).get("nudges", {}).get("max_per_day")
        )
        if not isinstance(max_nudges_per_day, int):
            from notification_prefs import effective_notification_prefs

            max_nudges_per_day = effective_notification_prefs(prefs)["nudges"][
                "max_per_day"
            ]

        last_report_ts = self._d._state.get("last_report_ts")
        if last_report_ts:
            elapsed = abs(
                (
                    datetime.now() - datetime.fromisoformat(last_report_ts)
                ).total_seconds()
            )
            if elapsed < COACH_SUPPRESSION_S:
                logger.info(
                    "Nudge suppressed: within %.0f min of scheduled report",
                    elapsed / 60,
                )
                return (
                    False,
                    "report_recent",
                    {"minutes_since_report": round(elapsed / 60, 1)},
                )
        if self._is_report_imminent():
            logger.info("Nudge suppressed: scheduled report imminent")
            return False, "report_imminent", None

        today_str = date.today().isoformat()

        if self._d._state.get("nudge_date") != today_str:
            self._d._state["nudge_count_today"] = 0
            self._d._state["nudge_date"] = today_str

        if self._d._state.get("nudge_count_today", 0) >= max_nudges_per_day:
            logger.info(
                "Nudge suppressed: daily limit (%d) reached", max_nudges_per_day
            )
            return (
                False,
                "daily_cap",
                {
                    "sent_today": self._d._state.get("nudge_count_today", 0),
                    "max_per_day": max_nudges_per_day,
                },
            )

        last_ts = self._d._state.get("last_nudge_ts")
        if last_ts:
            elapsed = (datetime.now() - datetime.fromisoformat(last_ts)).total_seconds()
            if elapsed < MIN_NUDGE_INTERVAL_S:
                logger.info(
                    "Nudge suppressed: %.0f min since last (min %.0f min)",
                    elapsed / 60,
                    MIN_NUDGE_INTERVAL_S / 60,
                )
                return (
                    False,
                    "min_interval",
                    {
                        "minutes_since_last": round(elapsed / 60, 1),
                        "min_interval_min": round(MIN_NUDGE_INTERVAL_S / 60, 1),
                    },
                )

        return True, None, None

    def _record_nudge(self, text: str, trigger: str) -> None:
        """Update state after a nudge is sent.

        Args:
            text: The nudge text that was sent.
            trigger: The trigger type that prompted the nudge.
        """
        from daemon import _save_state

        today_str = date.today().isoformat()
        if self._d._state.get("nudge_date") != today_str:
            self._d._state["nudge_count_today"] = 0
            self._d._state["nudge_date"] = today_str
        self._d._state["nudge_count_today"] = (
            self._d._state.get("nudge_count_today", 0) + 1
        )
        now = datetime.now()
        self._d._state["last_nudge_ts"] = now.isoformat()

        entry = {"ts": now.isoformat(), "trigger": trigger, "text": text}
        recent: list[dict] = self._d._state.get("recent_nudges", [])
        recent.insert(0, entry)
        self._d._state["recent_nudges"] = recent[:3]  # Keep last 3

        _save_state(self._d._state)

    def _can_send_report(self, report_type: str) -> bool:
        """Check whether a report of the given type may be sent today.

        Args:
            report_type: "review" for full-week or "progress" for mid-week.

        Returns:
            True if report may be sent; False if already sent today.
        """
        key = f"last_{report_type}_date"
        skipped_key = f"last_{report_type}_skip_date"
        today_str = date.today().isoformat()
        if self._d._state.get(key) == today_str:
            logger.info("%s report suppressed: already sent today", report_type)
            return False
        if self._d._state.get(skipped_key) == today_str:
            logger.info("%s report suppressed: already skipped today", report_type)
            return False
        return True

    def _record_report(self, report_type: str) -> None:
        """Update state after a report is sent.

        Args:
            report_type: "review" for full-week or "progress" for mid-week.
        """
        from daemon import _save_state

        self._d._state[f"last_{report_type}_date"] = date.today().isoformat()
        _save_state(self._d._state)

    # ------------------------------------------------------------------
    # Data snapshot helpers
    # ------------------------------------------------------------------

    def _data_snapshot(self) -> dict:
        """Snapshot table-level markers used to compute import deltas.

        Returns:
            A dict with row counts and max-date markers for the daily,
            workout_all, and sleep_all tables. Empty dict on failure.
        """
        try:
            conn = sqlite3.connect(str(self._d.db))
            cur = conn.cursor()
            snap: dict = {}
            for table, date_col in (
                ("daily", "date"),
                ("workout_all", "start_utc"),
                ("sleep_all", "date"),
            ):
                try:
                    row = cur.execute(
                        f"SELECT COUNT(*), MAX({date_col}) FROM {table}"
                    ).fetchone()
                except sqlite3.Error:
                    continue
                snap[f"{table}_count"] = row[0] if row else 0
                snap[f"{table}_max"] = row[1] if row else None
            conn.close()
            return snap
        except sqlite3.Error as exc:
            logger.warning("Data snapshot failed: %s", exc)
            return {}

    def _format_data_delta(self, before: dict, after: dict) -> str:
        """Describe what records arrived between two data snapshots.

        Args:
            before: Snapshot taken before the import ran.
            after: Snapshot taken after the import ran.

        Returns:
            Human-readable text the LLM can use to know what is actually new.
            Falls back to a generic line when nothing identifiable changed.
        """
        if not after:
            return "New health data synced (delta unavailable)."

        lines: list[str] = []

        # New workouts: rows with start_utc strictly greater than the prior max.
        prev_workout_max = before.get("workout_all_max")
        try:
            conn = sqlite3.connect(str(self._d.db))
            conn.row_factory = sqlite3.Row
            if prev_workout_max:
                rows = conn.execute(
                    "SELECT start_utc, date, type, category, duration_min, "
                    "gpx_distance_km FROM workout_all "
                    "WHERE start_utc > ? ORDER BY start_utc",
                    (prev_workout_max,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT start_utc, date, type, category, duration_min, "
                    "gpx_distance_km FROM workout_all "
                    "ORDER BY start_utc DESC LIMIT 3"
                ).fetchall()
            for r in rows:
                dur = r["duration_min"]
                dur_s = f"{dur:.0f} min" if dur is not None else "?"
                dist = r["gpx_distance_km"]
                dist_s = f", {dist:.2f} km" if dist is not None else ""
                lines.append(
                    f"- New workout: {r['type']} ({r['category']}), "
                    f"{dur_s}{dist_s} on {r['date']}"
                )

            # New sleep nights: rows with date strictly greater than prior max.
            prev_sleep_max = before.get("sleep_all_max")
            if prev_sleep_max:
                rows = conn.execute(
                    "SELECT date, sleep_total_h, sleep_efficiency_pct "
                    "FROM sleep_all WHERE date > ? ORDER BY date",
                    (prev_sleep_max,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT date, sleep_total_h, sleep_efficiency_pct "
                    "FROM sleep_all ORDER BY date DESC LIMIT 2"
                ).fetchall()
            for r in rows:
                h = r["sleep_total_h"]
                eff = r["sleep_efficiency_pct"]
                h_s = f"{h:.1f}h" if h is not None else "?h"
                eff_s = f", {eff:.0f}% efficiency" if eff is not None else ""
                lines.append(f"- New sleep night: {r['date']} — {h_s}{eff_s}")

            # New daily metric rows for dates beyond the previous max.
            prev_daily_max = before.get("daily_max")
            if prev_daily_max:
                rows = conn.execute(
                    "SELECT date, steps, hrv_ms, resting_hr FROM daily "
                    "WHERE date > ? ORDER BY date",
                    (prev_daily_max,),
                ).fetchall()
                for r in rows:
                    parts = []
                    if r["steps"] is not None:
                        parts.append(f"steps {r['steps']}")
                    if r["hrv_ms"] is not None:
                        parts.append(f"HRV {r['hrv_ms']:.0f} ms")
                    if r["resting_hr"] is not None:
                        parts.append(f"RHR {r['resting_hr']:.0f} bpm")
                    detail = ", ".join(parts) if parts else "(no metrics yet)"
                    lines.append(f"- New daily row: {r['date']} — {detail}")
            conn.close()
        except sqlite3.Error as exc:
            logger.warning("Delta query failed: %s", exc)
            return "New health data synced (delta query failed)."

        if not lines:
            # No new identifiable rows — most likely an in-place refresh
            # of today's metrics (e.g. a late HRV reading landing).
            return (
                "Health data refreshed but no new completed activities or sleep "
                "nights since the previous sync. Today's metrics may have "
                "updated in place."
            )

        return "Records added in this import:\n" + "\n".join(lines)

    def _format_context_trigger(self, stem: str, trigger: str) -> str:
        """Describe a context-file edit so the LLM knows where to look.

        Args:
            stem: File stem that was edited (``log``, ``strategy``, ``me``).
            trigger: The mapped trigger type string.

        Returns:
            One-line description pointing the LLM to the relevant section.
        """
        section_map = {
            "log": ("Recent User Notes", "log.md"),
            "strategy": ("Strategy", "strategy.md"),
            "me": ("About the User", "me.md"),
        }
        section, filename = section_map.get(stem, ("Recent User Notes", f"{stem}.md"))
        return (
            f"The user just edited {filename} (trigger: {trigger}). "
            f"The current contents are in the '{section}' section above — "
            "respond to what changed there."
        )

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def _run_import(self) -> None:
        """Import the latest health data from the iCloud directory into the DB."""
        from commands import cmd_import

        args = types.SimpleNamespace(
            data_dir=str(self._d.health_dir),
            source="autoexport",
            db=str(self._d.db),
        )
        before = self._data_snapshot()
        try:
            logger.info("Importing health data from %s", self._d.health_dir)
            cmd_import(args)
        except SystemExit:
            logger.error("Import failed — proceeding with existing DB data")
            self._d._record_event("import", "failed", "Health data import failed")
            return
        after = self._data_snapshot()
        changed = any(
            before.get(k) != after.get(k)
            for k in after
            if k.endswith("_count") or k.endswith("_max")
        )
        if changed:
            delta = {
                "daily_added": max(
                    0,
                    (after.get("daily_count") or 0) - (before.get("daily_count") or 0),
                ),
                "workouts_added": max(
                    0,
                    (after.get("workout_all_count") or 0)
                    - (before.get("workout_all_count") or 0),
                ),
                "sleep_added": max(
                    0,
                    (after.get("sleep_all_count") or 0)
                    - (before.get("sleep_all_count") or 0),
                ),
            }
            summary = (
                f"Imported: {delta['daily_added']} days, "
                f"{delta['workouts_added']} workouts, {delta['sleep_added']} sleep"
            )
            self._d._record_event("import", "new_data", summary, delta)
        else:
            self._d._record_event("import", "no_changes", "Import ran, no new rows")

    # ------------------------------------------------------------------
    # Report runners
    # ------------------------------------------------------------------

    def _run_review(
        self,
        *,
        week: str = "last",
        skip_import: bool = False,
    ) -> None:
        """Run a manual review report and send it via Telegram."""
        from daemon import _capture_last_error, _save_state

        if week not in {"current", "last"}:
            raise ValueError(f"Unsupported review week: {week}")

        if not skip_import:
            self._d._run_import()

        from cmd_insights import cmd_insights

        args = types.SimpleNamespace(
            db=str(self._d.db),
            model=self._d.model,
            telegram=True,
            week=week,
            months=3,
            no_update_baselines=False,
            no_update_history=False,
            explain=False,
            data_dir=None,
            reasoning_effort="medium",
        )
        with _capture_last_error() as cap:
            try:
                logger.info("Running manual review report (%s)", week)
                result = cmd_insights(args)
                self._d._attach_feedback_button(result, "insights")
                self._d._record_report("review" if week == "last" else "progress")
                self._d._state["last_report_ts"] = datetime.now().isoformat()
                _save_state(self._d._state)
            except SystemExit:
                # Snapshot before our own logger.error overwrites the capture.
                captured = cap.last_message
                logger.error("Manual review report failed (%s)", week)
                self._d._notify_user_failure(f"Manual review ({week})", captured)

    def _run_weekly_report(self) -> None:
        """Run the full weekly insights report and send via Telegram."""
        from daemon import _capture_last_error, _save_state
        from notification_prefs import evaluate_report_delivery

        now = datetime.now().astimezone()
        prefs = self._d._load_notification_prefs(now=now)
        decision = evaluate_report_delivery(prefs, "weekly_insights", now=now)
        if decision["status"] != "allowed":
            self._d._state["last_review_skip_date"] = date.today().isoformat()
            _save_state(self._d._state)
            reason = decision.get("reason", "unknown")
            logger.info("Weekly insights suppressed: %s", reason)
            self._d._record_event(
                "insights",
                "prefs_suppressed",
                f"Weekly report suppressed: {reason}",
                {"reason": reason, "kind": "weekly"},
            )
            return
        if not self._can_send_report("review"):
            self._d._record_event(
                "insights",
                "already_ran",
                "Weekly report skipped: already ran/skipped today",
                {"kind": "weekly"},
            )
            return

        self._d._run_import()

        from cmd_insights import cmd_insights

        args = types.SimpleNamespace(
            db=str(self._d.db),
            model=self._d.model,
            telegram=True,
            week="last",
            months=3,
            no_update_baselines=False,
            no_update_history=False,
            explain=False,
            data_dir=None,
            reasoning_effort="medium",
        )
        with _capture_last_error() as cap:
            try:
                logger.info("Running weekly review report")
                result = cmd_insights(args)
                self._d._attach_feedback_button(result, "insights")
                self._d._record_report("review")
                self._d._state["last_report_ts"] = datetime.now().isoformat()
                _save_state(self._d._state)
                self._d._record_event(
                    "insights",
                    "fired",
                    "Weekly review report sent",
                    {"kind": "weekly"},
                    llm_call_id=result.llm_call_id,
                )
                self._d._run_coach(week="last", skip_import=True)
            except SystemExit:
                captured = cap.last_message
                logger.error("Weekly review report failed")
                self._d._state["last_review_skip_date"] = date.today().isoformat()
                _save_state(self._d._state)
                self._d._notify_user_failure("Weekly review", captured)
                self._d._record_event(
                    "insights",
                    "failed",
                    "Weekly review report failed",
                    {"kind": "weekly", "error": (captured or "")[:500]},
                )

    def _run_midweek_report(self) -> None:
        """Run a mid-week progress report and send via Telegram."""
        from daemon import _capture_last_error, _save_state
        from notification_prefs import evaluate_report_delivery

        now = datetime.now().astimezone()
        prefs = self._d._load_notification_prefs(now=now)
        decision = evaluate_report_delivery(prefs, "midweek_report", now=now)
        if decision["status"] != "allowed":
            self._d._state["last_progress_skip_date"] = date.today().isoformat()
            _save_state(self._d._state)
            reason = decision.get("reason", "unknown")
            logger.info("Midweek report suppressed: %s", reason)
            self._d._record_event(
                "insights",
                "prefs_suppressed",
                f"Midweek report suppressed: {reason}",
                {"reason": reason, "kind": "midweek"},
            )
            return
        if not self._can_send_report("progress"):
            self._d._record_event(
                "insights",
                "already_ran",
                "Midweek report skipped: already ran/skipped today",
                {"kind": "midweek"},
            )
            return

        self._d._run_import()

        from cmd_insights import cmd_insights

        args = types.SimpleNamespace(
            db=str(self._d.db),
            model=self._d.model,
            telegram=True,
            week="current",
            months=3,
            no_update_baselines=False,
            no_update_history=False,
            explain=False,
            data_dir=None,
            reasoning_effort="medium",
        )
        with _capture_last_error() as cap:
            try:
                logger.info("Running mid-week progress report")
                result = cmd_insights(args)
                self._d._attach_feedback_button(result, "insights")
                self._d._record_report("progress")
                self._d._state["last_report_ts"] = datetime.now().isoformat()
                _save_state(self._d._state)
                self._d._record_event(
                    "insights",
                    "fired",
                    "Mid-week progress report sent",
                    {"kind": "midweek"},
                    llm_call_id=result.llm_call_id,
                )
            except SystemExit:
                captured = cap.last_message
                logger.error("Mid-week progress report failed")
                self._d._state["last_progress_skip_date"] = date.today().isoformat()
                _save_state(self._d._state)
                self._d._notify_user_failure("Mid-week progress", captured)
                self._d._record_event(
                    "insights",
                    "failed",
                    "Mid-week progress report failed",
                    {"kind": "midweek", "error": (captured or "")[:500]},
                )

    # ------------------------------------------------------------------
    # Nudge runner
    # ------------------------------------------------------------------

    def _run_nudge(
        self,
        trigger: str,
        *,
        trigger_context: str | None = None,
        _from_drain: bool = False,
    ) -> None:
        """Run a nudge and send via Telegram.

        Passes recent nudge history so the LLM can decide whether there is
        anything new worth saying (SKIP if not).

        Args:
            trigger: Trigger type string passed to cmd_nudge.
            trigger_context: Optional human-readable description of *what*
                the trigger refers to (e.g. which records were imported,
                which file was edited). When None, a generic placeholder is
                used.
            _from_drain: Internal flag — True when called from the deferred
                queue drain path.
        """
        from daemon import _capture_last_error
        from notification_prefs import evaluate_nudge_delivery

        now = datetime.now().astimezone()
        prefs = self._d._load_notification_prefs(now=now)
        decision = evaluate_nudge_delivery(prefs, now=now)
        if decision["status"] == "suppressed":
            reason = decision.get("reason", "unknown")
            logger.info("Nudge suppressed by notification prefs: %s", reason)
            self._d._record_event(
                "nudge",
                "prefs_suppressed",
                f"Nudge suppressed ({trigger}): {reason}",
                {"trigger": trigger, "reason": reason, "from_drain": _from_drain},
            )
            return
        if decision["status"] == "deferred":
            if _from_drain:
                logger.info("Deferred nudge still blocked at drain time; skipping")
                self._d._record_event(
                    "nudge",
                    "quiet_dropped",
                    f"Deferred nudge dropped at drain ({trigger}): still blocked",
                    {"trigger": trigger, "until": decision.get("until")},
                )
                return
            self._d._queue_nudge_trigger(trigger, now=now)
            queue_size = len(self._d._state.get("quiet_queue", []))
            logger.info(
                "Nudge deferred until %s (trigger: %s, queue size: %d)",
                decision.get("until", "later"),
                trigger,
                queue_size,
            )
            self._d._record_event(
                "nudge",
                "quiet_deferred",
                f"Nudge deferred ({trigger}) until {decision.get('until', 'later')}",
                {
                    "trigger": trigger,
                    "until": decision.get("until"),
                    "queue_size": queue_size,
                },
            )
            return

        allowed, rl_reason, rl_details = self._check_nudge_rate_limit()
        if not allowed:
            self._d._record_event(
                "nudge",
                "rate_limited",
                f"Nudge rate-limited ({trigger}): {rl_reason}",
                {"trigger": trigger, "reason": rl_reason, **(rl_details or {})},
            )
            return

        from cmd_nudge import cmd_nudge

        args = types.SimpleNamespace(
            db=str(self._d.db),
            model=self._d.model,
            telegram=True,
            trigger=trigger,
            months=1,
            recent_nudges=self._d._state.get("recent_nudges", []),
            last_coach_summary=self._d._state.get("last_coach_summary", ""),
            last_coach_summary_date=self._d._state.get("last_coach_summary_date", ""),
            trigger_context=trigger_context or "",
        )
        with _capture_last_error() as cap:
            try:
                logger.info("Running nudge (trigger: %s)", trigger)
                result = cmd_nudge(args)
                if result.text:
                    self._record_nudge(result.text, trigger)
                    self._d._attach_feedback_button(result, "nudge")
                    self._d._record_event(
                        "nudge",
                        "fired",
                        f"Nudge sent ({trigger})",
                        {"trigger": trigger, "chars": len(result.text)},
                        llm_call_id=result.llm_call_id,
                    )
                else:
                    self._d._record_event(
                        "nudge",
                        "llm_skip",
                        f"LLM returned SKIP ({trigger})",
                        {"trigger": trigger},
                        llm_call_id=result.llm_call_id,
                    )
            except SystemExit:
                captured = cap.last_message
                logger.error("Nudge failed (trigger: %s)", trigger)
                self._d._notify_user_failure(f"Nudge ({trigger})", captured)
                self._d._record_event(
                    "nudge",
                    "failed",
                    f"Nudge failed ({trigger})",
                    {"trigger": trigger, "error": (captured or "")[:500]},
                )

    def _drain_quiet_queue(self) -> None:
        """Process deferred triggers as a single consolidated nudge."""
        from daemon import _save_state

        queue: list[dict] = self._d._state.get("quiet_queue", [])
        if not queue:
            return

        # Clear the queue before sending to avoid re-processing on failure
        self._d._state["quiet_queue"] = []
        _save_state(self._d._state)

        # Pick the most "interesting" trigger (user-initiated > system)
        priority = {
            "strategy_updated": 4,
            "log_update": 2,
            "profile_updated": 1,
            "new_data": 0,
        }
        best = max(queue, key=lambda e: priority.get(e["trigger"], 0))

        logger.info(
            "Draining quiet queue: %d triggers, sending consolidated nudge (trigger: %s)",
            len(queue),
            best["trigger"],
        )
        self._d._record_event(
            "nudge",
            "quiet_drain",
            f"Draining {len(queue)} quiet-hour trigger(s) as {best['trigger']}",
            {"queue_size": len(queue), "chosen_trigger": best["trigger"]},
        )
        # Compose a consolidated trigger_context from every queued event so
        # the nudge has the full picture of what accumulated during quiet hours.
        parts = [
            f"- {e['trigger']} at {e['ts'][:16]}" for e in queue if e.get("trigger")
        ]
        trigger_context = (
            "Multiple triggers accumulated during quiet hours; choosing the "
            "highest-priority one to drive the message:\n" + "\n".join(parts)
            if len(queue) > 1
            else None
        )
        self._d._run_nudge(
            best["trigger"], trigger_context=trigger_context, _from_drain=True
        )

    # ------------------------------------------------------------------
    # Coach runner
    # ------------------------------------------------------------------

    def _run_coach(
        self,
        *,
        week: str = "last",
        skip_import: bool = False,
        force: bool = False,
    ) -> None:
        """Run a coaching review and send proposals via Telegram.

        Proposes concrete edits to strategy.md based on the
        week's data. Each proposal is sent as an inline Approve/Reject
        button. When the model returns SKIP (no strategy changes
        warranted), nothing is sent — the coach is silent on no-change
        weeks, mirroring the nudge SKIP behavior.

        Args:
            week: Which week to review (``"last"`` or ``"current"``).
            skip_import: Skip the pre-run import pass (used when a caller
                has already imported, e.g. the weekly report path).
            force: Bypass the "already ran today" guard. Set by manual
                triggers like the ``/coach`` Telegram command so the user
                can re-run on demand.
        """
        from daemon import _capture_last_error, _save_state

        last_coach = self._d._state.get("last_coach_date", "")
        today_str = date.today().isoformat()
        if last_coach == today_str and not force:
            logger.debug("Coach already ran today, skipping")
            return

        if not skip_import:
            self._d._run_import()

        from cmd_coach import cmd_coach

        args = types.SimpleNamespace(
            db=str(self._d.db),
            model=self._d.model,
            week=week,
            months=3,
            recent_nudges=self._d._state.get("recent_nudges", []),
            reasoning_effort="medium",
        )
        with _capture_last_error() as cap:
            try:
                logger.info("Running coaching review")
                cmd_result, proposals = cmd_coach(args)
                self._send_coach_bundle(cmd_result, proposals, force=force)
                self._d._state["last_coach_date"] = today_str
                if cmd_result.text:
                    self._d._state["last_coach_summary"] = cmd_result.text[:500]
                    self._d._state["last_coach_summary_date"] = today_str
                _save_state(self._d._state)
                if cmd_result.text:
                    self._d._record_event(
                        "coach",
                        "fired",
                        f"Coach review sent ({len(proposals)} proposal(s))",
                        {"week": week, "proposals": len(proposals), "force": force},
                        llm_call_id=cmd_result.llm_call_id,
                    )
                else:
                    self._d._record_event(
                        "coach",
                        "llm_skip",
                        "Coach returned SKIP — no strategy changes",
                        {"week": week, "force": force},
                        llm_call_id=cmd_result.llm_call_id,
                    )
            except SystemExit:
                captured = cap.last_message
                logger.error("Coaching review failed")
                self._d._notify_user_failure("Coaching review", captured)
                self._d._record_event(
                    "coach",
                    "failed",
                    "Coach review failed",
                    {"week": week, "error": (captured or "")[:500]},
                )

    def _send_coach_bundle(
        self,
        cmd_result: "CommandResult",
        proposals: list["CoachProposal"],
        *,
        force: bool,
    ) -> None:
        """Deliver a coach review as one bundled Telegram message.

        Composes the narrative + per-edit diff blocks (already in
        ``cmd_result.text``) and an inline keyboard with one Accept / Reject
        / Diff row per proposal plus a final feedback row. Long bundles are
        chunked by :meth:`TelegramPoller.send_message_with_keyboard`, which
        attaches the keyboard to the final chunk so the user always sees
        the buttons at the bottom of the conversation.

        SKIP handling: when ``cmd_result.text`` is None and ``force`` is
        True (manual ``/coach``), send a short acknowledgment so the
        "Running coaching review…" placeholder doesn't dangle. Scheduled
        weekly runs stay silent on SKIP to avoid noise after the insights
        report.

        Args:
            cmd_result: Result returned by :func:`commands.cmd_coach`.
            proposals: Validated proposals returned alongside it.
            force: Whether this run was user-initiated (``/coach``) and
                therefore deserves a SKIP acknowledgment.
        """
        from telegram_bot import feedback_keyboard

        if self._d._poller is None:
            return

        # SKIP path.
        if not cmd_result.text:
            if not force:
                return
            skip_text = (
                "Coach reviewed the week — no strategy changes warranted. "
                "Current strategy is working."
            )
            skip_msg_id = self._d._poller.send_reply(skip_text)
            if skip_msg_id is not None and cmd_result.llm_call_id is not None:
                self._d._poller.edit_message_reply_markup(
                    skip_msg_id,
                    feedback_keyboard(cmd_result.llm_call_id, "coach"),
                )
            return

        # No proposals, but coach still has narrative to deliver — rare
        # (the prompt forces SKIP otherwise) but possible from the
        # iteration-cap synthesis path. Send as a regular reply with the
        # feedback keyboard attached.
        if not proposals:
            msg_id = self._d._poller.send_reply(cmd_result.text)
            if msg_id is not None and cmd_result.llm_call_id is not None:
                self._d._poller.edit_message_reply_markup(
                    msg_id,
                    feedback_keyboard(cmd_result.llm_call_id, "coach"),
                )
            return

        # Bundled path. Mint one PendingEdit per proposal so the inline
        # buttons can route accept/reject callbacks back to the right edit.
        accept_rows: list[list[dict[str, str]]] = []
        for i, proposal in enumerate(proposals, start=1):
            edit_id = self._d._pending_edits.store(
                proposal.edit, source="coach", preview=proposal.preview
            )
            accept_rows.append(
                [
                    {
                        "text": f"\u2705 Accept #{i}",
                        "callback_data": f"ctx_accept:{edit_id}",
                    },
                    {
                        "text": f"\u274c Reject #{i}",
                        "callback_data": f"ctx_reject:{edit_id}",
                    },
                    {
                        "text": f"\U0001f50d Diff #{i}",
                        "callback_data": f"ctx_diff:{edit_id}",
                    },
                ]
            )

        # Append a feedback row reusing the existing thumbs-down keyboard
        # so the user can flag the whole review without losing the per-edit
        # buttons. feedback_keyboard returns rows (list[list[button]]).
        keyboard_rows = list(accept_rows)
        if cmd_result.llm_call_id is not None:
            keyboard_rows.extend(feedback_keyboard(cmd_result.llm_call_id, "coach"))

        self._d._poller.send_message_with_keyboard(cmd_result.text, keyboard_rows)
