"""Notification delivery — email and Telegram.

Public API:
    md_to_html              — convert markdown to a styled HTML document.
    md_to_telegram_html     — convert markdown to Telegram-compatible HTML.
    send_email              — send HTML report via Resend API.
    send_telegram           — send formatted report via Telegram Bot API.
    chunk_text              — split text into chunks respecting line boundaries.

Example:
    from notify import send_email, send_telegram
    send_email(report_text, "Week 2026-W11 Review")
"""

from __future__ import annotations

import json
import logging
import os
import re
from html import escape as html_escape

logger = logging.getLogger(__name__)


def md_to_html(md_text: str) -> str:
    """Convert markdown to a styled HTML document for email.

    Args:
        md_text: Markdown report text.

    Returns:
        A complete HTML document string with inline styling.
    """
    import markdown

    body = markdown.markdown(md_text, extensions=["tables", "fenced_code"])
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
             max-width: 640px; margin: 0 auto; padding: 20px;
             color: #1a1a1a; line-height: 1.6;">
<style>
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
  th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
  th {{ background: #f5f5f5; font-weight: 600; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  h1 {{ font-size: 1.4em; border-bottom: 2px solid #333; padding-bottom: 0.3em; }}
  h2 {{ font-size: 1.15em; color: #333; margin-top: 1.5em; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 1.5em 0; }}
  code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
</style>
{body}
</body>
</html>"""


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


def send_email(report: str, week_label: str) -> None:
    """Send the report as a styled HTML email via Resend.

    Requires RESEND_API_KEY and EMAIL_TO in environment / .env.

    Args:
        report: The markdown report text.
        week_label: Human-readable week label for the subject line.
    """
    import resend

    api_key = os.environ.get("RESEND_API_KEY")
    email_to = os.environ.get("EMAIL_TO")
    email_from = os.environ.get("EMAIL_FROM", "zdrowskit <onboarding@resend.dev>")

    if not api_key:
        logger.error("RESEND_API_KEY not set. Add it to your .env file.")
        return
    if not email_to:
        logger.error("EMAIL_TO not set. Add it to your .env file.")
        return

    resend.api_key = api_key
    html = md_to_html(report)
    try:
        resend.Emails.send(
            {
                "from": email_from,
                "to": [email_to],
                "subject": f"zdrowskit — {week_label}",
                "html": html,
            }
        )
        logger.info("Email sent to %s", email_to)
    except Exception as e:
        logger.error("Failed to send email: %s", e)


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


def _send_telegram_chunk(url: str, chat_id: str, text: str, html_text: str) -> bool:
    """Send a single Telegram message chunk, falling back to plain text.

    Tries HTML parse_mode first.  If Telegram rejects the markup (e.g.
    malformed tags), retries the same chunk as plain text so the message
    is never lost.

    Args:
        url: Telegram sendMessage API URL.
        chat_id: Target chat ID.
        text: Original plain-text version (fallback).
        html_text: HTML-formatted version (preferred).

    Returns:
        True if the chunk was sent successfully, False otherwise.
    """
    import urllib.error
    import urllib.request

    payload: dict = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req)  # noqa: S310
        return True
    except urllib.error.HTTPError as e:
        logger.warning("HTML send failed (%s), retrying as plain text", e)
    except Exception as e:
        logger.error("Failed to send Telegram message: %s", e)
        return False

    # Fallback: plain text, no parse_mode.
    fallback_payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    data = json.dumps(fallback_payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req)  # noqa: S310
        return True
    except Exception as e:
        logger.error("Fallback plain-text send also failed: %s", e)
        return False


def send_telegram(report: str, week_label: str) -> None:
    """Send the report via Telegram Bot API with HTML formatting.

    Converts markdown to Telegram-compatible HTML.  Falls back to plain
    text if Telegram rejects the markup.

    Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in environment / .env.

    Args:
        report: The markdown report text.
        week_label: Human-readable week label for the message.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set. Add it to your .env file.")
        return
    if not chat_id:
        logger.error("TELEGRAM_CHAT_ID not set. Add it to your .env file.")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    html_report = md_to_telegram_html(report)

    plain_chunks = chunk_text(report)
    html_chunks = chunk_text(html_report)

    for i, html in enumerate(html_chunks):
        plain = plain_chunks[i] if i < len(plain_chunks) else html
        if not _send_telegram_chunk(url, chat_id, plain, html):
            logger.error("Aborting Telegram send at chunk %d", i + 1)
            return

    logger.info("Telegram message sent to chat %s", chat_id)
