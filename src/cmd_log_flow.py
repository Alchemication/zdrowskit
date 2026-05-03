"""LLM-backed /log tap-flow construction."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from cmd_llm_common import route_kwargs, single_model_attempts
from config import CONTEXT_DIR, MAX_TOKENS_LOG_FLOW
from llm import call_llm, strip_json_fences
from llm_context import build_messages, load_context
from store import open_db

logger = logging.getLogger(__name__)

# Hard caps must match log_flow_prompt.md and log-flow handler expectations.
# The initial call returns exactly 1 step (the state check); a second step
# may be appended later by build_log_step_followup, giving 2 steps total.
MAX_LOG_FLOW_INITIAL_STEPS = 1
MAX_LOG_FLOW_STEPS = 2
MAX_LOG_FLOW_OPTIONS_PER_STEP = 8


class LogFlowStep(BaseModel):
    """One step in the /log tap-through flow.

    Pydantic-backed so the LLM can produce instances directly via
    ``response_format`` and so cross-field constraints (option count,
    ask_end_date_if_selected membership) live with the schema rather than in
    a separate coercion helper.
    """

    id: str = Field(description="Stable identifier for the step.")
    question: str = Field(description="Prompt shown to the user above the buttons.")
    options: list[str] = Field(
        description=(
            "Tappable options. At least one entry, at most "
            f"{MAX_LOG_FLOW_OPTIONS_PER_STEP}."
        )
    )
    multi_select: bool = Field(
        default=False, description="True when the user can pick multiple options."
    )
    optional: bool = Field(
        default=False, description="True when the user may skip without selecting."
    )
    ask_end_date_if_selected: list[str] | None = Field(
        default=None,
        description=(
            "Options that, if selected, prompt for an end-date follow-up. "
            "Each entry must appear in `options`."
        ),
    )

    @field_validator("id", "question", mode="after")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must be a non-empty string")
        return cleaned

    @field_validator("options", mode="after")
    @classmethod
    def _validate_options(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("must include at least one option")
        if len(value) > MAX_LOG_FLOW_OPTIONS_PER_STEP:
            raise ValueError(
                f"too many options ({len(value)}); max {MAX_LOG_FLOW_OPTIONS_PER_STEP}"
            )
        cleaned: list[str] = []
        for opt in value:
            stripped = opt.strip()
            if not stripped:
                raise ValueError("options must be non-empty strings")
            cleaned.append(stripped)
        return cleaned

    @model_validator(mode="after")
    def _validate_end_date_membership(self) -> LogFlowStep:
        if self.ask_end_date_if_selected is None:
            return self
        normalized = [x.strip() for x in self.ask_end_date_if_selected]
        unknown = [x for x in normalized if x not in self.options]
        if unknown:
            raise ValueError(
                f"ask_end_date_if_selected references unknown options: {unknown}"
            )
        self.ask_end_date_if_selected = normalized
        return self


@dataclass
class LogFlow:
    """A complete /log interview returned by the LLM."""

    steps: list[LogFlowStep]
    llm_call_id: int | None = None
    model: str | None = None


class _LogFlowPayload(BaseModel):
    """Strict LLM payload for the initial /log tap-flow call."""

    steps: list[LogFlowStep] = Field(
        description=(
            f"Initial tap-through. Exactly {MAX_LOG_FLOW_INITIAL_STEPS} step "
            "for the first call; a follow-up may be appended later."
        )
    )

    @field_validator("steps", mode="after")
    @classmethod
    def _bounded_initial_steps(cls, value: list[LogFlowStep]) -> list[LogFlowStep]:
        if not value:
            raise ValueError("must include at least one step")
        if len(value) > MAX_LOG_FLOW_INITIAL_STEPS:
            raise ValueError(
                f"too many steps ({len(value)}); "
                f"max {MAX_LOG_FLOW_INITIAL_STEPS} for the initial call"
            )
        return value


class _LogFollowupPayload(BaseModel):
    """Strict LLM payload for the reactive follow-up call."""

    step: LogFlowStep | None = Field(
        default=None,
        description=(
            "Tailored follow-up step, or null when the bullet should commit "
            "without a follow-up."
        ),
    )


def _query_today_snapshot(conn, today: date) -> str:
    """Build a short human-readable snapshot of today's signals for /log.

    Pulls today's `daily` row, last night's `sleep_all`, and today's
    `workout_all` entries. Kept tiny so the prompt stays focused.
    """
    lines: list[str] = [f"Date: {today.isoformat()}"]

    row = conn.execute(
        "SELECT hrv_ms, resting_hr, recovery_index, steps FROM daily WHERE date = ?",
        (today.isoformat(),),
    ).fetchone()
    if row:
        parts = []
        if row[0] is not None:
            parts.append(f"HRV {row[0]:.1f} ms")
        if row[1] is not None:
            parts.append(f"RHR {int(row[1])}")
        if row[2] is not None:
            parts.append(f"recovery_index {row[2]:.2f}")
        if row[3] is not None:
            parts.append(f"steps {int(row[3])}")
        if parts:
            lines.append("Today daily: " + ", ".join(parts))
        else:
            lines.append("Today daily: row present, all nulls")
    else:
        lines.append("Today daily: (no row yet)")

    sleep_date = today - timedelta(days=1)
    sleep_row = conn.execute(
        "SELECT sleep_total_h, sleep_efficiency_pct FROM sleep_all WHERE date = ?",
        (sleep_date.isoformat(),),
    ).fetchone()
    if sleep_row and sleep_row[0] is not None:
        effpart = (
            f", {sleep_row[1]:.0f}% efficiency" if sleep_row[1] is not None else ""
        )
        lines.append(f"Last night: {sleep_row[0]:.2f}h{effpart}")
    else:
        lines.append("Last night: (no sleep data)")

    workouts = conn.execute(
        "SELECT category, duration_min FROM workout_all "
        "WHERE date = ? ORDER BY start_utc, category, duration_min",
        (today.isoformat(),),
    ).fetchall()
    if workouts:
        summary = ", ".join(
            f"{cat or 'other'} {int(dur or 0)}m" for cat, dur in workouts
        )
        lines.append(f"Today workouts: {summary}")
    else:
        lines.append("Today workouts: (none logged)")

    return "\n".join(lines)


def _fallback_log_flow(today_snapshot: str) -> LogFlow:
    """Return a deterministic initial flow when model JSON is unusable."""
    if "Today workouts: (none logged)" in today_snapshot:
        options = ["rest day", "solid", "tired", "off"]
    else:
        options = ["solid", "easy", "tired legs", "off"]
    return LogFlow(
        steps=[
            LogFlowStep(
                id="state",
                question="How did today feel?",
                options=options,
                multi_select=False,
                optional=False,
            )
        ],
        llm_call_id=None,
        model="deterministic-fallback",
    )


def build_log_flow(
    *,
    db: str | Path,
    now: datetime | None = None,
    model: str | None = None,
) -> LogFlow:
    """Ask the LLM to design an adaptive /log interview for today.

    Reads me.md, strategy.md, the entire log.md (self-trimmed by history.md
    pipeline), and a lean snapshot of today's DB signals, then returns a
    validated one-step flow.
    """
    now = now or datetime.now().astimezone()
    today = now.date()

    try:
        context = load_context(
            CONTEXT_DIR,
            prompt_file="log_flow_prompt",
            max_history=0,
            max_log=0,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        raise

    conn = open_db(Path(db))
    try:
        context["today_snapshot"] = _query_today_snapshot(conn, today)

        messages = build_messages(
            context,
            health_data_text="{}",
            baselines=None,
            week_complete=False,
            today=today,
        )

        route = route_kwargs("log_flow", model)
        temperature = route.pop("temperature", 0)
        attempts = single_model_attempts(route)
        last_error: Exception | None = None
        for attempt in attempts:
            try:
                result = call_llm(
                    messages,
                    **attempt,
                    max_tokens=MAX_TOKENS_LOG_FLOW,
                    temperature=temperature,
                    response_format=_LogFlowPayload,
                    conn=conn,
                    request_type="log_flow",
                    metadata={"date": today.isoformat()},
                )
                payload = _LogFlowPayload.model_validate_json(
                    strip_json_fences(result.text)
                )
                return LogFlow(
                    steps=list(payload.steps),
                    llm_call_id=result.llm_call_id,
                    model=result.model,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "log_flow attempt with %s failed validation/call: %s",
                    attempt.get("model", "(unknown)"),
                    exc,
                    exc_info=True,
                )

        logger.error(
            "All log_flow model attempts failed; using deterministic fallback: %s",
            last_error,
        )
        return _fallback_log_flow(context["today_snapshot"])
    finally:
        conn.close()


def build_log_step_followup(
    *,
    prior_step: LogFlowStep,
    prior_answer: list[str],
    db: str | Path,
    now: datetime | None = None,
    model: str | None = None,
) -> LogFlowStep | None:
    """Design a reactive follow-up step given the user's step-1 answer.

    Returns a tailored ``LogFlowStep`` whose options react to
    ``prior_answer`` (affirmative tags after a positive state, disruption
    tags after a negative state), or ``None`` when step 1 already
    captured everything worth knowing and the bullet should commit now.
    """
    now = now or datetime.now().astimezone()
    today = now.date()

    try:
        context = load_context(
            CONTEXT_DIR,
            prompt_file="log_followup_prompt",
            max_history=0,
            max_log=0,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        raise

    conn = open_db(Path(db))
    try:
        context["today_snapshot"] = _query_today_snapshot(conn, today)
        context["prior_question"] = prior_step.question
        context["prior_options"] = ", ".join(prior_step.options)
        context["prior_answer"] = ", ".join(prior_answer) if prior_answer else "(none)"

        messages = build_messages(
            context,
            health_data_text="{}",
            baselines=None,
            week_complete=False,
            today=today,
        )

        route = route_kwargs("log_flow", model)
        temperature = route.pop("temperature", 0)
        attempts = single_model_attempts(route)
        last_error: Exception | None = None
        for attempt in attempts:
            try:
                result = call_llm(
                    messages,
                    **attempt,
                    max_tokens=MAX_TOKENS_LOG_FLOW,
                    temperature=temperature,
                    response_format=_LogFollowupPayload,
                    conn=conn,
                    request_type="log_flow_followup",
                    metadata={
                        "date": today.isoformat(),
                        "prior_step_id": prior_step.id,
                        "prior_answer": prior_answer,
                    },
                )
                payload = _LogFollowupPayload.model_validate_json(
                    strip_json_fences(result.text)
                )
                return payload.step
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "log_flow_followup attempt with %s failed validation/call: %s",
                    attempt.get("model", "(unknown)"),
                    exc,
                    exc_info=True,
                )

        logger.error(
            "All log_flow_followup model attempts failed; committing without "
            "follow-up: %s",
            last_error,
        )
        return None
    finally:
        conn.close()
