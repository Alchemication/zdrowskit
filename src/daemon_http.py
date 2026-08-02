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
from http_ingest import (
    HttpIngestManager,
    HttpIngestServer,
    TokenRegistry,
)

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
            )
            if profiles
            else None
        )
        self._registry = TokenRegistry(HTTP_INGEST_TOKEN_FILE) if profiles else None
        self._server: HttpIngestServer | None = None

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
        before = runtime._runners._data_snapshot()
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
        after = runtime._runners._data_snapshot()
        trigger_context = runtime._runners._format_data_delta(before, after)
        runtime._state["last_data_snapshot"] = after
        runtime._save_state()
        runtime._runners._run_nudge("new_data", trigger_context=trigger_context)

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
            for profile_name, pair_digest in self._manager.pending_pairs():
                self._queue_pair(profile_name, pair_digest)
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
