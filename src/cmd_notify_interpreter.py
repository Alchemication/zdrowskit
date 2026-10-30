"""LLM interpreter for Telegram /notify requests."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from cmd_llm_common import route_kwargs
from config import CONTEXT_DIR, MAX_TOKENS_NOTIFY, NOTIFICATION_PREFS_PATH
from llm import call_llm, strip_json_fences
from llm_context import build_messages, load_context
from notification_prefs import (
    DEFAULT_NOTIFICATION_PREFS,
    active_temporary_mutes,
    effective_notification_prefs,
    validate_notification_changes,
)
from store import open_db

logger = logging.getLogger(__name__)


class SetChange(BaseModel):
    """Set one preference path to a value."""

    action: Literal["set"]
    path: str = Field(description="Dotted preference path, e.g. nudges.max_per_day.")
    value: bool | int | float | str = Field(
        description="New value: boolean, number, weekday name, or HH:MM time."
    )


class ResetChange(BaseModel):
    """Reset one preference path or section to its default."""

    action: Literal["reset"]
    path: str = Field(description="Dotted path or section name, or 'all'.")


class ResetAllChange(BaseModel):
    """Reset every override and clear temporary mutes."""

    action: Literal["reset_all"]


class MuteChange(BaseModel):
    """Mute one target until an exact future timestamp."""

    action: Literal["mute_until"]
    target: str = Field(description="Mute target, e.g. nudges or all.")
    expires_at: str = Field(description="ISO-8601 timestamp with offset.")
    source_text: str = Field(description="The user's wording for the mute.")


NotifyChange = SetChange | ResetChange | ResetAllChange | MuteChange


class NotifyResponse(BaseModel):
    """Structured notify-request interpretation produced by the LLM.

    Each change is typed by its ``action`` so the response format itself
    requires the fields that action needs. A free-form dict left ``action`` to
    the prompt's examples: DeepSeek followed them, but Claude Haiku returned
    changes with no ``action`` at all, and every request failed. Paths and
    values are still validated semantically by
    :func:`validate_notification_changes`, since the legal ones depend on
    runtime constants.
    """

    status: Literal["proposal", "needs_clarification", "unsupported"] = Field(
        description=(
            "Overall interpretation. 'proposal' = clear and actionable, "
            "'needs_clarification' = ambiguous (ask one question), "
            "'unsupported' = capability not available."
        )
    )
    intent: Literal[
        "show", "set", "enable", "disable", "reset", "reset_all", "mute_until"
    ] = Field(description="High-level user intent.")
    changes: list[NotifyChange] = Field(
        default_factory=list,
        description=(
            "Concrete change items (see Change schema in the system prompt). "
            "Empty for 'show' or 'needs_clarification'."
        ),
    )
    summary: str = Field(
        default="", description="Short one-line description of the proposal."
    )
    clarification_question: str | None = Field(
        default=None,
        description=(
            "Single short question when status is 'needs_clarification'; "
            "otherwise null."
        ),
    )
    reason: str = Field(
        default="", description="Short debug explanation of the interpretation."
    )


def interpret_notify_request(
    request_text: str,
    *,
    db: str | Path,
    prefs: dict[str, Any],
    now: datetime | None = None,
    clarification_answer: str | None = None,
    model: str | None = None,
    context_dir: Path = CONTEXT_DIR,
    prefs_path: Path = NOTIFICATION_PREFS_PATH,
    model_prefs_path: Path | None = None,
) -> dict[str, Any]:
    """Interpret a Telegram /notify request into a structured payload."""
    now = now or datetime.now().astimezone()

    try:
        context = load_context(
            context_dir,
            prompt_file="notify_prompt",
            max_history=0,
            max_log=0,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        raise

    context["current_settings"] = json.dumps(
        effective_notification_prefs(prefs), indent=2, sort_keys=True
    )
    context["default_settings"] = json.dumps(
        effective_notification_prefs(DEFAULT_NOTIFICATION_PREFS),
        indent=2,
        sort_keys=True,
    )
    context["active_mutes"] = json.dumps(
        active_temporary_mutes(prefs, now=now), indent=2, sort_keys=True
    )
    context["notify_request"] = request_text
    context["clarification_answer"] = clarification_answer or "(none)"
    context["timezone"] = now.tzname() or str(now.tzinfo) or "local"

    messages = build_messages(
        context,
        health_data_text="{}",
        baselines=None,
        week_complete=False,
        today=now.date(),
    )

    conn = open_db(Path(db))
    route = route_kwargs("notify", model, prefs_path=model_prefs_path)
    temperature = route.pop("temperature", 0)
    try:
        result = call_llm(
            messages,
            **route,
            max_tokens=MAX_TOKENS_NOTIFY,
            temperature=temperature,
            response_format=NotifyResponse,
            conn=conn,
            request_type="notify",
            metadata={
                "request_text": request_text,
                "clarification_answer": clarification_answer,
                "prefs_path": str(prefs_path),
            },
        )
    finally:
        conn.close()

    try:
        parsed = NotifyResponse.model_validate_json(strip_json_fences(result.text))
    except ValidationError as exc:
        raise ValueError(f"Notify interpreter returned invalid payload: {exc}") from exc

    if parsed.status == "needs_clarification":
        question = (parsed.clarification_question or "").strip()
        if not question:
            raise ValueError("Notify clarification must include a question")
        clarification = question
    else:
        clarification = None

    payload: dict[str, Any] = {
        "status": parsed.status,
        "intent": parsed.intent,
        "changes": validate_notification_changes(
            [change.model_dump() for change in parsed.changes]
        ),
        "summary": parsed.summary.strip(),
        "clarification_question": clarification,
        "reason": parsed.reason.strip(),
        "llm_call_id": result.llm_call_id,
        "model": result.model,
    }
    return payload
