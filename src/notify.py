"""Notification delivery — email and Telegram.

Public API:
    md_to_html      — convert markdown to a styled HTML document.
    send_email      — send HTML report via Resend API.
    send_telegram   — send plain-text report via Telegram Bot API.
    chunk_text      — split text into chunks respecting line boundaries.

Example:
    from notify import send_email, send_telegram
    send_email(report_text, "Week 2026-W11 Review")
"""

from __future__ import annotations

import json
import logging
import os

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


def send_telegram(report: str, week_label: str) -> None:
    """Send the report via Telegram Bot API.

    Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in environment / .env.

    Args:
        report: The markdown report text.
        week_label: Human-readable week label for the message.
    """
    import urllib.request

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set. Add it to your .env file.")
        return
    if not chat_id:
        logger.error("TELEGRAM_CHAT_ID not set. Add it to your .env file.")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    for i, text in enumerate(chunk_text(report)):
        data = json.dumps(
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req)  # noqa: S310
        except Exception as e:
            logger.error("Failed to send Telegram message (chunk %d): %s", i + 1, e)
            return

    logger.info("Telegram message sent to chat %s", chat_id)
