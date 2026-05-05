"""Telegram-facing Claude Code runner.

Sibling of :mod:`daemon_agent_flow` for the Anthropic ``claude`` CLI. Spawns
the CLI in ``--print`` mode with ``--permission-mode acceptEdits`` so it can
read and edit files in the repository checkout, while still requiring approval
for tools like Bash that the headless mode cannot grant.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

CLAUDE_TIMEOUT_S = 600
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


@dataclass(frozen=True)
class ClaudeRunResult:
    """Result from a Claude CLI turn.

    Attributes:
        text: Final assistant-facing text.
        session_id: Claude session id to resume on the next Telegram turn.
    """

    text: str
    session_id: str | None


class ClaudeRunError(RuntimeError):
    """Raised when the Claude CLI cannot produce a usable reply."""


def run_claude_workspace(
    prompt: str,
    *,
    cwd: Path,
    session_id: str | None = None,
    timeout_s: int = CLAUDE_TIMEOUT_S,
    executable: str | None = None,
    progress_callback: object | None = None,
) -> ClaudeRunResult:
    """Run one Claude turn with acceptEdits permissions.

    Args:
        prompt: User prompt text.
        cwd: Project directory Claude should inspect.
        session_id: Existing Claude session to resume, if any.
        timeout_s: Maximum wall time for the CLI process.
        executable: Claude executable name/path. Defaults to
            ZDROWSKIT_CLAUDE_EXECUTABLE, then PATH lookup, then "claude".
        progress_callback: Optional callable receiving short progress text
            parsed from Claude stream-json stdout as the process runs.

    Returns:
        Parsed Claude result with final text and best-known session id.

    Raises:
        ValueError: If prompt is empty.
        ClaudeRunError: If the CLI fails, times out, or returns no text.
    """
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("Prompt is empty.")

    executable = executable or _default_claude_executable()
    streaming = progress_callback is not None
    cmd = _claude_command(
        executable,
        prompt,
        session_id=session_id,
        streaming=streaming,
    )

    if streaming:
        return _run_claude_workspace_streaming(
            cmd,
            cwd=cwd,
            session_id=session_id,
            timeout_s=timeout_s,
            progress_callback=progress_callback,
        )

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
        raise ClaudeRunError(
            "Claude CLI not found. Install it, run "
            "`uv run python main.py daemon-install`, then retry."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeRunError(f"Claude timed out after {timeout_s}s.") from exc

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    if proc.returncode != 0:
        details = _clip(stderr.strip() or stdout.strip() or "no output")
        raise ClaudeRunError(f"Claude failed: {details}")

    text, parsed_session = _parse_claude_output(stdout)
    parsed_session = parsed_session or session_id
    if not text.strip():
        raise ClaudeRunError("Claude returned an empty response.")

    return ClaudeRunResult(text=text.strip(), session_id=parsed_session)


def _claude_command(
    executable: str,
    prompt: str,
    *,
    session_id: str | None,
    streaming: bool,
) -> list[str]:
    """Build the Claude CLI command for a single turn."""
    cmd = [
        executable,
        "--print",
        "--output-format",
        "stream-json" if streaming else "json",
    ]
    if streaming:
        cmd.extend(["--verbose", "--include-partial-messages"])
    cmd.extend(["--permission-mode", "acceptEdits"])
    if session_id:
        cmd.extend(["--resume", session_id])
    cmd.append(prompt)
    return cmd


def _run_claude_workspace_streaming(
    cmd: list[str],
    *,
    cwd: Path,
    session_id: str | None,
    timeout_s: int,
    progress_callback: object,
) -> ClaudeRunResult:
    """Run Claude with live stream-json progress callbacks."""
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise ClaudeRunError(
            "Claude CLI not found. Install it, run "
            "`uv run python main.py daemon-install`, then retry."
        ) from exc

    stdout_thread = threading.Thread(
        target=_read_claude_stdout,
        args=(proc.stdout, stdout_lines, progress_callback),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_stream_lines,
        args=(proc.stderr, stderr_lines),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    try:
        returncode = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)
        raise ClaudeRunError(f"Claude timed out after {timeout_s}s.") from exc

    stdout_thread.join(timeout=1.0)
    stderr_thread.join(timeout=1.0)

    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)
    if returncode != 0:
        details = _clip(stderr.strip() or stdout.strip() or "no output")
        raise ClaudeRunError(f"Claude failed: {details}")

    text, parsed_session = _parse_claude_output(stdout)
    parsed_session = parsed_session or session_id
    if not text.strip():
        raise ClaudeRunError("Claude returned an empty response.")

    return ClaudeRunResult(text=text.strip(), session_id=parsed_session)


def _read_claude_stdout(
    stream: object,
    stdout_lines: list[str],
    progress_callback: object,
) -> None:
    """Read Claude stdout and emit progress updates for each stream event."""
    for line in _iter_stream_lines(stream):
        stdout_lines.append(line)
        progress = _claude_progress_text(line)
        if progress:
            _emit_progress(progress_callback, progress)


def _read_stream_lines(stream: object, lines: list[str]) -> None:
    """Read text stream lines into ``lines``."""
    for line in _iter_stream_lines(stream):
        lines.append(line)


def _iter_stream_lines(stream: object) -> Iterator[str]:
    """Yield lines from a text stream."""
    if stream is None:
        return
    yield from stream


def _emit_progress(progress_callback: object, text: str) -> None:
    """Call a user-supplied progress callback if it is callable."""
    if callable(progress_callback):
        progress_callback(text)


def _claude_progress_text(line: str) -> str:
    """Return compact progress text for one Claude stream-json event."""
    line = line.strip()
    if not line:
        return ""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return _clip_one_line(line)
    if not isinstance(event, dict):
        return _clip_one_line(str(event))

    event_type = str(event.get("type") or "progress").replace("_", " ")
    subtype = event.get("subtype")
    if isinstance(subtype, str) and subtype:
        event_type = f"{event_type} {subtype.replace('_', ' ')}"

    if isinstance(event.get("result"), str):
        return "final answer"
    value = _find_key(event, {"name", "tool_name", "message", "text"})
    if isinstance(value, str) and value.strip():
        return f"{event_type}: {_clip_one_line(value)}"
    return _clip_one_line(event_type)


def _default_claude_executable() -> str:
    """Return the best Claude executable for daemon subprocesses."""
    configured = os.environ.get("ZDROWSKIT_CLAUDE_EXECUTABLE")
    if configured and configured.strip():
        return configured.strip()
    return shutil.which("claude") or "claude"


def claude_usage() -> str:
    """Return Telegram help text for the Claude command."""
    return (
        "Claude commands:\n"
        "/claude <prompt> — Ask Claude about this repo.\n"
        "/claude on [prompt] — Turn on Claude mode.\n"
        "/claude off — Turn off Claude mode.\n"
        "/claude reset [prompt] — Clear Claude context.\n"
        "/claude new <prompt> — Start a fresh Claude session.\n"
        "/claude stop — Clear Claude context and turn mode off.\n\n"
        "After /claude on, plain non-command messages go to Claude automatically "
        "until /claude off, /claude stop, or 30 min of inactivity.\n"
        "acceptEdits mode: Claude can edit files in this repo."
    )


def _parse_claude_output(stdout: str) -> tuple[str, str | None]:
    """Extract ``(final_text, session_id)`` from Claude JSON output.

    Handles both single-result ``json`` and newline-delimited ``stream-json``.
    Falls back to a UUID scan if stdout is not clean JSON.
    """
    stdout = stdout.strip()
    if not stdout:
        return "", None

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payloads = _iter_jsonl(stdout)
        if payloads:
            return _parse_claude_events(payloads, stdout)
        else:
            match = _UUID_RE.search(stdout)
            return stdout, match.group(0) if match else None

    if isinstance(payload, dict):
        return _parse_claude_events([payload], stdout)
    return "", None


def _parse_claude_events(events: list[object], stdout: str) -> tuple[str, str | None]:
    """Extract text and session id from Claude JSON event objects."""
    text_candidates: list[str] = []
    session: str | None = None
    for event in events:
        if not isinstance(event, dict):
            continue
        result_val = event.get("result")
        if isinstance(result_val, str) and result_val.strip():
            text_candidates.append(result_val.strip())
        message_text = _extract_message_text(event)
        if message_text:
            text_candidates.append(message_text)
        session_val = event.get("session_id")
        if isinstance(session_val, str) and session_val.strip():
            session = session_val.strip()
    if session is None:
        match = _UUID_RE.search(stdout)
        session = match.group(0) if match else None
    return text_candidates[-1] if text_candidates else "", session


def _extract_message_text(event: dict) -> str:
    """Best-effort text extraction from a Claude message event."""
    message = event.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts).strip()


def _iter_jsonl(stdout: str) -> list[object]:
    """Parse JSONL stdout, skipping non-JSON status lines."""
    events: list[object] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON Claude stdout line: %s", line[:120])
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


def _clip_one_line(text: str, limit: int = 220) -> str:
    """Clip progress text to a single Telegram-friendly line."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _clip(text: str, limit: int = 700) -> str:
    """Clip subprocess diagnostics for Telegram."""
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + "\n[...truncated]"
