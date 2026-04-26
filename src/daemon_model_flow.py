"""/models Telegram command flow.

Button-first control panel for model routing. Mirrors the ergonomics of the
/log and /add flows: token-scoped sessions with TTL, isolated primary action
buttons, demoted cancel buttons, and confirmation step before persisting any
multi-field change.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from model_prefs import (
    FEATURE_LABELS,
    REASONING_CHOICES,
    TELEGRAM_FEATURE_GROUPS,
    TEMPERATURE_CHOICES,
    doctor_findings,
    model_button_label,
    model_label,
    profile_fallback_for,
    reasoning_label,
    reset_all_routes,
    reset_feature_route,
    resolve_model_route,
    routes_summary,
    selectable_models,
    set_feature_route,
    temperature_label,
    update_multiple_features,
)

if TYPE_CHECKING:
    from daemon import ZdrowskitDaemon

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 10 * 60


@dataclass
class PendingModelChange:
    """A pending model route change awaiting confirmation."""

    group: str
    features: tuple[str, ...]
    primary: str
    # ``None`` means "use profile fallback" (we pop the per-feature key on
    # apply so the resolver falls through). A string is an explicit override.
    fallback: str | None
    preview: str
    created_at: float = field(default_factory=time.time)


class ModelFlowHandler:
    """State machine for the /models Telegram command."""

    def __init__(self, daemon: "ZdrowskitDaemon") -> None:
        self._daemon = daemon
        self._lock = threading.Lock()
        self._pending: dict[str, PendingModelChange] = {}

    @property
    def _poller(self):  # type: ignore[no-untyped-def]
        return self._daemon._poller

    # ------------------------------------------------------------------
    # Command entry
    # ------------------------------------------------------------------

    def handle_command(self, message_id: int) -> None:
        """Show current model routes and the main button panel."""
        with self._lock:
            self._sweep_expired_locked()

        self._poller.send_message_with_keyboard(
            self._summary_text(),
            self._main_keyboard(),
            reply_to_message_id=message_id,
        )

    # ------------------------------------------------------------------
    # Callback dispatch
    # ------------------------------------------------------------------

    def handle_callback(self, cb_id: str, data: str, msg_id: int | None) -> None:
        """Dispatch ``model_*`` callbacks."""
        with self._lock:
            self._sweep_expired_locked()

        parts = data.split(":")
        action = parts[0]
        try:
            if action == "model_cancel":
                self._handle_cancel(cb_id, msg_id)
                return
            if action == "model_home":
                self._handle_home(cb_id, msg_id)
                return
            if action == "model_reset" and len(parts) == 2:
                self._handle_reset(cb_id, parts[1], msg_id)
                return
            if action == "model_reset_all":
                self._handle_reset_all(cb_id, msg_id)
                return
            if action == "model_group" and len(parts) == 2:
                self._handle_group(cb_id, parts[1], msg_id)
                return
            if action == "model_pick_primary" and len(parts) == 2:
                self._handle_pick_primary(cb_id, parts[1], msg_id)
                return
            if action == "model_primary" and len(parts) == 3:
                self._handle_primary(cb_id, parts[1], int(parts[2]), msg_id)
                return
            if action == "model_fallback" and len(parts) == 4:
                self._handle_fallback(cb_id, parts[1], int(parts[2]), parts[3], msg_id)
                return
            if action == "model_accept" and len(parts) == 2:
                self._handle_accept(cb_id, parts[1], msg_id)
                return
            if action == "model_doctor":
                self._handle_doctor(cb_id, msg_id)
                return
            if action == "model_pick_reasoning" and len(parts) == 2:
                self._handle_pick_reasoning(cb_id, parts[1], msg_id)
                return
            if action == "model_set_reasoning" and len(parts) == 3:
                self._handle_set_reasoning(cb_id, parts[1], parts[2], msg_id)
                return
            if action == "model_pick_temperature" and len(parts) == 2:
                self._handle_pick_temperature(cb_id, parts[1], msg_id)
                return
            if action == "model_set_temperature" and len(parts) == 3:
                self._handle_set_temperature(cb_id, parts[1], parts[2], msg_id)
                return
        except (ValueError, IndexError):
            logger.warning("Malformed /models callback: %s", data, exc_info=True)
            self._poller.answer_callback_query(cb_id, "Invalid action.")
            return

        self._poller.answer_callback_query(cb_id, "Unknown action.")

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _sweep_expired_locked(self) -> None:
        """Drop pending confirmations older than SESSION_TTL_SECONDS."""
        now = time.time()
        expired = [
            token
            for token, pending in self._pending.items()
            if now - pending.created_at > SESSION_TTL_SECONDS
        ]
        for token in expired:
            logger.info("Expiring /models pending change %s", token)
            self._pending.pop(token, None)

    def _safe_edit(
        self,
        msg_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]],
    ) -> None:
        """Edit a message, swallowing 'message is not modified' errors."""
        try:
            self._poller.edit_message_with_keyboard(msg_id, text, keyboard)
        except Exception:
            logger.debug("Redundant edit on message %d suppressed", msg_id)

    # ------------------------------------------------------------------
    # Top-level handlers
    # ------------------------------------------------------------------

    def _handle_cancel(self, cb_id: str, msg_id: int | None) -> None:
        self._poller.answer_callback_query(cb_id, "Cancelled.")
        if msg_id:
            self._poller.edit_message(msg_id, "Cancelled.")
            self._poller.edit_message_reply_markup(msg_id, None)

    def _handle_home(self, cb_id: str, msg_id: int | None) -> None:
        self._poller.answer_callback_query(cb_id)
        if msg_id:
            self._safe_edit(msg_id, self._summary_text(), self._main_keyboard())

    def _handle_reset(self, cb_id: str, group: str, msg_id: int | None) -> None:
        features = TELEGRAM_FEATURE_GROUPS.get(group)
        if not features:
            self._poller.answer_callback_query(cb_id, "Unknown feature.")
            return
        for feature in features:
            reset_feature_route(feature)
        self._poller.answer_callback_query(cb_id, "Reset.")
        if msg_id:
            self._safe_edit(
                msg_id,
                f"Reset {self._group_label(group)}.\n\n" + self._summary_text(),
                self._main_keyboard(),
            )

    def _handle_reset_all(self, cb_id: str, msg_id: int | None) -> None:
        reset_all_routes()
        self._poller.answer_callback_query(cb_id, "All routes reset.")
        if msg_id:
            self._safe_edit(
                msg_id,
                "Reset all routes to defaults.\n\n" + self._summary_text(),
                self._main_keyboard(),
            )

    def _handle_group(self, cb_id: str, group: str, msg_id: int | None) -> None:
        if group not in TELEGRAM_FEATURE_GROUPS:
            self._poller.answer_callback_query(cb_id, "Unknown feature.")
            return
        self._poller.answer_callback_query(cb_id)
        if msg_id:
            self._safe_edit(
                msg_id,
                self._group_panel_text(group),
                self._group_panel_keyboard(group),
            )

    def _handle_pick_primary(self, cb_id: str, group: str, msg_id: int | None) -> None:
        if group not in TELEGRAM_FEATURE_GROUPS:
            self._poller.answer_callback_query(cb_id, "Unknown feature.")
            return
        self._poller.answer_callback_query(cb_id)
        if msg_id:
            self._safe_edit(
                msg_id,
                f"Choose primary model for {self._group_label(group)}.",
                self._model_keyboard("model_primary", group),
            )

    def _handle_primary(
        self,
        cb_id: str,
        group: str,
        primary_idx: int,
        msg_id: int | None,
    ) -> None:
        models = selectable_models()
        if group not in TELEGRAM_FEATURE_GROUPS or not 0 <= primary_idx < len(models):
            self._poller.answer_callback_query(cb_id, "Invalid selection.")
            return
        self._poller.answer_callback_query(cb_id)
        if msg_id:
            self._safe_edit(
                msg_id,
                (
                    f"{self._group_label(group)} primary: "
                    f"{model_label(models[primary_idx])}\n\nChoose fallback."
                ),
                self._fallback_keyboard(group, primary_idx),
            )

    def _handle_fallback(
        self,
        cb_id: str,
        group: str,
        primary_idx: int,
        fallback_token: str,
        msg_id: int | None,
    ) -> None:
        models = selectable_models()
        if group not in TELEGRAM_FEATURE_GROUPS or not 0 <= primary_idx < len(models):
            self._poller.answer_callback_query(cb_id, "Invalid selection.")
            return
        primary = models[primary_idx]
        fallback: str | None
        fallback_label: str
        if fallback_token == "auto":
            # Resolve and *show* the profile fallback so the user knows what
            # they're getting. We persist it as "auto" by popping the key on
            # apply, so future profile changes propagate.
            fallback = None
            resolved = profile_fallback_for(TELEGRAM_FEATURE_GROUPS[group][0])
            fallback_label = (
                f"auto ({model_label(resolved)})" if resolved else "auto (none)"
            )
        else:
            fallback_idx = int(fallback_token)
            if not 0 <= fallback_idx < len(models):
                self._poller.answer_callback_query(cb_id, "Invalid selection.")
                return
            fallback = models[fallback_idx]
            fallback_label = model_label(fallback)

        preview = (
            f"{self._group_label(group)}\n"
            f"Primary: {model_label(primary)}\n"
            f"Fallback: {fallback_label}"
        )
        token = f"mf_{secrets.token_hex(4)}"
        with self._lock:
            self._pending[token] = PendingModelChange(
                group=group,
                features=TELEGRAM_FEATURE_GROUPS[group],
                primary=primary,
                fallback=fallback,
                preview=preview,
            )
        self._poller.answer_callback_query(cb_id)
        if msg_id:
            self._safe_edit(
                msg_id,
                f"Apply this model route?\n\n{preview}",
                [
                    [
                        {
                            "text": "✅ Apply",
                            "callback_data": f"model_accept:{token}",
                        },
                    ],
                    [{"text": "❌ cancel", "callback_data": "model_cancel"}],
                ],
            )

    def _handle_accept(self, cb_id: str, token: str, msg_id: int | None) -> None:
        with self._lock:
            pending = self._pending.pop(token, None)
        if pending is None:
            self._poller.answer_callback_query(cb_id, "This change expired.")
            if msg_id:
                self._poller.edit_message(msg_id, "This model change has expired.")
            return
        update_multiple_features(
            pending.features,
            primary=pending.primary,
            fallback=pending.fallback,
        )
        self._poller.answer_callback_query(cb_id, "Applied.")
        if msg_id:
            self._safe_edit(
                msg_id,
                f"✅ Applied model route.\n\n{pending.preview}\n\n"
                + self._summary_text(),
                self._main_keyboard(),
            )

    def _handle_doctor(self, cb_id: str, msg_id: int | None) -> None:
        findings = doctor_findings()
        self._poller.answer_callback_query(cb_id)
        if not findings:
            text = "✅ Model routing looks OK.\n\n" + self._summary_text()
        else:
            text = (
                "⚠️ Model routing findings:\n"
                + "\n".join(f"- {finding}" for finding in findings)
                + "\n\n"
                + self._summary_text()
            )
        if msg_id:
            self._safe_edit(msg_id, text, self._main_keyboard())

    # ------------------------------------------------------------------
    # Reasoning / temperature pickers (chat group only)
    # ------------------------------------------------------------------

    def _handle_pick_reasoning(
        self, cb_id: str, group: str, msg_id: int | None
    ) -> None:
        if group not in TELEGRAM_FEATURE_GROUPS:
            self._poller.answer_callback_query(cb_id, "Unknown feature.")
            return
        self._poller.answer_callback_query(cb_id)
        if msg_id:
            self._safe_edit(
                msg_id,
                f"Reasoning effort for {self._group_label(group)}.",
                self._reasoning_keyboard(group),
            )

    def _handle_set_reasoning(
        self, cb_id: str, group: str, value_token: str, msg_id: int | None
    ) -> None:
        if group not in TELEGRAM_FEATURE_GROUPS:
            self._poller.answer_callback_query(cb_id, "Unknown feature.")
            return
        value: str | None
        if value_token == "off":
            value = None
        elif value_token in {"low", "medium", "high"}:
            value = value_token
        else:
            self._poller.answer_callback_query(cb_id, "Invalid value.")
            return
        for feature in TELEGRAM_FEATURE_GROUPS[group]:
            set_feature_route(feature, reasoning_effort=value)
        self._poller.answer_callback_query(cb_id, "Set.")
        if msg_id:
            self._safe_edit(
                msg_id,
                self._group_panel_text(group),
                self._group_panel_keyboard(group),
            )

    def _handle_pick_temperature(
        self, cb_id: str, group: str, msg_id: int | None
    ) -> None:
        if group not in TELEGRAM_FEATURE_GROUPS:
            self._poller.answer_callback_query(cb_id, "Unknown feature.")
            return
        self._poller.answer_callback_query(cb_id)
        if msg_id:
            self._safe_edit(
                msg_id,
                f"Temperature for {self._group_label(group)}.",
                self._temperature_keyboard(group),
            )

    def _handle_set_temperature(
        self, cb_id: str, group: str, value_token: str, msg_id: int | None
    ) -> None:
        if group not in TELEGRAM_FEATURE_GROUPS:
            self._poller.answer_callback_query(cb_id, "Unknown feature.")
            return
        value: float | None
        if value_token == "omit":
            value = None
        else:
            value = float(value_token)
        for feature in TELEGRAM_FEATURE_GROUPS[group]:
            set_feature_route(feature, temperature=value)
        self._poller.answer_callback_query(cb_id, "Set.")
        if msg_id:
            self._safe_edit(
                msg_id,
                self._group_panel_text(group),
                self._group_panel_keyboard(group),
            )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _summary_text(self) -> str:
        lines = ["Model routes:"]
        for route in routes_summary():
            if route.feature in {"verification", "verification_rewrite"}:
                continue
            label = FEATURE_LABELS.get(route.feature, route.feature)
            fallback = (
                model_label(route.fallback)
                if route.fallback
                else f"auto ({route.profile} profile)"
            )
            params = ""
            if route.params:
                bits = []
                if "reasoning_effort" in route.params:
                    bits.append(f"reasoning={reasoning_label(route.reasoning_effort)}")
                if "temperature" in route.params:
                    bits.append(f"temperature={temperature_label(route.temperature)}")
                params = f" ({', '.join(bits)})"
            lines.append(
                f"- {label}: {model_label(route.primary)} → {fallback}{params}"
            )
        return "\n".join(lines)

    def _group_panel_text(self, group: str) -> str:
        feature = TELEGRAM_FEATURE_GROUPS[group][0]
        route = resolve_model_route(feature)
        fallback = (
            model_label(route.fallback)
            if route.fallback
            else f"auto ({route.profile} profile)"
        )
        lines = [
            f"⚙️ {self._group_label(group)}",
            f"Primary: {model_label(route.primary)}",
            f"Fallback: {fallback}",
        ]
        if "reasoning_effort" in route.params:
            lines.append(f"Reasoning: {reasoning_label(route.reasoning_effort)}")
        if "temperature" in route.params:
            lines.append(f"Temperature: {temperature_label(route.temperature)}")
        return "\n".join(lines)

    def _main_keyboard(self) -> list[list[dict[str, str]]]:
        return [
            [
                {"text": "💬 Chat", "callback_data": "model_group:chat"},
                {"text": "📊 Reports", "callback_data": "model_group:reports"},
            ],
            [
                {"text": "🧭 Coach", "callback_data": "model_group:coach"},
                {"text": "🔔 Nudges", "callback_data": "model_group:nudges"},
            ],
            [
                {"text": "🛠 Utilities", "callback_data": "model_group:utilities"},
                {"text": "🩺 Doctor", "callback_data": "model_doctor"},
            ],
            [{"text": "↻ Reset all", "callback_data": "model_reset_all"}],
            [{"text": "❌ cancel", "callback_data": "model_cancel"}],
        ]

    def _group_panel_keyboard(self, group: str) -> list[list[dict[str, str]]]:
        rows: list[list[dict[str, str]]] = [
            [
                {
                    "text": "🤖 Change model",
                    "callback_data": f"model_pick_primary:{group}",
                }
            ]
        ]
        feature = TELEGRAM_FEATURE_GROUPS[group][0]
        route = resolve_model_route(feature)
        if "reasoning_effort" in route.params:
            rows.append(
                [
                    {
                        "text": (
                            f"🧠 Reasoning: {reasoning_label(route.reasoning_effort)}"
                        ),
                        "callback_data": f"model_pick_reasoning:{group}",
                    }
                ]
            )
        if "temperature" in route.params:
            rows.append(
                [
                    {
                        "text": (
                            f"🌡 Temperature: {temperature_label(route.temperature)}"
                        ),
                        "callback_data": f"model_pick_temperature:{group}",
                    }
                ]
            )
        rows.append(
            [
                {"text": "↻ Reset", "callback_data": f"model_reset:{group}"},
                {"text": "← back", "callback_data": "model_home"},
            ]
        )
        rows.append([{"text": "❌ cancel", "callback_data": "model_cancel"}])
        return rows

    def _model_keyboard(self, action: str, group: str) -> list[list[dict[str, str]]]:
        rows: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for idx, model in enumerate(selectable_models()):
            row.append(
                {
                    "text": model_button_label(model),
                    "callback_data": f"{action}:{group}:{idx}",
                }
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": "← back", "callback_data": f"model_group:{group}"}])
        rows.append([{"text": "❌ cancel", "callback_data": "model_cancel"}])
        return rows

    def _fallback_keyboard(
        self, group: str, primary_idx: int
    ) -> list[list[dict[str, str]]]:
        feature = TELEGRAM_FEATURE_GROUPS[group][0]
        resolved = profile_fallback_for(feature)
        auto_label = (
            f"⚡ Auto ({model_label(resolved)})" if resolved else "⚡ Auto (none)"
        )
        rows: list[list[dict[str, str]]] = [
            [
                {
                    "text": auto_label,
                    "callback_data": f"model_fallback:{group}:{primary_idx}:auto",
                }
            ]
        ]
        row: list[dict[str, str]] = []
        for idx, model in enumerate(selectable_models()):
            if idx == primary_idx:
                continue
            row.append(
                {
                    "text": model_button_label(model),
                    "callback_data": f"model_fallback:{group}:{primary_idx}:{idx}",
                }
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(
            [{"text": "← back", "callback_data": f"model_pick_primary:{group}"}]
        )
        rows.append([{"text": "❌ cancel", "callback_data": "model_cancel"}])
        return rows

    def _reasoning_keyboard(self, group: str) -> list[list[dict[str, str]]]:
        feature = TELEGRAM_FEATURE_GROUPS[group][0]
        route = resolve_model_route(feature)
        rows: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for value in REASONING_CHOICES:
            token = "off" if value is None else value
            mark = "✓ " if route.reasoning_effort == value else ""
            row.append(
                {
                    "text": f"{mark}{reasoning_label(value)}",
                    "callback_data": f"model_set_reasoning:{group}:{token}",
                }
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": "← back", "callback_data": f"model_group:{group}"}])
        rows.append([{"text": "❌ cancel", "callback_data": "model_cancel"}])
        return rows

    def _temperature_keyboard(self, group: str) -> list[list[dict[str, str]]]:
        feature = TELEGRAM_FEATURE_GROUPS[group][0]
        route = resolve_model_route(feature)
        rows: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for value in TEMPERATURE_CHOICES:
            token = "omit" if value is None else f"{value:g}"
            mark = "✓ " if route.temperature == value else ""
            row.append(
                {
                    "text": f"{mark}{temperature_label(value)}",
                    "callback_data": f"model_set_temperature:{group}:{token}",
                }
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": "← back", "callback_data": f"model_group:{group}"}])
        rows.append([{"text": "❌ cancel", "callback_data": "model_cancel"}])
        return rows

    def _group_label(self, group: str) -> str:
        if group == "reports":
            return "Reports"
        if group == "nudges":
            return "Nudges"
        if group == "utilities":
            return "Utilities"
        feature = TELEGRAM_FEATURE_GROUPS[group][0]
        route = resolve_model_route(feature)
        return FEATURE_LABELS.get(route.feature, group.title())
