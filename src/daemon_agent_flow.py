"""Telegram-facing Codex runner.

This module keeps subprocess execution and Codex JSONL parsing out of the
Telegram command router.  It runs Codex with workspace-write sandboxing so it
can edit files in the repository, while still denying approval escalation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
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


def run_codex_workspace(
    prompt: str,
    *,
    cwd: Path,
    session_id: str | None = None,
    timeout_s: int = CODEX_TIMEOUT_S,
    executable: str | None = None,
    progress_callback: object | None = None,
) -> CodexRunResult:
    """Run one Codex turn with workspace-write sandboxing.

    Args:
        prompt: User prompt text.
        cwd: Project directory Codex should inspect.
        session_id: Existing Codex session to resume, if any.
        timeout_s: Maximum wall time for the CLI process.
        executable: Codex executable name/path. Defaults to
            ZDROWSKIT_CODEX_EXECUTABLE, then PATH lookup, then "codex".
        progress_callback: Optional callable receiving short progress text
            parsed from Codex JSONL stdout as the process runs.

    Returns:
        Parsed Codex result with final text and best-known session id.

    Raises:
        ValueError: If prompt is empty.
        CodexRunError: If the CLI fails, times out, or returns no text.
    """
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("Prompt is empty.")

    executable = executable or _default_codex_executable()

    with tempfile.NamedTemporaryFile(prefix="zdrowskit-codex-", delete=False) as out:
        output_path = Path(out.name)

    cmd = _codex_command(executable, cwd, output_path, prompt, session_id)

    if progress_callback is not None:
        return _run_codex_workspace_streaming(
            cmd,
            cwd=cwd,
            output_path=output_path,
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
        _unlink_quietly(output_path)
        raise CodexRunError(
            "Codex CLI not found. Install it, run `uv run python main.py daemon-install`, then retry."
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


def _codex_command(
    executable: str,
    cwd: Path,
    output_path: Path,
    prompt: str,
    session_id: str | None,
) -> list[str]:
    """Build the Codex CLI command for a single turn."""
    base_cmd = [
        executable,
        "--ask-for-approval",
        "never",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(cwd),
        "exec",
    ]
    if session_id:
        return [
            *base_cmd,
            "resume",
            "--json",
            "--output-last-message",
            str(output_path),
            session_id,
            prompt,
        ]
    return [
        *base_cmd,
        "--json",
        "--output-last-message",
        str(output_path),
        prompt,
    ]


def _run_codex_workspace_streaming(
    cmd: list[str],
    *,
    cwd: Path,
    output_path: Path,
    session_id: str | None,
    timeout_s: int,
    progress_callback: object,
) -> CodexRunResult:
    """Run Codex with live stdout progress callbacks."""
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
        _unlink_quietly(output_path)
        raise CodexRunError(
            "Codex CLI not found. Install it, run `uv run python main.py daemon-install`, then retry."
        ) from exc

    stdout_thread = threading.Thread(
        target=_read_codex_stdout,
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
        _unlink_quietly(output_path)
        raise CodexRunError(f"Codex timed out after {timeout_s}s.") from exc

    stdout_thread.join(timeout=1.0)
    stderr_thread.join(timeout=1.0)

    try:
        file_text = output_path.read_text(encoding="utf-8").strip()
    except OSError:
        file_text = ""
    finally:
        _unlink_quietly(output_path)

    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)
    parsed_session_id = _extract_session_id(stdout) or session_id

    if returncode != 0:
        details = _clip(stderr.strip() or stdout.strip() or "no output")
        raise CodexRunError(f"Codex failed: {details}")

    text = file_text or _extract_final_text(stdout)
    if not text.strip():
        raise CodexRunError("Codex returned an empty response.")

    return CodexRunResult(text=text.strip(), session_id=parsed_session_id)


def _read_codex_stdout(
    stream: object,
    stdout_lines: list[str],
    progress_callback: object,
) -> None:
    """Read Codex stdout and emit progress updates for each JSONL event."""
    for line in _iter_stream_lines(stream):
        stdout_lines.append(line)
        progress = _codex_progress_text(line)
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


def _codex_progress_text(line: str) -> str:
    """Return a compact human-readable progress line for one Codex JSONL event."""
    line = line.strip()
    if not line:
        return ""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return _clip_one_line(line)

    if not isinstance(event, dict):
        return _clip_one_line(str(event))

    event_type = event.get("type")
    label = str(event_type).replace("_", " ") if event_type else "working"
    value = _find_key(event, {"message", "text", "content", "command", "cmd", "name"})
    if isinstance(value, str) and value.strip():
        return f"{label}: {_clip_one_line(value)}"
    return _clip_one_line(label)


def _default_codex_executable() -> str:
    """Return the best Codex executable for daemon subprocesses."""
    configured = os.environ.get("ZDROWSKIT_CODEX_EXECUTABLE")
    if configured and configured.strip():
        return configured.strip()
    return shutil.which("codex") or "codex"


def codex_usage() -> str:
    """Return Telegram help text for the Codex command."""
    return (
        "Codex commands:\n"
        "/codex <prompt> — Ask Codex about this repo.\n"
        "/codex on [prompt] — Turn on Codex mode.\n"
        "/codex off — Turn off Codex mode.\n"
        "/codex reset [prompt] — Clear Codex context.\n"
        "/codex new <prompt> — Start a fresh Codex session.\n"
        "/codex stop — Clear Codex context and turn mode off.\n\n"
        "After /codex on, plain non-command messages go to Codex automatically "
        "until /codex off, /codex stop, or 30 min of inactivity.\n"
        "Workspace-write mode: Codex can edit files in this repo."
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


def _clip_one_line(text: str, limit: int = 220) -> str:
    """Clip progress text to a single Telegram-friendly line."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _unlink_quietly(path: Path) -> None:
    """Remove a temporary output file if it exists."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to delete temp Codex output %s", path, exc_info=True)
