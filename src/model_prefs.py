"""Persistent model routing preferences for CLI and Telegram.

The preference file stores user-facing routing choices separately from
``.env``. Environment variables still seed the built-in defaults, while this
module resolves the effective model, fallback, and small provider quirks for
each feature at call time.

A feature's stored ``fallback`` key has three meanings:

* **absent** or ``null`` — fall through to the profile's fallback at resolve
  time. Persisting ``null`` lets the user override a built-in feature-level
  fallback (e.g. ``chat``) without mutating the defaults.
* **string** — explicit per-feature fallback override.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import (
    ANTHROPIC_HAIKU_MODEL,
    ANTHROPIC_OPUS_4_7_MODEL,
    ANTHROPIC_OPUS_MODEL,
    DEFAULT_ADD_CLONE_MODEL,
    DEFAULT_CHAT_MODEL,
    DEFAULT_COACH_MODEL,
    DEFAULT_INSIGHTS_MODEL,
    DEFAULT_LOG_FLOW_MODEL,
    DEFAULT_NOTIFY_MODEL,
    DEFAULT_NUDGE_MODEL,
    FALLBACK_FLASH_MODEL,
    FALLBACK_PRO_MODEL,
    MODEL_PREFS_PATH,
    PRIMARY_FLASH_MODEL,
    PRIMARY_PRO_MODEL,
    VERIFICATION_MODEL,
    VERIFICATION_REWRITE_MODEL,
)

PREFS_VERSION = 1

PRO_FEATURES: tuple[str, ...] = (
    "insights",
    "coach",
    "nudge",
    "chat",
    "verification",
)
FLASH_FEATURES: tuple[str, ...] = (
    "notify",
    "log_flow",
    "add_clone",
    "verification_rewrite",
)
FEATURES: tuple[str, ...] = PRO_FEATURES + FLASH_FEATURES

FEATURE_LABELS: dict[str, str] = {
    "insights": "Reports",
    "coach": "Coach",
    "nudge": "Nudges",
    "chat": "Chat",
    "notify": "Notify parser",
    "log_flow": "Log flow",
    "add_clone": "Add workout",
    "verification": "Verifier",
    "verification_rewrite": "Verifier rewrite",
}

TELEGRAM_FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "chat": ("chat",),
    "reports": ("insights",),
    "coach": ("coach",),
    "nudges": ("nudge",),
    "utilities": ("notify", "log_flow", "add_clone"),
}

# Capability tier shown next to each model in Telegram buttons. Helps users
# pick something appropriate for the feature without memorising provider
# pricing.
MODEL_TIERS: dict[str, str] = {
    PRIMARY_PRO_MODEL: "pro",
    FALLBACK_PRO_MODEL: "premium",
    PRIMARY_FLASH_MODEL: "flash",
    FALLBACK_FLASH_MODEL: "lite",
    ANTHROPIC_OPUS_MODEL: "premium",
    ANTHROPIC_OPUS_4_7_MODEL: "premium",
    ANTHROPIC_HAIKU_MODEL: "lite",
}

# Reasoning and temperature pickers exposed in the Telegram /models flow.
REASONING_CHOICES: tuple[str | None, ...] = (None, "low", "medium", "high")
TEMPERATURE_CHOICES: tuple[float | None, ...] = (None, 0.0, 0.3, 0.7, 1.0)


@dataclass(frozen=True)
class ModelRoute:
    """Effective model routing for one feature."""

    feature: str
    primary: str
    fallback: str | None
    profile: str
    reasoning_effort: str | None
    temperature: float | None
    params: dict[str, Any]
    source: str

    def call_kwargs(self) -> dict[str, Any]:
        """Return kwargs to pass into ``llm.call_llm`` for this route."""
        kwargs: dict[str, Any] = {"model": self.primary}
        if self.fallback:
            kwargs["fallback_models"] = [self.fallback]
        if "reasoning_effort" in self.params:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if "temperature" in self.params:
            kwargs["temperature"] = self.temperature
        return kwargs


def default_model_prefs() -> dict[str, Any]:
    """Return built-in model preferences seeded from ``config.py``."""
    return {
        "version": PREFS_VERSION,
        "profiles": {
            "pro": {
                "primary": PRIMARY_PRO_MODEL,
                "fallback": FALLBACK_PRO_MODEL,
            },
            "flash": {
                "primary": PRIMARY_FLASH_MODEL,
                "fallback": FALLBACK_FLASH_MODEL,
            },
        },
        "features": {
            "insights": {"profile": "pro", "primary": DEFAULT_INSIGHTS_MODEL},
            "coach": {"profile": "pro", "primary": DEFAULT_COACH_MODEL},
            "nudge": {"profile": "pro", "primary": DEFAULT_NUDGE_MODEL},
            "chat": {
                "profile": "pro",
                "primary": ANTHROPIC_OPUS_4_7_MODEL,
                "fallback": DEFAULT_CHAT_MODEL,
            },
            "notify": {"profile": "flash", "primary": DEFAULT_NOTIFY_MODEL},
            "log_flow": {"profile": "flash", "primary": DEFAULT_LOG_FLOW_MODEL},
            "add_clone": {"profile": "flash", "primary": DEFAULT_ADD_CLONE_MODEL},
            "verification": {"profile": "pro", "primary": VERIFICATION_MODEL},
            "verification_rewrite": {
                "profile": "flash",
                "primary": VERIFICATION_REWRITE_MODEL,
            },
        },
    }


def selectable_models() -> list[str]:
    """Return the button-safe model IDs known by ``config.py``."""
    candidates = [
        PRIMARY_PRO_MODEL,
        FALLBACK_PRO_MODEL,
        PRIMARY_FLASH_MODEL,
        FALLBACK_FLASH_MODEL,
        ANTHROPIC_OPUS_MODEL,
        ANTHROPIC_OPUS_4_7_MODEL,
        ANTHROPIC_HAIKU_MODEL,
        DEFAULT_INSIGHTS_MODEL,
        DEFAULT_COACH_MODEL,
        DEFAULT_NUDGE_MODEL,
        DEFAULT_CHAT_MODEL,
        DEFAULT_NOTIFY_MODEL,
        DEFAULT_LOG_FLOW_MODEL,
        DEFAULT_ADD_CLONE_MODEL,
        VERIFICATION_MODEL,
        VERIFICATION_REWRITE_MODEL,
    ]
    seen: set[str] = set()
    models: list[str] = []
    for model in candidates:
        if model and model not in seen:
            seen.add(model)
            models.append(model)
    return models


def model_label(model: str) -> str:
    """Return a compact label for tables (no tier annotation)."""
    return model.split("/", 1)[1] if "/" in model else model


def model_button_label(model: str) -> str:
    """Return a Telegram-button label with capability tier annotation."""
    short = model_label(model).removeprefix("claude-")
    tier = MODEL_TIERS.get(model)
    return f"{short} · {tier}" if tier else short


def reasoning_label(value: str | None) -> str:
    """Return a Telegram-friendly label for a reasoning effort value."""
    return "off" if value is None else value


def temperature_label(value: float | None) -> str:
    """Return a Telegram-friendly label for a temperature value."""
    return "omit" if value is None else f"{value:g}"


def _prefs_path(path: Path | None = None) -> Path:
    """Return the active model prefs path."""
    return path or MODEL_PREFS_PATH


def load_model_prefs(path: Path | None = None) -> dict[str, Any]:
    """Load model preferences, merging missing keys with built-ins."""
    path = _prefs_path(path)
    prefs = default_model_prefs()
    if not path.exists():
        return prefs
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return prefs
    if not isinstance(raw, dict):
        return prefs

    raw_profiles = raw.get("profiles")
    if isinstance(raw_profiles, dict):
        for name, profile in raw_profiles.items():
            if name in prefs["profiles"] and isinstance(profile, dict):
                prefs["profiles"][name].update(
                    {
                        key: value
                        for key, value in profile.items()
                        if key in {"primary", "fallback"} and isinstance(value, str)
                    }
                )

    raw_features = raw.get("features")
    if isinstance(raw_features, dict):
        for feature, override in raw_features.items():
            if feature in prefs["features"] and isinstance(override, dict):
                clean = {
                    key: value
                    for key, value in override.items()
                    if key
                    in {
                        "profile",
                        "primary",
                        "fallback",
                        "reasoning_effort",
                        "temperature",
                    }
                }
                prefs["features"][feature].update(clean)
    return prefs


def save_model_prefs(
    prefs: dict[str, Any],
    path: Path | None = None,
) -> None:
    """Persist model preferences as pretty JSON."""
    path = _prefs_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(prefs, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def resolve_model_route(
    feature: str,
    *,
    prefs: dict[str, Any] | None = None,
    path: Path | None = None,
) -> ModelRoute:
    """Resolve the effective model route for a feature."""
    if feature not in FEATURES:
        raise ValueError(f"Unknown model feature: {feature}")
    prefs = prefs or load_model_prefs(path)
    feature_cfg = dict(prefs["features"][feature])
    profile = str(feature_cfg.get("profile") or _default_profile(feature))
    profile_cfg = dict(prefs["profiles"].get(profile, {}))

    primary = str(feature_cfg.get("primary") or profile_cfg.get("primary"))
    raw_fallback = feature_cfg.get("fallback")
    if raw_fallback is None:
        # Missing or explicit ``null`` both mean "fall through to profile".
        raw_fallback = profile_cfg.get("fallback")
    fallback = str(raw_fallback) if raw_fallback else None

    # Effective reasoning/temperature: per-feature override wins; otherwise
    # the primary model's documented requirements apply (Opus 4.7 must omit
    # temperature and run with reasoning off); otherwise leave to call_llm
    # to apply its global defaults.
    primary_defaults = _primary_model_defaults(primary)
    params: dict[str, Any] = {}
    for key in ("reasoning_effort", "temperature"):
        if key in feature_cfg:
            params[key] = feature_cfg[key]
        elif key in primary_defaults:
            params[key] = primary_defaults[key]

    source = "feature" if "primary" in feature_cfg else f"profile:{profile}"
    return ModelRoute(
        feature=feature,
        primary=primary,
        fallback=fallback,
        profile=profile,
        reasoning_effort=params.get("reasoning_effort"),
        temperature=params.get("temperature"),
        params=params,
        source=source,
    )


def _primary_model_defaults(primary: str) -> dict[str, Any]:
    """Return reasoning/temperature defaults forced by the primary model."""
    if primary == ANTHROPIC_OPUS_4_7_MODEL:
        return {"reasoning_effort": None, "temperature": None}
    return {}


def routes_summary(
    *,
    prefs: dict[str, Any] | None = None,
    path: Path | None = None,
) -> list[ModelRoute]:
    """Return effective routes for every feature."""
    prefs = prefs or load_model_prefs(path)
    return [resolve_model_route(feature, prefs=prefs) for feature in FEATURES]


def profile_fallback_for(
    feature: str,
    *,
    prefs: dict[str, Any] | None = None,
    path: Path | None = None,
) -> str | None:
    """Return the profile-level fallback for ``feature`` (no per-feature override)."""
    if feature not in FEATURES:
        raise ValueError(f"Unknown model feature: {feature}")
    prefs = prefs or load_model_prefs(path)
    profile_name = prefs["features"][feature].get("profile") or _default_profile(
        feature
    )
    profile_cfg = prefs["profiles"].get(profile_name, {})
    raw = profile_cfg.get("fallback")
    return str(raw) if raw else None


def _normalize_primary_params(entry: dict[str, Any], primary: str) -> None:
    """Reset reasoning/temperature when primary model family changes.

    Switching to Opus 4.7 forces both off (the model omits temperature and
    has reasoning controls disabled by spec). Switching to anything else
    drops Opus-specific overrides so the route picks up the global call
    defaults again.
    """
    if primary == ANTHROPIC_OPUS_4_7_MODEL:
        entry["reasoning_effort"] = None
        entry["temperature"] = None
    else:
        entry.pop("reasoning_effort", None)
        entry.pop("temperature", None)


def set_feature_route(
    feature: str,
    *,
    primary: str | object = ...,
    fallback: str | None | object = ...,
    reasoning_effort: str | None | object = ...,
    temperature: float | None | object = ...,
    path: Path | None = None,
) -> dict[str, Any]:
    """Persist a feature-level route override.

    Sentinels:
        ``...`` means "leave unchanged". ``None`` for ``fallback`` clears the
        per-feature override so the profile fallback applies. ``None`` for
        reasoning/temperature is an explicit "off / omit" value.
    """
    if feature not in FEATURES:
        raise ValueError(f"Unknown model feature: {feature}")
    prefs = load_model_prefs(path)
    entry = dict(prefs["features"].get(feature, {}))
    primary_changed = False
    if primary is not ...:
        primary_changed = entry.get("primary") != primary
        entry["primary"] = primary  # type: ignore[assignment]
    if primary_changed:
        _normalize_primary_params(entry, str(entry["primary"]))
    if fallback is not ...:
        # ``None`` persists as JSON null and means "use profile fallback".
        # The resolver treats null and missing identically, but storing
        # null lets us override built-in feature-level fallbacks (e.g. chat).
        entry["fallback"] = fallback  # type: ignore[assignment]
    if reasoning_effort is not ...:
        entry["reasoning_effort"] = reasoning_effort
    if temperature is not ...:
        entry["temperature"] = temperature
    prefs["features"][feature] = entry
    save_model_prefs(prefs, path)
    return prefs


def set_profile_route(
    profile: str,
    *,
    primary: str,
    fallback: str,
    path: Path | None = None,
) -> dict[str, Any]:
    """Persist a Pro or Flash profile route."""
    if profile not in {"pro", "flash"}:
        raise ValueError("Profile must be 'pro' or 'flash'")
    prefs = load_model_prefs(path)
    prefs["profiles"][profile] = {"primary": primary, "fallback": fallback}
    save_model_prefs(prefs, path)
    return prefs


def update_multiple_features(
    features: tuple[str, ...],
    *,
    primary: str,
    fallback: str | None | object = ...,
    path: Path | None = None,
) -> None:
    """Persist the same primary/fallback route across a feature group."""
    prefs = load_model_prefs(path)
    for feature in features:
        if feature not in FEATURES:
            raise ValueError(f"Unknown model feature: {feature}")
        entry = dict(prefs["features"].get(feature, {}))
        entry["primary"] = primary
        _normalize_primary_params(entry, primary)
        if fallback is not ...:
            # ``None`` persists as null and means "use profile fallback".
            entry["fallback"] = fallback  # type: ignore[assignment]
        prefs["features"][feature] = entry
    save_model_prefs(prefs, path)


def reset_feature_route(
    feature: str,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """Reset a feature to the built-in default route."""
    if feature not in FEATURES:
        raise ValueError(f"Unknown model feature: {feature}")
    prefs = load_model_prefs(path)
    prefs["features"][feature] = default_model_prefs()["features"][feature]
    save_model_prefs(prefs, path)
    return prefs


def reset_all_routes(path: Path | None = None) -> dict[str, Any]:
    """Reset every feature and both profiles to built-in defaults."""
    prefs = default_model_prefs()
    save_model_prefs(prefs, path)
    return prefs


def doctor_findings(
    *,
    prefs: dict[str, Any] | None = None,
    path: Path | None = None,
) -> list[str]:
    """Return actionable model-routing warnings."""
    prefs = prefs or load_model_prefs(path)
    findings: list[str] = []
    for profile_name, cfg in prefs["profiles"].items():
        if cfg.get("primary") and cfg.get("primary") == cfg.get("fallback"):
            findings.append(
                f"{profile_name} profile primary and fallback are the same model."
            )

    routes = routes_summary(prefs=prefs)
    for route in routes:
        if route.fallback and route.primary == route.fallback:
            findings.append(
                f"{FEATURE_LABELS.get(route.feature, route.feature)} primary and "
                "fallback are the same model."
            )

    provider_envs = {
        "anthropic/": "ANTHROPIC_API_KEY",
        "deepseek/": "DEEPSEEK_API_KEY",
        "openai/": "OPENAI_API_KEY",
        "gemini/": "GEMINI_API_KEY",
    }
    used_models: set[str] = set()
    for route in routes:
        used_models.add(route.primary)
        if route.fallback:
            used_models.add(route.fallback)
    for provider, env_name in provider_envs.items():
        if any(model.startswith(provider) for model in used_models):
            if not os.environ.get(env_name):
                findings.append(f"{env_name} is not set.")

    chat = resolve_model_route("chat", prefs=prefs)
    if chat.primary == ANTHROPIC_OPUS_4_7_MODEL:
        if chat.temperature is not None:
            findings.append("Chat Opus 4.7 should omit temperature.")
        if chat.reasoning_effort is not None:
            findings.append("Chat Opus 4.7 should run with reasoning off.")
    return findings


def _default_profile(feature: str) -> str:
    return "flash" if feature in FLASH_FEATURES else "pro"
