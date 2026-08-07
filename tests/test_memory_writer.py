"""Tests for the standalone weekly memory call."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from memory_writer import build_memory_messages, write_memory


def _messages(**overrides: str | None) -> list[dict[str, str]]:
    kwargs: dict[str, str | None] = {
        "report": "Two runs, both easy. HRV held.",
        "week_label": "2026-W31",
        "review_facts": "Runs: 2. Lifts: 0.",
        "log": "Knee felt fine Thursday.",
        "history": "## 2026-W30\n- Watching the knee",
    }
    kwargs.update(overrides)
    return build_memory_messages(**kwargs)  # type: ignore[arg-type]


class TestBuildMemoryMessages:
    def test_renders_a_single_user_message(self) -> None:
        """No soul and no system role: this call makes no judgement about the
        person, it decides what to keep from text already written."""
        messages = _messages()

        assert len(messages) == 1
        assert messages[0]["role"] == "user"

    def test_includes_the_report_and_existing_memory(self) -> None:
        content = _messages()[0]["content"]

        assert "Two runs, both easy. HRV held." in content
        assert "2026-W31" in content
        assert "Watching the knee" in content
        assert "Knee felt fine Thursday." in content

    def test_leaves_no_unrendered_placeholders(self) -> None:
        content = _messages()[0]["content"]

        assert "{report}" not in content
        assert "{history}" not in content

    def test_missing_context_renders_a_placeholder_not_none(self) -> None:
        """A fresh profile has no history and no notes. Rendering the literal
        string "None" into the prompt reads as a value."""
        content = _messages(history=None, log=None, review_facts=None)[0]["content"]

        assert "None" not in content
        assert "(nothing yet)" in content
        assert "(not provided)" in content


class TestWriteMemory:
    def _write(self, response_text: str) -> str | None:
        result = MagicMock()
        result.text = response_text
        with (
            patch("memory_writer.call_llm", return_value=result),
            patch("model_prefs.resolve_model_route") as route,
        ):
            route.return_value.call_kwargs.return_value = {"model": "test/model"}
            return write_memory(
                report="A report.",
                week_label="2026-W31",
                review_facts=None,
                log=None,
                history=None,
            )

    def test_extracts_the_block(self) -> None:
        assert self._write("<memory>\n- Tempo deferred twice\n</memory>") == (
            "- Tempo deferred twice"
        )

    def test_empty_block_returns_none(self) -> None:
        """An unremarkable week carries nothing forward, and the prompt asks
        for an empty block rather than an invented bullet."""
        assert self._write("<memory>\n</memory>") is None

    def test_missing_block_returns_none(self) -> None:
        assert self._write("Nothing worth keeping this week.") is None

    def test_llm_failure_does_not_raise(self) -> None:
        """The report is saved and sent before this runs. A provider fault must
        cost one week of continuity, not the report."""
        with (
            patch("memory_writer.call_llm", side_effect=RuntimeError("provider down")),
            patch("model_prefs.resolve_model_route") as route,
        ):
            route.return_value.call_kwargs.return_value = {"model": "test/model"}
            assert (
                write_memory(
                    report="A report.",
                    week_label="2026-W31",
                    review_facts=None,
                    log=None,
                    history=None,
                )
                is None
            )


class TestMemoryFeatureRouting:
    def test_memory_is_a_routable_flash_feature(self) -> None:
        from model_prefs import FEATURES, FLASH_FEATURES, resolve_model_route

        assert "memory" in FEATURES
        assert "memory" in FLASH_FEATURES

        route = resolve_model_route("memory")
        assert route.primary
        assert route.temperature is None

    @pytest.mark.parametrize("feature", ["memory"])
    def test_memory_appears_in_the_telegram_model_panel(self, feature: str) -> None:
        """A feature absent from every group is unreachable from /models."""
        from model_prefs import TELEGRAM_FEATURE_GROUPS

        grouped = {f for features in TELEGRAM_FEATURE_GROUPS.values() for f in features}
        assert feature in grouped
