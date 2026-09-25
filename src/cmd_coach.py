"""LLM-powered coaching review command."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from baselines import compute_baselines, unestablished_metrics
from data_maturity import build_data_maturity
from cmd_llm_common import (
    CommandResult,
    apply_verification,
    fail_without_week_data,
    normalize_reasoning_effort,
    route_kwargs,
    save_baselines,
)
from config import (
    CONTEXT_DIR,
    MAX_TOKENS_COACH,
    MAX_TOOL_ITERATIONS_COACH,
    METRIC_TRUST_WINDOW_DAYS,
)
from context_edit import ContextEdit, EditPreviewError, build_edit_preview
from llm import call_llm
from llm_context import build_messages, load_context, load_prompt_text
from llm_health import (
    build_llm_data,
    build_review_facts,
    format_recent_nudges,
    render_health_data,
)
from llm_verify import extract_tool_evidence, slim_source_messages
from challenges import (
    PROPOSAL_NOTED,
    ChallengeError,
    Proposal,
    coerce_proposal,
    challenge_status_text,
    describe_challenge,
    describe_history,
    propose,
    propose_challenge_tool,
)
from goal_check import build_goal_check, record_triggers
from milestones import compute_milestones
from store import create_llm_trace, open_db
from weekly_targets import known_activity_types

logger = logging.getLogger(__name__)


@dataclass
class CoachProposal:
    """A validated coach edit ready to be presented to the user.

    Attributes:
        edit: The proposed ContextEdit.
        preview: Pre-rendered unified-diff preview string for the edit.
    """

    edit: ContextEdit
    preview: str


def _extract_strategy_sections(strategy_md: str) -> list[str]:
    """Return the ordered list of `## ` headings inside strategy.md."""
    if not strategy_md or strategy_md == "(not provided)":
        return []
    headings: list[str] = []
    for line in strategy_md.splitlines():
        stripped = line.rstrip()
        if stripped.startswith("## ") and not stripped.startswith("### "):
            headings.append(stripped)
    return headings


def _coach_proposal_evidence(
    proposals: list[CoachProposal],
) -> list[dict[str, str | None]]:
    """Serialize coach proposals for verifier evidence."""
    return [
        {
            "file": proposal.edit.file,
            "action": proposal.edit.action,
            "section": proposal.edit.section,
            "summary": proposal.edit.summary,
            "content": proposal.edit.content,
            "preview": proposal.preview,
        }
        for proposal in proposals
    ]


def needs_edit_followup(text: str, proposed_edits: int) -> bool:
    """Return whether a coach reply is a review that forgot its edit call.

    The prompt requires every proposed change to carry an ``update_context``
    call. Luna writes the review and drops the call on roughly a quarter of the
    reviews a goal-check trigger forces, which reaches the user as a proposal
    with nothing to accept.

    Args:
        text: The reply that ended the tool loop.
        proposed_edits: ``update_context`` calls made so far this run.

    Returns:
        True when the reply is a review and no edit has been proposed.
    """
    stripped = (text or "").strip()
    return bool(stripped) and stripped.upper() != "SKIP" and proposed_edits == 0


def coach_followup(
    text: str,
    *,
    edits: int,
    challenge_proposed: bool,
    challenge_due: bool,
    already_sent: set[str],
) -> str | None:
    """Return the follow-up prompt to send when the coach stops, or None.

    Each follow-up is sent at most once per run. A due challenge that was not
    proposed comes first — including after a SKIP, which is not allowed then —
    because Luna sometimes writes the push as a time-bound strategy edit. A
    review with no proposal at all is asked for its tool call.

    Args:
        text: The reply that ended the tool loop.
        edits: ``update_context`` calls made so far.
        challenge_proposed: Whether ``propose_challenge`` was called.
        challenge_due: Whether the goal check made a challenge due.
        already_sent: Follow-up prompt names already sent this run.

    Returns:
        A prompt name under ``src/prompts``, or None.
    """
    if (
        challenge_due
        and not challenge_proposed
        and "coach_challenge_followup" not in already_sent
    ):
        return "coach_challenge_followup"
    proposed = edits + (1 if challenge_proposed else 0)
    if "coach_tool_followup" not in already_sent and needs_edit_followup(
        text, proposed
    ):
        return "coach_tool_followup"
    return None


def cmd_coach(
    args: argparse.Namespace,
) -> tuple[CommandResult, list[CoachProposal]]:
    """Generate coaching proposals for strategy.md updates.

    Reviews the week's data against the current strategy (goals + weekly plan
    + diet + sleep) and proposes concrete edits via the ``update_context``
    tool. Returns the validated proposals so the daemon can present them as
    Approve/Reject buttons inside a single bundled Telegram message.

    Args:
        args: Parsed CLI arguments with db, model, week, months, and
            (optionally) recent_nudges and reasoning_effort.

    Returns:
        A tuple of (CommandResult, list_of_proposals). For SKIP, text is
        None and proposals is empty. Otherwise text holds the bundled
        narrative + proposal summaries (also `print()`ed to stdout) and
        proposals contains one CoachProposal per validated edit.
    """
    from context_edit import context_edit_from_tool_call
    from llm_context import context_update_tool
    from tools import execute_run_sql, run_sql_tool

    try:
        context_dir = Path(getattr(args, "context_dir", CONTEXT_DIR))
        context = load_context(context_dir, prompt_file="coach_prompt")
    except FileNotFoundError as e:
        logger.error("%s", e)
        sys.exit(1)

    conn = open_db(Path(args.db))
    week = getattr(args, "week", "current")
    health_data = build_llm_data(conn, getattr(args, "months", 3), week=week)
    if health_data["current_week"]["summary"] is None:
        fail_without_week_data(conn, health_data)

    baselines = compute_baselines(conn)
    milestones = compute_milestones(conn)
    save_baselines(context_dir, baselines)

    week_complete = health_data.get("week_complete", False)
    week_label = health_data.get("week_label")
    context["review_facts"] = build_review_facts(
        {**health_data, "week_label": week_label},
        context,
        week_complete=week_complete,
        unestablished=unestablished_metrics(conn, METRIC_TRUST_WINDOW_DAYS),
    )

    # Computed adherence and the triggers that forbid a SKIP. The coach used
    # to skip every week because it was handed no evidence anything was off.
    today = date.today()
    goal_check = build_goal_check(conn, context.get("strategy"), today=today)
    context["goal_check"] = goal_check.text
    # Code decides when a challenge is due; the model only decides which.
    challenge_open = goal_check.challenge_due
    context["challenge_status"] = challenge_status_text(conn, today=today)
    context["challenge_history"] = describe_history(conn)

    # Inject the live list of strategy.md section headings so the model only
    # proposes replace_section edits against headings that actually exist.
    strategy_sections = _extract_strategy_sections(context.get("strategy", ""))
    if strategy_sections:
        context["strategy_sections"] = "\n".join(f"- `{h}`" for h in strategy_sections)
    else:
        context["strategy_sections"] = "(strategy.md has no level-2 sections)"

    # Cross-message awareness: inject recent nudges
    recent_nudge_entries: list[dict] = getattr(args, "recent_nudges", [])
    context["recent_nudges"] = format_recent_nudges(
        recent_nudge_entries,
        empty_text="(none)",
    )

    health_data_text = render_health_data(
        health_data,
        prompt_kind="coach",
        week=week,
        unestablished=unestablished_metrics(conn, METRIC_TRUST_WINDOW_DAYS),
    )

    try:
        messages = build_messages(
            context,
            health_data_text,
            baselines=baselines,
            milestones=milestones,
            week_complete=week_complete,
            data_maturity=build_data_maturity(conn, context),
        )
    except (KeyError, ValueError) as e:
        logger.error("Failed to render coach_prompt.md template: %s", e)
        sys.exit(1)

    trace_id = create_llm_trace(
        conn,
        "coach",
        metadata={
            "week": week,
            "months": getattr(args, "months", 3),
            "goal_check_triggers": [t.key for t in goal_check.triggers],
        },
    )

    route = route_kwargs(
        "coach",
        getattr(args, "model", None),
        prefs_path=getattr(args, "model_prefs_path", None),
    )
    model = route["model"]
    fallback_models = route.get("fallback_models")
    tools = run_sql_tool() + context_update_tool(allowed_files=["strategy"])
    if challenge_open:
        tools = tools + propose_challenge_tool()
    raw_edits: list[ContextEdit] = []
    challenge_proposal: Proposal | None = None
    known_types = known_activity_types(conn)
    narrative_parts: list[str] = []
    max_iterations = MAX_TOOL_ITERATIONS_COACH
    reasoning_effort = normalize_reasoning_effort(
        getattr(args, "reasoning_effort", "medium")
    )
    if "reasoning_effort" in route:
        reasoning_effort = route["reasoning_effort"]
    temperature = route.get("temperature", 0.7)

    logger.info(
        "Calling %s for coaching review (reasoning=%s) ...",
        model,
        reasoning_effort or "off",
    )
    followups_sent: set[str] = set()
    for iteration in range(max_iterations):
        try:
            result = call_llm(
                messages,
                model=model,
                max_tokens=MAX_TOKENS_COACH,
                temperature=temperature,
                tools=tools,
                reasoning_effort=reasoning_effort,
                fallback_models=fallback_models,
                conn=conn,
                request_type="coach",
                trace_id=trace_id,
                metadata={
                    "week": week,
                    "iteration": iteration,
                    "reasoning_effort": reasoning_effort,
                },
            )
        except Exception as e:
            err_name = type(e).__name__
            if "authentication" in err_name.lower() or "auth" in str(e).lower():
                logger.error(
                    "Authentication failed. Set ANTHROPIC_API_KEY in your .env file."
                )
            else:
                logger.error("LLM call failed: %s: %s", err_name, e)
            sys.exit(1)

        # Capture this iteration's narrative text before we move on.
        iter_text = (result.text or "").strip()
        if iter_text and iter_text != "SKIP":
            narrative_parts.append(iter_text)

        if not result.tool_calls:
            followup = coach_followup(
                iter_text,
                edits=len(raw_edits),
                challenge_proposed=challenge_proposal is not None,
                challenge_due=challenge_open,
                already_sent=followups_sent,
            )
            if followup is not None:
                logger.info("Coach follow-up: %s", followup)
                messages.append(
                    result.raw_message or {"role": "assistant", "content": iter_text}
                )
                messages.append({"role": "user", "content": load_prompt_text(followup)})
                followups_sent.add(followup)
                continue
            break

        messages.append(result.raw_message)
        for tc in result.tool_calls:
            fn_name = tc.function.name
            raw_args = tc.function.arguments
            try:
                args_dict = (
                    json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                )
            except (ValueError, json.JSONDecodeError):
                args_dict = {}

            if fn_name == "update_context":
                edit = context_edit_from_tool_call(tc)
                if edit:
                    raw_edits.append(edit)
                    tool_result = "Proposed. User will be asked to confirm."
                else:
                    tool_result = (
                        "Not proposed: invalid context update. Check the "
                        "update_context schema, target section, and compact "
                        "log-entry rules before retrying."
                    )
            elif fn_name == "propose_challenge" and challenge_open:
                if challenge_proposal is not None:
                    tool_result = "Not proposed: one challenge per review."
                else:
                    try:
                        challenge_proposal = coerce_proposal(args_dict, known_types)
                        tool_result = PROPOSAL_NOTED
                    except ChallengeError as exc:
                        tool_result = f"Not proposed: {exc}."
            elif fn_name == "run_sql":
                logger.info("Coach SQL: %s", args_dict.get("query", "")[:200])
                tool_result = execute_run_sql(Path(args.db), args_dict)
            else:
                tool_result = json.dumps({"error": f"Unknown tool: {fn_name}"})

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                }
            )

    # If the iteration cap was reached while the model still wanted to call
    # tools, force one final tool-less synthesis call.
    if not (result.text or "").strip() and result.tool_calls:
        logger.warning(
            "Coach loop hit iteration cap with pending tool_calls; forcing final synthesis"
        )
        messages.append(result.raw_message)
        for tc in result.tool_calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(
                        {"error": load_prompt_text("tool_budget_synthesize")}
                    ),
                }
            )
        try:
            result = call_llm(
                messages,
                model=model,
                max_tokens=MAX_TOKENS_COACH,
                temperature=temperature,
                tools=None,
                reasoning_effort=reasoning_effort,
                fallback_models=fallback_models,
                conn=conn,
                request_type="coach",
                trace_id=trace_id,
                metadata={
                    "week": week,
                    "iteration": "final_synthesis",
                    "reasoning_effort": reasoning_effort,
                },
            )
        except Exception as e:
            logger.error("Coach final synthesis call failed: %s", e)
            sys.exit(1)
        synthesis_text = (result.text or "").strip()
        if synthesis_text and synthesis_text != "SKIP":
            narrative_parts.append(synthesis_text)

    narrative = "\n\n".join(part for part in narrative_parts if part).strip()

    # Validate edits against the current strategy.md.
    proposals: list[CoachProposal] = []
    for edit in raw_edits:
        try:
            preview = build_edit_preview(context_dir, edit, strict=True)
        except EditPreviewError as exc:
            logger.warning(
                "Dropping invalid coach edit for %s.md (section=%r): %s",
                edit.file,
                edit.section,
                exc,
            )
            continue
        proposals.append(CoachProposal(edit=edit, preview=preview))

    if not proposals and not narrative and challenge_proposal is None:
        logger.info("Coach returned SKIP — no strategy changes warranted")
        return (
            CommandResult(text=None, llm_call_id=result.llm_call_id),
            [],
        )

    # Protocol violation fallback: edits but no narrative.
    if not narrative and (proposals or challenge_proposal):
        logger.warning(
            "Coach returned %d edit(s) with empty narrative — "
            "prompt compliance failure; sending fallback wrapper",
            len(proposals),
        )
        narrative = (
            "Proposals from this week's data "
            "(rationale missing — review them carefully)."
        )

    bundled_text = _format_coach_bundle(narrative, proposals, challenge_proposal)
    verified_text = apply_verification(
        kind="coach",
        draft=bundled_text,
        evidence={
            "health_data_text": health_data_text,
            "review_facts": context.get("review_facts"),
            "baselines": baselines,
            "milestones": milestones,
            "week_complete": week_complete,
            "week_label": week_label,
            "strategy_sections": strategy_sections,
            "recent_nudges_text": context.get("recent_nudges"),
            "coach_feedback": context.get("coach_feedback"),
            "proposals": _coach_proposal_evidence(proposals),
            "challenge": _challenge_evidence(challenge_proposal),
            "tool_calls": extract_tool_evidence(messages),
        },
        source_messages=slim_source_messages(messages, bundled_text),
        conn=conn,
        metadata={
            "source_llm_call_id": result.llm_call_id,
            "week": week,
            "week_label": week_label,
            "proposal_count": len(proposals),
        },
        trace_id=trace_id,
        strict=True,
        model_prefs_path=getattr(args, "model_prefs_path", None),
    )
    if verified_text is None or verified_text.strip().upper() == "SKIP":
        logger.info("Coach verification suppressed the review/proposals")
        return (
            CommandResult(text=None, llm_call_id=result.llm_call_id),
            [],
        )
    bundled_text = verified_text
    print(bundled_text)
    if challenge_proposal is not None:
        try:
            propose(conn, challenge_proposal, llm_call_id=result.llm_call_id)
        except ChallengeError as exc:
            logger.warning("Coach challenge not stored: %s", exc)
    # Only a delivered review raises a trigger; a skip leaves it to fire again.
    record_triggers(
        conn, goal_check.triggers, today=today, llm_call_id=result.llm_call_id
    )

    cmd_result = CommandResult(
        text=bundled_text,
        llm_call_id=result.llm_call_id,
    )
    return cmd_result, proposals


def _challenge_evidence(proposal: Proposal | None) -> dict | None:
    """Serialize a challenge proposal for verifier evidence."""
    if proposal is None:
        return None
    return {
        "spec": describe_challenge(proposal),
        "title": proposal.title,
        "goal": proposal.goal,
        "rationale": proposal.rationale,
    }


def _format_coach_bundle(
    narrative: str,
    proposals: list[CoachProposal],
    challenge: Proposal | None = None,
) -> str:
    """Render the consolidated coach review for stdout / Telegram."""
    if not proposals and challenge is None:
        return narrative
    parts: list[str] = []
    if narrative:
        parts.append(narrative)
    parts.append("──────────────")
    for i, proposal in enumerate(proposals, start=1):
        block = (
            f"📋 **Proposed change {i}** — {proposal.edit.file}.md "
            f"_({proposal.edit.section or proposal.edit.action})_\n"
            f"{proposal.edit.summary}\n\n"
            f"```diff\n{proposal.preview}\n```"
        )
        parts.append(block)
    if challenge is not None:
        # The single place a challenge is described: one line, with the reason
        # and the goal it serves folded into an expandable quote.
        lines = [f"🎯 **Challenge:** {describe_challenge(challenge)}"]
        if challenge.rationale:
            lines.append(f">> {challenge.rationale}")
        lines.append(f">> Serves: {challenge.goal}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
