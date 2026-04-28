"""/add manual-activity flow for the Telegram bot.

Handles the inline-keyboard wizard for logging a workout or sleep entry by
hand. Extracted from ``daemon.py`` to keep that module focused on the file
watcher and scheduling loop.

The flow is driven by the daemon's Telegram callback dispatcher, which
forwards every ``add_*`` callback to :meth:`AddFlowHandler.handle_callback`.
The handler owns its own pending-state map and lock; it borrows the daemon's
``db`` path and ``_poller`` for I/O.

Flow shape (after the feel reorder):

Workout:
    pick_type -> pick_duration -> pick_workout_date -> pick_feel
        -> LLM clone + deterministic feel adjustment -> confirm_workout -> save

Sleep:
    pick_type -> pick_sleep_date -> pick_sleep_dur -> pick_sleep_feel
        -> deterministic in-bed padding -> confirm_sleep -> save

The LLM sees type + category + chosen duration + date when picking the
clone; feel is applied deterministically on top so the clone selection
stays about "which historical session is the right analog" and not "how
hard did it feel".
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

from config import MAX_TOKENS_ADD_CLONE

if TYPE_CHECKING:
    from daemon import ZdrowskitDaemon

logger = logging.getLogger(__name__)


_PENDING_ADD_TTL_S = 600  # 10 min


@dataclass
class PendingAdd:
    """In-flight /add manual activity flow state."""

    step: str
    message_id: int
    created_at: float  # time.monotonic() for TTL cleanup
    type_options: list[dict] | None = None  # [{type, category, count}, ...]
    workout_type: str | None = None
    category: str | None = None
    chosen_duration_min: float | None = None  # set before clone runs
    clone_row: dict | None = None  # full workout column dict from LLM + feel adjust
    date: str | None = None
    feel: str | None = None
    feel_adjusted: bool = False
    sleep_total_h: float | None = None
    sleep_in_bed_h: float | None = None
    saved_id: int | None = None  # row id after save, for undo
    saved_table: str | None = None  # "manual_workout" or "manual_sleep"


def find_workout_clone(
    conn: sqlite3.Connection,
    workout_type: str,
    category: str,
    duration_min: float | None = None,
    target_date: str | None = None,
) -> dict:
    """Find the best historical workout to clone via LLM.

    Queries recent workouts and asks a lightweight LLM to pick (or
    synthesize) the best match for the requested type at the requested
    duration. Falls back to the most recent same-type workout on LLM
    failure.

    Args:
        conn: Open database connection.
        workout_type: Requested workout type name (e.g. "Outdoor Run").
        category: Workout category (e.g. "run").
        duration_min: Chosen duration in minutes. When provided, the LLM is
            asked to scale / pick a clone that fits this duration directly.
        target_date: ISO date for the entry. Used for prompt context only.

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
            "duration_min": duration_min or 30,
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

    ask_bits = [f"Log this workout: {workout_type} (category: {category})"]
    if duration_min is not None:
        ask_bits.append(f"Duration: {duration_min:.0f} min")
    if target_date:
        ask_bits.append(f"Date: {target_date}")

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
                "- If the user specified a duration, match it exactly in the "
                "returned duration_min.\n"
                "- Pick an analog that reflects a TYPICAL session of this "
                "type — a separate deterministic layer applies feel-based "
                "adjustments on top, so do NOT factor in effort/feel here.\n"
                "- counts_as_lift should be 1 for strength workouts, 0 otherwise.\n"
                "- Return valid JSON only, no markdown fences."
            ),
        },
        {
            "role": "user",
            "content": (
                "\n".join(ask_bits)
                + f"\n\nRecent history ({len(history)} workouts):\n"
                + json.dumps(history, default=str)
            ),
        },
    ]

    try:
        from model_prefs import resolve_model_route

        route = resolve_model_route("add_clone").call_kwargs()
        temperature = route.pop("temperature", 0.2)
        result = call_llm(
            messages,
            **route,
            max_tokens=MAX_TOKENS_ADD_CLONE,
            temperature=temperature,
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
        if duration_min is not None:
            clone["duration_min"] = duration_min
        else:
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
                out = {
                    col: r[col] if col in r.keys() else None
                    for col in _WORKOUT_CLONE_COLUMNS
                } | {"source_note": f"most recent {workout_type}"}
                if duration_min is not None:
                    out = _scale_clone_to_duration(out, duration_min)
                return out
        for r in rows:
            if r["category"] == category:
                out = {
                    col: r[col] if col in r.keys() else None
                    for col in _WORKOUT_CLONE_COLUMNS
                } | {
                    "type": workout_type,
                    "source_note": f"most recent {category} workout",
                }
                if duration_min is not None:
                    out = _scale_clone_to_duration(out, duration_min)
                return out
        first = rows[0]
        out = {
            col: first[col] if col in first.keys() else None
            for col in _WORKOUT_CLONE_COLUMNS
        } | {
            "type": workout_type,
            "category": category,
            "source_note": "most recent workout",
        }
        if duration_min is not None:
            out = _scale_clone_to_duration(out, duration_min)
        return out


def _scale_clone_to_duration(clone: dict, new_duration_min: float) -> dict:
    """Scale energy and distance proportionally when falling back without LLM."""
    old = clone.get("duration_min") or new_duration_min
    if old and old > 0 and old != new_duration_min:
        ratio = new_duration_min / old
        if clone.get("active_energy_kj"):
            clone["active_energy_kj"] = round(clone["active_energy_kj"] * ratio, 1)
        if clone.get("gpx_distance_km"):
            clone["gpx_distance_km"] = round(clone["gpx_distance_km"] * ratio, 2)
    clone["duration_min"] = new_duration_min
    return clone


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

    # --- Entry -----------------------------------------------------------

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
            [{"text": "\U0001f634 Sleep", "callback_data": f"add_sleep:{add_id}"}]
        )
        rows.append([{"text": "❌ cancel", "callback_data": f"add_x:{add_id}"}])

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

    # --- Dispatch --------------------------------------------------------

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
        elif action == "add_d":
            self._handle_duration(cb_id, add_id, pending, float(param), msg_id)
        elif action == "add_dt":
            self._handle_date(cb_id, add_id, pending, param, msg_id)
        elif action == "add_sd":
            self._handle_sleep_duration(cb_id, add_id, pending, float(param), msg_id)
        elif action == "add_feel":
            self._handle_feel(cb_id, add_id, pending, param, msg_id)
        elif action == "add_ok":
            self._handle_confirm(cb_id, add_id, pending, msg_id)
        elif action == "add_undo":
            self._handle_undo(cb_id, add_id, pending, msg_id)
        elif action == "add_x":
            self._handle_cancel(cb_id, add_id, msg_id)
        else:
            self._poller.answer_callback_query(cb_id, "Unknown action.")

    # --- Workout path ---------------------------------------------------

    def _handle_type(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        type_index: int,
        msg_id: int | None,
    ) -> None:
        """User picked a workout type — collect duration next (LLM waits)."""
        self._poller.answer_callback_query(cb_id)
        if not pending.type_options or type_index >= len(pending.type_options):
            return

        chosen = pending.type_options[type_index]
        pending.workout_type = chosen["type"]
        pending.category = chosen["category"]
        pending.step = "pick_duration"
        self._show_duration_picker(add_id, pending, msg_id)

    def _show_duration_picker(
        self, add_id: str, pending: PendingAdd, msg_id: int | None
    ) -> None:
        """Show category-aware duration presets before the LLM clone runs."""
        cat = pending.category or ""
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
        rows.append([{"text": "❌ cancel", "callback_data": f"add_x:{add_id}"}])

        if msg_id:
            self._poller.edit_message_with_keyboard(
                msg_id, f"How long was the {pending.workout_type}?", rows
            )

    def _handle_duration(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        new_dur: float,
        msg_id: int | None,
    ) -> None:
        """User picked the workout duration — advance to date picker."""
        self._poller.answer_callback_query(cb_id)
        pending.chosen_duration_min = new_dur
        pending.step = "pick_workout_date"
        self._show_workout_date_picker(add_id, msg_id)

    def _show_workout_date_picker(self, add_id: str, msg_id: int | None) -> None:
        """Show the workout date picker (today / yesterday / 2 days ago)."""
        buttons = [
            [
                {"text": "Today", "callback_data": f"add_dt:{add_id}:today"},
                {"text": "Yesterday", "callback_data": f"add_dt:{add_id}:yest"},
                {"text": "2 days ago", "callback_data": f"add_dt:{add_id}:bfr"},
            ],
            [{"text": "❌ cancel", "callback_data": f"add_x:{add_id}"}],
        ]
        if msg_id:
            self._poller.edit_message_with_keyboard(
                msg_id, "When did you do it?", buttons
            )

    def _handle_date(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        param: str,
        msg_id: int | None,
    ) -> None:
        """Apply a date choice, then advance to the next step for this path."""
        self._poller.answer_callback_query(cb_id)
        today = date.today()
        if param == "today":
            pending.date = today.isoformat()
        elif param == "yest":
            pending.date = (today - timedelta(days=1)).isoformat()
        elif param == "bfr":
            pending.date = (today - timedelta(days=2)).isoformat()
        else:
            return

        if pending.step == "pick_workout_date":
            pending.step = "pick_feel"
            self._show_workout_feel_picker(add_id, pending, msg_id)
        elif pending.step == "pick_sleep_date":
            self._show_sleep_duration_picker(add_id, pending, msg_id)

    def _show_workout_feel_picker(
        self, add_id: str, pending: PendingAdd, msg_id: int | None
    ) -> None:
        """Show the feel picker for workouts (easy/solid/hard/wrecked + skip)."""
        rows = [
            [
                {"text": "easy", "callback_data": f"add_feel:{add_id}:easy"},
                {"text": "solid", "callback_data": f"add_feel:{add_id}:solid"},
            ],
            [
                {"text": "hard", "callback_data": f"add_feel:{add_id}:hard"},
                {"text": "wrecked", "callback_data": f"add_feel:{add_id}:wrecked"},
            ],
            [
                {
                    "text": "\U0001f937 skip",
                    "callback_data": f"add_feel:{add_id}:skip",
                }
            ],
            [{"text": "❌ cancel", "callback_data": f"add_x:{add_id}"}],
        ]
        if msg_id:
            self._poller.edit_message_with_keyboard(
                msg_id, f"How did the {pending.workout_type} feel?", rows
            )

    def _show_sleep_feel_picker(
        self, add_id: str, pending: PendingAdd, msg_id: int | None
    ) -> None:
        """Show the feel picker for sleep (solid/ok/restless/wrecked + skip)."""
        rows = [
            [
                {"text": "solid", "callback_data": f"add_feel:{add_id}:solid"},
                {"text": "ok", "callback_data": f"add_feel:{add_id}:ok"},
            ],
            [
                {"text": "restless", "callback_data": f"add_feel:{add_id}:restless"},
                {"text": "wrecked", "callback_data": f"add_feel:{add_id}:wrecked"},
            ],
            [
                {
                    "text": "\U0001f937 skip",
                    "callback_data": f"add_feel:{add_id}:skip",
                }
            ],
            [{"text": "❌ cancel", "callback_data": f"add_x:{add_id}"}],
        ]
        if msg_id:
            self._poller.edit_message_with_keyboard(
                msg_id, f"How was the sleep? ({pending.sleep_total_h}h)", rows
            )

    def _handle_feel(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        param: str,
        msg_id: int | None,
    ) -> None:
        """Route feel selection to workout or sleep based on current step."""
        if pending.step == "pick_feel":
            self._handle_workout_feel(cb_id, add_id, pending, param, msg_id)
        elif pending.step == "pick_sleep_feel":
            self._handle_sleep_feel(cb_id, add_id, pending, param, msg_id)
        else:
            self._poller.answer_callback_query(cb_id, "Unexpected state.")

    def _handle_workout_feel(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        param: str,
        msg_id: int | None,
    ) -> None:
        """User picked feel (or skipped) — run LLM clone + deterministic adjust."""
        from feel_adjust import apply_workout_feel
        from store import open_db

        self._poller.answer_callback_query(cb_id)
        pending.feel = None if param == "skip" else param

        if msg_id:
            self._poller.edit_message(
                msg_id, f"⏳ Finding best match for {pending.workout_type}..."
            )

        conn = open_db(self._db)
        try:
            clone = find_workout_clone(
                conn,
                pending.workout_type or "",
                pending.category or "",
                duration_min=pending.chosen_duration_min,
                target_date=pending.date,
            )
        finally:
            conn.close()

        adjusted, flag = apply_workout_feel(clone, pending.feel)
        pending.clone_row = adjusted
        pending.feel_adjusted = flag
        pending.step = "confirm_workout"
        self._show_workout_confirm(add_id, pending, msg_id)

    def _handle_sleep_feel(
        self,
        cb_id: str,
        add_id: str,
        pending: PendingAdd,
        param: str,
        msg_id: int | None,
    ) -> None:
        """User picked sleep feel — compute in_bed via multiplier, show confirm."""
        from feel_adjust import apply_sleep_feel

        self._poller.answer_callback_query(cb_id)
        pending.feel = None if param == "skip" else param

        in_bed, flag = apply_sleep_feel(pending.sleep_total_h or 0.0, pending.feel)
        pending.sleep_in_bed_h = in_bed
        pending.feel_adjusted = flag
        pending.step = "confirm_sleep"
        self._show_sleep_confirm(add_id, pending, msg_id)

    # --- Sleep path ------------------------------------------------------

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
            [{"text": "❌ cancel", "callback_data": f"add_x:{add_id}"}],
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
        rows.append([{"text": "❌ cancel", "callback_data": f"add_x:{add_id}"}])

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
        """User picked sleep duration — advance to feel picker."""
        self._poller.answer_callback_query(cb_id)
        pending.sleep_total_h = hours
        pending.step = "pick_sleep_feel"
        self._show_sleep_feel_picker(add_id, pending, msg_id)

    # --- Confirm & save -------------------------------------------------

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
                return f"⚠️ A {workout_type} already exists for {target_date}.\n"
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
                return f"⚠️ Sleep data already exists for {target_date} — saving will replace it.\n"
            return ""
        finally:
            conn.close()

    def _show_workout_confirm(
        self, add_id: str, pending: PendingAdd, msg_id: int | None
    ) -> None:
        """Final Save/Cancel screen for a workout."""
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
        if pending.feel:
            text += f" · feel: {pending.feel}"
        if note:
            text += f"\n_{note}_"

        buttons = [
            [{"text": "✅ Save", "callback_data": f"add_ok:{add_id}"}],
            [{"text": "❌ cancel", "callback_data": f"add_x:{add_id}"}],
        ]
        if msg_id:
            self._poller.edit_message_with_keyboard(msg_id, text, buttons)

    def _show_sleep_confirm(
        self, add_id: str, pending: PendingAdd, msg_id: int | None
    ) -> None:
        """Final Save/Cancel screen for sleep."""
        warning = self._check_existing_sleep(pending.date or "")
        text = f"{warning}**Sleep** — {pending.sleep_total_h}h\nDate: {pending.date}"
        if pending.feel:
            text += f" · feel: {pending.feel}"
        if pending.feel_adjusted and pending.sleep_in_bed_h:
            text += f"\n_in bed ≈ {pending.sleep_in_bed_h}h (adjusted for '{pending.feel}' feel)_"
        buttons = [
            [{"text": "✅ Save", "callback_data": f"add_ok:{add_id}"}],
            [{"text": "❌ cancel", "callback_data": f"add_x:{add_id}"}],
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
                    feel=pending.feel,
                    feel_adjusted=pending.feel_adjusted,
                )
                pending.saved_id = row_id
                pending.saved_table = "manual_workout"

                clone = pending.clone_row
                dur = clone.get("duration_min", 0)
                text = (
                    f"✅ Saved: {pending.workout_type} · {dur:.0f} min · {pending.date}"
                )
                if pending.feel:
                    text += f" · {pending.feel}"
            elif pending.step == "confirm_sleep" and pending.sleep_total_h:
                row_id = insert_manual_sleep(
                    conn,
                    date=pending.date or date.today().isoformat(),
                    sleep_total_h=pending.sleep_total_h,
                    sleep_in_bed_h=pending.sleep_in_bed_h,
                    feel=pending.feel,
                    feel_adjusted=pending.feel_adjusted,
                )
                pending.saved_id = row_id
                pending.saved_table = "manual_sleep"
                text = f"✅ Saved: Sleep · {pending.sleep_total_h}h · {pending.date}"
                if pending.feel:
                    text += f" · {pending.feel}"
            else:
                self._poller.answer_callback_query(cb_id, "Nothing to save.")
                return
        finally:
            conn.close()

        buttons = [[{"text": "↩ Undo", "callback_data": f"add_undo:{add_id}"}]]
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
                self._poller.edit_message(msg_id, "↩ Undone.")
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
