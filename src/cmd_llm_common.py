"""Shared types and helpers for LLM-powered command modules."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from config import (
    ENABLE_LLM_VERIFICATION,
    MAX_VERIFICATION_REVISIONS,
    VERIFY_COACH,
    VERIFY_INSIGHTS,
    VERIFY_NUDGE,
)
from llm import LLMResult
from llm_verify import VerificationKind, verify_and_rewrite
from model_prefs import resolve_model_route
from store import load_date_range

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """Return value from LLM-powered commands that send via Telegram.

    Attributes:
        text: The LLM response text (or None if skipped).
        llm_call_id: Database row id of the logged LLM call.
        telegram_message_id: Message id of the last Telegram message sent.
    """

    text: str | None = None
    llm_call_id: int | None = None
    telegram_message_id: int | None = None


def normalize_reasoning_effort(value: str | None) -> str | None:
    """Normalize a CLI reasoning-effort value to what call_llm expects."""
    if value is None or value == "none":
        return None
    return value


def telegram_chat_id(args: Any) -> str | None:
    """Return the resolved profile's Telegram destination, or None.

    Commands run with an explicit ``--db`` have no roster entry and therefore
    no destination; returning None lets ``notify`` log one actionable error
    instead of posting an empty ``chat_id`` to the Telegram API.
    """
    raw = getattr(args, "telegram_id", None)
    return str(raw) if raw is not None else None


def route_kwargs(
    feature: str,
    explicit_model: str | None = None,
    *,
    prefs_path: Path | None = None,
) -> dict:
    """Return model-routing kwargs for a feature unless explicitly overridden."""
    if explicit_model:
        return {"model": explicit_model}
    return resolve_model_route(feature, path=prefs_path).call_kwargs()


def single_model_attempts(route: dict) -> list[dict]:
    """Expand a route into one-model attempts for validation-aware retries."""
    raw_models = [route.get("model"), *(route.get("fallback_models") or [])]
    seen: set[str] = set()
    models: list[str] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, str) or not raw_model or raw_model in seen:
            continue
        seen.add(raw_model)
        models.append(raw_model)

    attempts: list[dict] = []
    for model in models:
        attempt = {
            key: value for key, value in route.items() if key != "fallback_models"
        }
        attempt["model"] = model
        # call_llm's built-in fallback only handles transport/provider errors.
        # These attempts need to validate the response body before moving on.
        attempt["fallback_models"] = []
        attempts.append(attempt)
    return attempts


def save_baselines(context_dir: Path, baselines: str) -> None:
    """Write auto-computed baselines to a dedicated baselines.md file."""
    path = context_dir / "baselines.md"
    path.write_text(baselines.rstrip() + "\n", encoding="utf-8")
    logger.info("Saved baselines to %s", path)


def hit_token_ceiling(result: LLMResult) -> bool:
    """Return True when an LLM result likely ended because max_tokens was hit."""
    return result.max_tokens is not None and result.output_tokens >= result.max_tokens


def _verification_enabled(kind: VerificationKind) -> bool:
    """Return True when LLM verification should run for a surface."""
    if not ENABLE_LLM_VERIFICATION:
        return False
    return {
        "insights": VERIFY_INSIGHTS,
        "coach": VERIFY_COACH,
        "nudge": VERIFY_NUDGE,
    }[kind]


def apply_verification(
    *,
    kind: VerificationKind,
    draft: str,
    evidence: dict[str, Any],
    source_messages: list[dict[str, Any]],
    conn: sqlite3.Connection,
    metadata: dict[str, Any],
    trace_id: int | None = None,
    strict: bool = False,
    model_prefs_path: Path | None = None,
) -> str | None:
    """Run verifier/rewrite and return the approved text, or None on fail.

    When *strict* is True, any non-pass verdict is treated as fail and the
    rewriter is bypassed — used by surfaces (e.g. coach) where shipping a
    rewritten partial is worse than shipping nothing.
    """
    if not _verification_enabled(kind):
        return draft
    verifier_route = (
        resolve_model_route("verification", path=model_prefs_path)
        if model_prefs_path is not None
        else resolve_model_route("verification")
    )
    rewrite_route = (
        resolve_model_route("verification_rewrite", path=model_prefs_path)
        if model_prefs_path is not None
        else resolve_model_route("verification_rewrite")
    )

    result = verify_and_rewrite(
        kind=kind,
        draft=draft,
        evidence=evidence,
        source_messages=source_messages,
        conn=conn,
        metadata=metadata,
        trace_id=trace_id,
        model=verifier_route.primary,
        rewrite_model=rewrite_route.primary,
        fallback_models=[verifier_route.fallback] if verifier_route.fallback else None,
        temperature=verifier_route.temperature,
        reasoning_effort=verifier_route.reasoning_effort,
        rewrite_temperature=rewrite_route.temperature,
        rewrite_reasoning_effort=rewrite_route.reasoning_effort,
        max_revisions=MAX_VERIFICATION_REVISIONS,
        strict=strict,
    )
    issue_summary = ", ".join(
        f"{issue.severity}: {issue.problem}" for issue in result.issues
    )
    logger.info(
        "%s verification verdict=%s confidence=%s issues=%d%s",
        kind,
        result.verdict,
        result.confidence,
        len(result.issues),
        f" ({issue_summary})" if issue_summary else "",
    )
    if result.verdict == "fail":
        return None
    return result.revised_text if result.revised_text is not None else draft


class InsufficientWeekData(Exception):
    """The requested week has no data, though the database itself is not empty.

    Distinct from an empty database, which is a setup problem the user must
    act on. This one resolves itself: a profile onboarded mid-week simply has
    no complete week to report on yet. Callers that run unattended should
    record it as a skip rather than surface it as a failure.
    """


def fail_without_week_data(
    conn: sqlite3.Connection,
    health_data: dict[str, Any],
) -> NoReturn:
    """Stop a report that found no data for the week it was asked to cover.

    An empty database and a database that simply does not reach the requested
    week are different problems with different fixes, and reporting both as
    "run import first" sends a user with a freshly imported database off to
    re-import it. A cold-start profile whose first readings land mid-week hits
    the second case on every scheduled run until its data catches up, so it
    raises `InsufficientWeekData` and the daemon logs a skip instead of
    notifying the user that their report failed.

    Args:
        conn: Open health database connection.
        health_data: The assembled payload whose week summary came back empty.

    Raises:
        InsufficientWeekData: The database holds data, but none for this week.
        SystemExit: The database holds no data at all.
    """
    date_range = load_date_range(conn)
    if date_range is None:
        logger.error("The health database is empty. Run 'import' first.")
        raise SystemExit(1)
    first, last = date_range
    week_label = health_data.get("week_label") or "the requested week"
    raise InsufficientWeekData(
        f"No health data for {week_label}. The database covers {first} to "
        f"{last}. Import the missing days with 'import', or wait until that "
        "week has data."
    )
