"""LLM call infrastructure: retry logic, model fallback, and response types.

Handles the mechanics of calling litellm with exponential backoff, model
fallback on overload, and structured result packaging. All app-domain
concerns (context loading, prompt assembly, health data rendering) live
in ``llm_context`` and ``llm_health``.

Public API:
    call_llm       — call litellm and return an LLMResult with text + metadata
    extract_memory — pull <memory> block from LLM response
    LLMResult      — dataclass holding response text and usage metadata
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

import litellm

from store import log_llm_call

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "anthropic/claude-opus-4-6"
FALLBACK_MODEL = "anthropic/claude-sonnet-4-6"

# Exponential backoff delays (seconds) between retries on overloaded errors.
_RETRY_DELAYS = [10, 30, 90]


@dataclass
class LLMResult:
    """Container for LLM response text and call metadata.

    Attributes:
        text: The LLM's response text.
        model: The model string used for the call.
        input_tokens: Number of input tokens reported by the API.
        output_tokens: Number of output tokens reported by the API.
        total_tokens: Total tokens (input + output).
        latency_s: Wall-clock time for the LLM call in seconds.
        cost: Actual cost in USD as reported by litellm, or None if unavailable.
    """

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_s: float
    cost: float | None = None
    tool_calls: list | None = None
    raw_message: dict | None = None
    """The assistant message dict suitable for appending back to the messages
    list in a tool-calling loop (includes ``tool_calls`` when present)."""
    llm_call_id: int | None = None
    """Database row id from ``llm_call`` table, set when the call is logged."""


def _is_overloaded(exc: Exception) -> bool:
    """Return True if *exc* is an Anthropic overloaded error."""
    return "overloaded_error" in str(exc) or "Overloaded" in str(exc)


def _call_with_retry(
    kwargs: dict,
    model: str,
) -> tuple:
    """Call litellm.completion with retries and model fallback.

    Retries on overloaded errors using exponential backoff.  After exhausting
    retries on the primary model, switches to FALLBACK_MODEL and retries once
    more.  Re-raises the last exception if all attempts fail.

    Args:
        kwargs: litellm.completion keyword arguments (may be mutated for fallback).
        model: Primary model string.

    Returns:
        A (response, effective_model) tuple.
    """
    for attempt, delay in enumerate(_RETRY_DELAYS + [None]):
        try:
            response = litellm.completion(**{**kwargs, "model": model})
            return response, model
        except Exception as exc:
            if not _is_overloaded(exc):
                raise
            if delay is not None:
                logger.warning(
                    "Anthropic overloaded (attempt %d/%d), retrying in %ds ...",
                    attempt + 1,
                    len(_RETRY_DELAYS),
                    delay,
                )
                time.sleep(delay)
            else:
                logger.warning(
                    "All retries exhausted on %s, switching to fallback %s",
                    model,
                    FALLBACK_MODEL,
                )

    # Fallback model — same retry schedule.
    model = FALLBACK_MODEL
    last_exc: Exception | None = None
    for attempt, delay in enumerate(_RETRY_DELAYS + [None]):
        try:
            response = litellm.completion(**{**kwargs, "model": model})
            logger.info("Fallback model %s succeeded", model)
            return response, model
        except Exception as exc:
            if not _is_overloaded(exc):
                raise
            last_exc = exc
            if delay is not None:
                logger.warning(
                    "Fallback %s also overloaded (attempt %d/%d), retrying in %ds ...",
                    model,
                    attempt + 1,
                    len(_RETRY_DELAYS),
                    delay,
                )
                time.sleep(delay)

    raise last_exc  # type: ignore[misc]


def _message_to_dict(message: Any) -> dict[str, Any]:
    """Return a JSON-safe LiteLLM message dict, preserving provider fields."""
    model_dump = getattr(message, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json", exclude_none=True)
        except TypeError:
            dumped = model_dump(exclude_none=True)
        if isinstance(dumped, dict):
            return dict(dumped)

    if isinstance(message, dict):
        return dict(message)

    role = getattr(message, "role", "assistant") or "assistant"
    content = getattr(message, "content", "") or ""
    return {
        "role": role if isinstance(role, str) else "assistant",
        "content": content if isinstance(content, str) else str(content),
    }


def _tool_call_to_dict(tool_call: Any) -> dict[str, Any]:
    """Normalize a LiteLLM tool call object into chat-message JSON."""
    if isinstance(tool_call, dict):
        return {
            "id": tool_call.get("id"),
            "type": tool_call.get("type", "function"),
            "function": dict(tool_call.get("function", {}) or {}),
        }

    return {
        "id": tool_call.id,
        "type": "function",
        "function": {
            "name": tool_call.function.name,
            "arguments": tool_call.function.arguments,
        },
    }


def _tool_calls_to_dicts(tool_calls: Any) -> list[dict[str, Any]]:
    """Normalize an iterable of tool calls; tolerate mock objects with none."""
    if not isinstance(tool_calls, list | tuple):
        return []
    return [_tool_call_to_dict(tc) for tc in tool_calls]


def call_llm(
    messages: list[dict[str, Any]],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    temperature: float | None = 0.7,
    reasoning_effort: str | None = None,
    tools: list[dict] | None = None,
    conn: sqlite3.Connection | None = None,
    request_type: str = "",
    metadata: dict | None = None,
) -> LLMResult:
    """Call the LLM via litellm and return the response with metadata.

    All calls are logged to the database when *conn* and *request_type* are
    provided. A logging failure is never propagated — it is logged as a
    warning and the result is returned normally.

    Args:
        messages: System + user messages for the LLM.
        model: litellm model string.
        max_tokens: Maximum tokens in the response.
        temperature: Sampling temperature. Pass ``None`` to omit the parameter
            entirely for models that reject it (e.g. claude-opus-4-7, which
            deprecated the field).
        reasoning_effort: Optional reasoning effort hint (model-dependent).
        tools: Optional list of tool definitions for function calling.
        conn: Open DB connection for logging. None to skip logging.
        request_type: Product-level call type, e.g. "insights" or "nudge".
        metadata: Product context dict stored alongside the call.

    Returns:
        An LLMResult containing the response text and usage metadata.

    Raises:
        litellm.AuthenticationError: If the API key is missing or invalid.
        litellm.APIError: On network or API failures.
    """
    # Anthropic's extended thinking requires temperature=1; any other value
    # is rejected with a BadRequestError. Force it here so callers can keep
    # passing their preferred sampling temperature without having to know
    # about this constraint.
    if temperature is None:
        effective_temperature: float | None = None
    elif reasoning_effort is not None:
        effective_temperature = 1.0
    else:
        effective_temperature = temperature

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if effective_temperature is not None:
        kwargs["temperature"] = effective_temperature
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    if tools is not None:
        kwargs["tools"] = tools

    t0 = time.perf_counter()
    response, model = _call_with_retry(kwargs, model)
    latency = time.perf_counter() - t0
    usage = response.usage

    try:
        cost = litellm.completion_cost(completion_response=response)
    except Exception:
        cost = None

    message = response.choices[0].message
    raw_tool_calls = getattr(message, "tool_calls", None)
    tool_call_dicts = _tool_calls_to_dicts(raw_tool_calls)

    # Build a raw message dict for tool-calling loops. Some providers require
    # their assistant-side reasoning fields to be replayed with tool results.
    raw_msg = _message_to_dict(message)
    if not isinstance(raw_msg.get("role"), str):
        raw_msg["role"] = "assistant"
    raw_msg["role"] = raw_msg.get("role") or "assistant"
    raw_msg["content"] = raw_msg.get("content") or ""
    if tool_call_dicts:
        raw_msg["tool_calls"] = tool_call_dicts

    result = LLMResult(
        text=message.content or "",
        model=model,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        latency_s=latency,
        cost=cost,
        tool_calls=raw_tool_calls if tool_call_dicts else None,
        raw_message=raw_msg,
    )

    if conn and request_type:
        params: dict = {"max_tokens": max_tokens}
        if effective_temperature is not None:
            params["temperature"] = effective_temperature
        if reasoning_effort is not None:
            params["reasoning_effort"] = reasoning_effort
        try:
            row_id = log_llm_call(
                conn,
                request_type=request_type,
                model=model,
                messages=messages,
                response_text=result.text,
                params=params,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=result.total_tokens,
                latency_s=result.latency_s,
                cost=result.cost,
                metadata=metadata,
            )
            result.llm_call_id = row_id
        except Exception:
            logger.warning("Failed to log LLM call to DB", exc_info=True)

    return result


def extract_memory(response: str) -> str | None:
    """Extract the <memory> block from the LLM response.

    Args:
        response: Full LLM response text.

    Returns:
        The memory content (stripped), or None if no block found.
    """
    match = re.search(r"<memory>(.*?)</memory>", response, re.DOTALL)
    return match.group(1).strip() if match else None
