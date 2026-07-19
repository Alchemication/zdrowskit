"""Google Drive polling lifecycle for the zdrowskit daemon."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from daemon import ZdrowskitDaemon

logger = logging.getLogger(__name__)


class GoogleDrivePollHandler:
    """Poll Drive, import changed exports, and trigger new-data nudges."""

    def __init__(self, daemon: "ZdrowskitDaemon") -> None:
        self._d = daemon
        # A persistent failure (bad folder ID, revoked share, expired key)
        # would otherwise add one failure event per poll interval. Record the
        # first failure, then stay quiet until a poll succeeds again.
        self._failure_event_recorded = False

    def poll_once(self, *, force_import: bool) -> bool:
        """Poll Drive once, import changes, and trigger a new-data nudge.

        Args:
            force_import: Parse the cache even when Drive has no changed files.
                Used for the first poll after daemon startup.

        Returns:
            True when the poll and any required import completed successfully.
        """
        try:
            with self._d._import_lock:
                before = self._d._runners._data_snapshot()
                result = self._d._runners._run_import(
                    skip_if_drive_unchanged=not force_import,
                    record_no_changes=False,
                    record_failure=not self._failure_event_recorded,
                )
                after = self._d._runners._data_snapshot()
        except Exception as exc:
            logger.exception("Google Drive poll failed: %s", exc)
            if not self._failure_event_recorded:
                self._d._record_event(
                    "import",
                    "failed",
                    "Google Drive poll failed",
                    {"error": str(exc)},
                )
                self._failure_event_recorded = True
            return False

        if result is None:
            # _run_import already recorded the failure event when this is the
            # first failure of the streak.
            self._failure_event_recorded = True
            return False
        if self._failure_event_recorded:
            logger.info("Google Drive poll recovered")
            self._failure_event_recorded = False
        if result.drive_files_downloaded == 0:
            logger.debug("Google Drive poll found no changed files")
            return True

        self._d._record_event(
            "import",
            "drive_update",
            f"Google Drive fetched {result.drive_files_downloaded} changed file(s)",
            {
                "downloaded": result.drive_files_downloaded,
                "current": result.drive_files_skipped,
            },
        )
        if result.parsed_days == 0:
            return True

        trigger_context = self._d._runners._format_data_delta(before, after)
        self._d._state["last_data_snapshot"] = after
        from daemon import _save_state

        _save_state(self._d._state)
        self._d._runners._run_nudge("new_data", trigger_context=trigger_context)
        return True

    def run(self) -> None:
        """Poll Drive immediately, then at the configured interval."""
        needs_full_import = True
        while not self._d._stop_event.is_set():
            if self.poll_once(force_import=needs_full_import):
                needs_full_import = False
            if self._d._stop_event.wait(self._d.google_drive_poll_interval_s):
                return
