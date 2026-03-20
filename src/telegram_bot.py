"""Telegram Bot API client for interactive chat.

Long-polling listener and conversation buffer for two-way coaching
conversations via Telegram.

Public API:
    TelegramPoller      — long-polling client that receives and sends messages.
    ConversationBuffer  — fixed-size buffer of recent chat messages.

Example:
    poller = TelegramPoller(bot_token="...", chat_id="123")
    poller.poll_loop(on_message=handle, stop_event=threading.Event())
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from collections import deque

from config import MAX_CONVERSATION_MESSAGES
from notify import chunk_text

logger = logging.getLogger(__name__)

# Retry delay after a polling error before trying again.
_POLL_ERROR_RETRY_S = 5


class ConversationBuffer:
    """Fixed-size buffer of recent chat messages.

    Thread-safe: all access is guarded by an internal lock.

    Args:
        max_messages: Maximum number of messages to retain.
    """

    def __init__(self, max_messages: int = MAX_CONVERSATION_MESSAGES) -> None:
        self._max = max_messages
        self._messages: deque[dict[str, str]] = deque(maxlen=max_messages)
        self._lock = threading.Lock()

    def add(self, role: str, content: str) -> None:
        """Append a message, evicting the oldest if at capacity.

        Args:
            role: Message role — ``"user"`` or ``"assistant"``.
            content: Message text.
        """
        with self._lock:
            self._messages.append({"role": role, "content": content})

    def to_messages(self) -> list[dict[str, str]]:
        """Return the buffered messages in litellm format.

        Returns:
            A list of ``{"role": ..., "content": ...}`` dicts, oldest first.
        """
        with self._lock:
            return list(self._messages)

    def clear(self) -> None:
        """Remove all messages from the buffer."""
        with self._lock:
            self._messages.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)


class TelegramPoller:
    """Long-polling client for the Telegram Bot API.

    Only processes text messages from the configured *chat_id*; all
    other updates are silently acknowledged and skipped.

    Args:
        bot_token: Telegram Bot API token.
        chat_id: Allowed chat ID (string). Messages from other chats are ignored.
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._base_url = f"https://api.telegram.org/bot{bot_token}"

    def send_typing(self) -> None:
        """Send a 'typing...' chat action indicator."""
        url = f"{self._base_url}/sendChatAction"
        data = json.dumps({"chat_id": self._chat_id, "action": "typing"}).encode(
            "utf-8"
        )
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req)  # noqa: S310
        except Exception:
            pass  # Best-effort, not worth logging

    def get_updates(self, offset: int, timeout: int = 30) -> list[dict]:
        """Fetch new updates via long polling.

        Args:
            offset: Update offset — only updates with ID >= offset are returned.
            timeout: Long-polling timeout in seconds.

        Returns:
            A list of update dicts from the Telegram API, or an empty list
            on error.
        """
        url = (
            f"{self._base_url}/getUpdates"
            f"?offset={offset}&timeout={timeout}"
            f"&allowed_updates=%5B%22message%22%5D"  # ["message"]
        )
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout + 10) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return data.get("result", [])
            logger.warning("Telegram getUpdates not ok: %s", data)
            return []
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("Telegram polling error: %s", exc)
            return []

    def send_reply(self, text: str, reply_to_message_id: int | None = None) -> None:
        """Send a text message, chunking if necessary.

        Args:
            text: Message text to send.
            reply_to_message_id: Optional message ID to reply to.
        """
        url = f"{self._base_url}/sendMessage"
        for i, chunk in enumerate(chunk_text(text)):
            payload: dict = {
                "chat_id": self._chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            # Only set reply on the first chunk.
            if i == 0 and reply_to_message_id is not None:
                payload["reply_to_message_id"] = reply_to_message_id

            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}
            )
            try:
                urllib.request.urlopen(req)  # noqa: S310
            except Exception:
                logger.error(
                    "Failed to send Telegram reply (chunk %d)", i + 1, exc_info=True
                )
                return

    def poll_loop(
        self,
        on_message: callable,
        stop_event: threading.Event,
    ) -> None:
        """Run the long-polling loop until *stop_event* is set.

        For each text message from the allowed chat, calls
        ``on_message(message_dict)``.

        Args:
            on_message: Callback receiving a Telegram message dict.
            stop_event: Event that signals the loop to stop.
        """
        offset = 0
        logger.info("Telegram poller started (chat_id=%s)", self._chat_id)

        while not stop_event.is_set():
            updates = self.get_updates(offset)
            if not updates and stop_event.is_set():
                break

            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg:
                    continue

                # Security: only process messages from the configured chat.
                msg_chat_id = str(msg.get("chat", {}).get("id", ""))
                if msg_chat_id != self._chat_id:
                    logger.debug(
                        "Ignoring message from chat %s (expected %s)",
                        msg_chat_id,
                        self._chat_id,
                    )
                    continue

                if not msg.get("text"):
                    continue

                try:
                    on_message(msg)
                except Exception:
                    logger.error("Error handling Telegram message", exc_info=True)

            # On error (empty updates not from timeout), brief pause.
            if not updates and not stop_event.is_set():
                stop_event.wait(_POLL_ERROR_RETRY_S)
