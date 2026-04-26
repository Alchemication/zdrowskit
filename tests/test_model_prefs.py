"""Tests for model routing preferences."""

from __future__ import annotations

from config import (
    ANTHROPIC_OPUS_4_7_MODEL,
    FALLBACK_PRO_MODEL,
    PRIMARY_FLASH_MODEL,
    PRIMARY_PRO_MODEL,
)
from model_prefs import (
    apply_chat_opus_preset,
    reset_feature_route,
    resolve_model_route,
    selectable_models,
    set_feature_route,
    set_profile_route,
)


class TestModelPrefs:
    def test_chat_default_uses_opus_without_reasoning_or_temperature(self, tmp_path):
        route = resolve_model_route("chat", path=tmp_path / "models.json")

        assert route.primary == ANTHROPIC_OPUS_4_7_MODEL
        assert route.fallback == PRIMARY_PRO_MODEL
        assert route.call_kwargs()["reasoning_effort"] is None
        assert route.call_kwargs()["temperature"] is None

    def test_feature_override_and_reset(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route(
            "nudge",
            primary=ANTHROPIC_OPUS_4_7_MODEL,
            fallback=PRIMARY_PRO_MODEL,
            path=path,
        )

        route = resolve_model_route("nudge", path=path)
        assert route.primary == ANTHROPIC_OPUS_4_7_MODEL
        assert route.fallback == PRIMARY_PRO_MODEL

        reset_feature_route("nudge", path=path)

        route = resolve_model_route("nudge", path=path)
        assert route.primary == PRIMARY_PRO_MODEL
        assert route.fallback == FALLBACK_PRO_MODEL

    def test_profile_route_updates_inherited_fallback(self, tmp_path):
        path = tmp_path / "models.json"
        set_profile_route(
            "flash",
            primary=PRIMARY_FLASH_MODEL,
            fallback=ANTHROPIC_OPUS_4_7_MODEL,
            path=path,
        )

        route = resolve_model_route("notify", path=path)
        assert route.primary == PRIMARY_FLASH_MODEL
        assert route.fallback == ANTHROPIC_OPUS_4_7_MODEL

    def test_none_fallback_overrides_builtin_feature_fallback(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route(
            "chat",
            primary=ANTHROPIC_OPUS_4_7_MODEL,
            fallback=None,
            path=path,
        )

        route = resolve_model_route("chat", path=path)
        assert route.fallback is None

    def test_chat_opus_preset_restores_builtin_chat_route(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route(
            "chat", primary=PRIMARY_PRO_MODEL, fallback=FALLBACK_PRO_MODEL, path=path
        )

        apply_chat_opus_preset(path=path)

        route = resolve_model_route("chat", path=path)
        assert route.primary == ANTHROPIC_OPUS_4_7_MODEL
        assert route.temperature is None

    def test_selectable_models_include_configured_choices(self):
        models = selectable_models()

        assert PRIMARY_PRO_MODEL in models
        assert PRIMARY_FLASH_MODEL in models
        assert ANTHROPIC_OPUS_4_7_MODEL in models
