"""Persistent model routing preferences for CLI and Telegram.

The preference file stores user-facing routing choices separately from
``.env``. Environment variables still seed the built-in defaults, while this
module resolves the effective model, fallback, and small provider quirks for
each feature at call time.
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
                "reasoning_effort": None,
                "temperature": None,
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
    """Return a compact label for Telegram buttons and tables."""
    return model.split("/", 1)[1] if "/" in model else model


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
    fallback = feature_cfg.get("fallback", profile_cfg.get("fallback"))
    if fallback is not None:
        fallback = str(fallback)

    source = "feature" if "primary" in feature_cfg else f"profile:{profile}"
    return ModelRoute(
        feature=feature,
        primary=primary,
        fallback=fallback,
        profile=profile,
        reasoning_effort=feature_cfg.get("reasoning_effort"),
        temperature=feature_cfg.get("temperature"),
        params={
            key: feature_cfg[key]
            for key in ("reasoning_effort", "temperature")
            if key in feature_cfg
        },
        source=source,
    )


def routes_summary(
    *,
    prefs: dict[str, Any] | None = None,
    path: Path | None = None,
) -> list[ModelRoute]:
    """Return effective routes for every feature."""
    prefs = prefs or load_model_prefs(path)
    return [resolve_model_route(feature, prefs=prefs) for feature in FEATURES]


def set_feature_route(
    feature: str,
    *,
    primary: str,
    fallback: str | None | object = ...,
    reasoning_effort: str | None | object = ...,
    temperature: float | None | object = ...,
    path: Path | None = None,
) -> dict[str, Any]:
    """Persist a feature-level route override."""
    if feature not in FEATURES:
        raise ValueError(f"Unknown model feature: {feature}")
    prefs = load_model_prefs(path)
    entry = dict(prefs["features"].get(feature, {}))
    entry["primary"] = primary
    if primary == ANTHROPIC_OPUS_4_7_MODEL:
        entry.setdefault("reasoning_effort", None)
        entry.setdefault("temperature", None)
    if fallback is not ...:
        if fallback is None:
            entry["fallback"] = None
        else:
            entry["fallback"] = fallback
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
        if primary == ANTHROPIC_OPUS_4_7_MODEL:
            entry["reasoning_effort"] = None
            entry["temperature"] = None
        else:
            entry.pop("reasoning_effort", None)
            entry.pop("temperature", None)
        if fallback is not ...:
            if fallback is None:
                entry["fallback"] = None
            else:
                entry["fallback"] = fallback
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


def apply_chat_opus_preset(path: Path | None = None) -> dict[str, Any]:
    """Apply the built-in low-latency Opus chat preset."""
    prefs = load_model_prefs(path)
    prefs["features"]["chat"] = default_model_prefs()["features"]["chat"]
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
    for profile, cfg in prefs["profiles"].items():
        if cfg.get("primary") == cfg.get("fallback"):
            findings.append(f"{profile} primary and fallback are the same.")

    for provider, env_name in {
        "anthropic/": "ANTHROPIC_API_KEY",
        "deepseek/": "DEEPSEEK_API_KEY",
    }.items():
        if any(
            route.primary.startswith(provider) for route in routes_summary(prefs=prefs)
        ):
            if not os.environ.get(env_name):
                findings.append(f"{env_name} is not set.")

    chat = resolve_model_route("chat", prefs=prefs)
    if chat.primary == ANTHROPIC_OPUS_4_7_MODEL and chat.temperature is not None:
        findings.append("Chat Opus 4.7 should omit temperature.")
    return findings


def _default_profile(feature: str) -> str:
    return "flash" if feature in FLASH_FEATURES else "pro"
