"""LLM call infrastructure: retry logic, model fallback, and response types.

Handles the mechanics of calling litellm with exponential backoff, provider
fallback, and structured result packaging. All app-domain
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
from datetime import UTC, datetime
from typing import Any

import litellm

from config import (
    DEEPSEEK_EXTRA_BODY,
    DEFAULT_MODEL,
    FALLBACK_FLASH_MODEL,
    FALLBACK_PRO_MODEL,
    MAX_TOKENS_DEFAULT,
    PRIMARY_FLASH_MODEL,
    PRIMARY_PRO_MODEL,
)
from store import log_llm_call

logger = logging.getLogger(__name__)

# Exponential backoff delays (seconds) between retries on overloaded errors.
_RETRY_DELAYS = [10, 30, 90]


def _warn_on_aliased_fallback() -> None:
    """One-shot warning when a primary model equals its fallback.

    Silent footgun: if a user sets ``ZDROWSKIT_FALLBACK_PRO_MODEL`` to the
    same id as ``ZDROWSKIT_PRIMARY_PRO_MODEL`` (typo, copy-paste), the
    fallback chain collapses to length 1 and nothing crosses providers.
    """
    if PRIMARY_PRO_MODEL == FALLBACK_PRO_MODEL:
        logger.warning(
            "Pro primary and fallback are both %s; provider fallback is disabled",
            PRIMARY_PRO_MODEL,
        )
    if PRIMARY_FLASH_MODEL == FALLBACK_FLASH_MODEL:
        logger.warning(
            "Flash primary and fallback are both %s; provider fallback is disabled",
            PRIMARY_FLASH_MODEL,
        )


_warn_on_aliased_fallback()


@dataclass(frozen=True)
class _TokenPricingWindow:
    """Temporary per-token pricing until LiteLLM carries the model entry."""

    effective_from: datetime
    effective_until: datetime | None
    input_cache_hit_per_1m: float
    input_cache_miss_per_1m: float
    output_per_1m: float


# Verified against https://api-docs.deepseek.com/quick_start/pricing/ on
# 2026-04-30. Keep this narrow so it is easy to delete once LiteLLM catches up.
_DEEPSEEK_V4_PRICING: dict[str, list[_TokenPricingWindow]] = {
    "deepseek-v4-flash": [
        _TokenPricingWindow(
            effective_from=datetime(2026, 4, 25, tzinfo=UTC),
            effective_until=datetime(2026, 4, 26, 12, 15, tzinfo=UTC),
            input_cache_hit_per_1m=0.028,
            input_cache_miss_per_1m=0.14,
            output_per_1m=0.28,
        ),
        _TokenPricingWindow(
            effective_from=datetime(2026, 4, 26, 12, 15, tzinfo=UTC),
            effective_until=None,
            input_cache_hit_per_1m=0.0028,
            input_cache_miss_per_1m=0.14,
            output_per_1m=0.28,
        )
    ],
    "deepseek-v4-pro": [
        _TokenPricingWindow(
            effective_from=datetime(2026, 4, 25, tzinfo=UTC),
            effective_until=datetime(2026, 4, 26, 12, 15, tzinfo=UTC),
            input_cache_hit_per_1m=0.03625,
            input_cache_miss_per_1m=0.435,
            output_per_1m=0.87,
        ),
        _TokenPricingWindow(
            effective_from=datetime(2026, 4, 26, 12, 15, tzinfo=UTC),
            effective_until=datetime(2026, 5, 31, 15, 59, tzinfo=UTC),
            input_cache_hit_per_1m=0.003625,
            input_cache_miss_per_1m=0.435,
            output_per_1m=0.87,
        ),
        _TokenPricingWindow(
            effective_from=datetime(2026, 5, 31, 15, 59, tzinfo=UTC),
            effective_until=None,
            input_cache_hit_per_1m=0.0145,
            input_cache_miss_per_1m=1.74,
            output_per_1m=3.48,
        ),
    ],
}


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
    max_tokens: int | None = None
    tool_calls: list | None = None
    raw_message: dict | None = None
    """The assistant message dict suitable for appending back to the messages
    list in a tool-calling loop (includes ``tool_calls`` when present)."""
    llm_call_id: int | None = None
    """Database row id from ``llm_call`` table, set when the call is logged."""


def _numeric(value: Any) -> float | None:
    """Return a numeric value from API data without accepting mock sentinels."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _field(obj: Any, name: str) -> Any:
    """Read a field from dicts, pydantic extras, or simple response objects."""
    if isinstance(obj, dict):
        return obj.get(name)
    model_extra = getattr(obj, "model_extra", None)
    if isinstance(model_extra, dict) and name in model_extra:
        return model_extra[name]
    return getattr(obj, name, None)


def _provider_reported_cost(response: Any) -> float | None:
    """Return provider-reported response cost when present."""
    usage = _field(response, "usage")
    if usage is None:
        return None
    return _numeric(_field(usage, "cost"))


def _direct_deepseek_v4_model(model: str) -> str | None:
    """Return the direct DeepSeek v4 model id, excluding proxy providers."""
    if model.startswith("openrouter/"):
        return None
    if model.startswith("deepseek/"):
        model = model.removeprefix("deepseek/")
    if model in _DEEPSEEK_V4_PRICING:
        return model
    return None


def _deepseek_v4_pricing_window(
    model: str,
    *,
    at: datetime | None = None,
) -> _TokenPricingWindow | None:
    """Return the active temporary DeepSeek v4 price window for *model*."""
    direct_model = _direct_deepseek_v4_model(model)
    if direct_model is None:
        return None

    now = at or datetime.now(tz=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    for window in _DEEPSEEK_V4_PRICING[direct_model]:
        if now < window.effective_from:
            continue
        if window.effective_until is not None and now >= window.effective_until:
            continue
        return window
    return None


def _deepseek_v4_cost(
    response: Any,
    model: str,
    *,
    at: datetime | None = None,
) -> float | None:
    """Estimate direct DeepSeek v4 cost from usage while LiteLLM lacks pricing."""
    window = _deepseek_v4_pricing_window(model, at=at)
    if window is None:
        return None

    usage = _field(response, "usage")
    if usage is None:
        return None

    prompt_tokens = _numeric(_field(usage, "prompt_tokens"))
    completion_tokens = _numeric(_field(usage, "completion_tokens"))
    if completion_tokens is None:
        return None

    cache_hit_tokens = _numeric(_field(usage, "prompt_cache_hit_tokens"))
    cache_miss_tokens = _numeric(_field(usage, "prompt_cache_miss_tokens"))

    if cache_hit_tokens is None and cache_miss_tokens is None:
        prompt_details = _field(usage, "prompt_tokens_details")
        cache_hit_tokens = _numeric(_field(prompt_details, "cached_tokens"))

    if prompt_tokens is not None:
        if cache_hit_tokens is not None and cache_miss_tokens is None:
            cache_miss_tokens = max(prompt_tokens - cache_hit_tokens, 0.0)
        elif cache_miss_tokens is not None and cache_hit_tokens is None:
            cache_hit_tokens = max(prompt_tokens - cache_miss_tokens, 0.0)
        elif cache_hit_tokens is None and cache_miss_tokens is None:
            cache_hit_tokens = 0.0
            cache_miss_tokens = prompt_tokens

    if cache_hit_tokens is None or cache_miss_tokens is None:
        return None

    return (
        cache_hit_tokens * window.input_cache_hit_per_1m
        + cache_miss_tokens * window.input_cache_miss_per_1m
        + completion_tokens * window.output_per_1m
    ) / 1_000_000


def _response_cost(response: Any, model: str) -> float | None:
    """Return response cost from LiteLLM, provider metadata, or local fallbacks."""
    try:
        cost = litellm.completion_cost(completion_response=response)
    except Exception:
        cost = None

    numeric_cost = _numeric(cost)
    if numeric_cost is not None:
        return numeric_cost

    provider_cost = _provider_reported_cost(response)
    if provider_cost is not None:
        return provider_cost

    return _deepseek_v4_cost(response, model)


def _is_overloaded(exc: Exception) -> bool:
    """Return True if *exc* is an Anthropic overloaded error."""
    return "overloaded_error" in str(exc) or "Overloaded" in str(exc)


def _is_deepseek_model(model: str) -> bool:
    """Return True when a LiteLLM model id targets DeepSeek."""
    normalized = model.lower()
    return normalized.startswith("deepseek/") or normalized.startswith(
        "openrouter/deepseek/"
    )


def _is_anthropic_model(model: str) -> bool:
    """Return True when a LiteLLM model id targets Anthropic."""
    normalized = model.lower()
    return normalized.startswith("anthropic/") or normalized.startswith(
        "openrouter/anthropic/"
    )


def _is_budget_model(model: str) -> bool:
    """Return True for low-cost model variants that should get cheap fallback."""
    normalized = model.lower()
    return "haiku" in normalized or "flash" in normalized


def _model_accepts_reasoning_effort(model: str) -> bool:
    """Return True when we should pass LiteLLM reasoning_effort to *model*."""
    normalized = model.lower()
    return normalized.startswith("anthropic/claude-") or normalized.startswith(
        "openrouter/anthropic/claude-"
    )


def _model_accepts_response_format(model: str) -> bool:
    """Return True when we should pass OpenAI-style response_format."""
    return _is_deepseek_model(model)


def _model_accepts_extra_body(model: str) -> bool:
    """Return True when provider-specific extra_body should be passed."""
    return _is_deepseek_model(model)


def _dedupe_models(models: list[str]) -> list[str]:
    """Remove duplicate model ids while preserving order."""
    seen: set[str] = set()
    deduped: list[str] = []
    for model in models:
        if model in seen:
            continue
        seen.add(model)
        deduped.append(model)
    return deduped


def _fallback_chain(model: str, fallback_models: list[str] | None = None) -> list[str]:
    """Return the provider-crossing fallback chain for *model*.

    Pro-class primary models fall back to the configured Pro fallback, and
    Flash-class primary models fall back to the configured Flash fallback.
    The chain also works in reverse so Anthropic calls cross back to DeepSeek.
    Unknown providers fall back to the general default model.
    """
    if fallback_models is not None:
        return _dedupe_models([model, *fallback_models])
    if model == PRIMARY_PRO_MODEL:
        return _dedupe_models([model, FALLBACK_PRO_MODEL])
    if model == PRIMARY_FLASH_MODEL:
        return _dedupe_models([model, FALLBACK_FLASH_MODEL])
    if model == FALLBACK_PRO_MODEL:
        return _dedupe_models([model, PRIMARY_PRO_MODEL])
    if model == FALLBACK_FLASH_MODEL:
        return _dedupe_models([model, PRIMARY_FLASH_MODEL])
    if _is_deepseek_model(model):
        return _dedupe_models(
            [
                model,
                FALLBACK_FLASH_MODEL if _is_budget_model(model) else FALLBACK_PRO_MODEL,
            ]
        )
    if _is_anthropic_model(model):
        return _dedupe_models(
            [
                model,
                PRIMARY_FLASH_MODEL if _is_budget_model(model) else PRIMARY_PRO_MODEL,
            ]
        )
    return _dedupe_models([model, DEFAULT_MODEL])


def _completion_kwargs_for_model(kwargs: dict, model: str) -> dict:
    """Return LiteLLM kwargs adjusted for a specific attempted model."""
    adjusted = {k: v for k, v in kwargs.items() if k != "model"}
    requested_reasoning = adjusted.pop("reasoning_effort", None)
    requested_response_format = adjusted.pop("response_format", None)
    requested_extra_body = adjusted.pop("extra_body", None)
    has_temperature = "temperature" in adjusted
    requested_temperature = adjusted.pop("temperature", None)

    effective_reasoning = (
        requested_reasoning if _model_accepts_reasoning_effort(model) else None
    )
    if has_temperature:
        effective_temperature = (
            1.0 if effective_reasoning is not None else requested_temperature
        )
        if effective_temperature is not None:
            adjusted["temperature"] = effective_temperature
    if effective_reasoning is not None:
        adjusted["reasoning_effort"] = effective_reasoning
    if requested_response_format is not None and _model_accepts_response_format(model):
        adjusted["response_format"] = requested_response_format
    if requested_extra_body is not None and _model_accepts_extra_body(model):
        adjusted["extra_body"] = requested_extra_body
    adjusted["model"] = model
    return adjusted


def _effective_params_for_model(
    *,
    model: str,
    max_tokens: int,
    temperature: float | None,
    reasoning_effort: str | None,
    response_format: dict[str, Any] | None,
    extra_body: dict[str, Any] | None,
    requested_model: str,
) -> dict[str, Any]:
    """Return call params as they were effectively sent to the final model."""
    params: dict[str, Any] = {"max_tokens": max_tokens}
    effective_reasoning = (
        reasoning_effort if _model_accepts_reasoning_effort(model) else None
    )
    if temperature is not None:
        params["temperature"] = 1.0 if effective_reasoning is not None else temperature
    if effective_reasoning is not None:
        params["reasoning_effort"] = effective_reasoning
    elif reasoning_effort is not None:
        params["requested_reasoning_effort"] = reasoning_effort
        params["reasoning_effort_omitted_for_model"] = True
    if response_format is not None:
        if _model_accepts_response_format(model):
            params["response_format"] = response_format
        else:
            params["requested_response_format"] = response_format
            params["response_format_omitted_for_model"] = True
    if extra_body is not None:
        if _model_accepts_extra_body(model):
            params["extra_body"] = extra_body
        else:
            params["requested_extra_body"] = extra_body
            params["extra_body_omitted_for_model"] = True
    if requested_model != model:
        params["requested_model"] = requested_model
        params["fallback_used"] = True
    return params


def _call_with_retry(
    kwargs: dict,
    model: str,
    fallback_models: list[str] | None = None,
) -> tuple:
    """Call litellm.completion with retries and provider fallback.

    Retries overloaded errors on the same model using exponential backoff. Any
    provider failure then falls across the Anthropic/DeepSeek boundary, so a
    DeepSeek outage tries Anthropic and an Anthropic outage tries DeepSeek.
    Re-raises the last exception if all attempts fail.

    Args:
        kwargs: litellm.completion keyword arguments (may be mutated for fallback).
        model: Primary model string.
        fallback_models: Optional explicit fallback models. When omitted,
            provider-crossing fallback is inferred from configured profiles.

    Returns:
        A (response, effective_model) tuple.
    """
    last_exc: Exception | None = None
    chain = _fallback_chain(model, fallback_models=fallback_models)
    for model_index, candidate in enumerate(chain):
        for attempt, delay in enumerate(_RETRY_DELAYS + [None]):
            try:
                response = litellm.completion(
                    **_completion_kwargs_for_model(kwargs, candidate)
                )
                if candidate != model:
                    logger.info("Fallback model %s succeeded", candidate)
                return response, candidate
            except Exception as exc:
                last_exc = exc
                if _is_overloaded(exc) and delay is not None:
                    logger.warning(
                        "%s overloaded (attempt %d/%d), retrying in %ds ...",
                        candidate,
                        attempt + 1,
                        len(_RETRY_DELAYS),
                        delay,
                    )
                    time.sleep(delay)
                    continue

                next_model = (
                    chain[model_index + 1] if model_index + 1 < len(chain) else None
                )
                if next_model:
                    logger.warning(
                        "%s failed (%s: %s); switching to fallback %s",
                        candidate,
                        type(exc).__name__,
                        exc,
                        next_model,
                    )
                break

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
    max_tokens: int = MAX_TOKENS_DEFAULT,
    temperature: float | None = 0.7,
    reasoning_effort: str | None = None,
    response_format: dict[str, Any] | None = None,
    extra_body: dict[str, Any] | None = None,
    tools: list[dict] | None = None,
    fallback_models: list[str] | None = None,
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
        response_format: Optional OpenAI-compatible response format hint.
        extra_body: Optional provider-specific request body extras. When omitted,
            DeepSeek model attempts receive the configured DeepSeek default.
        tools: Optional list of tool definitions for function calling.
        fallback_models: Explicit fallback chain after the requested model.
        conn: Open DB connection for logging. None to skip logging.
        request_type: Product-level call type, e.g. "insights" or "nudge".
        metadata: Product context dict stored alongside the call.

    Returns:
        An LLMResult containing the response text and usage metadata.

    Raises:
        litellm.AuthenticationError: If the API key is missing or invalid.
        litellm.APIError: On network or API failures.
    """
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    if response_format is not None:
        kwargs["response_format"] = response_format
    effective_extra_body = extra_body if extra_body is not None else DEEPSEEK_EXTRA_BODY
    if effective_extra_body is not None:
        kwargs["extra_body"] = effective_extra_body
    if tools is not None:
        kwargs["tools"] = tools

    requested_model = model
    t0 = time.perf_counter()
    response, model = _call_with_retry(kwargs, model, fallback_models=fallback_models)
    latency = time.perf_counter() - t0
    usage = response.usage

    cost = _response_cost(response, model)

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
        max_tokens=max_tokens,
        tool_calls=raw_tool_calls if tool_call_dicts else None,
        raw_message=raw_msg,
    )

    if conn and request_type:
        logged_extra_body = effective_extra_body
        if extra_body is None and not _model_accepts_extra_body(model):
            logged_extra_body = None
        params = _effective_params_for_model(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            response_format=response_format,
            extra_body=logged_extra_body,
            requested_model=requested_model,
        )
        log_metadata = dict(metadata or {})
        if requested_model != model:
            log_metadata.setdefault("requested_model", requested_model)
            log_metadata.setdefault("effective_model", model)
            log_metadata.setdefault("fallback_used", True)
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
                metadata=log_metadata,
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
