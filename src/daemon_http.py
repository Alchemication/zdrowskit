"""HTTP ingest lifecycle and complete-pair import flow for the daemon."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from config import (
    HTTP_INGEST_HOST,
    HTTP_INGEST_MAX_BYTES,
    HTTP_INGEST_PAIR_WINDOW_S,
    HTTP_INGEST_PORT,
    HTTP_INGEST_TOKEN_FILE,
)
from daemon_data import changed_workout_ids, data_snapshot, format_data_delta
from http_ingest import HttpIngestManager, TokenRegistry
from http_ingest_server import HttpIngestServer

if TYPE_CHECKING:
    from daemon import Daemon, ProfileRuntime
    from profiles import Profile

logger = logging.getLogger(__name__)


class DaemonHttpIngestHandler:
    """Own the receiver and queue verified pairs on profile workers."""

    def __init__(self, daemon: Daemon, profiles: dict[str, Profile]) -> None:
        """Initialize HTTP state without binding a socket yet."""
        self._daemon = daemon
        self._manager = (
            HttpIngestManager(
                profiles,
                pair_window_s=HTTP_INGEST_PAIR_WINDOW_S,
                on_pair_ready=self._queue_pair,
                on_upload=self._record_upload,
            )
            if profiles
            else None
        )
        self._registry = TokenRegistry(HTTP_INGEST_TOKEN_FILE) if profiles else None
        self._server: HttpIngestServer | None = None

    def _record_upload(
        self, profile_name: str, kind: str, gap_s: float | None, duplicate: bool
    ) -> None:
        """Record one upload's arrival, so the real cadence can be measured.

        Liveness is about to be read off this stream instead of being inferred
        from pair formation, and a threshold needs the distribution it is being
        set against. The configured schedule is no guide: the automations are
        set to five minutes and iOS delivers them whenever it allows a
        background run, so only the arrivals themselves say what is achievable.

        Written per half rather than per pair because that is the asymmetry
        that matters — when the 2026-09-21 outage began, Metrics was 20.7h
        stale and Workouts already 50.3h, a 29-hour head start that pair-level
        events could never have shown.

        Args:
            profile_name: Profile the upload belongs to.
            kind: ``metrics`` or ``workouts``.
            gap_s: Seconds since the previous upload of this kind, or None for
                the first one ever seen.
            duplicate: Whether the body was identical to the previous upload.
        """
        runtime = self._daemon.runtimes.get(profile_name)
        if runtime is None:
            return
        gap = f"{gap_s / 60:.1f} min after the previous one" if gap_s else "first seen"
        runtime._record_event(
            "ingest",
            "upload_received",
            f"{kind.capitalize()} upload arrived, {gap}"
            + (" (identical body)" if duplicate else ""),
            {
                "kind": kind,
                "gap_s": round(gap_s) if gap_s is not None else None,
                "duplicate": duplicate,
            },
        )

    def _queue_pair(self, profile_name: str, pair_digest: str) -> None:
        """Queue one complete pair on its profile's serialized worker."""
        runtime = self._daemon.runtimes.get(profile_name)
        if runtime is not None:
            self._daemon._submit(runtime, self._import_pair, runtime, pair_digest)

    def _import_pair(
        self,
        runtime: ProfileRuntime,
        pair_digest: str,
    ) -> None:
        """Import one immutable pair and record its durable receipt."""
        if self._manager is None:
            return
        generation = self._manager.begin_import(runtime.name, pair_digest)
        if generation is None:
            return
        runtime._record_event(
            "import",
            "started",
            "Complete HTTP Metrics and Workouts pair received",
        )
        before = data_snapshot(runtime.db)
        try:
            result = runtime._runners._run_import(data_dir=generation)
            if result is None:
                self._manager.finish_import(
                    runtime.name,
                    pair_digest,
                    success=False,
                    error="Health import failed; check daemon logs.",
                )
                return
            self._manager.finish_import(
                runtime.name,
                pair_digest,
                success=True,
            )
        except Exception as exc:
            self._manager.finish_import(
                runtime.name,
                pair_digest,
                success=False,
                error=str(exc),
            )
            raise
        after = data_snapshot(runtime.db)
        trigger_context = format_data_delta(runtime.db, before, after)
        standout_workout_ids = changed_workout_ids(before, after)
        runtime._state["last_data_snapshot"] = after
        runtime._save_state()
        runtime._runners._run_nudge(
            "new_data",
            trigger_context=trigger_context,
            standout_workout_ids=standout_workout_ids,
        )

    def sweep(self) -> None:
        """Queue every staged pair that is now due to import.

        An arriving upload is what normally triggers an import, so a pair whose
        halves missed each other has nothing left to re-examine it — the next
        upload may be days away, or blocked entirely by a Funnel outage. This
        runs on a timer so the wait ends on schedule instead of on luck.
        """
        if self._manager is None:
            return
        for profile_name, pair_digest in self._manager.due_pairs():
            logger.info("Queuing due HTTP pair for %s", profile_name)
            self._queue_pair(profile_name, pair_digest)

    def start(self) -> None:
        """Bind the loopback receiver and recover any durable pending pairs."""
        if self._manager is None or self._registry is None:
            return
        try:
            self._server = HttpIngestServer(
                HTTP_INGEST_HOST,
                HTTP_INGEST_PORT,
                registry=self._registry,
                manager=self._manager,
                max_bytes=HTTP_INGEST_MAX_BYTES,
            )
            self._server.start()
            logger.info(
                "HTTP ingest listening on http://%s:%d/v1/auto-export",
                HTTP_INGEST_HOST,
                HTTP_INGEST_PORT,
            )
            self.sweep()
        except OSError as exc:
            logger.error(
                "HTTP ingest could not bind %s:%d: %s. Telegram and scheduled "
                "work will continue; run `main.py ingest status` to diagnose it.",
                HTTP_INGEST_HOST,
                HTTP_INGEST_PORT,
                exc,
            )

    def stop(self) -> None:
        """Stop the receiver when it was started successfully."""
        if self._server is not None:
            self._server.stop()
            self._server = None
