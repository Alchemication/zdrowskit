"""Context file editing from chat conversations.

Parses <context_update> blocks from LLM responses, validates them, and
applies edits to markdown context files.

Public API:
    ContextEdit              — dataclass describing a proposed edit
    extract_context_update   — parse <context_update> block from LLM response
    strip_context_update     — remove <context_update> block from visible reply
    apply_edit               — write the edit to the target file
    PendingEdits             — thread-safe store for edits awaiting confirmation

Example:
    edit = extract_context_update(llm_response)
    if edit:
        visible = strip_context_update(llm_response)
        apply_edit(context_dir, edit)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from config import EDITABLE_CONTEXT_FILES

logger = logging.getLogger(__name__)

VALID_ACTIONS: set[str] = {"append", "replace_section"}
PENDING_EDIT_TTL_S: float = 600  # 10 minutes

_CONTEXT_UPDATE_RE = re.compile(r"<context_update>(.*?)</context_update>", re.DOTALL)


@dataclass
class ContextEdit:
    """A proposed edit to a context file."""

    file: str
    action: str
    content: str
    summary: str
    section: str | None = None


def _parse_context_update_block(raw: str) -> ContextEdit | None:
    """Parse a single raw JSON string into a ContextEdit.

    Args:
        raw: The JSON content from inside a ``<context_update>`` tag.

    Returns:
        A validated ContextEdit, or None if invalid.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Invalid JSON in <context_update>: %s", raw[:200])
        return None

    file_stem = data.get("file", "")
    if file_stem not in EDITABLE_CONTEXT_FILES:
        logger.warning("Disallowed context file in update: %r", file_stem)
        return None

    action = data.get("action", "")
    if action not in VALID_ACTIONS:
        logger.warning("Unknown context edit action: %r", action)
        return None

    content = data.get("content", "")
    summary = data.get("summary", "")
    if not content or not summary:
        logger.warning("Missing content or summary in context update")
        return None

    section = data.get("section")
    if action == "replace_section" and not section:
        logger.warning("replace_section requires a section heading")
        return None

    return ContextEdit(
        file=file_stem,
        action=action,
        content=content,
        summary=summary,
        section=section,
    )


def extract_context_update(response: str) -> ContextEdit | None:
    """Extract a <context_update> block from the LLM response.

    Args:
        response: Full LLM response text.

    Returns:
        A ContextEdit if a valid block was found, or None.
    """
    match = _CONTEXT_UPDATE_RE.search(response)
    if not match:
        return None
    return _parse_context_update_block(match.group(1).strip())


def extract_all_context_updates(response: str) -> list[ContextEdit]:
    """Extract all <context_update> blocks from the LLM response.

    Args:
        response: Full LLM response text.

    Returns:
        A list of validated ContextEdits (may be empty).
    """
    edits: list[ContextEdit] = []
    for match in _CONTEXT_UPDATE_RE.finditer(response):
        edit = _parse_context_update_block(match.group(1).strip())
        if edit is not None:
            edits.append(edit)
    return edits


def context_edit_from_tool_call(tool_call: object) -> ContextEdit | None:
    """Build a ContextEdit from a litellm tool_call object.

    Validates the same constraints as extract_context_update but reads
    from parsed tool call arguments instead of regex-extracted JSON.

    Args:
        tool_call: A tool call object from litellm with .function.name
            and .function.arguments attributes.

    Returns:
        A ContextEdit if valid, or None.
    """
    fn = getattr(tool_call, "function", None)
    if fn is None or getattr(fn, "name", None) != "update_context":
        return None

    raw_args = getattr(fn, "arguments", "")
    try:
        data = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except (json.JSONDecodeError, ValueError):
        logger.warning("Invalid JSON in tool call arguments: %s", raw_args[:200])
        return None

    file_stem = data.get("file", "")
    if file_stem not in EDITABLE_CONTEXT_FILES:
        logger.warning("Disallowed context file in tool call: %r", file_stem)
        return None

    action = data.get("action", "")
    if action not in VALID_ACTIONS:
        logger.warning("Unknown context edit action in tool call: %r", action)
        return None

    content = data.get("content", "")
    summary = data.get("summary", "")
    if not content or not summary:
        logger.warning("Missing content or summary in tool call")
        return None

    section = data.get("section")
    if action == "replace_section" and not section:
        logger.warning("replace_section tool call requires a section heading")
        return None

    return ContextEdit(
        file=file_stem,
        action=action,
        content=content,
        summary=summary,
        section=section,
    )


def strip_context_update(response: str) -> str:
    """Remove the <context_update> block from the visible reply.

    Args:
        response: Full LLM response text.

    Returns:
        The response with the block removed and whitespace cleaned up.
    """
    return _CONTEXT_UPDATE_RE.sub("", response).strip()


def strip_all_context_updates(response: str) -> str:
    """Remove all <context_update> blocks from the visible reply.

    Args:
        response: Full LLM response text.

    Returns:
        The response with all blocks removed and whitespace cleaned up.
    """
    return _CONTEXT_UPDATE_RE.sub("", response).strip()


def apply_edit(context_dir: Path, edit: ContextEdit) -> None:
    """Write the edit to the target context file.

    Args:
        context_dir: Directory containing the .md context files.
        edit: The validated ContextEdit to apply.
    """
    path = context_dir / f"{edit.file}.md"

    if path.exists():
        content = path.read_text(encoding="utf-8")
    else:
        content = ""

    if edit.action == "append":
        new_content = _apply_append(content, edit.content)
    elif edit.action == "replace_section":
        assert edit.section is not None
        new_content = _apply_replace_section(content, edit.section, edit.content)
    else:
        logger.error("Unhandled action %r — skipping", edit.action)
        return

    # Atomic write: write to temp file, then rename.
    tmp_path = path.with_suffix(".md.tmp")
    tmp_path.write_text(new_content, encoding="utf-8")
    tmp_path.rename(path)
    logger.info("Applied context edit to %s: %s", path.name, edit.summary)


def _apply_append(existing: str, new_text: str) -> str:
    """Append new_text to the end of existing content."""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    if existing and not existing.endswith("\n\n"):
        existing += "\n"
    return existing + new_text.rstrip("\n") + "\n"


def _apply_replace_section(existing: str, heading: str, new_text: str) -> str:
    """Replace a ## heading section in the file.

    Matches from the heading line to just before the next ## heading or EOF.
    If the heading is not found, appends the new text instead.
    """
    escaped = re.escape(heading)
    pattern = rf"(?m)^{escaped}\s*\n.*?(?=^## |\Z)"
    match = re.search(pattern, existing, re.DOTALL)
    if match:
        replacement = new_text.rstrip("\n") + "\n\n"
        return (
            existing[: match.start()]
            + replacement
            + existing[match.end() :].lstrip("\n")
        )
    # Section not found — append.
    logger.warning("Section %r not found in file, appending instead", heading)
    return _apply_append(existing, new_text)


class PendingEdits:
    """Thread-safe store for context edits awaiting user confirmation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._edits: dict[str, tuple[ContextEdit, float]] = {}
        self._counter = 0

    def store(self, edit: ContextEdit) -> str:
        """Store an edit and return its callback ID.

        Args:
            edit: The proposed context edit.

        Returns:
            A short ID string like ``"ce_1"``.
        """
        with self._lock:
            self._cleanup()
            self._counter += 1
            edit_id = f"ce_{self._counter}"
            self._edits[edit_id] = (edit, time.monotonic())
            return edit_id

    def pop(self, edit_id: str) -> ContextEdit | None:
        """Remove and return an edit, or None if expired/missing.

        Args:
            edit_id: The callback ID returned by :meth:`store`.

        Returns:
            The ContextEdit, or None if not found or expired.
        """
        with self._lock:
            self._cleanup()
            entry = self._edits.pop(edit_id, None)
            if entry is None:
                return None
            return entry[0]

    def _cleanup(self) -> None:
        """Remove entries older than PENDING_EDIT_TTL_S. Must hold lock."""
        now = time.monotonic()
        expired = [
            k for k, (_, ts) in self._edits.items() if now - ts > PENDING_EDIT_TTL_S
        ]
        for k in expired:
            del self._edits[k]
