"""Tests for src/telegram_bot.py and src/notify.chunk_text."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
from unittest.mock import MagicMock, patch

from notify import chunk_text
from telegram_bot import ConversationBuffer, TelegramPoller, TelegramSender


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


def _telegram_http_error(description: str) -> urllib.error.HTTPError:
    body = json.dumps(
        {"ok": False, "error_code": 400, "description": description}
    ).encode()
    return urllib.error.HTTPError(
        "https://api.telegram.org/botfake/editMessageText",
        400,
        "Bad Request",
        {},
        io.BytesIO(body),
    )


class TestTelegramPollerGetUpdates:
    def test_parses_successful_response(self) -> None:
        poller = TelegramPoller("fake-token")
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
        status = poller.status()
        assert status["last_poll_update_count"] == 1
        assert status["last_poll_at"] is not None

    def test_returns_empty_on_error(self) -> None:
        poller = TelegramPoller("fake-token")
        with patch(
            "telegram_bot.urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            updates = poller.get_updates(offset=0)

        assert updates == []

    def test_returns_empty_on_malformed_body(self) -> None:
        """A non-JSON gateway page must not raise out of get_updates."""
        poller = TelegramPoller("fake-token")
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"<html>502 Bad Gateway</html>"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("telegram_bot.urllib.request.urlopen", return_value=mock_resp):
            updates = poller.get_updates(offset=0)

        assert updates == []

    def test_returns_empty_on_409_conflict(self) -> None:
        """A 409 (another poller) is swallowed, not raised."""
        poller = TelegramPoller("fake-token")
        err = urllib.error.HTTPError(
            url="x", code=409, msg="Conflict", hdrs=None, fp=io.BytesIO(b"{}")
        )
        with patch("telegram_bot.urllib.request.urlopen", side_effect=err):
            updates = poller.get_updates(offset=0)

        assert updates == []


class TestTelegramPollerSendReply:
    def test_sends_single_chunk(self) -> None:
        poller = TelegramSender("fake-token", "12345")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {
                "ok": True,
                "result": {"message_id": 99},
            }
        ).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch(
            "telegram_bot.urllib.request.urlopen", return_value=mock_resp
        ) as mock_urlopen:
            result = poller.send_reply("short message", reply_to_message_id=42)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data)
        assert payload["text"] == "short message"
        assert payload["chat_id"] == "12345"
        assert payload["reply_to_message_id"] == 42
        assert result == 99

    def test_chunks_long_message(self) -> None:
        poller = TelegramSender("fake-token", "12345")
        # Create a message that will be split into multiple chunks
        long_text = "\n".join(["x" * 500 for _ in range(10)])
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {
                "ok": True,
                "result": {"message_id": 77},
            }
        ).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch(
            "telegram_bot.urllib.request.urlopen", return_value=mock_resp
        ) as mock_urlopen:
            result = poller.send_reply(long_text, reply_to_message_id=1)

        assert mock_urlopen.call_count > 1
        # Only first chunk should have reply_to_message_id
        first_payload = json.loads(mock_urlopen.call_args_list[0][0][0].data)
        assert "reply_to_message_id" in first_payload
        second_payload = json.loads(mock_urlopen.call_args_list[1][0][0].data)
        assert "reply_to_message_id" not in second_payload
        assert result == 77

    def test_force_reply_adds_reply_markup(self) -> None:
        poller = TelegramSender("fake-token", "12345")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {
                "ok": True,
                "result": {"message_id": 55},
            }
        ).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "telegram_bot.urllib.request.urlopen", return_value=mock_resp
        ) as mock_urlopen:
            poller.send_reply("Why rejected?", force_reply=True)

        payload = json.loads(mock_urlopen.call_args[0][0].data)
        assert payload["reply_markup"] == {"force_reply": True, "selective": True}


class TestTelegramPollerEditMessageWithKeyboard:
    def test_retries_plain_text_after_html_error(self) -> None:
        poller = TelegramSender("fake-token", "12345")

        with patch(
            "telegram_bot.urllib.request.urlopen",
            side_effect=[
                _telegram_http_error("Bad Request: can't parse entities"),
                MagicMock(),
            ],
        ) as mock_urlopen:
            poller.edit_message_with_keyboard(
                99,
                "**done**",
                [[{"text": "OK", "callback_data": "ok"}]],
            )

        assert mock_urlopen.call_count == 2
        html_payload = json.loads(mock_urlopen.call_args_list[0][0][0].data)
        plain_payload = json.loads(mock_urlopen.call_args_list[1][0][0].data)
        assert html_payload["parse_mode"] == "HTML"
        assert plain_payload["text"] == "**done**"
        assert "parse_mode" not in plain_payload

    def test_suppresses_redundant_edit_without_plain_retry(self) -> None:
        poller = TelegramSender("fake-token", "12345")

        with patch(
            "telegram_bot.urllib.request.urlopen",
            side_effect=_telegram_http_error(
                "Bad Request: message is not modified: specified new message "
                "content and reply markup are exactly the same"
            ),
        ) as mock_urlopen:
            poller.edit_message_with_keyboard(
                99,
                "Codex: on",
                [[{"text": "Turn off", "callback_data": "agent:off:codex"}]],
            )

        mock_urlopen.assert_called_once()


class TestTelegramPollerOutboundTimeouts:
    def test_callback_answer_uses_short_timeout(self) -> None:
        poller = TelegramSender("fake-token", "12345")

        with patch("telegram_bot.urllib.request.urlopen") as mock_urlopen:
            poller.answer_callback_query("callback-1")

        assert mock_urlopen.call_args.kwargs["timeout"] > 0


class TestTelegramPollerPollLoop:
    def test_process_poller_dispatches_raw_updates_without_chat_filter(self) -> None:
        poller = TelegramPoller("fake-token")
        callback = MagicMock()
        stop = threading.Event()
        updates = [
            {
                "update_id": 1,
                "message": {
                    "from": {"id": 999},
                    "chat": {"id": 999, "type": "private"},
                    "text": "route me later",
                },
            }
        ]
        calls = 0

        def fake_get_updates(offset, timeout=30):
            nonlocal calls
            calls += 1
            if calls == 1:
                return updates
            stop.set()
            return []

        poller.get_updates = fake_get_updates
        poller.poll_loop(callback, stop)

        callback.assert_called_once_with(updates[0])
