"""Ingest health monitoring, alerting and Tailscale repair for one profile.

Split out of ``daemon.py``, where it had grown to roughly a third of the file
and all of one domain: deciding whether a profile's phone can still feed the
system, telling its owner once when it cannot, repairing what is repairable
here, and recording every one of those decisions.

Public API:
    IngestHealthHandler — one profile's ingest-health checks and repairs
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from config import (
    DATA_HEALTH_REALERT_S,
    FUNNEL_DNS_CONFIRM_AFTER_MIN,
    FUNNEL_OUTAGE_ESCALATE_AFTER_H,
    FUNNEL_PROBE_OBSERVE_ONLY,
    FUNNEL_PROBE_TIMEOUT_S,
    FUNNEL_REPAIR_VERIFY_TIMEOUT_S,
    FUNNEL_UNREACHABLE_REPAIR_AFTER_MIN,
    HTTP_INGEST_PAIR_WINDOW_S,
    INSTANCE_NAME,
    NODE_OFFLINE_REPAIR_AFTER_MIN,
    TAILSCALE_APP_NAME,
    TAILSCALE_RECONNECT_TIMEOUT_S,
    TAILSCALE_RESTART_TIMEOUT_S,
)

if TYPE_CHECKING:
    from daemon import ProfileRuntime

logger = logging.getLogger(__name__)


def _older_than(timestamp: str, seconds: float) -> bool:
    """Return whether a stored ISO timestamp is further back than *seconds*.

    An unparseable timestamp counts as old so a corrupted state file cannot
    silence an alert forever.

    Args:
        timestamp: ISO-8601 timestamp previously written to state.
        seconds: Age threshold.
    """
    try:
        parsed = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - parsed > timedelta(seconds=seconds)


def _format_date_ranges(days: list[date]) -> str:
    """Format sorted dates as compact contiguous ranges.

    Args:
        days: Sorted missing dates.

    Returns:
        Comma-separated ISO dates or inclusive ranges.
    """
    ranges: list[tuple[date, date]] = []
    for day in days:
        if ranges and day == ranges[-1][1] + timedelta(days=1):
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ", ".join(
        start.isoformat()
        if start == end
        else f"{start.isoformat()} to {end.isoformat()}"
        for start, end in ranges
    )


def _parse_utc(moment: object) -> datetime | None:
    """Parse an ISO timestamp into an aware UTC datetime.

    Compares timestamps by instant rather than by text: the state file and the
    ingest receipts are written by different code paths, and an offset that
    differs by so much as its spelling makes a string comparison silently
    wrong in whichever direction the characters happen to sort.

    Args:
        moment: ISO 8601 timestamp, or anything else.

    Returns:
        An aware datetime, or None when the value is missing or unparseable.
    """
    if not isinstance(moment, str):
        return None
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _hours_since(moment: str | None, *, now: datetime) -> float | None:
    """Return how many hours have passed since an ISO timestamp.

    Args:
        moment: ISO 8601 timestamp, or None when the caller has no marker.
        now: Current time to measure against.

    Returns:
        Elapsed hours, or None when the timestamp is missing or unparseable.
    """
    if not isinstance(moment, str):
        return None
    try:
        started = datetime.fromisoformat(moment)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (now - started).total_seconds() / 3600


def _recovery_message(
    alerted: object, present_metric_dates: set[str] | None
) -> str | None:
    """Compose the notice that a reported ingest fault has cleared.

    A recovery that cost the user nothing is not news. Auto Export backfills the
    days it missed, so most stale alerts end with every missing day present and
    nothing to act on — announcing that only doubles the message count for the
    fault. What earns a message is a gap that stayed a gap: those dates remain
    absent, and reports covering them may be incomplete even though newer data
    is importing again.

    A stalled pipe is different. The user was told to go fix something, so the
    confirmation that it worked is the answer to an action they took.

    Args:
        alerted: The recorded ``data_health_alert`` state entry.
        present_metric_dates: Metric-bearing dates from the alerted range. None
            for pipe faults that did not record a missing range.

    Returns:
        The message to send, or None when the recovery is not worth one.
    """
    missing_from = alerted.get("missing_from") if isinstance(alerted, dict) else None
    missing_to = alerted.get("missing_to") if isinstance(alerted, dict) else None
    if not isinstance(missing_from, str) or not isinstance(missing_to, str):
        return "✅ **Sync is working again** — health data is importing normally."

    try:
        first = date.fromisoformat(missing_from)
        last = date.fromisoformat(missing_to)
    except ValueError:
        return "✅ **Sync is working again** — health data is importing normally."
    if last < first:
        return "✅ **Sync is working again** — health data is importing normally."
    present = present_metric_dates or set()
    missing = [
        first + timedelta(days=offset)
        for offset in range((last - first).days + 1)
        if (first + timedelta(days=offset)).isoformat() not in present
    ]
    if not missing:
        return None

    still_missing = _format_date_ranges(missing)
    return (
        "✅ **Sync is working again** — daily metrics are importing normally.\n\n"
        f"Daily metrics for {still_missing} are still missing, so reports "
        "covering those days may be incomplete."
    )


def _resolution_summary(
    status: str | None,
    *,
    repair_note: str | None,
    delivered: bool,
    wrote_message: bool,
    delivery: str,
) -> str:
    """Compose the event line recording that a sync alert has cleared.

    Says what the user was told as well as what happened, because the two come
    apart routinely: a backfilled gap resolves in silence by design, and a
    muted profile resolves without hearing either. An event that recorded only
    the fault clearing would make those three look identical afterwards.

    Args:
        status: The condition that had been alerted on.
        repair_note: What the daemon attempted and observed, for pipe faults.
        delivered: Whether Telegram accepted an all-clear.
        wrote_message: Whether an all-clear was composed at all.
        delivery: The preference decision — ``allowed`` or a suppression.

    Returns:
        A one-line human-readable summary.
    """
    opening = (
        f"Uploads can get through again; {repair_note}."
        if repair_note
        else f"The {status or 'sync'} alert cleared; data is importing again."
    )
    if delivered:
        return f"{opening} The all-clear was sent."
    if not wrote_message:
        return f"{opening} Nothing was sent: the gap backfilled completely."
    if delivery != "allowed":
        return f"{opening} Nothing was sent: the user has silenced sync alerts."
    return f"{opening} The all-clear could not be delivered."


class IngestHealthHandler:
    """Own one profile's ingest-health checks, alerts and repairs.

    Holds a back-reference to its runtime for the seven things it needs from
    it — the profile, the database, the state file, event recording and the
    Telegram sender — following the same collaborator shape as
    ``TelegramChatHandler``, ``DaemonRunnerHandler`` and
    ``GoogleDrivePollHandler``.
    """

    def __init__(self, runtime: "ProfileRuntime") -> None:
        """Bind this handler to the runtime whose profile it watches."""
        self._runtime = runtime

    def _newest_data_date(self, *, through: str) -> str | None:
        """Return the newest stored metric date through a completed day.

        Args:
            through: Latest completed ISO date eligible for freshness.

        Returns:
            An ISO date string, or None when the database holds no eligible
            metric.
        """
        from store import latest_metric_date, open_db

        conn = open_db(self._runtime.db)
        try:
            return latest_metric_date(conn, through=through)
        finally:
            conn.close()

    def _metric_dates(self, *, start: str, end: str) -> set[str]:
        """Return metric-bearing dates in an inclusive recovery range.

        Args:
            start: Inclusive first ISO date.
            end: Inclusive last ISO date.

        Returns:
            Stored metric-bearing dates in the range.
        """
        from store import metric_dates_in_range, open_db

        conn = open_db(self._runtime.db)
        try:
            return metric_dates_in_range(conn, start=start, end=end)
        finally:
            conn.close()

    def _restart_tailscale_app(self) -> tuple[bool, str]:
        """Quit and relaunch Tailscale, without judging whether it helped.

        Shared by both repairs, which differ only in what they watch afterwards:
        a disconnected node watches its own connection state, an unreachable
        Funnel watches the public path. Neither may infer success from this
        returning True — relaunching the app is an action, not an outcome.

        Returns:
            Whether the relaunch command succeeded, and a reason when it did not.
        """
        try:
            subprocess.run(
                ["osascript", "-e", f'quit app "{TAILSCALE_APP_NAME}"'],
                capture_output=True,
                text=True,
                timeout=TAILSCALE_RESTART_TIMEOUT_S,
                check=False,
            )
            completed = subprocess.run(
                ["open", "-a", TAILSCALE_APP_NAME],
                capture_output=True,
                text=True,
                timeout=TAILSCALE_RESTART_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.exception("Could not restart %s", TAILSCALE_APP_NAME)
            return False, str(exc)[:200]
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:200]
            logger.error(
                "Could not relaunch %s (exit %d): %s",
                TAILSCALE_APP_NAME,
                completed.returncode,
                detail,
            )
            return False, (
                f"Relaunching {TAILSCALE_APP_NAME} exited "
                f"{completed.returncode}. {detail}".strip()
            )
        return True, ""

    def _maybe_repair_unreachable_endpoint(self, *, now: datetime) -> None:
        """Restart Tailscale when the public path is dead and the node says it is fine.

        The gap this closes: ``_attempt_node_repair`` only ever runs on a node
        Tailscale reports as *offline*. On 2026-09-21 the node reported
        ``Online: true`` with ``Health: []`` and a clean DERP path for twenty
        hours while no phone could complete a TLS handshake to the Funnel, so
        the one repair the daemon owns could never have fired. Restarting the
        app cleared it in fifteen seconds. A healthy control connection does
        not imply a healthy ingress registration.

        Gated harder than the alert it shadows, because the evidence is younger
        and the action is machine-wide. The probe must have read unreachable
        continuously for ``FUNNEL_UNREACHABLE_REPAIR_AFTER_MIN`` — several
        consecutive failures at the scheduler's tick rate, so one flap cannot
        restart a working tailnet — and one outage still draws one attempt.

        Verified against the public path rather than the node, since the node
        was never the thing that was broken. The restart is credited only if
        the endpoint answers afterwards, and the event says plainly when it did
        not.

        Args:
            now: The moment of this assessment, in UTC.
        """
        from cmd_ingest import _tailscale_dns_name
        from http_ingest import public_endpoint_health, tailscale_node_health

        if INSTANCE_NAME:
            # Same reasoning as the node repair: Tailscale is machine-wide and
            # a lab instance must not restart it for the default installation.
            return
        if self._runtime.profile is None or not self._runtime.profile.operator:
            return

        seen = self._runtime._state.get("funnel_probe")
        if not isinstance(seen, dict) or seen.get("reachable") is not False:
            return
        unreachable_since = seen.get("changed_at")
        held_h = _hours_since(unreachable_since, now=now)
        if held_h is None or held_h * 60 < FUNNEL_UNREACHABLE_REPAIR_AFTER_MIN:
            return

        previous = self._runtime._state.get("endpoint_repair")
        if isinstance(previous, dict) and previous.get("since") == unreachable_since:
            return

        connected, _node_detail = tailscale_node_health()
        if connected is not True:
            # A disconnected node is the other repair's to own, and it explains
            # an unreachable endpoint outright. Restarting twice for one fault
            # would double the interruption and confuse the attribution.
            return

        self._runtime._state["endpoint_repair"] = {
            "since": unreachable_since,
            "attempted_at": now.isoformat(),
        }
        self._runtime._save_state()
        logger.warning(
            "The Funnel has been unreachable for %.1fh while the node reports "
            "online; restarting %s.",
            held_h,
            TAILSCALE_APP_NAME,
        )

        restarted, failure = self._restart_tailscale_app()
        if not restarted:
            self._runtime._record_event("ingest", "endpoint_repair_failed", failure)
            return

        dns_name = _tailscale_dns_name()
        deadline = time.monotonic() + FUNNEL_REPAIR_VERIFY_TIMEOUT_S
        reachable: bool | None = None
        detail = "the Tailscale DNS name could not be determined"
        while dns_name and time.monotonic() < deadline:
            reachable, detail = public_endpoint_health(
                dns_name, timeout_s=FUNNEL_PROBE_TIMEOUT_S
            )
            if reachable is True:
                break
            if self._runtime._stop_event.wait(10):
                break

        took = FUNNEL_REPAIR_VERIFY_TIMEOUT_S - max(deadline - time.monotonic(), 0)
        if reachable is True:
            self._runtime._state["endpoint_repair"]["recovered_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            self._runtime._save_state()
            logger.info("The Funnel answered %.0fs after the restart", took)
            self._runtime._record_event(
                "ingest",
                "endpoint_repair_recovered",
                f"Restarted {TAILSCALE_APP_NAME} after {held_h:.1f}h unreachable; "
                f"the public path answered {took:.0f}s later. {detail}",
                {"unreachable_h": round(held_h, 2), "recovered_after_s": round(took)},
            )
            return

        logger.error(
            "The Funnel was still unreachable %.0fs after restarting %s",
            took,
            TAILSCALE_APP_NAME,
        )
        self._runtime._record_event(
            "ingest",
            "endpoint_repair_failed",
            f"Restarted {TAILSCALE_APP_NAME} after {held_h:.1f}h unreachable, and "
            f"the public path was still not answering {took:.0f}s later, so this "
            f"restart fixed nothing. {detail}",
            {"unreachable_h": round(held_h, 2)},
        )

    def _attempt_node_repair(self, outage_since: str | None) -> str | None:
        """Restart Tailscale once per outage and verify whether it reconnected.

        This replaces re-asserting the Funnel mapping, which was measured three
        times against live outages and never changed either the node state or
        the DNS record. On 2026-08-29 the node was disconnected from the
        coordination server while the machine's own network was fine, and only
        restarting the app — which respawns the system network extension
        holding that connection — brought it back, in about twelve seconds.
        ``tailscale down`` followed by ``tailscale up`` was tried first and did
        nothing; it re-runs the login flow rather than the wedged process.

        The result is verified here rather than left to the next health cycle.
        A repair whose effect is inferred from what recovered afterwards is how
        "recreate the Funnel is the fix" entered the docs on no evidence, so
        this observes the one thing it can actually attribute: whether the node
        came back online within seconds of the restart it performed.

        Args:
            outage_since: Start of the current outage, so one outage draws one
                restart however many cycles it spans.

        Returns:
            ``"reconnected"`` or ``"failed"`` when a restart ran, or None when
            none was attempted.
        """
        from http_ingest import tailscale_node_health

        if INSTANCE_NAME:
            # Tailscale is machine-wide. A lab instance restarting it would drop
            # the live installation's tailnet to repair a fault it does not own,
            # which is the same reasoning that stops it re-pointing the Funnel.
            logger.info(
                "Not restarting Tailscale: instance %r must not restart a "
                "machine-wide service the default installation depends on.",
            )
            return None

        previous = self._runtime._state.get("node_repair")
        if isinstance(previous, dict) and previous.get("since") == outage_since:
            return None

        offline_h = _hours_since(outage_since, now=datetime.now(timezone.utc))
        if offline_h is not None and offline_h * 60 < NODE_OFFLINE_REPAIR_AFTER_MIN:
            # Tailscale reconnects on its own after a wake or a network change.
            # Restarting it during one of those interrupts a tailnet that was
            # about to recover anyway.
            return None

        self._runtime._state["node_repair"] = {
            "since": outage_since,
            "attempted_at": datetime.now(timezone.utc).isoformat(),
        }
        self._runtime._save_state()

        logger.warning(
            "Tailscale has been disconnected for %s; restarting %s to reconnect it.",
            f"{offline_h:.1f}h" if offline_h is not None else "a while",
            TAILSCALE_APP_NAME,
        )
        restarted, failure = self._restart_tailscale_app()
        if not restarted:
            self._runtime._record_event("ingest", "node_repair_failed", failure)
            return "failed"

        deadline = time.monotonic() + TAILSCALE_RECONNECT_TIMEOUT_S
        connected: bool | None = None
        while time.monotonic() < deadline:
            connected, _detail = tailscale_node_health()
            if connected is True:
                break
            if self._runtime._stop_event.wait(3):
                break

        took = TAILSCALE_RECONNECT_TIMEOUT_S - max(deadline - time.monotonic(), 0)
        if connected is True:
            self._runtime._state["node_repair"]["reconnected_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            self._runtime._save_state()
            logger.info("Tailscale reconnected %.0fs after the restart", took)
            self._runtime._record_event(
                "ingest",
                "node_repair_reconnected",
                f"Restarted {TAILSCALE_APP_NAME}; the node reported online again "
                f"{took:.0f}s later. The public DNS record follows within about "
                "ten minutes.",
            )
            return "reconnected"

        logger.error(
            "Tailscale did not reconnect within %.0fs of the restart",
            TAILSCALE_RECONNECT_TIMEOUT_S,
        )
        self._runtime._record_event(
            "ingest",
            "node_repair_failed",
            f"Restarted {TAILSCALE_APP_NAME}, but the node was still offline "
            f"{TAILSCALE_RECONNECT_TIMEOUT_S:.0f}s later.",
        )
        return "failed"

    def _observe_funnel_reachability(self, health, *, now: datetime) -> None:
        """Record what the public-path probe sees, without acting on it.

        The probe asks the question the DNS lookup only approximates — can a
        phone reach the receiver — and on 2026-09-21 the two disagreed for
        twenty hours: the record resolved, the handshake died at the ingress,
        and the assessment read ``ok`` while nothing could upload. It is better
        evidence, and it is not yet measured evidence, so while
        ``FUNNEL_PROBE_OBSERVE_ONLY`` holds it alerts on nothing and only
        writes down what it saw.

        Runs on every cycle rather than after a stretch of silence, unlike the
        DNS lookup it shadows. The unknown worth measuring is how often it
        reports a fault while the pipe is demonstrably fine, and that number
        only exists in the cycles where uploads are arriving normally.

        Only a change of verdict is recorded. A row every half hour would bury
        the transitions in the same table the alert history now lives in, and
        the transitions are the measurement.

        Args:
            health: The condition assessed this cycle, recorded alongside the
                probe so a disagreement between them is visible afterwards.
            now: The moment of this assessment, in UTC.
        """
        from cmd_ingest import _tailscale_dns_name
        from http_ingest import public_endpoint_health

        if not FUNNEL_PROBE_OBSERVE_ONLY:
            # The probe is the funnel resolver now, so its verdict is already
            # the assessment and shadowing it would only double the requests.
            return
        if self._runtime.profile is None or not self._runtime.profile.operator:
            return

        dns_name = _tailscale_dns_name()
        if not dns_name:
            return
        try:
            reachable, detail = public_endpoint_health(
                dns_name, timeout_s=FUNNEL_PROBE_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 - an observation must never break a check
            logger.exception("Funnel reachability probe failed to run")
            return

        previous = self._runtime._state.get("funnel_probe")
        last = previous.get("reachable") if isinstance(previous, dict) else None
        if isinstance(previous, dict) and reachable is last:
            return

        self._runtime._state["funnel_probe"] = {
            "reachable": reachable,
            "changed_at": now.isoformat(),
            "detail": detail[:300],
        }
        self._runtime._save_state()
        verdict = {True: "reachable", False: "unreachable", None: "unknown"}[reachable]
        agrees = (reachable is not False) == (health.status != "funnel")
        self._runtime._record_event(
            "ingest",
            "funnel_probe",
            f"The public path is {verdict}: {detail}"
            + ("" if agrees else f" The DNS check disagrees ({health.status})."),
            {
                "reachable": reachable,
                "previous": last,
                "health_status": health.status,
                "agrees_with_dns_check": agrees,
                "observe_only": True,
            },
        )

    def _funnel_miss_confirmed(self, health, *, now: datetime) -> bool:
        """Return whether a missing DNS record has persisted long enough to act on.

        The record is read over DoH from outside the tailnet, and one failed
        lookup is not an outage: a single miss on 2026-09-14 sat inside an
        ordinary afternoon lull, resolved by the next check, and cost two
        messages — the alert and its all-clear — for a fault that never
        existed. Requiring the miss to survive ``FUNNEL_DNS_CONFIRM_AFTER_MIN``
        delays a genuine outage by one scheduled check and removes that class.

        An unconfirmed miss is not an all-clear either, which is why this
        answers False rather than letting the caller fall through to the
        freshness check: a standing alert must not be resolved by a blip.

        Args:
            health: The condition just assessed.
            now: The moment of this assessment, in UTC.

        Returns:
            True when the caller should act on ``health``, False while a first
            DNS miss is still unconfirmed.
        """
        if health.status != "funnel":
            if self._runtime._state.pop("funnel_dns_miss", None) is not None:
                self._runtime._save_state()
            return True

        seen = self._runtime._state.get("funnel_dns_miss")
        if not isinstance(seen, dict):
            # The first miss of this outage. Any other status clears the marker
            # above, so a marker that survives to here was left by an unbroken
            # run of failed lookups and needs no outage identity of its own.
            self._runtime._state["funnel_dns_miss"] = {
                "since": health.since,
                "first_seen": now.isoformat(),
            }
            self._runtime._save_state()
            logger.info(
                "Funnel DNS record missing for %s; waiting %.0f min for a "
                "second check before reporting it.",
                self._runtime.profile.name if self._runtime.profile else "profile",
                FUNNEL_DNS_CONFIRM_AFTER_MIN,
            )
            return False

        held_h = _hours_since(seen.get("first_seen"), now=now)
        if held_h is not None and held_h * 60 < FUNNEL_DNS_CONFIRM_AFTER_MIN:
            return False
        return True

    def _data_has_resumed(self, alerted: object, health, *, now: datetime) -> bool:
        """Return whether data has actually arrived since an alert was sent.

        The fault clearing and the data returning are separate events, and
        announcing the first as if it were the second is how "✅ Sync is working
        again" reached the user twice while nothing was syncing: on 2026-09-15
        23 hours before the next upload, and on 2026-09-21 with the phone 20
        hours silent. Both times the Funnel's DNS record had come back, which is
        all the funnel condition ever measured.

        So the all-clear waits for an import newer than the alert. Every
        condition is gated the same way — a stale gap closes by importing the
        missing days, a stalled pipe by importing what it held — so one rule
        covers them without a per-status exception to keep in step.

        Args:
            alerted: The recorded ``data_health_alert`` state entry.
            health: The condition just assessed.
            now: The moment of this assessment, in UTC.

        Returns:
            True when an import has succeeded since the alert was sent.
        """
        if not isinstance(alerted, dict):
            return True
        sent_at = alerted.get("sent_at")
        alerted_at = _parse_utc(sent_at)
        if alerted_at is None:
            # A record written before this field existed, or a corrupted one.
            # Clearing it is the safe reading: the alternative is an alert that
            # can never resolve and therefore never fires again.
            return True
        imported_at = _parse_utc(health.last_import)
        if imported_at is not None and imported_at > alerted_at:
            return True

        if not alerted.get("awaiting_data_since"):
            alerted["awaiting_data_since"] = now.isoformat()
            self._runtime._save_state()
            logger.info(
                "Ingest fault for %s cleared but nothing has imported since the "
                "alert; holding the all-clear until data arrives.",
                self._runtime.profile.name if self._runtime.profile else "profile",
            )
        return False

    def _node_repair_attribution(self, *, now: datetime) -> str:
        """Describe what the daemon attempted against a pipe fault, and what it saw.

        Records what was attempted and what was observed, and stops there. An
        earlier version wrote "1.0h after the re-assert", which reads as a cause
        and was wrong: the re-assert had done nothing, and a hand-run app
        restart the daemon never saw was what reconnected the node. Crediting
        whichever command ran last is exactly how a fix that has never worked
        stayed in the docs for weeks.

        Args:
            now: The moment of this assessment, in UTC.

        Returns:
            A clause naming the attempt and its observed outcome.
        """
        attempt = self._runtime._state.get("node_repair")
        if not isinstance(attempt, dict):
            return "no repair was attempted by the daemon"
        since_attempt = _hours_since(attempt.get("attempted_at"), now=now)
        when = (
            f"{since_attempt:.1f}h earlier" if since_attempt is not None else "earlier"
        )
        if attempt.get("reconnected_at") is not None:
            return f"the daemon restarted Tailscale {when} and saw the node come back online"
        return (
            f"the daemon restarted Tailscale {when} and never saw the node come "
            "back online, so this recovery is not attributable to it"
        )

    def check(self, prefs: dict) -> None:
        """Report a sustained ingest failure once, and its recovery once.

        A broken phone is otherwise completely silent: uploads simply stop, or
        stop pairing, and the only trace is a daemon log nobody reads. That is
        survivable for the operator and fatal for a hosted profile, whose owner
        would conclude the product is dead.

        Both ends of that report are evidence-gated. A fault is not believed on
        one failed DNS lookup (:meth:`_funnel_miss_confirmed`), and it is not
        declared over until data has actually arrived again
        (:meth:`_data_has_resumed`). Each message that does go out is written to
        the events table, because the daemon log rotates within a week and the
        alert history is what these thresholds have to be tuned against.

        Args:
            prefs: Raw notification preferences for this profile.
        """
        from cmd_ingest import _tailscale_dns_name
        from http_ingest import (
            assess_ingest_health,
            newest_expected_day,
            public_dns_health,
            public_endpoint_health,
            tailscale_node_health,
        )
        from notification_prefs import (
            effective_notification_prefs,
            evaluate_data_health_delivery,
        )

        if (
            self._runtime.profile is None
            or self._runtime.profile.import_source != "http"
        ):
            return

        settings = effective_notification_prefs(prefs)["data_health"]
        assessment_now = datetime.now(timezone.utc)

        def resolve_funnel_dns() -> tuple[bool | None, str]:
            """Resolve the Funnel hostname from outside the tailnet.

            Only called after a stretch of upload silence, so the cost is a
            single DoH request on an already-suspicious cycle. An unknown
            hostname reports None rather than False: a host that cannot name
            its own Funnel has not proved the record is missing.
            """
            dns_name = _tailscale_dns_name()
            if not dns_name:
                return None, "the Tailscale DNS name could not be determined"
            return public_dns_health(dns_name)

        def resolve_funnel_endpoint() -> tuple[bool | None, str]:
            """Ask whether a phone could reach the receiver, not whether a name resolves.

            Same signature as the DNS resolver so it drops into the same slot;
            which one is used is `FUNNEL_PROBE_OBSERVE_ONLY`.
            """
            dns_name = _tailscale_dns_name()
            if not dns_name:
                return None, "the Tailscale DNS name could not be determined"
            return public_endpoint_health(dns_name, timeout_s=FUNNEL_PROBE_TIMEOUT_S)

        # One Funnel serves every profile on this host, so the record is checked
        # once and reported to the operator alone. Assessing it per profile
        # would send N people the same alert about one fault that only the
        # operator can act on, and spend N lookups to do it. A hosted profile
        # loses nothing by not hearing: the rolling exports re-send the backlog
        # once the record returns.
        funnel_resolver = None
        if self._runtime.profile.operator:
            funnel_resolver = (
                resolve_funnel_dns
                if FUNNEL_PROBE_OBSERVE_ONLY
                else resolve_funnel_endpoint
            )
        # Same reasoning as the Funnel record, and the same owner: one Mac holds
        # one Tailscale connection for every profile on it, so asking per
        # profile would spend N probes on one answer only the operator can act
        # on. A hosted profile hears nothing and loses nothing — the restart and
        # the backlog re-send both happen without them.
        node_resolver = (
            tailscale_node_health if self._runtime.profile.operator else None
        )

        try:
            newest_data_date = self._newest_data_date(
                through=newest_expected_day(assessment_now).isoformat()
            )
            health = assess_ingest_health(
                self._runtime.profile,
                newest_data_date=newest_data_date,
                stale_after_days=settings["stale_after_days"],
                split_after_h=settings["split_after_h"],
                pair_window_s=HTTP_INGEST_PAIR_WINDOW_S,
                resolve_funnel_dns=funnel_resolver,
                resolve_node_health=node_resolver,
                now=assessment_now,
            )
        except (OSError, sqlite3.Error):
            logger.exception(
                "Could not assess ingest health for %s", self._runtime.profile.name
            )
            return

        self._observe_funnel_reachability(health, now=assessment_now)
        self._maybe_repair_unreachable_endpoint(now=assessment_now)

        # One local now for every delivery decision below: quiet-hours gating is
        # wall-clock, so the recovery ping and the alert must agree on the time.
        now = assessment_now.astimezone()

        if not self._funnel_miss_confirmed(health, now=assessment_now):
            # A single failed DNS lookup is not yet an outage, and until it is
            # confirmed it is not an all-clear either: falling through here on
            # the first miss would let a blip resolve a standing alert.
            return

        alerted = self._runtime._state.get("data_health_alert")
        if not health.is_alerting:
            if alerted:
                if not self._data_has_resumed(alerted, health, now=assessment_now):
                    return
                recovery = evaluate_data_health_delivery(prefs, now=now)
                if recovery["status"] == "deferred":
                    # Hold the record, not just the message: an alert the user
                    # cannot tell has resolved is one they learn to ignore, so a
                    # fault that clears overnight still gets its morning notice.
                    # A suppressed one is dropped below, as the user asked for.
                    return
                message = None
                if recovery["status"] == "allowed":
                    missing_from = (
                        alerted.get("missing_from")
                        if isinstance(alerted, dict)
                        else None
                    )
                    missing_to = (
                        alerted.get("missing_to") if isinstance(alerted, dict) else None
                    )
                    present_metric_dates = None
                    if isinstance(missing_from, str) and isinstance(missing_to, str):
                        try:
                            present_metric_dates = self._metric_dates(
                                start=missing_from,
                                end=missing_to,
                            )
                        except (OSError, sqlite3.Error):
                            logger.exception(
                                "Could not check recovered metric dates for %s",
                                self._runtime.profile.name,
                            )
                            return
                    message = _recovery_message(alerted, present_metric_dates)

                status = alerted.get("status") if isinstance(alerted, dict) else None
                repair_note: str | None = None
                if status in {"funnel", "node"}:
                    repair_note = self._node_repair_attribution(now=assessment_now)
                    self._runtime._state.pop("node_repair", None)
                waited_h = _hours_since(
                    alerted.get("awaiting_data_since")
                    if isinstance(alerted, dict)
                    else None,
                    now=assessment_now,
                )
                self._runtime._state.pop("data_health_alert", None)
                self._runtime._save_state()

                delivered = False
                if recovery["status"] == "allowed" and message:
                    delivered = bool(self._runtime._poller.send_reply(message))
                self._runtime._record_event(
                    "ingest",
                    "alert_resolved",
                    _resolution_summary(
                        status,
                        repair_note=repair_note,
                        delivered=delivered,
                        wrote_message=message is not None,
                        delivery=recovery["status"],
                    ),
                    {
                        "status": status,
                        "notified": delivered,
                        "delivery": recovery["status"],
                        "awaited_data_h": (
                            round(waited_h, 2) if waited_h is not None else None
                        ),
                        "repair": repair_note,
                    },
                )
            return

        now_iso = now.astimezone(timezone.utc).isoformat()
        repair: str | None = None
        escalating = False
        if health.status == "node":
            repair = self._attempt_node_repair(health.since)
        if health.status == "funnel":
            outage_h = _hours_since(health.since, now=assessment_now)
            escalating = (
                outage_h is not None and outage_h >= FUNNEL_OUTAGE_ESCALATE_AFTER_H
            )

        if isinstance(alerted, dict) and alerted.get("status") == health.status:
            if health.status == "funnel":
                # A wait, not a task: repeating it every 24h would train the
                # operator to swipe away the channel that also carries the
                # faults they must act on. Only an outage that outlives the
                # known band earns a second message, and only one.
                if not escalating or alerted.get("escalated"):
                    return
            else:
                last_sent = alerted.get("sent_at")
                if last_sent and not _older_than(last_sent, DATA_HEALTH_REALERT_S):
                    return

        decision = evaluate_data_health_delivery(prefs, now=now)
        if decision["status"] == "deferred":
            # A quiet-hours hold, not a delivery failure: leave the alert
            # unrecorded so a later tick re-evaluates and delivers once the
            # window closes. Recording it as sent would let the 24h de-dup guard
            # above swallow the alert entirely.
            logger.info(
                "Ingest health alert for %s deferred until %s (quiet hours)",
                self._runtime.profile.name,
                decision.get("until", "morning"),
            )
            return

        # The missing range is recorded alongside the timestamp so the recovery
        # notice can say which of those days actually came back.
        alerted_record = {
            "status": health.status,
            "sent_at": now_iso,
            "missing_from": health.missing_from,
            "missing_to": health.missing_to,
            "escalated": escalating,
        }

        def _mark_alerted() -> None:
            """Record this alert as delivered, arming the 24h de-dup guard."""
            self._runtime._state["data_health_alert"] = alerted_record
            self._runtime._save_state()

        logger.warning(
            "Ingest health for %s is %s: %s",
            self._runtime.profile.name,
            health.status,
            health.detail,
        )

        if decision["status"] != "allowed":
            # A preference the user set, not a delivery failure. Record it so
            # the guard holds: they asked not to hear this.
            logger.info(
                "Ingest health alert suppressed by prefs: %s",
                decision.get("reason", "unknown"),
            )
            self._runtime._record_event(
                "ingest",
                "alert_suppressed",
                f"A {health.status} alert was due but the user has silenced "
                f"sync alerts ({decision.get('reason', 'unknown')}).",
                {"status": health.status, "reason": decision.get("reason")},
            )
            _mark_alerted()
            return
        headings = {
            "stale": "Daily health metrics are missing",
            # Named for the cause, not the symptom: neither of these is the
            # user's to chase, and the message exists so they stop looking.
            "funnel": "Your phone can't upload — Tailscale's side",
            "node": "Your phone can't upload — this Mac dropped off Tailscale",
            # Both describe a fault on this Mac while the phone is provably
            # still reaching us. Without them these fell through to the generic
            # heading below, which named the one thing that was working.
            "split": "Your phone's uploads are arriving but not importing",
            "error": "The last import of your health data failed",
        }
        heading = headings.get(health.status, "Health data isn't syncing")
        if escalating:
            heading = "Your phone hasn't been able to upload for two days"
        body = health.detail
        if escalating:
            body += (
                " This one has lasted longer than any before it, so it is no "
                "longer the usual pattern and is worth raising with Tailscale."
            )
        # Each line says what was done and what was observed. Nothing here
        # promises a fix the daemon has not watched take effect.
        elif repair == "reconnected":
            body += (
                " I restarted Tailscale and this Mac is connected again. "
                "Uploads should resume within about ten minutes, once the "
                "address is published again. Nothing is lost meanwhile — your "
                "phone re-sends the backlog."
            )
        elif repair == "failed":
            body += (
                " I restarted Tailscale and it did not reconnect, so this one "
                "needs a look: open the Tailscale app on the Mac and check it "
                "is signed in."
            )
        # Record only once Telegram has taken it. This used to be recorded
        # before the send, to stop a delivery failure from re-firing every
        # tick — but the 24h de-dup guard above then swallowed the alert
        # entirely. On 2 Sep 2026 that is exactly what happened: the send
        # raised "[Errno 49] Can't assign requested address", the state said
        # the user had been told, and three days of missing uploads went
        # unreported. Sends retry their own transport faults now, so reaching
        # here with nothing delivered means a real outage worth carrying to
        # the next tick rather than forgetting.
        if self._runtime._poller.send_reply(
            f"⚠️ **{heading}**\n\n{body}\n\n"
            "Say _mute sync alerts for a week_ to silence this."
        ):
            _mark_alerted()
            # Recorded here rather than in the daemon log alone. The log is the
            # only other trace and it rotates within about a week, so the
            # question this channel exists to answer — how often does it fire,
            # and was each one deserved — was unanswerable past the last few
            # days. The events table keeps it for as long as the database does.
            self._runtime._record_event(
                "ingest",
                "alert_sent",
                f"{heading}. {health.detail}",
                {
                    "status": health.status,
                    "missing_from": health.missing_from,
                    "missing_to": health.missing_to,
                    "escalated": escalating,
                    "repair": repair,
                    "since": health.since,
                },
            )
        else:
            logger.error(
                "Ingest health alert for %s could not be delivered; "
                "will try again on the next tick",
                self._runtime.profile.name,
            )
