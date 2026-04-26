"""Telegram /models button flow."""

from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from model_prefs import (
    FEATURE_LABELS,
    TELEGRAM_FEATURE_GROUPS,
    apply_chat_opus_preset,
    doctor_findings,
    model_label,
    reset_feature_route,
    resolve_model_route,
    routes_summary,
    selectable_models,
    update_multiple_features,
)

if TYPE_CHECKING:
    from daemon import ZdrowskitDaemon

logger = logging.getLogger(__name__)


@dataclass
class PendingModelChange:
    """A pending model route change awaiting confirmation."""

    group: str
    features: tuple[str, ...]
    primary: str
    fallback: str | None | object
    preview: str


class ModelFlowHandler:
    """Button-first Telegram model routing control panel."""

    def __init__(self, daemon: "ZdrowskitDaemon") -> None:
        self._daemon = daemon
        self._lock = threading.Lock()
        self._pending: dict[str, PendingModelChange] = {}

    @property
    def _poller(self):  # type: ignore[no-untyped-def]
        return self._daemon._poller

    def handle_command(self, message_id: int) -> None:
        """Show current model routes and the main button panel."""
        self._poller.send_message_with_keyboard(
            self._summary_text(),
            self._main_keyboard(),
            reply_to_message_id=message_id,
        )

    def handle_callback(self, cb_id: str, data: str, msg_id: int | None) -> None:
        """Dispatch ``model_*`` callbacks."""
        parts = data.split(":")
        action = parts[0]
        if action == "model_cancel":
            self._poller.answer_callback_query(cb_id, "Cancelled.")
            if msg_id:
                self._poller.edit_message(msg_id, "Cancelled.")
            return
        if action == "model_preset" and len(parts) == 2:
            self._handle_preset(cb_id, parts[1], msg_id)
            return
        if action == "model_reset" and len(parts) == 2:
            self._handle_reset(cb_id, parts[1], msg_id)
            return
        if action == "model_group" and len(parts) == 2:
            self._handle_group(cb_id, parts[1], msg_id)
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
        self._poller.answer_callback_query(cb_id, "Unknown action.")

    def _handle_preset(self, cb_id: str, preset: str, msg_id: int | None) -> None:
        if preset != "chat_opus":
            self._poller.answer_callback_query(cb_id, "Unknown preset.")
            return
        apply_chat_opus_preset()
        self._poller.answer_callback_query(cb_id, "Applied.")
        if msg_id:
            self._poller.edit_message_with_keyboard(
                msg_id,
                "Applied chat preset.\n\n" + self._summary_text(),
                self._main_keyboard(),
            )

    def _handle_reset(self, cb_id: str, group: str, msg_id: int | None) -> None:
        features = TELEGRAM_FEATURE_GROUPS.get(group)
        if not features:
            self._poller.answer_callback_query(cb_id, "Unknown feature.")
            return
        for feature in features:
            reset_feature_route(feature)
        self._poller.answer_callback_query(cb_id, "Reset.")
        if msg_id:
            self._poller.edit_message_with_keyboard(
                msg_id,
                f"Reset {self._group_label(group)}.\n\n" + self._summary_text(),
                self._main_keyboard(),
            )

    def _handle_group(self, cb_id: str, group: str, msg_id: int | None) -> None:
        if group not in TELEGRAM_FEATURE_GROUPS:
            self._poller.answer_callback_query(cb_id, "Unknown feature.")
            return
        self._poller.answer_callback_query(cb_id)
        if msg_id:
            self._poller.edit_message_with_keyboard(
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
        if group not in TELEGRAM_FEATURE_GROUPS or primary_idx >= len(models):
            self._poller.answer_callback_query(cb_id, "Invalid selection.")
            return
        self._poller.answer_callback_query(cb_id)
        if msg_id:
            self._poller.edit_message_with_keyboard(
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
        if group not in TELEGRAM_FEATURE_GROUPS or primary_idx >= len(models):
            self._poller.answer_callback_query(cb_id, "Invalid selection.")
            return
        primary = models[primary_idx]
        fallback: str | None | object
        fallback_label: str
        if fallback_token == "profile":
            fallback = None
            fallback_label = "profile fallback"
        else:
            fallback_idx = int(fallback_token)
            if fallback_idx >= len(models):
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
            self._poller.edit_message_with_keyboard(
                msg_id,
                f"Apply this model route?\n\n{preview}",
                [
                    [
                        {
                            "text": "✅ Accept",
                            "callback_data": f"model_accept:{token}",
                        },
                        {"text": "❌ cancel", "callback_data": "model_cancel"},
                    ]
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
            self._poller.edit_message_with_keyboard(
                msg_id,
                f"Applied model route.\n\n{pending.preview}",
                self._main_keyboard(),
            )

    def _handle_doctor(self, cb_id: str, msg_id: int | None) -> None:
        findings = doctor_findings()
        self._poller.answer_callback_query(cb_id)
        text = (
            "Model routing looks OK."
            if not findings
            else "\n".join(
                ["Model routing findings:", *[f"- {finding}" for finding in findings]]
            )
        )
        if msg_id:
            self._poller.edit_message_with_keyboard(msg_id, text, self._main_keyboard())

    def _summary_text(self) -> str:
        lines = ["Model routes:"]
        for route in routes_summary():
            if route.feature in {"verification", "verification_rewrite"}:
                continue
            label = FEATURE_LABELS.get(route.feature, route.feature)
            fallback = model_label(route.fallback) if route.fallback else "profile"
            params = ""
            if route.params:
                bits = []
                if "reasoning_effort" in route.params:
                    bits.append(f"reasoning={route.reasoning_effort or 'none'}")
                if "temperature" in route.params:
                    bits.append(
                        "temperature=omit"
                        if route.temperature is None
                        else f"temperature={route.temperature:g}"
                    )
                params = f" ({', '.join(bits)})"
            lines.append(
                f"- {label}: {model_label(route.primary)} -> {fallback}{params}"
            )
        return "\n".join(lines)

    def _main_keyboard(self) -> list[list[dict[str, str]]]:
        return [
            [
                {"text": "Chat", "callback_data": "model_group:chat"},
                {"text": "Reports", "callback_data": "model_group:reports"},
            ],
            [
                {"text": "Coach", "callback_data": "model_group:coach"},
                {"text": "Nudges", "callback_data": "model_group:nudges"},
            ],
            [
                {"text": "Utilities", "callback_data": "model_group:utilities"},
                {"text": "Doctor", "callback_data": "model_doctor"},
            ],
            [
                {
                    "text": "Use Opus for chat",
                    "callback_data": "model_preset:chat_opus",
                },
                {"text": "Reset chat", "callback_data": "model_reset:chat"},
            ],
            [{"text": "❌ cancel", "callback_data": "model_cancel"}],
        ]

    def _model_keyboard(self, action: str, group: str) -> list[list[dict[str, str]]]:
        rows: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for idx, model in enumerate(selectable_models()):
            row.append(
                {
                    "text": model_label(model),
                    "callback_data": f"{action}:{group}:{idx}",
                }
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(
            [
                {
                    "text": "Reset",
                    "callback_data": f"model_reset:{group}",
                },
                {"text": "❌ cancel", "callback_data": "model_cancel"},
            ]
        )
        return rows

    def _fallback_keyboard(
        self, group: str, primary_idx: int
    ) -> list[list[dict[str, str]]]:
        rows = [
            [
                {
                    "text": "Use profile fallback",
                    "callback_data": f"model_fallback:{group}:{primary_idx}:profile",
                }
            ]
        ]
        row: list[dict[str, str]] = []
        for idx, model in enumerate(selectable_models()):
            if idx == primary_idx:
                continue
            row.append(
                {
                    "text": model_label(model),
                    "callback_data": f"model_fallback:{group}:{primary_idx}:{idx}",
                }
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
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
