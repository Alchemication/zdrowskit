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
from concurrent.futures import ThreadPoolExecutor

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
            logger.debug("Failed to send typing indicator", exc_info=True)

    def start_typing_loop(self, stop: threading.Event) -> None:
        """Send 'typing...' every 4 seconds until *stop* is set.

        Telegram's typing indicator expires after ~5 seconds, so this
        keeps it alive during long LLM calls. Run in a daemon thread.

        Args:
            stop: Event that signals the loop to stop.
        """
        while not stop.is_set():
            self.send_typing()
            stop.wait(4)

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
            f"&allowed_updates=%5B%22message%22%2C%22callback_query%22%5D"  # ["message","callback_query"]
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
            logger.debug("Telegram polling error: %s", exc)
            return []

    def send_placeholder(self, reply_to_message_id: int | None = None) -> int | None:
        """Send a '...' placeholder message and return its message ID.

        The caller can later replace it with the real response via
        :meth:`edit_message`.

        Args:
            reply_to_message_id: Optional message ID to reply to.

        Returns:
            The message_id of the placeholder, or None on failure.
        """
        url = f"{self._base_url}/sendMessage"
        payload: dict = {
            "chat_id": self._chat_id,
            "text": "\u2026",  # ellipsis character
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("ok"):
                return body["result"]["message_id"]
        except Exception:
            logger.debug("Failed to send placeholder", exc_info=True)
        return None

    def edit_message(self, message_id: int, text: str) -> None:
        """Edit an existing message's text.

        If the text is too long for a single message, edits the first
        chunk into the placeholder and sends the rest as new messages.

        Args:
            message_id: ID of the message to edit.
            text: New text content.
        """
        chunks = chunk_text(text)
        url = f"{self._base_url}/editMessageText"
        payload: dict = {
            "chat_id": self._chat_id,
            "message_id": message_id,
            "text": chunks[0],
            "disable_web_page_preview": True,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req)  # noqa: S310
        except Exception:
            logger.warning("Failed to edit message %d", message_id, exc_info=True)
            return

        # Send remaining chunks as new messages.
        for chunk in chunks[1:]:
            self.send_reply(chunk)

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

    def send_message_with_keyboard(
        self,
        text: str,
        buttons: list[list[dict[str, str]]],
        reply_to_message_id: int | None = None,
    ) -> int | None:
        """Send a message with an inline keyboard.

        Args:
            text: Message text.
            buttons: Rows of inline keyboard buttons. Each button is a dict
                with ``"text"`` and ``"callback_data"`` keys.
            reply_to_message_id: Optional message ID to reply to.

        Returns:
            The message_id of the sent message, or None on failure.
        """
        url = f"{self._base_url}/sendMessage"
        payload: dict = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": buttons},
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("ok"):
                return body["result"]["message_id"]
        except Exception:
            logger.warning("Failed to send message with keyboard", exc_info=True)
        return None

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """Answer an inline keyboard callback to dismiss the loading indicator.

        Args:
            callback_query_id: The callback query ID from Telegram.
            text: Optional short text shown as a toast notification.
        """
        url = f"{self._base_url}/answerCallbackQuery"
        payload: dict = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req)  # noqa: S310
        except Exception:
            logger.debug("Failed to answer callback query", exc_info=True)

    def poll_loop(
        self,
        on_message: callable,
        stop_event: threading.Event,
        on_callback: callable | None = None,
    ) -> None:
        """Run the long-polling loop until *stop_event* is set.

        For each text message from the allowed chat, calls
        ``on_message(message_dict)``. For inline keyboard callbacks,
        calls ``on_callback(callback_query_dict)`` if provided.

        Handlers are dispatched to a thread pool so that long-running
        callbacks (e.g. LLM calls) never block the polling loop.

        Args:
            on_message: Callback receiving a Telegram message dict.
            stop_event: Event that signals the loop to stop.
            on_callback: Optional callback for inline keyboard button presses.
        """
        offset = 0
        logger.info("Telegram poller started (chat_id=%s)", self._chat_id)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="tg-handler") as pool:
            while not stop_event.is_set():
                updates = self.get_updates(offset)
                if not updates and stop_event.is_set():
                    break

                for update in updates:
                    offset = update["update_id"] + 1

                    # Handle inline keyboard callbacks.
                    cb = update.get("callback_query")
                    if cb and on_callback:
                        cb_chat_id = str(
                            cb.get("message", {}).get("chat", {}).get("id", "")
                        )
                        if cb_chat_id == self._chat_id:
                            pool.submit(self._safe_call, on_callback, cb)
                        continue

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

                    pool.submit(self._safe_call, on_message, msg)

                # On error (empty updates not from timeout), brief pause.
                if not updates and not stop_event.is_set():
                    stop_event.wait(_POLL_ERROR_RETRY_S)

    @staticmethod
    def _safe_call(fn: callable, *args: object) -> None:
        """Call *fn* and log any exception instead of crashing the pool."""
        try:
            fn(*args)
        except Exception:
            logger.error("Error in Telegram handler", exc_info=True)
