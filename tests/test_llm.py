"""Tests for pure functions in src/llm.py."""

from __future__ import annotations

from llm import _recent_history, extract_memory


class TestExtractMemory:
    def test_present(self) -> None:
        text = "Some report text.\n<memory>Key insight about training.</memory>\nMore text."
        assert extract_memory(text) == "Key insight about training."

    def test_absent(self) -> None:
        assert extract_memory("No memory block here.") is None

    def test_multiline(self) -> None:
        text = "<memory>\nLine 1\nLine 2\nLine 3\n</memory>"
        result = extract_memory(text)
        assert "Line 1" in result
        assert "Line 3" in result

    def test_strips_whitespace(self) -> None:
        text = "<memory>  padded content  </memory>"
        assert extract_memory(text) == "padded content"


class TestRecentHistory:
    def test_trims_to_n(self) -> None:
        entries = "\n\n".join(f"## 2026-03-{i:02d}\n\nEntry {i}" for i in range(1, 11))
        result = _recent_history(entries, 3)
        assert "## 2026-03-10" in result
        assert "## 2026-03-09" in result
        assert "## 2026-03-08" in result
        assert "## 2026-03-01" not in result

    def test_fewer_than_n_returns_original(self) -> None:
        content = "## 2026-03-01\n\nEntry 1\n\n## 2026-03-02\n\nEntry 2"
        result = _recent_history(content, 5)
        assert result == content

    def test_empty_string(self) -> None:
        assert _recent_history("", 5) == ""
