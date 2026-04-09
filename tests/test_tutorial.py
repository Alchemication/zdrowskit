"""Tests for src/tutorial.py — wizard rendering and navigation buttons."""

from __future__ import annotations

import pytest

from tutorial import TUTORIAL_STEPS, render_step


class TestRenderStep:
    def test_first_step_has_next_and_exit_only(self) -> None:
        text, buttons = render_step(0)
        assert len(buttons) == 1
        labels = [b["text"] for b in buttons[0]]
        assert "Next" in " ".join(labels)
        assert "Exit" in " ".join(labels)
        assert "Back" not in " ".join(labels)
        assert "Done" not in " ".join(labels)

    def test_middle_step_has_back_next_exit(self) -> None:
        mid = len(TUTORIAL_STEPS) // 2
        _text, buttons = render_step(mid)
        labels = [b["text"] for b in buttons[0]]
        joined = " ".join(labels)
        assert "Back" in joined
        assert "Next" in joined
        assert "Exit" in joined
        assert "Done" not in joined

    def test_last_step_has_back_and_done_only(self) -> None:
        last = len(TUTORIAL_STEPS) - 1
        _text, buttons = render_step(last)
        labels = [b["text"] for b in buttons[0]]
        joined = " ".join(labels)
        assert "Back" in joined
        assert "Done" in joined
        assert "Next" not in joined
        assert "Exit" not in joined

    def test_callback_data_encodes_destination_step(self) -> None:
        # From step 3, Back should go to 2 and Next to 4.
        _text, buttons = render_step(3)
        cbs = {b["text"]: b["callback_data"] for b in buttons[0]}
        back_label = next(k for k in cbs if "Back" in k)
        next_label = next(k for k in cbs if "Next" in k)
        assert cbs[back_label] == "tut:2"
        assert cbs[next_label] == "tut:4"

    def test_exit_callback_is_special_token(self) -> None:
        _text, buttons = render_step(0)
        exit_btn = next(b for b in buttons[0] if "Exit" in b["text"])
        assert exit_btn["callback_data"] == "tut:exit"

    def test_done_callback_is_special_token(self) -> None:
        _text, buttons = render_step(len(TUTORIAL_STEPS) - 1)
        done_btn = next(b for b in buttons[0] if "Done" in b["text"])
        assert done_btn["callback_data"] == "tut:done"

    def test_header_contains_progress_indicator(self) -> None:
        text, _buttons = render_step(2)
        # Step counter should appear in the header.
        assert f"Step 3 / {len(TUTORIAL_STEPS)}" in text

    def test_body_is_present_in_rendered_text(self) -> None:
        _emoji, _title, body = TUTORIAL_STEPS[1]
        text, _buttons = render_step(1)
        # The first non-trivial sentence of the body should appear verbatim.
        first_sentence = body.split(".")[0]
        assert first_sentence in text

    def test_invalid_index_raises(self) -> None:
        with pytest.raises(IndexError):
            render_step(-1)
        with pytest.raises(IndexError):
            render_step(len(TUTORIAL_STEPS))

    def test_all_steps_render_without_error(self) -> None:
        for idx in range(len(TUTORIAL_STEPS)):
            text, buttons = render_step(idx)
            assert text
            assert buttons
            # Telegram caps callback_data at 64 bytes.
            for row in buttons:
                for btn in row:
                    assert len(btn["callback_data"].encode("utf-8")) <= 64

    def test_step_bodies_fit_telegram_message_limit(self) -> None:
        # Sanity: keep each rendered step well under Telegram's 4096-char cap.
        for idx in range(len(TUTORIAL_STEPS)):
            text, _buttons = render_step(idx)
            assert len(text) < 4096


class TestTutorialContent:
    def test_step_count_is_stable(self) -> None:
        # If you change this, also update the "N short steps" copy in step 0.
        assert len(TUTORIAL_STEPS) == 8

    def test_each_step_has_emoji_title_body(self) -> None:
        for emoji, title, body in TUTORIAL_STEPS:
            assert emoji
            assert title
            assert body
