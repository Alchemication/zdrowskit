"""Tests for model routing preferences."""

from __future__ import annotations

import json

from config import (
    ANTHROPIC_HAIKU_MODEL,
    ANTHROPIC_OPUS_MODEL,
    FALLBACK_PRO_MODEL,
    DEFAULT_CHAT_MODEL,
    OPENAI_LUNA_MODEL,
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
    def test_async_defaults_use_opus_with_high_reasoning_no_temperature(self, tmp_path):
        for feature in ("insights", "coach"):
            route = resolve_model_route(feature, path=tmp_path / "models.json")

            assert route.primary == ANTHROPIC_OPUS_MODEL
            assert route.fallback == PRIMARY_PRO_MODEL
            assert route.call_kwargs()["reasoning_effort"] == "high"
            assert route.call_kwargs()["temperature"] is None

    def test_nudge_default_is_luna_falling_back_to_flash(self, tmp_path):
        """Opus 5, DeepSeek Pro and DeepSeek Flash all scored 40% on the nudge
        cases, so the fallback should be the cheapest of them. Pro is also a
        reasoning model on the same effort and token ceiling as Luna, which
        makes it liable to repeat the max_output_tokens failure it is there to
        catch."""
        route = resolve_model_route("nudge", path=tmp_path / "models.json")

        assert route.primary == OPENAI_LUNA_MODEL
        assert route.fallback == PRIMARY_FLASH_MODEL
        assert route.fallback != ANTHROPIC_OPUS_MODEL
        assert route.fallback != PRIMARY_PRO_MODEL
        assert route.call_kwargs()["reasoning_effort"] == "high"
        assert route.call_kwargs()["temperature"] is None

    def test_chat_default_uses_luna(self, tmp_path):
        route = resolve_model_route("chat", path=tmp_path / "models.json")

        assert route.primary == DEFAULT_CHAT_MODEL
        assert route.fallback == ANTHROPIC_HAIKU_MODEL
        assert route.call_kwargs()["reasoning_effort"] == "high"
        assert route.call_kwargs()["temperature"] is None

    def test_flash_utility_defaults_use_reasoning_without_temperature(self, tmp_path):
        for feature in ("add_clone", "verification_rewrite"):
            route = resolve_model_route(feature, path=tmp_path / "models.json")

            assert route.primary == PRIMARY_FLASH_MODEL
            assert route.fallback == ANTHROPIC_HAIKU_MODEL
            assert route.call_kwargs()["reasoning_effort"] == "high"
            assert route.call_kwargs()["temperature"] is None

    def test_feature_override_and_reset(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route(
            "nudge",
            primary=ANTHROPIC_OPUS_MODEL,
            fallback=PRIMARY_PRO_MODEL,
            path=path,
        )

        route = resolve_model_route("nudge", path=path)
        assert route.primary == ANTHROPIC_OPUS_MODEL
        assert route.fallback == PRIMARY_PRO_MODEL

        reset_feature_route("nudge", path=path)

        route = resolve_model_route("nudge", path=path)
        assert route.primary == OPENAI_LUNA_MODEL
        assert route.fallback == PRIMARY_FLASH_MODEL

    def test_profile_route_updates_inherited_fallback(self, tmp_path):
        path = tmp_path / "models.json"
        set_profile_route(
            "flash",
            primary=PRIMARY_FLASH_MODEL,
            fallback=ANTHROPIC_OPUS_MODEL,
            path=path,
        )

        route = resolve_model_route("notify", path=path)
        assert route.primary == PRIMARY_FLASH_MODEL
        assert route.fallback == ANTHROPIC_OPUS_MODEL

    def test_none_fallback_falls_through_to_profile(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route(
            "chat",
            primary=ANTHROPIC_OPUS_MODEL,
            fallback=None,
            path=path,
        )

        route = resolve_model_route("chat", path=path)
        # ``None`` overrides chat's built-in feature-level fallback so the
        # profile fallback applies.
        assert route.fallback == ANTHROPIC_HAIKU_MODEL
        # Persisted as JSON null so the resolver can distinguish "user
        # picked auto" from "default chat fallback".
        assert json.loads(path.read_text())["features"]["chat"]["fallback"] is None

    def test_missing_fallback_key_falls_through_to_profile(self, tmp_path):
        path = tmp_path / "models.json"
        path.write_text(json.dumps({"features": {"insights": {}}}))

        route = resolve_model_route("insights", path=path)
        assert route.fallback == PRIMARY_PRO_MODEL

    def test_legacy_v1_default_async_routes_migrate_to_new_defaults(self, tmp_path):
        path = tmp_path / "models.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "features": {
                        "insights": {
                            "profile": "pro",
                            "primary": PRIMARY_PRO_MODEL,
                        },
                        "nudge": {
                            "profile": "pro",
                            "primary": PRIMARY_PRO_MODEL,
                        },
                    },
                }
            )
        )

        insights = resolve_model_route("insights", path=path)
        nudge = resolve_model_route("nudge", path=path)

        assert insights.primary == ANTHROPIC_OPUS_MODEL
        assert nudge.primary == OPENAI_LUNA_MODEL
        assert nudge.reasoning_effort == "high"

    def test_legacy_default_chat_route_migrates_to_new_defaults(self, tmp_path):
        path = tmp_path / "models.json"
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "features": {
                        "chat": {
                            "profile": "flash",
                            "primary": PRIMARY_FLASH_MODEL,
                            "reasoning_effort": None,
                            "temperature": 0.7,
                        },
                    },
                }
            )
        )

        chat = resolve_model_route("chat", path=path)

        assert chat.primary == DEFAULT_CHAT_MODEL
        assert chat.reasoning_effort == "high"
        assert chat.temperature is None

    def test_legacy_default_flash_utility_routes_migrate_to_new_defaults(
        self, tmp_path
    ):
        path = tmp_path / "models.json"
        path.write_text(
            json.dumps(
                {
                    "version": 5,
                    "features": {
                        "add_clone": {
                            "profile": "flash",
                            "primary": PRIMARY_FLASH_MODEL,
                        },
                        "verification_rewrite": {
                            "profile": "flash",
                            "primary": PRIMARY_FLASH_MODEL,
                        },
                    },
                }
            )
        )

        add_clone = resolve_model_route("add_clone", path=path)
        verification_rewrite = resolve_model_route("verification_rewrite", path=path)

        assert add_clone.reasoning_effort == "high"
        assert add_clone.temperature is None
        assert verification_rewrite.reasoning_effort == "high"
        assert verification_rewrite.temperature is None

    def test_legacy_v1_explicit_async_override_is_preserved(self, tmp_path):
        path = tmp_path / "models.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "features": {
                        "nudge": {
                            "profile": "pro",
                            "primary": PRIMARY_PRO_MODEL,
                            "temperature": 0.3,
                        },
                    },
                }
            )
        )

        route = resolve_model_route("nudge", path=path)

        assert route.primary == PRIMARY_PRO_MODEL
        assert route.temperature == 0.3

    def test_reset_feature_restores_chat_default(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route(
            "chat",
            primary=ANTHROPIC_OPUS_MODEL,
            fallback=FALLBACK_PRO_MODEL,
            path=path,
        )

        reset_feature_route("chat", path=path)

        route = resolve_model_route("chat", path=path)
        assert route.primary == DEFAULT_CHAT_MODEL
        assert route.reasoning_effort == "high"
        assert route.temperature is None

    def test_reset_all_restores_every_feature(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route("nudge", primary=ANTHROPIC_OPUS_MODEL, path=path)
        set_profile_route(
            "flash",
            primary=ANTHROPIC_OPUS_MODEL,
            fallback=ANTHROPIC_OPUS_MODEL,
            path=path,
        )

        reset_all_routes(path=path)

        nudge = resolve_model_route("nudge", path=path)
        notify = resolve_model_route("notify", path=path)
        assert nudge.primary == OPENAI_LUNA_MODEL
        assert notify.fallback != ANTHROPIC_OPUS_MODEL

    def test_set_chat_feature_returns_to_chat_controls_when_leaving_opus(
        self, tmp_path
    ):
        path = tmp_path / "models.json"
        # Start with chat on Opus 4.8 (reasoning/temp explicit).
        set_feature_route(
            "chat",
            primary=ANTHROPIC_OPUS_MODEL,
            reasoning_effort=None,
            temperature=None,
            path=path,
        )
        # Switching primary to a different family drops Opus-specific overrides.
        set_feature_route("chat", primary=PRIMARY_PRO_MODEL, path=path)

        route = resolve_model_route("chat", path=path)
        assert route.primary == PRIMARY_PRO_MODEL
        assert route.reasoning_effort == "high"
        assert route.temperature is None

    def test_explicit_reasoning_override_persists(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route(
            "coach",
            primary=ANTHROPIC_OPUS_MODEL,
            reasoning_effort="medium",
            path=path,
        )

        route = resolve_model_route("coach", path=path)
        assert route.reasoning_effort == "medium"

    def test_opus_verifier_defaults_to_high_reasoning(self, tmp_path):
        path = tmp_path / "models.json"
        set_feature_route("verification", primary=ANTHROPIC_OPUS_MODEL, path=path)

        route = resolve_model_route("verification", path=path)

        assert route.reasoning_effort == "high"
        assert route.temperature is None

    def test_verifier_defaults_to_opus_fallback_with_high_reasoning(self, tmp_path):
        route = resolve_model_route("verification", path=tmp_path / "models.json")

        assert route.primary == PRIMARY_PRO_MODEL
        assert route.fallback == ANTHROPIC_OPUS_MODEL
        assert route.reasoning_effort == "high"
        assert route.temperature is None

    def test_legacy_default_verifier_route_migrates_to_new_defaults(self, tmp_path):
        path = tmp_path / "models.json"
        path.write_text(
            json.dumps(
                {
                    "version": 3,
                    "features": {
                        "verification": {
                            "profile": "pro",
                            "primary": PRIMARY_PRO_MODEL,
                        }
                    },
                }
            )
        )

        route = resolve_model_route("verification", path=path)

        assert route.fallback == ANTHROPIC_OPUS_MODEL
        assert route.reasoning_effort == "high"
        assert route.temperature is None

    def test_profile_fallback_for_returns_profile_fallback(self, tmp_path):
        path = tmp_path / "models.json"
        assert profile_fallback_for("chat", path=path) == ANTHROPIC_HAIKU_MODEL
        assert profile_fallback_for("notify", path=path) is not None

    def test_selectable_models_include_configured_choices(self):
        models = selectable_models()

        assert PRIMARY_PRO_MODEL in models
        assert PRIMARY_FLASH_MODEL in models
        assert ANTHROPIC_OPUS_MODEL in models

    def test_model_button_label_includes_tier(self):
        label = model_button_label(ANTHROPIC_OPUS_MODEL)

        assert "opus-5" in label
        assert MODEL_TIERS[ANTHROPIC_OPUS_MODEL] in label


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
            primary=ANTHROPIC_OPUS_MODEL,
            reasoning_effort="medium",
            path=path,
        )

        findings = doctor_findings(path=path)

        assert any("reasoning" in finding for finding in findings)

    def test_flags_any_opus_temperature_set(self, tmp_path, monkeypatch):
        path = tmp_path / "models.json"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
        set_feature_route(
            "nudge",
            primary=ANTHROPIC_OPUS_MODEL,
            temperature=0.7,
            path=path,
        )

        findings = doctor_findings(path=path)

        assert any(
            "Nudges" in finding and "temperature" in finding for finding in findings
        )
