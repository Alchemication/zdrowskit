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
) -> ClaudeRunResult:
    """Run one Claude turn with acceptEdits permissions.

    Args:
        prompt: User prompt text.
        cwd: Project directory Claude should inspect.
        session_id: Existing Claude session to resume, if any.
        timeout_s: Maximum wall time for the CLI process.
        executable: Claude executable name/path. Defaults to
            ZDROWSKIT_CLAUDE_EXECUTABLE, then PATH lookup, then "claude".

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

    cmd = [
        executable,
        "--print",
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
    cmd.append(prompt)

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
    """Extract ``(final_text, session_id)`` from Claude ``--output-format json``.

    Falls back to a UUID scan if stdout isn't a clean single JSON object —
    e.g. when the CLI emits warning lines before the result payload.
    """
    stdout = stdout.strip()
    if not stdout:
        return "", None

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        # Try to recover the last well-formed JSON object on its own line.
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        else:
            match = _UUID_RE.search(stdout)
            return stdout, match.group(0) if match else None

    text = ""
    session: str | None = None
    if isinstance(payload, dict):
        result_val = payload.get("result")
        if isinstance(result_val, str):
            text = result_val
        session_val = payload.get("session_id")
        if isinstance(session_val, str) and session_val.strip():
            session = session_val.strip()
    return text, session


def _clip(text: str, limit: int = 700) -> str:
    """Clip subprocess diagnostics for Telegram."""
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + "\n[...truncated]"
