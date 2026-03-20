"""Tests for src/telegram_bot.py and src/notify.chunk_text."""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

from notify import chunk_text
from telegram_bot import ConversationBuffer, TelegramPoller


# ---------------------------------------------------------------------------
# chunk_text (moved to notify.py, used by both notify and telegram_bot)
# ---------------------------------------------------------------------------


class TestChunkText:
    def test_short_text_single_chunk(self) -> None:
        assert chunk_text("hello") == ["hello"]

    def test_empty_text(self) -> None:
        assert chunk_text("") == [""]

    def test_exact_boundary(self) -> None:
        text = "a" * 4000
        assert chunk_text(text) == [text]

    def test_splits_long_text(self) -> None:
        # 10 lines of 500 chars each = 5000 chars total
        lines = ["x" * 500 for _ in range(10)]
        text = "\n".join(lines)
        chunks = chunk_text(text, max_len=2000)
        assert len(chunks) > 1
        # Reconstructed text matches (modulo split points)
        assert "\n".join(chunks) == text

    def test_respects_line_boundaries(self) -> None:
        lines = ["line1", "line2", "line3"]
        text = "\n".join(lines)
        chunks = chunk_text(text, max_len=11)
        # Each chunk should contain complete lines
        for chunk in chunks:
            assert not chunk.startswith("\n")

    def test_custom_max_len(self) -> None:
        text = "a" * 100
        chunks = chunk_text(text, max_len=50)
        # Single line longer than max_len — can't split further
        assert chunks == [text]


# ---------------------------------------------------------------------------
# ConversationBuffer
# ---------------------------------------------------------------------------


class TestConversationBuffer:
    def test_add_and_retrieve(self) -> None:
        buf = ConversationBuffer(max_messages=5)
        buf.add("user", "hello")
        buf.add("assistant", "hi there")
        msgs = buf.to_messages()
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "hello"}
        assert msgs[1] == {"role": "assistant", "content": "hi there"}

    def test_eviction_at_capacity(self) -> None:
        buf = ConversationBuffer(max_messages=3)
        buf.add("user", "msg1")
        buf.add("assistant", "msg2")
        buf.add("user", "msg3")
        buf.add("assistant", "msg4")
        msgs = buf.to_messages()
        assert len(msgs) == 3
        # Oldest message should have been evicted
        assert msgs[0]["content"] == "msg2"
        assert msgs[2]["content"] == "msg4"

    def test_empty_buffer(self) -> None:
        buf = ConversationBuffer()
        assert buf.to_messages() == []
        assert len(buf) == 0

    def test_len(self) -> None:
        buf = ConversationBuffer(max_messages=5)
        buf.add("user", "a")
        buf.add("assistant", "b")
        assert len(buf) == 2

    def test_thread_safety(self) -> None:
        """Concurrent adds should not raise or corrupt data."""
        buf = ConversationBuffer(max_messages=100)
        errors: list[Exception] = []

        def add_messages(start: int) -> None:
            try:
                for i in range(50):
                    buf.add("user", f"msg-{start}-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=add_messages, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(buf) == 100  # 4 threads * 50 messages, capped at 100


# ---------------------------------------------------------------------------
# TelegramPoller
# ---------------------------------------------------------------------------


class TestTelegramPollerGetUpdates:
    def test_parses_successful_response(self) -> None:
        poller = TelegramPoller("fake-token", "12345")
        fake_response = json.dumps(
            {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 10,
                            "chat": {"id": 12345},
                            "text": "hello",
                        },
                    }
                ],
            }
        ).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_response
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("telegram_bot.urllib.request.urlopen", return_value=mock_resp):
            updates = poller.get_updates(offset=0)

        assert len(updates) == 1
        assert updates[0]["message"]["text"] == "hello"

    def test_returns_empty_on_error(self) -> None:
        poller = TelegramPoller("fake-token", "12345")
        with patch(
            "telegram_bot.urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            updates = poller.get_updates(offset=0)

        assert updates == []


class TestTelegramPollerSendReply:
    def test_sends_single_chunk(self) -> None:
        poller = TelegramPoller("fake-token", "12345")
        with patch("telegram_bot.urllib.request.urlopen") as mock_urlopen:
            poller.send_reply("short message", reply_to_message_id=42)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data)
        assert payload["text"] == "short message"
        assert payload["chat_id"] == "12345"
        assert payload["reply_to_message_id"] == 42

    def test_chunks_long_message(self) -> None:
        poller = TelegramPoller("fake-token", "12345")
        # Create a message that will be split into multiple chunks
        long_text = "\n".join(["x" * 500 for _ in range(10)])
        with patch("telegram_bot.urllib.request.urlopen") as mock_urlopen:
            poller.send_reply(long_text, reply_to_message_id=1)

        assert mock_urlopen.call_count > 1
        # Only first chunk should have reply_to_message_id
        first_payload = json.loads(mock_urlopen.call_args_list[0][0][0].data)
        assert "reply_to_message_id" in first_payload
        second_payload = json.loads(mock_urlopen.call_args_list[1][0][0].data)
        assert "reply_to_message_id" not in second_payload


class TestTelegramPollerPollLoop:
    def test_filters_by_chat_id(self) -> None:
        """Messages from other chats should be ignored."""
        poller = TelegramPoller("fake-token", "12345")
        callback = MagicMock()
        stop = threading.Event()

        updates = [
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "chat": {"id": 99999},  # Wrong chat
                    "text": "sneaky",
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 11,
                    "chat": {"id": 12345},  # Correct chat
                    "text": "hello",
                },
            },
        ]

        call_count = 0

        def fake_get_updates(offset, timeout=30):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return updates
            stop.set()
            return []

        poller.get_updates = fake_get_updates
        poller.poll_loop(callback, stop)

        # Only the message from the correct chat should be passed through
        callback.assert_called_once()
        assert callback.call_args[0][0]["text"] == "hello"

    def test_skips_non_text_messages(self) -> None:
        """Messages without text (e.g. photos) should be skipped."""
        poller = TelegramPoller("fake-token", "12345")
        callback = MagicMock()
        stop = threading.Event()

        updates = [
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "chat": {"id": 12345},
                    # No "text" field — e.g. a photo
                },
            },
        ]

        call_count = 0

        def fake_get_updates(offset, timeout=30):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return updates
            stop.set()
            return []

        poller.get_updates = fake_get_updates
        poller.poll_loop(callback, stop)

        callback.assert_not_called()
