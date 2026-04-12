"""/add manual-activity flow for the Telegram bot.

Handles the inline-keyboard wizard for logging a workout or sleep entry by
hand. Extracted from ``daemon.py`` to keep that module focused on the file
watcher and scheduling loop.

The flow is driven by the daemon's Telegram callback dispatcher, which
forwards every ``add_*`` callback to :meth:`AddFlowHandler.handle_callback`.
The handler owns its own pending-state map and lock; it borrows the daemon's
``db`` path and ``_poller`` for I/O.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from daemon import ZdrowskitDaemon

logger = logging.getLogger(__name__)


_PENDING_ADD_TTL_S = 600  # 10 min


@dataclass
class PendingAdd:
    """In-flight /add manual activity flow state."""

    step: str  # pick_type | confirm_workout | pick_sleep_date | pick_sleep_dur | confirm_sleep
    message_id: int
    created_at: float  # time.monotonic() for TTL cleanup
    type_options: list[dict] | None = None  # [{type, category, count}, ...]
    workout_type: str | None = None
    category: str | None = None
    clone_row: dict | None = None  # full workout column dict from LLM
    date: str | None = None
    sleep_total_h: float | None = None
    sleep_in_bed_h: float | None = None
    saved_id: int | None = None  # row id after save, for undo
    saved_table: str | None = None  # "manual_workout" or "manual_sleep"


def find_workout_clone(
    conn: sqlite3.Connection,
    workout_type: str,
    category: str,
) -> dict:
    """Find the best historical workout to clone via LLM.

    Queries recent workouts and asks a lightweight LLM to pick (or
    synthesize) the best match for the requested type. Falls back to the
    most recent same-type workout on LLM failure.

    Args:
        conn: Open database connection.
        workout_type: Requested workout type name (e.g. "Outdoor Run").
        category: Workout category (e.g. "run").

    Returns:
        Dict with workout column values suitable for ``insert_manual_workout``.
    """
    from llm import call_llm
    from store import _WORKOUT_CLONE_COLUMNS

    rows = conn.execute(
        """
        SELECT * FROM workout_all
        ORDER BY date DESC
        LIMIT 20
        """,
    ).fetchall()

    if not rows:
        return {
            "type": workout_type,
            "category": category,
            "duration_min": 30,
            "source_note": "default (no history)",
        }

    history = []
    for r in rows:
        entry: dict = {}
        for col in _WORKOUT_CLONE_COLUMNS:
            val = r[col] if col in r.keys() else None
            if val is not None:
                entry[col] = val
        entry["date"] = r["date"]
        history.append(entry)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a fitness data assistant. The user wants to manually "
                "log a workout. Based on their recent history, pick the single "
                "best workout to clone as a template, or synthesize one from "
                "partial matches if no exact type match exists.\n\n"
                "Return ONLY a JSON object with these fields:\n"
                + ", ".join(_WORKOUT_CLONE_COLUMNS)
                + ", source_note\n\n"
                "source_note should briefly explain your choice "
                '(e.g. "cloned from Apr 1 Outdoor Run" or '
                '"scaled from 5K tempo to 2K distance").\n\n'
                "Rules:\n"
                "- Copy HR and sensor fields from the source workout as-is.\n"
                "- If scaling duration/distance, scale active_energy_kj proportionally.\n"
                "- counts_as_lift should be 1 for strength workouts, 0 otherwise.\n"
                "- Return valid JSON only, no markdown fences."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Log this workout: {workout_type} (category: {category})\n\n"
                f"Recent history ({len(history)} workouts):\n"
                f"{json.dumps(history, default=str)}"
            ),
        },
    ]

    try:
        result = call_llm(
            messages,
            model="anthropic/claude-haiku-4-5-20251001",
            max_tokens=512,
            temperature=0.2,
            conn=conn,
            request_type="add_clone",
        )
        text = result.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        clone = json.loads(text)
        clone.setdefault("type", workout_type)
        clone.setdefault("category", category)
        clone.setdefault("duration_min", 30)
        return clone
    except Exception:
        logger.warning(
            "LLM clone failed for %s, falling back to most recent",
            workout_type,
            exc_info=True,
        )
        for r in rows:
            if r["type"] == workout_type:
                return {
                    col: r[col] if col in r.keys() else None
                    for col in _WORKOUT_CLONE_COLUMNS
                } | {"source_note": f"most recent {workout_type}"}
        for r in rows:
            if r["category"] == category:
                return {
                    col: r[col] if col in r.keys() else None
                    for col in _WORKOUT_CLONE_COLUMNS
                } | {
                    "type": workout_type,
                    "source_note": f"most recent {category} workout",
                }
        first = rows[0]
        return {
            col: first[col] if col in first.keys() else None
            for col in _WORKOUT_CLONE_COLUMNS
        } | {
            "type": workout_type,
            "category": category,
            "source_note": "most recent workout",
        }


class AddFlowHandler:
    """State machine for the /add inline-keyboard wizard.

    Owns its own pending-state map and lock so it can be reasoned about in
    isolation. Borrows the daemon's ``db`` path and ``_poller`` for I/O.
    """

    def __init__(self, daemon: "ZdrowskitDaemon") -> None:
        self._daemon = daemon
        self._lock = threading.Lock()
        self._pending: dict[str, PendingAdd] = {}
        self._counter: int = 0

    @property
    def _db(self) -> Path:
        return self._daemon.db

    @property
    def _poller(self):  # type: ignore[no-untyped-def]
        return self._daemon._poller

    def _new_id(self) -> str:
        self._counter += 1
        return f"a{self._counter}"

    def _cleanup(self) -> None:
        """Remove expired pending entries. Must hold ``self._lock``."""
        now = time.monotonic()
        expired = [
            k
            for k, v in self._pending.items()
            if now - v.created_at > _PENDING_ADD_TTL_S
        ]
        for k in expired:
            del self._pending[k]

    def handle_command(self, message_id: int) -> None:
        """Start the /add flow: show personalized workout type buttons."""
        from store import get_frequent_workout_types, open_db

        conn = open_db(self._db)
        try:
            types = get_frequent_workout_types(conn, limit=4)
        finally:
            conn.close()

        with self._lock:
            self._cleanup()
            add_id = self._new_id()

        rows: list[list[dict[str, str]]] = []
        type_row: list[dict[str, str]] = []
        for i, t in enumerate(types):
            label = f"{t['type']} ({t['count']}x)"
            type_row.append({"text": label, "callback_data": f"add_type:{add_id}:{i}"})
            if len(type_row) == 2:
                rows.append(type_row)
                type_row = []
        if type_row:
            rows.append(type_row)
        rows.append(
            [
                {"text": "\U0001f634 Sleep", "callback_data": f"add_sleep:{add_id}"},
                {"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"},
            ]
        )

        sent_id = self._poller.send_message_with_keyboard(
            "What would you like to add?",
            rows,
            reply_to_message_id=message_id,
        )

        pending = PendingAdd(
            step="pick_type",
            message_id=sent_id or 0,
            created_at=time.monotonic(),
            type_options=types,
        )
        with self._lock:
            self._pending[add_id] = pending

    def handle_callback(self, cb_id: str, data: str, msg_id: int | None) -> None:
        """Dispatch /add inline keyboard callbacks."""
        parts = data.split(":")
        action = parts[0]
        add_id = parts[1] if len(parts) > 1 else ""
        param = parts[2] if len(parts) > 2 else ""

        with self._lock:
            self._cleanup()
            pending = self._pending.get(add_id)

        if pending is None:
            self._poller.answer_callback_query(cb_id, "This flow expired.")
            if msg_id:
                self._poller.edit_message(msg_id, "This /add flow has expired.")
            return

        if action == "add_type":
            self._handle_type(cb_id, add_id, pending, int(param), msg_id)
        elif action == "add_sleep":
            self._handle_sleep_start(cb_id, add_id, pending, msg_id)
        elif action == "add_ok":
            self._handle_confirm(cb_id, add_id, pending, msg_id)
        elif action == "add_dur":
            self._show_duration_picker(cb_id, add_id, pending, msg_id)
        elif action == "add_d":
            self._handle_duration(cb_id, add_id, pending, float(param), msg_id)
        elif action == "add_dt":
            self._handle_date(cb_id, add_id, pending, param, msg_id)
        elif action == "add_sd":
            self._handle_sleep_duration(cb_id, add_id, pending, float(param), msg_id)
        elif action == "add_undo":
            self._handle_undo(cb_id, add_id, pending, msg_id)
        elif action == "add_x":
            self._handle_cancel(cb_id, add_id, msg_id)
        else:
            self._poller.answer_callback_query(cb_id, "Unknown action.")

    def _handle_type(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        type_index: int,
        msg_id: int | None,
    ) -> None:
        """User picked a workout type — run LLM clone and show confirmation."""
        self._poller.answer_callback_query(cb_id)
        if not pending.type_options or type_index >= len(pending.type_options):
            return

        chosen = pending.type_options[type_index]
        today = date.today().isoformat()

        if msg_id:
            self._poller.edit_message(
                msg_id, f"\u23f3 Finding best match for {chosen['type']}..."
            )

        from store import open_db

        conn = open_db(self._db)
        try:
            clone = find_workout_clone(conn, chosen["type"], chosen["category"])
        finally:
            conn.close()

        pending.workout_type = chosen["type"]
        pending.category = chosen["category"]
        pending.clone_row = clone
        pending.date = today
        pending.step = "confirm_workout"

        self._show_workout_confirm(add_id, pending, msg_id)

    def _check_existing_workout(self, target_date: str, workout_type: str) -> str:
        """Warn if a workout of the same type already exists for ``target_date``."""
        from store import open_db

        conn = open_db(self._db)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM workout_all WHERE date = ? AND type = ?",
                (target_date, workout_type),
            ).fetchone()
            if row and row["n"] > 0:
                return (
                    f"\u26a0\ufe0f A {workout_type} already exists for {target_date}.\n"
                )
            return ""
        finally:
            conn.close()

    def _check_existing_sleep(self, target_date: str) -> str:
        """Warn if sleep data already exists for ``target_date``."""
        from store import open_db

        conn = open_db(self._db)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sleep_all WHERE date = ?",
                (target_date,),
            ).fetchone()
            if row and row["n"] > 0:
                return f"\u26a0\ufe0f Sleep data already exists for {target_date} — saving will replace it.\n"
            return ""
        finally:
            conn.close()

    def _show_workout_confirm(
        self, add_id: str, pending: PendingAdd, msg_id: int | None
    ) -> None:
        """Display the workout confirmation screen with Save/Adjust/Date buttons."""
        clone = pending.clone_row or {}
        dur = clone.get("duration_min", 0)
        energy = clone.get("active_energy_kj")
        dist = clone.get("gpx_distance_km")
        note = clone.get("source_note", "")

        parts = [f"**{pending.workout_type}** — {dur:.0f} min"]
        if dist:
            parts.append(f"{dist:.1f} km")
        if energy:
            parts.append(f"~{energy:.0f} kJ")
        summary = ", ".join(parts)

        warning = self._check_existing_workout(
            pending.date or "", pending.workout_type or ""
        )
        text = f"{warning}{summary}\nDate: {pending.date}"
        if note:
            text += f"\n_{note}_"

        buttons = [
            [
                {"text": "\u2705 Save", "callback_data": f"add_ok:{add_id}"},
                {"text": "\u23f1 Duration", "callback_data": f"add_dur:{add_id}"},
            ],
            [
                {"text": "\U0001f4c5 Date", "callback_data": f"add_dt:{add_id}:pick"},
                {"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"},
            ],
        ]
        if msg_id:
            self._poller.edit_message_with_keyboard(msg_id, text, buttons)

    def _show_duration_picker(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        msg_id: int | None,
    ) -> None:
        """Show duration adjustment buttons based on workout category."""
        self._poller.answer_callback_query(cb_id)
        cat = pending.clone_row.get("category", "") if pending.clone_row else ""
        if cat in ("run", "walk", "cycle"):
            durations = [15, 20, 30, 45, 60, 90]
        else:
            durations = [20, 30, 45, 60, 75, 90]

        rows: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for d in durations:
            row.append({"text": f"{d} min", "callback_data": f"add_d:{add_id}:{d}"})
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"}])

        if msg_id:
            self._poller.edit_message_with_keyboard(
                msg_id, f"Pick duration for {pending.workout_type}:", rows
            )

    def _handle_duration(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        new_dur: float,
        msg_id: int | None,
    ) -> None:
        """User picked a new duration — scale energy/distance proportionally."""
        self._poller.answer_callback_query(cb_id)
        clone = pending.clone_row
        if clone:
            old_dur = clone.get("duration_min") or new_dur
            if old_dur and old_dur > 0:
                ratio = new_dur / old_dur
                if clone.get("active_energy_kj"):
                    clone["active_energy_kj"] = round(
                        clone["active_energy_kj"] * ratio, 1
                    )
                if clone.get("gpx_distance_km"):
                    clone["gpx_distance_km"] = round(
                        clone["gpx_distance_km"] * ratio, 2
                    )
            clone["duration_min"] = new_dur
            note = clone.get("source_note", "")
            if ", adjusted to " in note:
                note = note[: note.index(", adjusted to ")]
            clone["source_note"] = (
                f"{note}, adjusted to {new_dur:.0f} min"
                if note
                else f"adjusted to {new_dur:.0f} min"
            )

        pending.step = "confirm_workout"
        self._show_workout_confirm(add_id, pending, msg_id)

    def _handle_date(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        param: str,
        msg_id: int | None,
    ) -> None:
        """Handle date selection — either show picker or apply a choice."""
        self._poller.answer_callback_query(cb_id)

        today = date.today()
        if param == "pick":
            buttons = [
                [
                    {"text": "Today", "callback_data": f"add_dt:{add_id}:today"},
                    {"text": "Yesterday", "callback_data": f"add_dt:{add_id}:yest"},
                    {"text": "2 days ago", "callback_data": f"add_dt:{add_id}:bfr"},
                ],
                [{"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"}],
            ]
            if msg_id:
                self._poller.edit_message_with_keyboard(
                    msg_id, "When did you do it?", buttons
                )
            return

        if param == "today":
            pending.date = today.isoformat()
        elif param == "yest":
            pending.date = (today - timedelta(days=1)).isoformat()
        elif param == "bfr":
            pending.date = (today - timedelta(days=2)).isoformat()

        if pending.step in ("confirm_workout", "pick_type"):
            pending.step = "confirm_workout"
            self._show_workout_confirm(add_id, pending, msg_id)
        else:
            self._show_sleep_duration_picker(add_id, pending, msg_id)

    def _handle_sleep_start(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        msg_id: int | None,
    ) -> None:
        """User chose Sleep — show date picker."""
        self._poller.answer_callback_query(cb_id)
        pending.step = "pick_sleep_date"

        # Sleep is stored under the night-start date (DB convention):
        # "Last night" on a Tuesday = Monday night = date Monday = yesterday.
        buttons = [
            [
                {"text": "Last night", "callback_data": f"add_dt:{add_id}:yest"},
                {"text": "Night before", "callback_data": f"add_dt:{add_id}:bfr"},
            ],
            [{"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"}],
        ]
        if msg_id:
            self._poller.edit_message_with_keyboard(
                msg_id,
                "When was this sleep?\n_(date = night-start, e.g. Mon night = Mon)_",
                buttons,
            )

    def _show_sleep_duration_picker(
        self, add_id: str, pending: PendingAdd, msg_id: int | None
    ) -> None:
        """Show sleep duration range buttons."""
        pending.step = "pick_sleep_dur"
        durations = [5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0]
        rows: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for h in durations:
            label = f"{h:.1f}h" if h != int(h) else f"{int(h)}h"
            row.append({"text": label, "callback_data": f"add_sd:{add_id}:{h}"})
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"}])

        if msg_id:
            self._poller.edit_message_with_keyboard(
                msg_id, f"Roughly how long did you sleep? (date: {pending.date})", rows
            )

    def _handle_sleep_duration(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        hours: float,
        msg_id: int | None,
    ) -> None:
        """User picked sleep duration — show confirmation."""
        self._poller.answer_callback_query(cb_id)
        pending.sleep_total_h = hours
        pending.sleep_in_bed_h = round(hours * 1.08, 2)
        pending.step = "confirm_sleep"

        warning = self._check_existing_sleep(pending.date or "")
        text = f"{warning}**Sleep** — {hours}h\nDate: {pending.date}"
        buttons = [
            [
                {"text": "\u2705 Save", "callback_data": f"add_ok:{add_id}"},
                {"text": "\u2716 Cancel", "callback_data": f"add_x:{add_id}"},
            ]
        ]
        if msg_id:
            self._poller.edit_message_with_keyboard(msg_id, text, buttons)

    def _handle_confirm(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        msg_id: int | None,
    ) -> None:
        """Save the manual activity and show confirmation with undo."""
        self._poller.answer_callback_query(cb_id, "Saved!")
        from store import insert_manual_sleep, insert_manual_workout, open_db

        conn = open_db(self._db)
        try:
            if pending.step == "confirm_workout" and pending.clone_row:
                row_id = insert_manual_workout(
                    conn,
                    clone_row=pending.clone_row,
                    date=pending.date or date.today().isoformat(),
                    source_note=pending.clone_row.get("source_note"),
                )
                pending.saved_id = row_id
                pending.saved_table = "manual_workout"

                clone = pending.clone_row
                dur = clone.get("duration_min", 0)
                text = f"\u2705 Saved: {pending.workout_type} \u00b7 {dur:.0f} min \u00b7 {pending.date}"
            elif pending.step == "confirm_sleep" and pending.sleep_total_h:
                row_id = insert_manual_sleep(
                    conn,
                    date=pending.date or date.today().isoformat(),
                    sleep_total_h=pending.sleep_total_h,
                    sleep_in_bed_h=pending.sleep_in_bed_h,
                )
                pending.saved_id = row_id
                pending.saved_table = "manual_sleep"
                text = f"\u2705 Saved: Sleep \u00b7 {pending.sleep_total_h}h \u00b7 {pending.date}"
            else:
                self._poller.answer_callback_query(cb_id, "Nothing to save.")
                return
        finally:
            conn.close()

        buttons = [[{"text": "\u21a9 Undo", "callback_data": f"add_undo:{add_id}"}]]
        if msg_id:
            self._poller.edit_message_with_keyboard(msg_id, text, buttons)

    def _handle_undo(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        msg_id: int | None,
    ) -> None:
        """Undo a saved manual activity."""
        from store import delete_manual_sleep, delete_manual_workout, open_db

        deleted = False
        conn = open_db(self._db)
        try:
            if pending.saved_table == "manual_workout" and pending.saved_id:
                deleted = delete_manual_workout(conn, pending.saved_id)
            elif pending.saved_table == "manual_sleep" and pending.date:
                deleted = delete_manual_sleep(conn, pending.date)
        finally:
            conn.close()

        with self._lock:
            self._pending.pop(add_id, None)

        if deleted:
            self._poller.answer_callback_query(cb_id, "Undone!")
            if msg_id:
                self._poller.edit_message(msg_id, "\u21a9 Undone.")
                self._poller.edit_message_reply_markup(msg_id, None)
        else:
            self._poller.answer_callback_query(cb_id, "Nothing to undo.")
            if msg_id:
                self._poller.edit_message(msg_id, "Nothing to undo — already removed.")
                self._poller.edit_message_reply_markup(msg_id, None)

    def _handle_cancel(self, cb_id: str, add_id: str, msg_id: int | None) -> None:
        """Cancel the /add flow."""
        with self._lock:
            self._pending.pop(add_id, None)
        self._poller.answer_callback_query(cb_id, "Cancelled.")
        if msg_id:
            self._poller.edit_message(msg_id, "Cancelled.")
            self._poller.edit_message_reply_markup(msg_id, None)
