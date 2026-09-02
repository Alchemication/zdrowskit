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

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

import litellm
from pydantic import BaseModel

from config import (
    DEFAULT_MODEL,
    FALLBACK_FLASH_MODEL,
    FALLBACK_PRO_MODEL,
    MAX_TOKENS_DEFAULT,
    PRIMARY_FLASH_MODEL,
    PRIMARY_PRO_MODEL,
)
from store import create_llm_trace, log_llm_call

logger = logging.getLogger(__name__)

# Exponential backoff delays (seconds) between retries on a transient failure.
_RETRY_DELAYS = [10, 30, 90]

# Exception type names that mean a transport fault but do not derive from
# OSError, so an isinstance check cannot see them.
_NETWORK_EXCEPTION_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
    }
)

# Substrings identifying a transport fault in a provider exception that has
# already flattened the underlying OSError to a string. Matched lowercase.
# "[Errno 49] Can't assign requested address" is the one observed in the wild:
# it arrived wrapped as a DeepSeek InternalServerError, was retried zero times,
# and took that Monday's weekly report with it.
_NETWORK_ERROR_SIGNALS = (
    "can't assign requested address",
    "connection aborted",
    "connection refused",
    "connection reset",
    "network is down",
    "network is unreachable",
    "no route to host",
    "nodename nor servname",
    "name or service not known",
    "remote end closed connection",
    "server disconnected",
    "temporary failure in name resolution",
)


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


def _response_cost(response: Any) -> float | None:
    """Return response cost from LiteLLM or provider metadata."""
    try:
        cost = litellm.completion_cost(completion_response=response)
    except Exception:
        cost = None

    numeric_cost = _numeric(cost)
    if numeric_cost is not None:
        return numeric_cost

    return _provider_reported_cost(response)


def _is_overloaded(exc: Exception) -> bool:
    """Return True if *exc* is an Anthropic overloaded error."""
    return "overloaded_error" in str(exc) or "Overloaded" in str(exc)


def _is_network_error(exc: Exception) -> bool:
    """Return True if *exc* is a connection-level fault rather than a refusal.

    Distinguished from :func:`_is_overloaded` because the two deserve opposite
    responses. An overloaded provider is a reason to try the other provider; a
    socket that cannot connect is not, since the next provider is reached
    through the same broken network.

    Walks the ``__cause__``/``__context__`` chain because litellm re-raises the
    underlying ``OSError`` as a provider exception whose type says nothing
    about the cause, and falls back to message matching for the providers that
    flatten it to a string.

    Args:
        exc: The exception raised by the completion attempt.

    Returns:
        True when the failure looks like a transport fault.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        # socket.gaierror and socket.timeout are both OSError subclasses, so
        # this one check covers DNS failure, refused connections, resets and
        # socket timeouts alike.
        if isinstance(current, OSError):
            return True
        if type(current).__name__ in _NETWORK_EXCEPTION_NAMES:
            return True
        current = current.__cause__ or current.__context__

    text = str(exc).lower()
    return any(signal in text for signal in _NETWORK_ERROR_SIGNALS)


def is_transient_error_text(text: str | None) -> bool:
    """Return True when a captured error message describes a passing fault.

    The daemon catches ``SystemExit`` from a failed command and only has the
    text of the last logged error, not the exception, so it cannot reuse
    :func:`_is_network_error` directly. Callers use this to tell a fault worth
    re-attempting later (the provider was down, the network was out) from a
    verdict that will repeat identically on every retry (the verifier refused
    the draft, the week has too little data).

    Args:
        text: Captured error message, or None when nothing was captured.

    Returns:
        True when the message names a transport fault or an overloaded
        provider. False for None, empty text, and every deterministic refusal.
    """
    if not text:
        return False
    lowered = text.lower()
    if "overloaded" in lowered:
        return True
    return any(signal in lowered for signal in _NETWORK_ERROR_SIGNALS)


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


def _is_openai_model(model: str) -> bool:
    """Return True when a LiteLLM model id targets OpenAI."""
    normalized = model.lower()
    return normalized.startswith("openai/") or normalized.startswith(
        "openrouter/openai/"
    )


def _is_openai_reasoning_model(model: str) -> bool:
    """Return True for OpenAI models that require an explicit reasoning effort.

    GPT-5.6 rejects a tool-carrying request unless ``reasoning_effort`` is
    stated outright — omitting the parameter and passing ``None`` both fail with
    the same 400. Chat sends tools on every turn, so a routed model matching
    this is unreachable without the explicit value.
    """
    return _is_openai_model(model) and "gpt-5" in model.lower()


def _is_budget_model(model: str) -> bool:
    """Return True for low-cost model variants that should get cheap fallback.

    Luna counts: it is the budget-tier primary here, backing chat, nudges and
    weekly memory, and it must not fail over to a premium reasoning model.
    """
    normalized = model.lower()
    return "haiku" in normalized or "flash" in normalized or "luna" in normalized


# DeepSeek thinking mode is binary; high/max reasoning_effort engages it via
# extra_body. Anything else (low/medium/none/None) sends no extra_body, leaving
# thinking off. Anthropic models receive reasoning_effort natively.
_DEEPSEEK_THINKING_ENABLED: dict[str, Any] = {"thinking": {"type": "enabled"}}

_OPENAI_REASONING_OFF = "none"
"""Effort OpenAI reasoning models take to mean "do not think"."""

# Opus 5 replaced the reasoning transport its predecessors accept. Sending
# reasoning_effort there is rejected with "thinking.type.enabled is not
# supported for this model", and because that arrives as a BadRequest the
# fallback chain answers from another provider instead — the config still says
# Opus 5 while nothing runs on it. Adaptive thinking plus an explicit effort is
# the supported shape, and it must be sent as top-level kwargs: this litellm
# version forwards extra_body to Anthropic verbatim and the API rejects it.
_OPUS5_THINKING_ADAPTIVE: dict[str, Any] = {"type": "adaptive"}


def _uses_output_config_effort(model: str) -> bool:
    """Return True for Anthropic models taking effort via ``output_config``."""
    return _is_anthropic_model(model) and "opus-5" in model.lower()


def _reasoning_engaged(model: str, reasoning_effort: str | None) -> bool:
    """Return True when *reasoning_effort* actually engages reasoning on *model*.

    For Anthropic, any non-None / non-"none" effort engages extended thinking.
    For DeepSeek, only "high" or "max" engages thinking mode. Other providers
    ignore reasoning entirely.
    """
    if reasoning_effort in (None, "none"):
        return False
    if _is_anthropic_model(model):
        return True
    if _is_deepseek_model(model):
        return reasoning_effort in {"high", "max"}
    return False


def _model_accepts_response_format(model: str) -> bool:
    """Return True when we should pass structured response hints."""
    return True


def _model_supports_json_schema(model: str) -> bool:
    """Return True when the provider accepts a Pydantic / JSON-schema response_format.

    DeepSeek currently rejects ``{"type": "json_schema", ...}`` with a 400; only
    legacy ``{"type": "json_object"}`` is honored. Anthropic, OpenAI, and Azure
    all accept Pydantic classes via litellm's automatic JSON-schema conversion.
    """
    normalized = model.lower()
    return (
        normalized.startswith("anthropic/")
        or normalized.startswith("openrouter/anthropic/")
        or normalized.startswith("openai/")
        or normalized.startswith("azure/")
    )


def _pydantic_schema_hint(cls: type[BaseModel]) -> str:
    """Render *cls* as a JSON-schema instruction block for prompt injection."""
    return (
        "Return ONE JSON object that conforms to this JSON Schema. "
        "Your entire response is exactly one JSON object — first character `{`, "
        "last `}`, no fences, no prose.\n\n"
        + json.dumps(cls.model_json_schema(), indent=2)
    )


def _inject_schema_hint(
    messages: list[dict[str, Any]], hint: str
) -> list[dict[str, Any]]:
    """Return a copy of *messages* with *hint* appended to the system message.

    If no system message exists, one is inserted at the front.
    """
    new_messages: list[dict[str, Any]] = [dict(m) for m in messages]
    for msg in new_messages:
        if msg.get("role") == "system":
            existing = msg.get("content") or ""
            msg["content"] = f"{existing}\n\n{hint}" if existing else hint
            return new_messages
    new_messages.insert(0, {"role": "system", "content": hint})
    return new_messages


def _response_format_for_log(response_format: Any) -> Any:
    """Return a JSON-serializable representation of a response format."""
    if response_format is None:
        return None
    if isinstance(response_format, dict):
        return response_format
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        return {
            "type": "pydantic",
            "name": response_format.__name__,
            "schema": response_format.model_json_schema(),
        }
    return str(response_format)


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
    if _is_openai_model(model):
        # Without this branch OpenAI models fell through to DEFAULT_MODEL,
        # which is DeepSeek Pro. That sent Luna — a budget model whose observed
        # failure is spending its output budget on reasoning — to a premium
        # reasoning model inheriting the same effort and ceiling.
        return _dedupe_models(
            [
                model,
                PRIMARY_FLASH_MODEL if _is_budget_model(model) else PRIMARY_PRO_MODEL,
            ]
        )
    return _dedupe_models([model, DEFAULT_MODEL])


def _completion_kwargs_for_model(kwargs: dict, model: str) -> dict:
    """Return LiteLLM kwargs adjusted for a specific attempted model.

    Per-attempt translation: Anthropic receives ``reasoning_effort`` natively,
    except Opus 5 which takes ``thinking={"type": "adaptive"}`` plus
    ``output_config={"effort": ...}``; DeepSeek translates ``reasoning_effort``
    of ``high`` or ``max`` into ``extra_body={"thinking": {"type": "enabled"}}``
    unless the caller passed an explicit ``extra_body`` (which always wins).
    """
    adjusted = {k: v for k, v in kwargs.items() if k != "model"}
    requested_reasoning = adjusted.pop("reasoning_effort", None)
    requested_response_format = adjusted.pop("response_format", None)
    requested_extra_body = adjusted.pop("extra_body", None)
    has_temperature = "temperature" in adjusted
    requested_temperature = adjusted.pop("temperature", None)

    anthropic_reasoning = requested_reasoning if _is_anthropic_model(model) else None
    if has_temperature:
        effective_temperature = (
            1.0 if anthropic_reasoning is not None else requested_temperature
        )
        if effective_temperature is not None:
            adjusted["temperature"] = effective_temperature
    if anthropic_reasoning is not None:
        if _uses_output_config_effort(model):
            adjusted["thinking"] = dict(_OPUS5_THINKING_ADAPTIVE)
            adjusted["output_config"] = {"effort": anthropic_reasoning}
        else:
            adjusted["reasoning_effort"] = anthropic_reasoning
    if requested_response_format is not None and _model_accepts_response_format(model):
        is_pydantic = isinstance(requested_response_format, type) and issubclass(
            requested_response_format, BaseModel
        )
        if is_pydantic and not _model_supports_json_schema(model):
            adjusted["response_format"] = {"type": "json_object"}
            adjusted["messages"] = _inject_schema_hint(
                adjusted.get("messages", []),
                _pydantic_schema_hint(requested_response_format),
            )
        else:
            adjusted["response_format"] = requested_response_format
    if _is_deepseek_model(model):
        if requested_extra_body is not None:
            adjusted["extra_body"] = requested_extra_body
        elif _reasoning_engaged(model, requested_reasoning):
            adjusted["extra_body"] = dict(_DEEPSEEK_THINKING_ENABLED)
    if _is_openai_reasoning_model(model):
        # Never drop to omitted here: GPT-5.6 rejects a tool call without an
        # explicit effort, so the surfaces that carry tools would fail over on
        # every single request.
        adjusted["reasoning_effort"] = requested_reasoning or _OPENAI_REASONING_OFF
    adjusted["model"] = model
    return adjusted


def _effective_params_for_model(
    *,
    model: str,
    max_tokens: int,
    temperature: float | None,
    reasoning_effort: str | None,
    response_format: dict[str, Any] | type[BaseModel] | None,
    extra_body: dict[str, Any] | None,
    requested_model: str,
) -> dict[str, Any]:
    """Return call params as they were effectively sent to the final model."""
    params: dict[str, Any] = {"max_tokens": max_tokens}
    anthropic_reasoning = reasoning_effort if _is_anthropic_model(model) else None
    if temperature is not None:
        params["temperature"] = 1.0 if anthropic_reasoning is not None else temperature
    if reasoning_effort is not None:
        # Always record the requested effort; transport differs by provider.
        params["reasoning_effort"] = reasoning_effort
    if response_format is not None:
        if _model_accepts_response_format(model):
            is_pydantic = isinstance(response_format, type) and issubclass(
                response_format, BaseModel
            )
            if is_pydantic and not _model_supports_json_schema(model):
                params["response_format"] = {"type": "json_object"}
                params["pydantic_schema_injected"] = _response_format_for_log(
                    response_format
                )
            else:
                params["response_format"] = _response_format_for_log(response_format)
        else:
            params["requested_response_format"] = _response_format_for_log(
                response_format
            )
            params["response_format_omitted_for_model"] = True
    effective_extra_body: dict[str, Any] | None = None
    if _is_deepseek_model(model):
        if extra_body is not None:
            effective_extra_body = extra_body
        elif _reasoning_engaged(model, reasoning_effort):
            effective_extra_body = dict(_DEEPSEEK_THINKING_ENABLED)
    elif extra_body is not None:
        params["requested_extra_body"] = extra_body
        params["extra_body_omitted_for_model"] = True
    if effective_extra_body is not None:
        params["extra_body"] = effective_extra_body
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

    Retries overloaded providers and transport faults on the same model using
    exponential backoff. A provider *refusal* then falls across the
    Anthropic/DeepSeek boundary, so a DeepSeek outage tries Anthropic and an
    Anthropic outage tries DeepSeek. A transport fault does not: the fallback
    is reached over the same network that just refused the connection, so the
    chain stops after one ladder rather than spending a second one to learn the
    same thing. Re-raises the last exception if all attempts fail.

    Args:
        kwargs: litellm.completion keyword arguments (may be mutated for fallback).
        model: Primary model string.
        fallback_models: Optional explicit fallback models. When omitted,
            provider-crossing fallback is inferred from configured profiles.

    Returns:
        A (response, effective_model) tuple.
    """
    last_exc: Exception | None = None
    unreachable = False
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
                network_fault = _is_network_error(exc)
                if (_is_overloaded(exc) or network_fault) and delay is not None:
                    logger.warning(
                        "%s %s (attempt %d/%d), retrying in %ds ...",
                        candidate,
                        "unreachable" if network_fault else "overloaded",
                        attempt + 1,
                        len(_RETRY_DELAYS),
                        delay,
                    )
                    time.sleep(delay)
                    continue

                if network_fault:
                    # The other provider is reached over the same socket layer
                    # that just refused us, so failing across the chain would
                    # only spend a second ladder of backoff to learn the same
                    # thing. Give up here and let the caller decide.
                    logger.warning(
                        "%s unreachable after %d attempts (%s: %s); "
                        "not trying a fallback provider over the same network",
                        candidate,
                        len(_RETRY_DELAYS) + 1,
                        type(exc).__name__,
                        exc,
                    )
                    unreachable = True
                    break

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
        if unreachable:
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
    response_format: dict[str, Any] | type[BaseModel] | None = None,
    extra_body: dict[str, Any] | None = None,
    tools: list[dict] | None = None,
    fallback_models: list[str] | None = None,
    conn: sqlite3.Connection | None = None,
    request_type: str = "",
    metadata: dict | None = None,
    trace_id: int | None = None,
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
            entirely for models that reject it (e.g. claude-opus-5, which
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
        trace_id: Optional llm_trace row grouping related LLM calls.

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
    if extra_body is not None:
        kwargs["extra_body"] = extra_body
    if tools is not None:
        kwargs["tools"] = tools

    requested_model = model
    t0 = time.perf_counter()
    response, model = _call_with_retry(kwargs, model, fallback_models=fallback_models)
    latency = time.perf_counter() - t0
    usage = response.usage

    cost = _response_cost(response)

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
        try:
            effective_trace_id = trace_id or create_llm_trace(conn, request_type)
            logged_messages = _completion_kwargs_for_model(kwargs, model).get(
                "messages", messages
            )
            params = _effective_params_for_model(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                response_format=response_format,
                extra_body=extra_body,
                requested_model=requested_model,
            )
            log_metadata = dict(metadata or {})
            if requested_model != model:
                log_metadata.setdefault("requested_model", requested_model)
                log_metadata.setdefault("effective_model", model)
                log_metadata.setdefault("fallback_used", True)
            row_id = log_llm_call(
                conn,
                request_type=request_type,
                model=model,
                messages=logged_messages,
                response_text=result.text,
                params=params,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=result.total_tokens,
                latency_s=result.latency_s,
                cost=result.cost,
                metadata=log_metadata,
                trace_id=effective_trace_id,
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


def strip_json_fences(text: str) -> str:
    """Drop a ```json``` fence wrapping the payload, if present.

    Args:
        text: Raw LLM output that may be wrapped in a Markdown code fence.

    Returns:
        The payload with the surrounding fence removed and whitespace trimmed.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    return candidate
