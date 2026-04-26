"""Tests for model routing preferences."""

from __future__ import annotations

import json

from config import (
    ANTHROPIC_OPUS_4_7_MODEL,
    FALLBACK_PRO_MODEL,
    PRIMARY_FLASH_MODEL,
    PRIMARY_PRO_MODEL,
)
from model_prefs import (
    MODEL_TIERS,
    doctor_findings,
    model_button_label,
    profile_fallback_for,
    reset_all_routes,
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

    def test_none_fallback_falls_through_to_profile(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route(
            "chat",
            primary=ANTHROPIC_OPUS_4_7_MODEL,
            fallback=None,
            path=path,
        )

        route = resolve_model_route("chat", path=path)
        # ``None`` overrides chat's built-in feature-level fallback so the
        # profile fallback (Pro: claude-opus-4-6) applies.
        assert route.fallback == FALLBACK_PRO_MODEL
        # Persisted as JSON null so the resolver can distinguish "user
        # picked auto" from "default chat fallback".
        assert json.loads(path.read_text())["features"]["chat"]["fallback"] is None

    def test_missing_fallback_key_falls_through_to_profile(self, tmp_path):
        path = tmp_path / "models.json"
        path.write_text(json.dumps({"features": {"insights": {}}}))

        route = resolve_model_route("insights", path=path)
        assert route.fallback == FALLBACK_PRO_MODEL

    def test_reset_feature_restores_chat_to_opus(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route(
            "chat",
            primary=PRIMARY_PRO_MODEL,
            fallback=FALLBACK_PRO_MODEL,
            path=path,
        )

        reset_feature_route("chat", path=path)

        route = resolve_model_route("chat", path=path)
        assert route.primary == ANTHROPIC_OPUS_4_7_MODEL
        assert route.temperature is None
        assert route.reasoning_effort is None

    def test_reset_all_restores_every_feature(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route("nudge", primary=ANTHROPIC_OPUS_4_7_MODEL, path=path)
        set_profile_route(
            "flash",
            primary=ANTHROPIC_OPUS_4_7_MODEL,
            fallback=ANTHROPIC_OPUS_4_7_MODEL,
            path=path,
        )

        reset_all_routes(path=path)

        nudge = resolve_model_route("nudge", path=path)
        notify = resolve_model_route("notify", path=path)
        assert nudge.primary == PRIMARY_PRO_MODEL
        assert notify.fallback != ANTHROPIC_OPUS_4_7_MODEL

    def test_set_feature_strips_reasoning_when_primary_changes_away_from_opus(
        self, tmp_path
    ):
        path = tmp_path / "models.json"
        # Start with chat on Opus 4.7 (reasoning/temp explicit).
        set_feature_route(
            "chat",
            primary=ANTHROPIC_OPUS_4_7_MODEL,
            reasoning_effort=None,
            temperature=None,
            path=path,
        )
        # Switching primary to a different family drops Opus-specific overrides.
        set_feature_route("chat", primary=PRIMARY_PRO_MODEL, path=path)

        route = resolve_model_route("chat", path=path)
        assert route.primary == PRIMARY_PRO_MODEL
        assert "reasoning_effort" not in route.params
        assert "temperature" not in route.params

    def test_explicit_reasoning_override_persists(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route(
            "coach",
            primary=PRIMARY_PRO_MODEL,
            reasoning_effort="medium",
            path=path,
        )

        route = resolve_model_route("coach", path=path)
        assert route.reasoning_effort == "medium"

    def test_profile_fallback_for_returns_profile_fallback(self, tmp_path):
        path = tmp_path / "models.json"
        assert profile_fallback_for("chat", path=path) == FALLBACK_PRO_MODEL
        assert profile_fallback_for("notify", path=path) is not None

    def test_selectable_models_include_configured_choices(self):
        models = selectable_models()

        assert PRIMARY_PRO_MODEL in models
        assert PRIMARY_FLASH_MODEL in models
        assert ANTHROPIC_OPUS_4_7_MODEL in models

    def test_model_button_label_includes_tier(self):
        label = model_button_label(ANTHROPIC_OPUS_4_7_MODEL)

        assert "opus-4-7" in label
        assert MODEL_TIERS[ANTHROPIC_OPUS_4_7_MODEL] in label


class TestDoctor:
    def test_flags_feature_primary_equal_to_fallback(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route(
            "nudge",
            primary=PRIMARY_PRO_MODEL,
            fallback=PRIMARY_PRO_MODEL,
            path=path,
        )

        findings = doctor_findings(path=path)

        assert any("Nudges" in finding for finding in findings)

    def test_flags_opus_chat_with_reasoning_set(self, tmp_path, monkeypatch):
        path = tmp_path / "models.json"
        # Suppress missing API key noise — focus on the reasoning warning.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
        set_feature_route(
            "chat",
            primary=ANTHROPIC_OPUS_4_7_MODEL,
            reasoning_effort="medium",
            path=path,
        )

        findings = doctor_findings(path=path)

        assert any("reasoning" in finding for finding in findings)
