"""Telegram-facing read-only Codex runner.

This module keeps subprocess execution and Codex JSONL parsing out of the
Telegram command router.  It intentionally supports only Codex in read-only
mode for the first pass.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

CODEX_TIMEOUT_S = 600
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_SESSION_KEYS = {"session_id", "conversation_id", "thread_id"}
_TEXT_KEYS = {"text", "message", "content", "last_message", "final_message"}


@dataclass(frozen=True)
class CodexRunResult:
    """Result from a Codex CLI turn.

    Attributes:
        text: Final assistant-facing text.
        session_id: Codex session id to resume on the next Telegram turn.
    """

    text: str
    session_id: str | None


class CodexRunError(RuntimeError):
    """Raised when the Codex CLI cannot produce a usable reply."""


def run_codex_readonly(
    prompt: str,
    *,
    cwd: Path,
    session_id: str | None = None,
    timeout_s: int = CODEX_TIMEOUT_S,
    executable: str = "codex",
) -> CodexRunResult:
    """Run one Codex turn in read-only mode.

    Args:
        prompt: User prompt text.
        cwd: Project directory Codex should inspect.
        session_id: Existing Codex session to resume, if any.
        timeout_s: Maximum wall time for the CLI process.
        executable: Codex executable name/path.

    Returns:
        Parsed Codex result with final text and best-known session id.

    Raises:
        ValueError: If prompt is empty.
        CodexRunError: If the CLI fails, times out, or returns no text.
    """
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("Prompt is empty.")

    with tempfile.NamedTemporaryFile(prefix="zdrowskit-codex-", delete=False) as out:
        output_path = Path(out.name)

    if session_id:
        cmd = [
            executable,
            "exec",
            "resume",
            "--json",
            "--output-last-message",
            str(output_path),
            session_id,
            prompt,
        ]
    else:
        cmd = [
            executable,
            "exec",
            "--cd",
            str(cwd),
            "--sandbox",
            "read-only",
            "--ask-for-approval",
            "never",
            "--json",
            "--output-last-message",
            str(output_path),
            prompt,
        ]

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        _unlink_quietly(output_path)
        raise CodexRunError(
            "Codex CLI not found. Install it and restart daemon."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        _unlink_quietly(output_path)
        raise CodexRunError(f"Codex timed out after {timeout_s}s.") from exc

    try:
        file_text = output_path.read_text(encoding="utf-8").strip()
    except OSError:
        file_text = ""
    finally:
        _unlink_quietly(output_path)

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    parsed_session_id = _extract_session_id(stdout) or session_id

    if proc.returncode != 0:
        details = _clip(stderr.strip() or stdout.strip() or "no output")
        raise CodexRunError(f"Codex failed: {details}")

    text = file_text or _extract_final_text(stdout)
    if not text.strip():
        raise CodexRunError("Codex returned an empty response.")

    return CodexRunResult(text=text.strip(), session_id=parsed_session_id)


def codex_usage() -> str:
    """Return Telegram help text for the read-only Codex command."""
    return (
        "Use /codex <prompt> to ask Codex about this repo.\n"
        "Use /codex new <prompt> to start a fresh Codex session.\n"
        "Use /codex stop to forget the current Codex session.\n"
        "Read-only mode only."
    )


def _extract_session_id(stdout: str) -> str | None:
    """Extract the first plausible Codex session id from JSONL stdout."""
    for event in _iter_jsonl(stdout):
        value = _find_key(event, _SESSION_KEYS)
        if isinstance(value, str) and value.strip():
            return value.strip()
    match = _UUID_RE.search(stdout)
    return match.group(0) if match else None


def _extract_final_text(stdout: str) -> str:
    """Best-effort final text extraction from Codex JSONL stdout."""
    candidates: list[str] = []
    for event in _iter_jsonl(stdout):
        value = _find_key(event, _TEXT_KEYS)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    return candidates[-1] if candidates else stdout.strip()


def _iter_jsonl(stdout: str) -> list[object]:
    """Parse JSONL stdout, skipping non-JSON status lines."""
    events: list[object] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON Codex stdout line: %s", line[:120])
    return events


def _find_key(value: object, keys: set[str]) -> object | None:
    """Recursively find the first value whose key is in keys."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys:
                return item
        for item in value.values():
            found = _find_key(item, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key(item, keys)
            if found is not None:
                return found
    return None


def _clip(text: str, limit: int = 700) -> str:
    """Clip subprocess diagnostics for Telegram."""
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + "\n[...truncated]"


def _unlink_quietly(path: Path) -> None:
    """Remove a temporary output file if it exists."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to delete temp Codex output %s", path, exc_info=True)
