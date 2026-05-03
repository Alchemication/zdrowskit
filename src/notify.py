"""Notification delivery via Telegram.

Public API:
    md_to_telegram_html     — convert markdown to Telegram-compatible HTML.
    send_telegram           — send formatted report via Telegram Bot API.
    send_telegram_photo     — send a photo via Telegram Bot API.
    send_telegram_report    — send a sectioned report with interleaved charts.
    split_report_sections   — split a markdown report on ## headers.
    chunk_text              — split text into chunks respecting line boundaries.

Example:
    from notify import send_telegram
    send_telegram(report_text, "Week 2026-W11 Review")
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from html import escape as html_escape

logger = logging.getLogger(__name__)


def md_to_telegram_html(md_text: str) -> str:
    """Convert markdown to Telegram-compatible HTML.

    Telegram supports a limited HTML subset: <b>, <i>, <u>, <s>, <code>,
    <pre>, <a>, <blockquote>.  This function converts standard markdown
    into that subset so messages render with rich formatting.

    Args:
        md_text: Markdown text (from LLM output or report).

    Returns:
        HTML string safe for Telegram's ``parse_mode: "HTML"``.
    """
    lines = md_text.split("\n")
    result: list[str] = []
    in_code_block = False
    code_block_lines: list[str] = []
    code_lang = ""

    for line in lines:
        # --- fenced code blocks ---
        if re.match(r"^```", line):
            if not in_code_block:
                in_code_block = True
                code_lang = line.lstrip("`").strip()
                code_block_lines = []
            else:
                inner = html_escape("\n".join(code_block_lines))
                if code_lang:
                    result.append(
                        f'<pre><code class="language-{html_escape(code_lang)}">'
                        f"{inner}</code></pre>"
                    )
                else:
                    result.append(f"<pre>{inner}</pre>")
                in_code_block = False
                code_lang = ""
            continue

        if in_code_block:
            code_block_lines.append(line)
            continue

        # --- horizontal rule ---
        if re.match(r"^-{3,}\s*$", line) or re.match(r"^\*{3,}\s*$", line):
            result.append("───────")
            continue

        # --- headers → bold ---
        hdr = re.match(r"^(#{1,6})\s+(.*)", line)
        if hdr:
            text = _inline_format(hdr.group(2))
            result.append(f"<b>{text}</b>")
            continue

        # --- unordered list ---
        ul = re.match(r"^[\s]*[-*+]\s+(.*)", line)
        if ul:
            text = _inline_format(ul.group(1))
            result.append(f"  • {text}")
            continue

        # --- ordered list ---
        ol = re.match(r"^[\s]*(\d+)[.)]\s+(.*)", line)
        if ol:
            text = _inline_format(ol.group(2))
            result.append(f"  {ol.group(1)}. {text}")
            continue

        # --- blockquote ---
        bq = re.match(r"^>\s?(.*)", line)
        if bq:
            text = _inline_format(bq.group(1))
            result.append(f"<blockquote>{text}</blockquote>")
            continue

        # --- regular line ---
        result.append(_inline_format(line))

    # Handle unclosed code block gracefully.
    if in_code_block and code_block_lines:
        inner = html_escape("\n".join(code_block_lines))
        result.append(f"<pre>{inner}</pre>")

    return "\n".join(result)


def _inline_format(text: str) -> str:
    """Apply inline markdown formatting to a single line.

    Handles bold, italic, inline code, and links.  Text outside of
    formatting markers is HTML-escaped.

    Args:
        text: A single line of markdown text.

    Returns:
        The line with inline markdown converted to Telegram HTML.
    """
    # Protect inline code first — don't format inside backticks.
    parts: list[str] = []
    segments = re.split(r"(`[^`]+`)", text)
    for seg in segments:
        if seg.startswith("`") and seg.endswith("`"):
            parts.append(f"<code>{html_escape(seg[1:-1])}</code>")
        else:
            chunk = html_escape(seg)
            # Links: [text](url)
            chunk = re.sub(
                r"\[([^\]]+)\]\(([^)]+)\)",
                r'<a href="\2">\1</a>',
                chunk,
            )
            # Bold: **text** or __text__
            chunk = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", chunk)
            chunk = re.sub(r"__(.+?)__", r"<b>\1</b>", chunk)
            # Italic: *text* or _text_ (but not inside words for _)
            chunk = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"<i>\1</i>", chunk)
            chunk = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", chunk)
            parts.append(chunk)
    return "".join(parts)


def chunk_text(text: str, max_len: int = 4000) -> list[str]:
    """Split text into chunks respecting line boundaries.

    Telegram has a 4096 character limit per message. This helper splits
    on newline boundaries to stay under *max_len* per chunk.

    Args:
        text: The text to split.
        max_len: Maximum characters per chunk.

    Returns:
        A list of text chunks, each at most *max_len* characters.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    lines = text.split("\n")
    buf: list[str] = []
    buf_len = 0
    for line in lines:
        if buf and buf_len + len(line) + 1 > max_len:
            chunks.append("\n".join(buf))
            buf = []
            buf_len = 0
        buf.append(line)
        buf_len += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def _send_telegram_chunk(
    url: str,
    chat_id: str,
    text: str,
    html_text: str,
    reply_markup: dict | None = None,
) -> int | None:
    """Send a single Telegram message chunk, falling back to plain text.

    Tries HTML parse_mode first.  If Telegram rejects the markup (e.g.
    malformed tags), retries the same chunk as plain text so the message
    is never lost.

    Args:
        url: Telegram sendMessage API URL.
        chat_id: Target chat ID.
        text: Original plain-text version (fallback).
        html_text: HTML-formatted version (preferred).
        reply_markup: Optional Telegram reply markup (e.g. inline keyboard).

    Returns:
        The message_id of the sent message, or None on failure.
    """
    import urllib.error
    import urllib.request

    payload: dict = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("ok"):
            return body["result"]["message_id"]
        return None
    except urllib.error.HTTPError as e:
        logger.warning("HTML send failed (%s), retrying as plain text", e)
    except Exception as e:
        logger.error("Failed to send Telegram message: %s", e)
        return None

    # Fallback: plain text, no parse_mode.
    fallback_payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        fallback_payload["reply_markup"] = reply_markup
    data = json.dumps(fallback_payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("ok"):
            return body["result"]["message_id"]
        return None
    except Exception as e:
        logger.error("Fallback plain-text send also failed: %s", e)
        return None


def send_telegram(
    report: str,
    week_label: str,
    reply_markup: dict | None = None,
) -> int | None:
    """Send the report via Telegram Bot API with HTML formatting.

    Converts markdown to Telegram-compatible HTML.  Falls back to plain
    text if Telegram rejects the markup.

    Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in environment / .env.

    Args:
        report: The markdown report text.
        week_label: Human-readable week label for the message.
        reply_markup: Optional Telegram reply markup (e.g. inline keyboard)
            attached to the **last** chunk only.

    Returns:
        The message_id of the last sent chunk, or None on failure.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set. Add it to your .env file.")
        return None
    if not chat_id:
        logger.error("TELEGRAM_CHAT_ID not set. Add it to your .env file.")
        return None

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    html_report = md_to_telegram_html(report)

    plain_chunks = chunk_text(report)
    html_chunks = chunk_text(html_report)

    last_message_id: int | None = None
    for i, html in enumerate(html_chunks):
        plain = plain_chunks[i] if i < len(plain_chunks) else html
        is_last = i == len(html_chunks) - 1
        chunk_markup = reply_markup if is_last else None
        msg_id = _send_telegram_chunk(url, chat_id, plain, html, chunk_markup)
        if msg_id is None:
            logger.error("Aborting Telegram send at chunk %d", i + 1)
            return None
        last_message_id = msg_id

    logger.info("Telegram message sent to chat %s", chat_id)
    return last_message_id


def _get_telegram_creds() -> tuple[str, str] | None:
    """Return (bot_token, chat_id) or None with logged errors."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set. Add it to your .env file.")
        return None
    if not chat_id:
        logger.error("TELEGRAM_CHAT_ID not set. Add it to your .env file.")
        return None
    return bot_token, chat_id


def send_telegram_photo(
    image_bytes: bytes,
    caption: str = "",
    *,
    bot_token: str | None = None,
    chat_id: str | None = None,
) -> bool:
    """Send a photo via Telegram Bot API ``sendPhoto``.

    Uses multipart/form-data encoding. Caption is sent as HTML (max 1024
    chars) with a plain-text fallback.

    Args:
        image_bytes: PNG image data.
        caption: Optional caption text (markdown — will be converted to HTML).
        bot_token: Override bot token (defaults to env var).
        chat_id: Override chat ID (defaults to env var).

    Returns:
        True if sent successfully, False otherwise.
    """
    import urllib.request

    if bot_token is None or chat_id is None:
        creds = _get_telegram_creds()
        if creds is None:
            return False
        bot_token, chat_id = creds

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    boundary = "----zdrowskitBoundary"

    # Build multipart body.
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
    body += f"{chat_id}\r\n".encode()

    if caption:
        html_caption = md_to_telegram_html(caption)[:1024]
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="caption"\r\n\r\n'
        body += f"{html_caption}\r\n".encode()
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="parse_mode"\r\n\r\n'
        body += b"HTML\r\n"

    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="photo"; filename="chart.png"\r\n'
    body += b"Content-Type: image/png\r\n\r\n"
    body += image_bytes
    body += f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    try:
        urllib.request.urlopen(req)  # noqa: S310
        logger.info("Telegram photo sent")
        return True
    except Exception as e:
        logger.error("Failed to send Telegram photo: %s", e)
        return False


def split_report_sections(report_md: str) -> list[str]:
    """Split a markdown report on ``## `` headers into separate sections.

    Content before the first ``## `` header (e.g. the ``# `` title) becomes
    the first section.  Each section includes its ``## `` header.  Sections
    exceeding the Telegram 4096-char limit are further split via
    :func:`chunk_text`.

    Args:
        report_md: Markdown report text.

    Returns:
        A flat list of text chunks, each suitable for one Telegram message.
    """
    parts = re.split(r"\n(?=## )", report_md)
    chunks: list[str] = []
    for part in parts:
        stripped = part.strip()
        if stripped:
            chunks.extend(chunk_text(stripped))
    return chunks


def send_telegram_report(
    report: str,
    week_label: str,
    charts: list | None = None,
    reply_markup: dict | None = None,
) -> int | None:
    """Send a report with optional chart photos followed by the full text.

    Chart photos are sent first (one ``sendPhoto`` per chart), then the
    full report text as a single message (chunked only if it exceeds the
    Telegram 4096-char limit).

    Args:
        report: The markdown report text (chart blocks already stripped).
        week_label: Human-readable week label.
        charts: Optional list of :class:`~charts.ChartResult` instances.
        reply_markup: Optional Telegram reply markup attached to the last
            text chunk.

    Returns:
        The message_id of the last sent text chunk, or None on failure.
    """
    creds = _get_telegram_creds()
    if creds is None:
        return None

    bot_token, chat_id = creds

    # Send chart photos first.
    from charts import chart_figure_caption

    for index, chart in enumerate(charts or [], start=1):
        send_telegram_photo(
            chart.image_bytes,
            caption=chart_figure_caption(index, chart.title),
            bot_token=bot_token,
            chat_id=chat_id,
        )
        time.sleep(0.3)

    # Send the full report as a single text message (chunked if needed).
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    html_report = md_to_telegram_html(report)
    plain_chunks = chunk_text(report)
    html_chunks = chunk_text(html_report)

    last_message_id: int | None = None
    for i, html in enumerate(html_chunks):
        plain = plain_chunks[i] if i < len(plain_chunks) else html
        is_last = i == len(html_chunks) - 1
        chunk_markup = reply_markup if is_last else None
        msg_id = _send_telegram_chunk(url, chat_id, plain, html, chunk_markup)
        if msg_id is None:
            logger.error("Aborting Telegram report send at chunk %d", i + 1)
            return None
        last_message_id = msg_id

    logger.info("Telegram report sent to chat %s", chat_id)
    return last_message_id
