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

import difflib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
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


@dataclass
class PendingContextEdit:
    """A proposed edit plus approval metadata."""

    edit: ContextEdit
    source: str
    preview: str


@dataclass
class CoachFeedbackEntry:
    """A persisted accept/reject decision for a context edit."""

    feedback_id: str
    created_at: str
    source: str
    file: str
    action: str
    summary: str
    decision: str
    section: str | None = None
    reason: str | None = None


class EditPreviewError(ValueError):
    """Raised when a proposed edit cannot be previewed or applied safely."""


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


def build_edit_preview(
    context_dir: Path,
    edit: ContextEdit,
    *,
    strict: bool = False,
    max_lines: int = 60,
    max_chars: int = 3200,
) -> str:
    """Build a compact unified diff preview for a proposed edit."""
    path = context_dir / f"{edit.file}.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    new_content = _render_edit(existing, edit, strict=strict)
    diff_lines = list(
        difflib.unified_diff(
            existing.splitlines(),
            new_content.splitlines(),
            fromfile=path.name,
            tofile=f"{path.name} (proposed)",
            lineterm="",
        )
    )
    if not diff_lines:
        diff_lines = ["(no textual change)"]
    if len(diff_lines) > max_lines:
        diff_lines = diff_lines[:max_lines] + ["... diff truncated ..."]

    diff_text = "\n".join(diff_lines)
    if len(diff_text) > max_chars:
        diff_text = diff_text[: max_chars - 20].rstrip() + "\n... truncated ..."
    return diff_text


def build_content_preview(
    edit: ContextEdit,
    *,
    max_lines: int = 40,
    max_chars: int = 2400,
) -> str:
    """Build a compact preview of the proposed content for a context edit.

    Unlike :func:`build_edit_preview` which shows a unified diff, this returns
    the raw content that would be written — easier to scan at a glance.
    """
    text = edit.content.rstrip("\n")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["... truncated ..."]
    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[: max_chars - 20].rstrip() + "\n... truncated ..."
    return result


def apply_edit(context_dir: Path, edit: ContextEdit, *, strict: bool = False) -> None:
    """Write the edit to the target context file.

    Args:
        context_dir: Directory containing the .md context files.
        edit: The validated ContextEdit to apply.
        strict: When True, reject unsafe fallback behavior such as silently
            appending a missing replace_section target.
    """
    path = context_dir / f"{edit.file}.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    new_content = _render_edit(existing, edit, strict=strict)

    # Atomic write: write to temp file, then rename.
    tmp_path = path.with_suffix(".md.tmp")
    tmp_path.write_text(new_content, encoding="utf-8")
    tmp_path.rename(path)
    logger.info("Applied context edit to %s: %s", path.name, edit.summary)


def append_coach_feedback(context_dir: Path, entry: CoachFeedbackEntry) -> None:
    """Append or replace a coach feedback entry in coach_feedback.md."""
    feedback_path = context_dir / "coach_feedback.md"
    new_entry = _format_feedback_entry(entry)
    entries = _load_feedback_entries(feedback_path)
    replaced = False
    for i, existing in enumerate(entries):
        if f"Feedback ID: {entry.feedback_id}" in existing:
            entries[i] = new_entry
            replaced = True
            break
    if not replaced:
        entries.append(new_entry)

    feedback_path.write_text("\n\n".join(entries) + "\n", encoding="utf-8")
    logger.info(
        "Coach feedback %s entry %s (%s)",
        "updated" if replaced else "appended",
        entry.feedback_id,
        entry.decision,
    )


def update_coach_feedback_reason(
    context_dir: Path,
    feedback_id: str,
    reason: str,
) -> bool:
    """Update an existing coach feedback entry with a rejection reason."""
    feedback_path = context_dir / "coach_feedback.md"
    entries = _load_feedback_entries(feedback_path)
    updated = False
    for i, raw in enumerate(entries):
        if f"Feedback ID: {feedback_id}" not in raw:
            continue
        lines = raw.splitlines()
        reason_line = f"Reason: {reason}"
        for j, line in enumerate(lines):
            if line.startswith("Reason: "):
                lines[j] = reason_line
                updated = True
                break
        if not updated:
            lines.append(reason_line)
            updated = True
        entries[i] = "\n".join(lines)
        break

    if not updated:
        return False

    feedback_path.write_text("\n\n".join(entries) + "\n", encoding="utf-8")
    logger.info("Updated coach feedback reason for %s", feedback_id)
    return True


def _apply_append(existing: str, new_text: str) -> str:
    """Append new_text to the end of existing content."""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    if existing and not existing.endswith("\n\n"):
        existing += "\n"
    return existing + new_text.rstrip("\n") + "\n"


def _render_edit(existing: str, edit: ContextEdit, *, strict: bool) -> str:
    """Return the file content that would result from applying an edit."""
    if edit.action == "append":
        return _apply_append(existing, edit.content)
    if edit.action == "replace_section":
        assert edit.section is not None
        return _apply_replace_section(
            existing,
            edit.section,
            edit.content,
            strict=strict,
        )
    msg = f"Unhandled action {edit.action!r}"
    logger.error("%s — skipping", msg)
    raise EditPreviewError(msg)


def _apply_replace_section(
    existing: str,
    heading: str,
    new_text: str,
    *,
    strict: bool = False,
) -> str:
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
    if strict:
        raise EditPreviewError(f"Section not found in file: {heading}")
    logger.warning("Section %r not found in file, appending instead", heading)
    return _apply_append(existing, new_text)


def _load_feedback_entries(path: Path) -> list[str]:
    """Load feedback entries split on heading boundaries."""
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)(?=^## )", content)
    return [p.strip() for p in parts if p.strip() and p.strip().startswith("## ")]


def _format_feedback_entry(entry: CoachFeedbackEntry) -> str:
    """Render a coach feedback entry as markdown."""
    lines = [
        f"## {entry.created_at}",
        f"Feedback ID: {entry.feedback_id}",
        f"Source: {entry.source}",
        f"Target: {entry.file}.md",
        f"Action: {entry.action}",
    ]
    if entry.section:
        lines.append(f"Section: {entry.section}")
    lines.extend(
        [
            f"Decision: {entry.decision}",
            f"Summary: {entry.summary}",
        ]
    )
    if entry.reason:
        lines.append(f"Reason: {entry.reason}")
    return "\n".join(lines)


class PendingEdits:
    """Thread-safe store for context edits awaiting user confirmation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._edits: dict[str, tuple[PendingContextEdit, float]] = {}
        self._counter = 0

    def store(self, edit: ContextEdit, *, source: str, preview: str) -> str:
        """Store an edit and return its callback ID.

        Args:
            edit: The proposed context edit.
            source: Origin of the proposal, e.g. ``"coach"`` or ``"chat"``.
            preview: Compact diff preview shown to the user.

        Returns:
            A short ID string like ``"ce_1"``.
        """
        with self._lock:
            self._cleanup()
            self._counter += 1
            edit_id = f"ce_{self._counter}"
            pending = PendingContextEdit(edit=edit, source=source, preview=preview)
            self._edits[edit_id] = (pending, time.monotonic())
            return edit_id

    def peek(self, edit_id: str) -> PendingContextEdit | None:
        """Return an edit without removing it, or None if expired/missing.

        Args:
            edit_id: The callback ID returned by :meth:`store`.

        Returns:
            The pending edit metadata, or None if not found or expired.
        """
        with self._lock:
            self._cleanup()
            entry = self._edits.get(edit_id)
            if entry is None:
                return None
            return entry[0]

    def pop(self, edit_id: str) -> PendingContextEdit | None:
        """Remove and return an edit, or None if expired/missing.

        Args:
            edit_id: The callback ID returned by :meth:`store`.

        Returns:
            The pending edit metadata, or None if not found or expired.
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


def new_feedback_entry(
    pending: PendingContextEdit,
    decision: str,
    *,
    reason: str | None = None,
) -> CoachFeedbackEntry:
    """Build a new feedback entry for an accepted or rejected proposal."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    feedback_id = f"cf_{int(time.time() * 1000)}"
    return CoachFeedbackEntry(
        feedback_id=feedback_id,
        created_at=ts,
        source=pending.source,
        file=pending.edit.file,
        action=pending.edit.action,
        summary=pending.edit.summary,
        decision=decision,
        section=pending.edit.section,
        reason=reason,
    )
