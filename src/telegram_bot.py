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
from notify import chunk_text, md_to_telegram_html, send_telegram_photo

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


# ---------------------------------------------------------------------------
# Feedback inline keyboards
# ---------------------------------------------------------------------------

FEEDBACK_CATEGORIES: dict[str, str] = {
    "inaccurate": "Inaccurate",
    "not_useful": "Not useful",
    "too_verbose": "Too verbose",
    "wrong_tone": "Wrong tone",
    "other": "Other",
}


def feedback_keyboard(
    llm_call_id: int,
    message_type: str,
) -> list[list[dict[str, str]]]:
    """Single 👎 button for an LLM response message."""
    return [
        [
            {
                "text": "\U0001f44e",
                "callback_data": f"fb_neg:{llm_call_id}:{message_type}",
            }
        ]
    ]


def feedback_category_keyboard(
    llm_call_id: int,
    message_type: str,
) -> list[list[dict[str, str]]]:
    """Category picker shown after the user taps 👎.

    Laid out two-per-row with a trailing single-button row if the number of
    categories is odd.
    """
    buttons = [
        {
            "text": label,
            "callback_data": f"fb_cat:{llm_call_id}:{message_type}:{key}",
        }
        for key, label in FEEDBACK_CATEGORIES.items()
    ]
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def feedback_undo_keyboard(
    feedback_id: int,
    llm_call_id: int,
    message_type: str,
    category: str,
) -> list[list[dict[str, str]]]:
    """Single Undo button shown after feedback is recorded."""
    return [
        [
            {
                "text": "Undo",
                "callback_data": (
                    f"fb_undo:{feedback_id}:{llm_call_id}:{message_type}:{category}"
                ),
            }
        ]
    ]


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

    def animate_message(
        self,
        message_id: int,
        stop: threading.Event,
        *,
        prefix: str = "",
        frames: tuple[str, ...] = (".", "..", "..."),
        interval: float = 0.9,
    ) -> None:
        """Animate a message by cycling frames after an optional prefix.

        Edits the message every *interval* seconds, cycling through the
        given *frames*. Stops when *stop* is set. Designed to run in a
        daemon thread alongside a long-running task so the user sees the
        placeholder is alive instead of a static character.

        Args:
            message_id: ID of the message to animate.
            stop: Event that signals the loop to stop.
            prefix: Optional text shown before the animated frame.
            frames: Sequence of strings to cycle through. Defaults to
                ``.``, ``..``, ``...``.
            interval: Seconds between frames. Kept ≥ ~0.7s to respect
                Telegram's per-chat edit rate limits.
        """
        url = f"{self._base_url}/editMessageText"
        i = 0
        while not stop.is_set():
            text = f"{prefix}{frames[i % len(frames)]}"
            payload = {
                "chat_id": self._chat_id,
                "message_id": message_id,
                "text": text,
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}
            )
            try:
                urllib.request.urlopen(req)  # noqa: S310
            except Exception:
                logger.debug("Animation edit failed", exc_info=True)
            i += 1
            stop.wait(interval)

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

    def edit_message(self, message_id: int, text: str) -> None:
        """Edit an existing message's text with HTML formatting.

        If the text is too long for a single message, edits the first
        chunk into the placeholder and sends the rest as new messages.
        Falls back to plain text if Telegram rejects the HTML.

        Args:
            message_id: ID of the message to edit.
            text: New text content (markdown).
        """
        html_text = md_to_telegram_html(text)
        html_chunks = chunk_text(html_text)
        plain_chunks = chunk_text(text)
        url = f"{self._base_url}/editMessageText"
        payload: dict = {
            "chat_id": self._chat_id,
            "message_id": message_id,
            "text": html_chunks[0],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req)  # noqa: S310
        except urllib.error.HTTPError:
            logger.warning(
                "HTML edit failed for message %d, retrying plain text", message_id
            )
            payload["text"] = plain_chunks[0]
            del payload["parse_mode"]
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}
            )
            try:
                urllib.request.urlopen(req)  # noqa: S310
            except Exception:
                logger.warning("Failed to edit message %d", message_id, exc_info=True)
                return
        except Exception:
            logger.warning("Failed to edit message %d", message_id, exc_info=True)
            return

        # Send remaining chunks as new messages (already HTML-converted).
        for chunk in html_chunks[1:]:
            self.send_reply(chunk, _pre_converted=True)

    def edit_message_with_keyboard(
        self,
        message_id: int,
        text: str,
        buttons: list[list[dict[str, str]]],
    ) -> None:
        """Edit an existing message's text and inline keyboard.

        Args:
            message_id: ID of the message to edit.
            text: New text content (markdown).
            buttons: Rows of inline keyboard buttons.
        """
        html_text = md_to_telegram_html(text)
        url = f"{self._base_url}/editMessageText"
        payload: dict = {
            "chat_id": self._chat_id,
            "message_id": message_id,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": buttons},
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req)  # noqa: S310
        except urllib.error.HTTPError:
            logger.warning(
                "HTML edit failed for message %d, retrying plain text", message_id
            )
            payload["text"] = text
            del payload["parse_mode"]
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}
            )
            try:
                urllib.request.urlopen(req)  # noqa: S310
            except Exception:
                logger.warning("Failed to edit message %d", message_id, exc_info=True)
        except Exception:
            logger.warning("Failed to edit message %d", message_id, exc_info=True)

    def edit_message_reply_markup(
        self,
        message_id: int,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        """Edit only the inline keyboard on an existing message.

        Uses Telegram's ``editMessageReplyMarkup`` so the message text
        is left untouched — safe for chunked messages.

        Args:
            message_id: ID of the message to edit.
            buttons: New inline keyboard rows, or None to remove the keyboard.
        """
        url = f"{self._base_url}/editMessageReplyMarkup"
        payload: dict = {
            "chat_id": self._chat_id,
            "message_id": message_id,
        }
        if buttons is not None:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        else:
            payload["reply_markup"] = {"inline_keyboard": []}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req)  # noqa: S310
        except Exception:
            logger.warning(
                "Failed to edit reply markup for message %d",
                message_id,
                exc_info=True,
            )

    def send_reply(
        self,
        text: str,
        reply_to_message_id: int | None = None,
        *,
        _pre_converted: bool = False,
        force_reply: bool = False,
    ) -> int | None:
        """Send a text message with HTML formatting, chunking if necessary.

        Falls back to plain text if Telegram rejects the HTML.

        Args:
            text: Message text to send (markdown, or HTML if *_pre_converted*).
            reply_to_message_id: Optional message ID to reply to.
            _pre_converted: If True, *text* is already Telegram HTML — skip
                conversion.  Used internally by :meth:`edit_message`.
            force_reply: When True, attach Telegram's ForceReply markup.

        Returns:
            The message_id of the first sent chunk, or None on failure.
        """
        if _pre_converted:
            html_text = text
        else:
            html_text = md_to_telegram_html(text)
        url = f"{self._base_url}/sendMessage"
        html_chunks = chunk_text(html_text)
        plain_chunks = chunk_text(text)
        first_message_id: int | None = None

        for i, html_chunk in enumerate(html_chunks):
            plain_chunk = plain_chunks[i] if i < len(plain_chunks) else html_chunk
            payload: dict = {
                "chat_id": self._chat_id,
                "text": html_chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            # Only set reply on the first chunk.
            if i == 0 and reply_to_message_id is not None:
                payload["reply_to_message_id"] = reply_to_message_id
            if i == 0 and force_reply:
                payload["reply_markup"] = {"force_reply": True, "selective": True}

            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req) as resp:  # noqa: S310
                    body = json.loads(resp.read().decode("utf-8"))
                if body.get("ok") and first_message_id is None:
                    first_message_id = body["result"]["message_id"]
            except urllib.error.HTTPError:
                logger.warning("HTML reply failed (chunk %d), retrying plain", i + 1)
                payload["text"] = plain_chunk
                del payload["parse_mode"]
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url, data=data, headers={"Content-Type": "application/json"}
                )
                try:
                    with urllib.request.urlopen(req) as resp:  # noqa: S310
                        body = json.loads(resp.read().decode("utf-8"))
                    if body.get("ok") and first_message_id is None:
                        first_message_id = body["result"]["message_id"]
                except Exception:
                    logger.error(
                        "Failed to send Telegram reply (chunk %d)",
                        i + 1,
                        exc_info=True,
                    )
                    return None
            except Exception:
                logger.error(
                    "Failed to send Telegram reply (chunk %d)", i + 1, exc_info=True
                )
                return None

        return first_message_id

    def send_photo(self, image_bytes: bytes, caption: str = "") -> bool:
        """Send a photo to the configured chat.

        Delegates to :func:`notify.send_telegram_photo` using this poller's
        credentials.

        Args:
            image_bytes: PNG image data.
            caption: Optional markdown caption.

        Returns:
            True if sent successfully, False otherwise.
        """
        return send_telegram_photo(
            image_bytes,
            caption,
            bot_token=self._token,
            chat_id=self._chat_id,
        )

    def send_message_with_keyboard(
        self,
        text: str,
        buttons: list[list[dict[str, str]]],
        reply_to_message_id: int | None = None,
    ) -> int | None:
        """Send a message with an inline keyboard and HTML formatting.

        Long text is automatically chunked: leading chunks are sent as
        plain replies via :meth:`send_reply` and only the **final** chunk
        carries the inline keyboard. This keeps the keyboard at the bottom
        of the conversation where the user expects it. Returns the
        message_id of the chunk that holds the keyboard so callers can
        attach a feedback button via :meth:`edit_message_reply_markup`.

        Falls back to plain text if Telegram rejects the HTML.

        Args:
            text: Message text (markdown).
            buttons: Rows of inline keyboard buttons. Each button is a dict
                with ``"text"`` and ``"callback_data"`` keys.
            reply_to_message_id: Optional message ID to reply to (applied
                to the first chunk only).

        Returns:
            The message_id of the chunk holding the keyboard, or None on
            failure.
        """
        html_text = md_to_telegram_html(text)
        html_chunks = chunk_text(html_text)
        plain_chunks = chunk_text(text)

        # Send all but the last chunk as plain replies. send_reply already
        # handles HTML/plain fallback and chunk reflow internally, so we hand
        # it the original markdown for chunks 0..n-2 and let it convert.
        if len(plain_chunks) > 1:
            leading_plain = "\n\n".join(plain_chunks[:-1])
            self.send_reply(leading_plain, reply_to_message_id=reply_to_message_id)
            reply_to_message_id = None  # Only attach reply to the first chunk.

        final_html = html_chunks[-1] if html_chunks else html_text
        final_plain = plain_chunks[-1] if plain_chunks else text

        url = f"{self._base_url}/sendMessage"
        payload: dict = {
            "chat_id": self._chat_id,
            "text": final_html,
            "parse_mode": "HTML",
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
        except urllib.error.HTTPError:
            logger.warning("HTML keyboard message failed, retrying plain text")
            payload["text"] = final_plain
            del payload["parse_mode"]
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

    def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
        """Register bot commands for Telegram's autocomplete menu.

        Calls the ``setMyCommands`` API so users see command suggestions
        when typing ``/`` and via the menu button next to the text field.

        Args:
            commands: List of ``{"command": "...", "description": "..."}`` dicts.

        Returns:
            True if the API call succeeded, False otherwise.
        """
        url = f"{self._base_url}/setMyCommands"
        payload = {"commands": commands}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8"))
            return body.get("ok", False)
        except Exception:
            logger.error("Failed to set bot commands", exc_info=True)
            return False

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
