"""LangGraph wiring for the master-router architecture.

Topology (per engagement):

    START
      \u2192 init
      \u2192 master_plan       (LLM: pick next phase + briefing)
            \u2192 END if briefing.done
            \u2192 plan             (planner emits SpecialistPlan, logs proposal)
                  \u2192 evaluate     (LLM critique)
                        \u2192 retry  (planner_attempts < max) \u2192 plan
                        \u2192 accept \u2192 master_review
                        \u2192 escalate \u2192 master_plan  (phase abandoned)
                  \u2190 master_review (LLM: commit or revise)
                        \u2192 revise (revise_budget > 0)     \u2192 plan
                        \u2192 commit                          \u2192 push
            \u2190 push          (BAS calls + memory update + artifacts)
                  \u2192 master_plan (next phase)

Hard caps per phase:
    * master revisions       : max_master_revisions (default 1 \u2192 2 total master decisions)
    * planner attempts       : max_planner_attempts (default 3)
    * planner tool calls     : max_planner_tool_calls (default 20)

Every plan emitted is logged into ``state["proposal_log"]`` with the full
``command_template`` strings of every stage so the audit trail captures the
exact tradecraft the planner proposed at each iteration.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..agents.evaluator import EvaluatorPolicy, EvaluatorVerdict, StaticAcceptEvaluator
from ..agents.master import (
    LLMMasterRouter,
    MasterDecision,
    MasterPolicy,
    MemoryUpdate,
    PhaseBriefing,
    StaticMasterRouter,
)
from ..agents.specialist import (
    Planner,
    PlanResult,
    PushResult,
    SpecialistPlan,
    plan_specialist,
    push_specialist,
)
from ..client import BasClient
from ..persistence import ArtifactStore
from ..phases import resolve_phases_to_skills, first_skill_for_phase, skills_for_phase
from ..results import (
    OperationResult,
    build_structural_summary,
    detect_issues,
    parse_operation_result,
    derive_phase_done,
)
from ..tools.skill_tool import SkillTool
from .memory import (
    CampaignMemory,
    _deep_merge,
    _merge_lists,
    project_for_prompt,
    persist_memory,
    load_memory_from_disk,
)
from .state import (
    DONE_SENTINEL, 
    PhaseRecord, 
    SessionState, 
    StageResult,
    phase_history_compact,
    skill_data_from_push,
    consolidate_phase_record,
    log_proposal,
    build_phase_asset_map,
)
from .utils import _now, _log_step

logger = logging.getLogger(__name__)


def _init_node(state: SessionState) -> dict[str, Any]:
    """Seed defaults exactly once per run."""
    run_id = state.get("run_id") or uuid.uuid4().hex
    phases = list(state.get("available_phases") or [])
    foothold_keys = list((state.get("foothold") or {}).keys())
    _log_step("INIT", f"run_id={run_id} phases={phases} foothold_keys={foothold_keys}")

    # Prefer state memory; fall back to disk snapshot if empty (crash recovery).
    memory = state.get("memory", {}) or {}
    completed_phases = list(state.get("completed_phases") or [])
    phase_history = list(state.get("phase_history") or [])
    current_phase = state.get("current_phase", "") or ""

    if not memory:
        snapshot = load_memory_from_disk(state.get("results_dir"))
        if snapshot:
            memory = snapshot.get("memory", {})
            progress = snapshot.get("campaign_progress", {})
            # Restore phase tracking if state doesn't have it
            if not completed_phases:
                completed_phases = list(progress.get("completed_phases") or [])
            if not phase_history:
                phase_history = list(progress.get("phase_history") or [])
            if not current_phase:
                current_phase = progress.get("current_phase", "")
            _log_step(
                "INIT",
                f"recovered from disk — completed={completed_phases} "
                f"current={current_phase!r} memory_keys={len(memory)}",
            )

    return {
        "run_id": run_id,
        "foothold": state.get("foothold", {}) or {},
        "kali_sidecar": state.get("kali_sidecar", {}) or {},
        "safety": state.get("safety", {}) or {},
        "memory": memory,
        "completed_stages": state.get("completed_stages", []) or [],
        "stage_results": state.get("stage_results", []) or [],
        # Always reset iteration and master_done so re-running on the same
        # LangGraph thread (e.g. in Studio) doesn't carry over stale state
        # that would cause the graph to terminate immediately.
        "iteration": 0,
        "max_iterations": int(state.get("max_iterations", 20) or 20),
        "log": state.get("log", []) or [],
        # master
        "available_phases": list(state.get("available_phases") or []),
        "completed_phases": completed_phases,
        "current_phase": current_phase,
        "master_briefing": state.get("master_briefing"),
        "master_revisions_used": int(state.get("master_revisions_used", 0) or 0),
        "max_master_revisions": int(state.get("max_master_revisions", 1) or 1),
        "master_revision_feedback": state.get("master_revision_feedback", "") or "",
        "master_done": False,  # always reset — never inherit from thread state
        # planner inner loop
        "planner_attempts": 0,
        "max_planner_attempts": int(state.get("max_planner_attempts", 3) or 3),
        "planner_tool_calls": 0,
        "max_planner_tool_calls": int(state.get("max_planner_tool_calls", 20) or 20),
        # proposals + plan state
        "proposal_log": list(state.get("proposal_log") or []),
        "phase_history": phase_history,
        "phase_skills": list(state.get("phase_skills") or []),
        "phase_skill_index": int(state.get("phase_skill_index", 0) or 0),
        "phase_skills_buffer": [],
        "current_plan": None,
        "current_plan_summary": [],
        "current_plan_error": None,
        "current_provider_id": None,
        "route_hint": None,
        "blocked_reason": None,
        "required_ack": None,
        "risk_level": "moderate",
        "last_evaluator_verdict": {},
        "phase_done": False,
        "feedback": "",
        "evaluator_action": "",
        "retry_same_phase": False,
        "issues_to_fix": [],
        "retry_feedback": [],
        "execution_summary": None,
    }


def _make_master_plan_node(master: MasterPolicy, skill_tool: SkillTool):
    """Master node — dual mode.

    REVIEW mode: an evaluator-finished plan is in state (``evaluator_action``
        is one of accept/escalate/retry-exhausted). The master reviews it and
        either commits or asks for one revise. Budget = ``max_master_revisions``.

    PICK mode: no pending plan. The master selects the next phase from
        ``available_phases`` and emits a ``PhaseBriefing`` for the planner.

    The evaluator never speaks to this node directly — it writes its verdict
    to state and the conditional edge routes here. From the master's view,
    the planner is handing up an approved (or failed) plan.
    """

    def master_plan_node(state: SessionState) -> dict[str, Any]:
        log = list(state.get("log") or [])
        eval_action = state.get("evaluator_action") or ""

        # -------------------- REVIEW mode --------------------
        if eval_action in ("accept", "escalate", "retry-exhausted"):
            return _master_review(state, master, log)

        # -------------------- PICK mode ----------------------
        return _master_pick_phase(state, master, skill_tool, log)

    return master_plan_node


def _master_review(
    state: SessionState, master: MasterPolicy, log: list[str]
) -> dict[str, Any]:
    phase = state.get("current_phase") or ""
    used = int(state.get("master_revisions_used", 0))
    max_rev = int(state.get("max_master_revisions", 1))
    eval_action = (state.get("last_evaluator_verdict") or {}).get("action", "")
    _log_step(
        "MASTER/REVIEW",
        f"phase={phase!r} evaluator={eval_action!r} "
        f"revise_budget={max(0, max_rev - used)}/{max_rev}",
    )
    revise_budget = max(0, max_rev - used)
    plan_summary = state.get("current_plan_summary") or []
    briefing = PhaseBriefing.model_validate(
        state.get("master_briefing") or {"phase": phase}
    )
    eval_verdict = state.get("last_evaluator_verdict") or {}

    proposals = log_proposal(
        state,
        audience="master",
        phase=phase,
        attempt=int(state.get("planner_attempts", 0)),
        plan_summary=plan_summary,
        note=f"evaluator={eval_verdict.get('action')}",
    )

    try:
        decision: MasterDecision = master.review_plan(
            briefing=briefing,
            plan_summary=plan_summary,
            memory=project_for_prompt(state.get("memory", {}) or {}),
            evaluator_verdict=eval_verdict,
            revise_budget=revise_budget,
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"[master_plan/review] master crashed: {exc!r}; committing"
        log.append(msg)
        logger.exception(msg)
        decision = MasterDecision(action="commit", confidence=0.0)

    if decision.action == "revise" and revise_budget > 0:
        msg = (
            f"[master_plan/review] phase={phase} action=revise "
            f"({used + 1}/{max_rev}) missing={decision.missing}"
        )
        log.append(msg)
        _log_step("MASTER/REVIEW", f"→ REVISE  ({used + 1}/{max_rev})  missing={decision.missing}", level="warning")
        return {
            "master_revision_feedback": decision.comments
            or "Master requires another pass; address `missing` items.",
            "master_revisions_used": used + 1,
            "planner_attempts": 0,  # reset inner loop for the revised round
            "feedback": "",
            "evaluator_action": "master_revise",
            "proposal_log": proposals,
            "log": log,
        }

    action_label = "commit" if decision.action == "commit" else "commit-forced"
    msg = (
        f"[master_plan/review] phase={phase} action={action_label} "
        f"confidence={decision.confidence:.2f}"
    )
    log.append(msg)
    _log_step("MASTER/REVIEW", f"→ {action_label.upper()}  confidence={decision.confidence:.2f}")
    return {
        "evaluator_action": "commit",
        "master_revision_feedback": "",
        "proposal_log": proposals,
        "log": log,
    }


def _master_pick_phase(
    state: SessionState,
    master: MasterPolicy,
    skill_tool: SkillTool,
    log: list[str],
) -> dict[str, Any]:
    available = list(state.get("available_phases") or [])
    completed = list(state.get("completed_phases") or [])
    attempt = state.get("iteration", 0) + 1

    _log_step(
        "MASTER/PICK",
        f"iter={attempt}  available={available}  completed={completed}",
    )

    if attempt > int(state.get("max_iterations", 20)):
        msg = "[master_plan/pick] max_iterations exhausted; emitting DONE"
        log.append(msg)
        _log_step("MASTER/PICK", "→ DONE (max_iterations exhausted)", level="warning")
        return {"master_done": True, "iteration": attempt, "log": log}

    try:
        briefing: PhaseBriefing = master.plan_phase(
            foothold=state.get("foothold", {}) or {},
            memory=project_for_prompt(state.get("memory", {}) or {}),
            available_phases=available,
            completed_phases=completed,
            phase_history=list(state.get("phase_history") or []),
            attempt=attempt,
            execution_summary=state.get("execution_summary"),
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"[master_plan/pick] master crashed: {exc!r}; halting"
        log.append(msg)
        logger.exception(msg)
        _log_step("MASTER/PICK", f"→ DONE (master crashed: {exc!r})", level="error")
        return {"master_done": True, "iteration": attempt, "log": log}

    if briefing.done or not briefing.phase:
        # Defensive guard: if the LLM says done but there are still
        # uncompleted phases, override and pick the first remaining one.
        remaining = [p for p in available if p not in completed]
        if remaining:
            override_phase = remaining[0]
            msg = (
                f"[master_plan/pick] master signalled done but {len(remaining)} "
                f"phases remain ({remaining}); overriding → {override_phase!r}"
            )
            log.append(msg)
            _log_step(
                "MASTER/PICK",
                f"→ OVERRIDE done signal — {len(remaining)} phases remain, "
                f"forcing {override_phase!r}",
                level="warning",
            )
            briefing = PhaseBriefing(
                phase=override_phase,
                objective=f"Continue campaign: execute {override_phase} phase.",
                constraints=briefing.constraints,
                open_questions=briefing.open_questions,
                rationale=f"Overridden — master signalled done prematurely with {remaining} phases remaining.",
                done=False,
            )
        else:
            msg = "[master_plan/pick] master signalled done; halting"
            log.append(msg)
            _log_step("MASTER/PICK", "→ DONE (master signalled done)")
            return {"master_done": True, "iteration": attempt, "log": log}

    if available and briefing.phase not in available:
        msg = (
            f"[master_plan/pick] master picked {briefing.phase!r} but it is not "
            f"in available_phases={available}; halting"
        )
        log.append(msg)
        _log_step("MASTER/PICK", f"→ DONE (phase {briefing.phase!r} not in available_phases)", level="warning")
        return {"master_done": True, "iteration": attempt, "log": log}

    if briefing.phase in completed:
        msg = (
            f"[master_plan/pick] master re-picked completed phase "
            f"{briefing.phase!r}; halting"
        )
        log.append(msg)
        _log_step("MASTER/PICK", f"→ DONE (phase {briefing.phase!r} already completed)", level="warning")
        return {"master_done": True, "iteration": attempt, "log": log}

    # ---- Per-phase retry cap (max 2 retries) --------------------------------
    MAX_PHASE_RETRIES = 2
    if briefing.retry_same_phase:
        phase_attempts = sum(
            1 for h in (state.get("phase_history") or [])
            if h.get("phase") == briefing.phase and h.get("execution_outcome")
        )
        if phase_attempts >= MAX_PHASE_RETRIES:
            msg = (
                f"[master_plan/pick] phase {briefing.phase!r} already retried "
                f"{phase_attempts} times; moving on"
            )
            log.append(msg)
            _log_step(
                "MASTER/PICK",
                f"→ SKIP RETRY (phase {briefing.phase!r} retried {phase_attempts}x)",
                level="warning",
            )
            # Force the phase into completed so master picks next
            return {
                "completed_phases": completed + [briefing.phase],
                "retry_same_phase": False,
                "issues_to_fix": [],
                "master_done": False,
                "iteration": attempt,
                "log": log,
            }

    next_skill = first_skill_for_phase(briefing.phase, skill_tool)
    if next_skill is None:
        # No skill registered — route directly to push (which skips gracefully)
        # instead of wasting a plan→evaluate→escalate→push-skip loop.
        msg = (
            f"[master_plan/pick] no skill registered for phase "
            f"{briefing.phase!r}; routing to skip"
        )
        log.append(msg)
        _log_step("MASTER/PICK", f"→ SKIP phase={briefing.phase!r} (no registered skill)", level="warning")
        return {
            "current_phase": briefing.phase,
            "master_briefing": briefing.model_dump(),
            "completed_phases": completed + [briefing.phase],
            "current_plan": None,
            "master_done": False,           # explicit: more phases may remain
            "evaluator_action": "commit",   # push_node's skip path handles this
            "phase_skills": [],
            "phase_skill_index": 0,
            "phase_skills_buffer": [],
            "iteration": attempt,
            "log": log,
        }

    all_skills = skills_for_phase(briefing.phase, skill_tool)
    msg = (
        f"[master_plan/pick] phase={briefing.phase} skills={all_skills} "
        f"objective={briefing.objective!r}"
    )
    log.append(msg)
    _log_step(
        "MASTER/PICK",
        f"→ phase={briefing.phase!r}  skills={all_skills}  "
        f"objective={briefing.objective!r}",
    )
    return {
        "current_phase": briefing.phase,
        "next_stage": next_skill,
        "phase_skills": all_skills,
        "phase_skill_index": 0,
        "phase_skills_buffer": [],      # clear buffer for the new phase
        "master_briefing": briefing.model_dump(),
        "master_revisions_used": 0,
        "master_revision_feedback": "",
        "master_done": False,           # explicit: we just picked a phase
        "retry_same_phase": briefing.retry_same_phase,
        "issues_to_fix": briefing.issues_to_fix,
        "execution_summary": None,      # consumed by this PICK; clear for next phase
        "planner_attempts": 0,
        "planner_tool_calls": 0,
        "current_plan": None,
        "current_plan_summary": [],
        "current_plan_error": None,
        "evaluator_action": "",
        "feedback": "",
        "iteration": attempt,
        "log": log,
    }


def _make_plan_node(planner: Planner, skill_tool: SkillTool):
    """Planner crafts a SpecialistPlan. No BAS calls.

    Builds a combined feedback string: master revision feedback (highest
    priority) + evaluator feedback (from a previous retry). Increments the
    tool-call counter and refuses to invoke the LLM if the per-phase budget
    is exhausted."""

    def plan_node(state: SessionState) -> dict[str, Any]:
        log = list(state.get("log") or [])
        phase = state.get("current_phase") or ""
        eval_action = state.get("evaluator_action") or ""

        # Hand-up mode: the evaluator finished (accept/escalate/retry-exhausted).
        # The planner is the only node that talks to the master, so it forwards
        # the existing plan + verdict upwards without invoking the LLM again.
        if eval_action in ("accept", "escalate", "retry-exhausted"):
            _log_step(
                "PLAN/FORWARD",
                f"phase={phase!r}  evaluator={eval_action!r} → handing to master",
            )
            msg = (
                f"[plan] phase={phase} forwarding plan to master "
                f"(evaluator={eval_action})"
            )
            log.append(msg)
            logger.info(msg)
            return {"log": log}

        attempt = int(state.get("planner_attempts", 0)) + 1
        max_attempts = int(state.get("max_planner_attempts", 3))
        _log_step(
            "PLAN",
            f"phase={phase!r}  attempt={attempt}/{max_attempts}  "
            f"skill={state.get('next_stage')!r}",
        )
        tool_calls = int(state.get("planner_tool_calls", 0))
        max_tool_calls = int(state.get("max_planner_tool_calls", 20))

        if tool_calls >= max_tool_calls:
            msg = (
                f"[plan] phase={phase} attempt={attempt} tool-call budget "
                f"exhausted ({tool_calls}/{max_tool_calls}); aborting plan"
            )
            log.append(msg)
            logger.warning(msg)
            return {
                "current_plan_error": "planner_tool_budget_exhausted",
                "planner_attempts": attempt,
                "log": log,
            }

        # Compose feedback: master revision wins; evaluator retry feedback is
        # additive when both exist.
        master_fb = (state.get("master_revision_feedback") or "").strip()
        eval_fb = (state.get("feedback") or "").strip()
        parts: list[str] = []
        if master_fb:
            parts.append("MASTER ROUTER REVISION (must obey):\n" + master_fb)
        if eval_fb:
            parts.append("EVALUATOR RETRY FEEDBACK:\n" + eval_fb)
        feedback = "\n\n".join(parts) or None

        briefing = state.get("master_briefing") or {}
        # Inject the master's briefing and a COMPACT phase history into memory
        # so the planner sees full campaign context without blowing the context
        # window (full history can have many long key_command strings).
        # Project memory (drop pending.*, cap narratives) before enriching with
        # the briefing + compact phase history so the planner prompt stays bounded.
        memory_with_brief = project_for_prompt(state.get("memory") or {})
        memory_with_brief["_master_briefing"] = briefing
        memory_with_brief["_phase_history"] = phase_history_compact(
            list(state.get("phase_history") or [])
        )

        plan_state: SessionState = dict(state)  # type: ignore[assignment]
        plan_state["memory"] = memory_with_brief

        planned: PlanResult = plan_specialist(
            plan_state, planner=planner, skill_tool=skill_tool, feedback=feedback
        )
        proposals = log_proposal(
            state,
            audience="evaluator",
            phase=phase,
            attempt=attempt,
            plan_summary=planned.plan_summary,
            note=(planned.error or ""),
        )

        msg = (
            f"[plan] phase={phase} attempt={attempt}/{max_attempts} "
            f"success={planned.success} abilities={len(planned.plan_summary)} "
            f"tool_calls={tool_calls + 1}/{max_tool_calls}"
            + (f" error={planned.error!r}" if planned.error else "")
        )
        log.append(msg)
        (logger.info if planned.success else logger.error)(msg)
        _log_step(
            "PLAN",
            f"→ success={planned.success}  abilities={len(planned.plan_summary)}  "
            f"tool_calls={tool_calls + 1}/{max_tool_calls}"
            + (f"  error={planned.error!r}" if planned.error else ""),
            level="info" if planned.success else "error",
        )

        # Only overwrite current_plan when we actually produced one. Preserving
        # a previously-accepted plan across a failed revise round means a
        # forced commit (revise_budget=0) can still push something coherent
        # instead of dropping the engagement on the floor.
        result: dict[str, Any] = {
            "current_provider_id": planned.provider_id,
            "current_plan_error": planned.error,
            "planner_attempts": attempt,
            "planner_tool_calls": tool_calls + 1,
            "proposal_log": proposals,
            "log": log,
        }
        if planned.plan is not None:
            result["current_plan"] = planned.plan.model_dump(mode="json")
            result["current_plan_summary"] = planned.plan_summary
            result["route_hint"] = planned.plan.route_hint
            result["blocked_reason"] = planned.plan.blocked_reason
            result["required_ack"] = planned.plan.required_ack
            result["risk_level"] = planned.plan.risk_level
        elif state.get("current_plan"):
            # Failure but a prior plan exists; flag this in the human log so
            # the audit trail shows the fallback is intentional.
            fallback_msg = (
                f"[plan] phase={phase} attempt={attempt} failed; keeping "
                f"prior accepted plan as fallback (abilities="
                f"{len(state.get('current_plan_summary') or [])})"
            )
            log.append(fallback_msg)
            logger.warning(fallback_msg)
            result["log"] = log
        return result

    return plan_node


def _make_evaluate_node(evaluator: EvaluatorPolicy, skill_tool: SkillTool):
    def evaluate_node(state: SessionState) -> dict[str, Any]:
        log = list(state.get("log") or [])
        phase = state.get("current_phase") or ""
        skill_name = state.get("next_stage")
        attempt = int(state.get("planner_attempts", 0))
        max_attempts = int(state.get("max_planner_attempts", 3))

        _log_step(
            "EVALUATE",
            f"phase={phase!r}  skill={skill_name!r}  attempt={attempt}/{max_attempts}",
        )

        if state.get("current_plan_error") or not state.get("current_plan"):
            # planner failed. Retry inside budget; otherwise escalate to master.
            if attempt < max_attempts:
                msg = (
                    f"[evaluate] phase={phase} planner failed "
                    f"({state.get('current_plan_error')!r}); retrying"
                )
                log.append(msg)
                logger.warning(msg)
                return {
                    "feedback": state.get("current_plan_error") or "planner failed",
                    "evaluator_action": "retry",
                    "log": log,
                }
            msg = (
                f"[evaluate] phase={phase} planner failed and attempts exhausted; "
                f"escalating to master"
            )
            log.append(msg)
            logger.error(msg)
            return {
                "evaluator_action": "escalate",
                "last_evaluator_verdict": {
                    "action": "escalate",
                    "feedback": state.get("current_plan_error") or "",
                    "mismatches": ["planner failed"],
                    "phase_done": False,
                    "confidence": 0.0,
                },
                "log": log,
            }

        plan_summary = state.get("current_plan_summary") or []
        try:
            verdict: EvaluatorVerdict = evaluator.evaluate(
                skill=skill_tool.read(skill_name),
                foothold=state.get("foothold", {}) or {},
                memory=project_for_prompt(state.get("memory", {}) or {}),
                completed_stages=list(state.get("completed_stages") or []),
                plan_summary=plan_summary,
                push_success=False,
                push_error=None,
                attempt=attempt,
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"[evaluate] phase={phase} evaluator crashed: {exc!r}; accepting"
            log.append(msg)
            logger.exception(msg)
            verdict = EvaluatorVerdict(action="accept", confidence=0.0)

        if verdict.action == "retry" and attempt < max_attempts:
            msg = (
                f"[evaluate] phase={phase} action=retry attempt={attempt}/"
                f"{max_attempts} mismatches={verdict.mismatches}"
            )
            log.append(msg)
            _log_step("EVALUATE", f"→ RETRY  mismatches={verdict.mismatches}", level="warning")
            return {
                "feedback": verdict.feedback,
                "evaluator_action": "retry",
                "last_evaluator_verdict": verdict.model_dump(),
                "log": log,
            }

        if verdict.action == "escalate":
            action_label = "escalate"
        elif verdict.action == "retry":
            action_label = "retry-exhausted"  # treat as escalate downstream
        else:
            action_label = "accept"

        msg = (
            f"[evaluate] phase={phase} action={action_label} "
            f"phase_done={verdict.phase_done} confidence={verdict.confidence:.2f} "
            f"mismatches={verdict.mismatches}"
        )
        log.append(msg)
        _log_step(
            "EVALUATE",
            f"→ {action_label.upper()}  confidence={verdict.confidence:.2f}  "
            f"phase_done={verdict.phase_done}  mismatches={verdict.mismatches}",
            level="info" if action_label == "accept" else "warning",
        )
        return {
            "feedback": "",
            "evaluator_action": action_label,
            "last_evaluator_verdict": verdict.model_dump(),
            "phase_done": bool(verdict.phase_done),
            "route_hint": verdict.route_hint or state.get("route_hint"),
            "blocked_reason": verdict.blocked_reason or state.get("blocked_reason"),
            "required_ack": verdict.required_ack or state.get("required_ack"),
            "risk_level": verdict.risk_level or state.get("risk_level") or "moderate",
            "log": log,
        }

    return evaluate_node


def _make_push_node(
    master: MasterPolicy,
    skill_tool: SkillTool,
    bas: BasClient,
    artifacts: ArtifactStore | None,
    planner: Planner | None = None,
):
    """Push the committed plan and update master memory."""

    def _missing_required_ack(state: SessionState, skill_name: str) -> str | None:
        safety = state.get("safety") or {}
        acked = {str(x).strip().lower() for x in (safety.get("acks") or [])}
        required: set[str] = set()

        explicit = state.get("required_ack")
        if explicit:
            required.add(str(explicit).strip().lower())

        if (state.get("risk_level") or "").lower() == "destructive":
            required.add("destructive")

        if skill_name and skill_tool.has(skill_name):
            skill = skill_tool.read(skill_name)
            budget = skill.frontmatter.budget
            destructive_default = None
            if budget is not None:
                destructive_default = (budget.model_extra or {}).get("destructive_default")
            if destructive_default == "ask":
                required.add(skill.frontmatter.stage or skill_name)

        for token in sorted(required):
            if token and token not in acked:
                return token
        return None

    def push_node(state: SessionState) -> dict[str, Any]:
        log = list(state.get("log") or [])
        phase = state.get("current_phase") or ""
        skill_name = state.get("next_stage") or ""
        raw_plan = state.get("current_plan")

        _log_step("PUSH", f"phase={phase!r}  skill={skill_name!r}")

        # No plan to push (planner failed or evaluator escalated and master
        # committed anyway). Still advance the phase so the master picks a new
        # one next iteration; do NOT touch BAS.
        if not raw_plan or not skill_name:
            completed_phases = list(state.get("completed_phases") or [])
            if phase and phase not in completed_phases:
                completed_phases.append(phase)
            msg = (
                f"[push] phase={phase} skipped (no plan to commit; "
                f"evaluator={(state.get('last_evaluator_verdict') or {}).get('action')})"
            )
            log.append(msg)
            _log_step("PUSH", f"→ SKIP  phase={phase!r}  (no plan)", level="warning")
            # Record a skipped phase in history so the master knows what happened.
            history = list(state.get("phase_history") or [])
            briefing_obj = PhaseBriefing.model_validate(
                state.get("master_briefing") or {"phase": phase}
            )
            history.append(PhaseRecord(
                phase=phase,
                objective=briefing_obj.objective,
                outcome="skipped",
                master_revisions=int(state.get("master_revisions_used", 0)),
                planner_attempts=int(state.get("planner_attempts", 0)),
            ).model_dump(mode="json"))
            return {
                "completed_phases": completed_phases,
                "phase_history": history,
                # reset multi-skill tracking so next phase starts clean
                "phase_skills": [],
                "phase_skill_index": 0,
                "phase_skills_buffer": [],
                "current_plan": None,
                "current_plan_summary": [],
                "current_plan_error": None,
                "master_revision_feedback": "",
                "evaluator_action": "",
                "feedback": "",
                "log": log,
            }

        plan = SpecialistPlan.model_validate(raw_plan)

        missing_ack = _missing_required_ack(state, skill_name)
        if state.get("blocked_reason") or missing_ack:
            completed_phases = list(state.get("completed_phases") or [])
            if phase and phase not in completed_phases:
                completed_phases.append(phase)
            reason = state.get("blocked_reason") or f"missing required ACK token: {missing_ack}"
            msg = f"[push] phase={phase} blocked by safety gate: {reason}"
            log.append(msg)
            _log_step("PUSH", f"→ BLOCKED  phase={phase!r}  reason={reason}", level="warning")
            history = list(state.get("phase_history") or [])
            briefing_obj = PhaseBriefing.model_validate(
                state.get("master_briefing") or {"phase": phase}
            )
            history.append(PhaseRecord(
                phase=phase,
                objective=briefing_obj.objective,
                skills_used=[skill_name] if skill_name else [],
                outcome="blocked",
                master_revisions=int(state.get("master_revisions_used", 0)),
                planner_attempts=int(state.get("planner_attempts", 0)),
            ).model_dump(mode="json"))
            return {
                "completed_phases": completed_phases,
                "phase_history": history,
                "current_plan": None,
                "current_plan_summary": [],
                "current_plan_error": None,
                "blocked_reason": reason,
                "required_ack": missing_ack or state.get("required_ack"),
                "evaluator_action": "",
                "feedback": "",
                "log": log,
            }

        # ---- retry handling ------------------------------------------------
        # A retry (master set retry_same_phase) re-pushes the CORRECTED plan as a
        # fresh operation rather than patching the prior operation's stages via
        # /ai/operation-feedback. The old feedback path could not map a reshaped
        # retry plan back onto the original stage_ids (it silently sent zero
        # corrections); re-pushing a clean operation always applies the fix. The
        # `retry_same_phase` / `issues_to_fix` flags are consumed in the
        # phase-complete return below.
        push: PushResult = push_specialist(
            state,
            plan=plan,
            skill_tool=skill_tool,
            bas=bas,
            artifacts=artifacts,
            provider_id=state.get("current_provider_id"),
            catalog=getattr(planner, "_catalog", None),
        )

        results = list(state.get("stage_results") or [])
        results.append(
            StageResult(
                skill=skill_name,
                success=push.success,
                abilities_pushed=len(push.ability_ids),
                adversary_id=push.adversary_id,
                notes=push.error or push.rationale or "",
                extras={
                    "phase": phase,
                    "ability_ids": push.ability_ids,
                    "stage_ids": push.stage_ids,
                    "linked_ability_ids": push.linked_ability_ids,
                    "provider_id": push.provider_id,
                    "plan_summary": push.plan_summary,
                    "phase_done": state.get("phase_done", False),
                    "master_revisions_used": state.get("master_revisions_used", 0),
                    "planner_attempts": state.get("planner_attempts", 0),
                },
            )
        )

        completed_stages = list(state.get("completed_stages") or [])
        mem_update = MemoryUpdate(facts={}, narrative="")
        new_memory = dict(state.get("memory") or {})

        msg = (
            f"[push] phase={phase} skill={skill_name} success={push.success} "
            f"adversary={push.adversary_id} abilities={len(push.ability_ids)} "
            "memory=pending-only"
            + (f" error={push.error!r}" if push.error else "")
        )
        log.append(msg)
        _log_step(
            "PUSH",
            f"→ success={push.success}  adversary={push.adversary_id}  "
            f"abilities={len(push.ability_ids)}  "
            "memory=pending-only"
            + (f"  error={push.error!r}" if push.error else ""),
            level="info" if push.success else "error",
        )

        # ---- accumulate per-skill data in buffer ----------------------------
        skill_data = skill_data_from_push(skill_name, push, mem_update)
        skills_buffer = list(state.get("phase_skills_buffer") or [])
        skills_buffer.append(skill_data)

        # ---- multi-skill: advance to next skill or consolidate phase --------
        phase_skills = list(state.get("phase_skills") or [])
        skill_idx = int(state.get("phase_skill_index", 0))
        next_idx = skill_idx + 1

        if next_idx < len(phase_skills):
            # More skills remain in this phase — loop back to plan the next
            # skill. Do NOT write to phase_history yet; the buffer accumulates.
            next_skill = phase_skills[next_idx]
            msg_ms = (
                f"[push] phase={phase} advancing to skill "
                f"({next_idx + 1}/{len(phase_skills)}): {next_skill}"
            )
            log.append(msg_ms)
            _log_step(
                "PUSH",
                f"→ NEXT SKILL  ({next_idx + 1}/{len(phase_skills)}) {next_skill!r}",
            )
            return {
                "stage_results": results,
                "completed_stages": completed_stages,
                "memory": new_memory,
                "phase_skills_buffer": skills_buffer,   # keep accumulating
                "next_stage": next_skill,
                "phase_skill_index": next_idx,
                "current_plan": None,
                "current_plan_summary": [],
                "current_plan_error": None,
                "master_revision_feedback": "",
                "evaluator_action": "",
                "feedback": "",
                "planner_attempts": 0,
                "planner_tool_calls": 0,
                "log": log,
            }

        # ---- phase complete — consolidate all skills into ONE PhaseRecord ---
        briefing_obj = PhaseBriefing.model_validate(
            state.get("master_briefing") or {"phase": phase}
        )
        phase_rec = consolidate_phase_record(phase, briefing_obj, skills_buffer, state)
        history = list(state.get("phase_history") or [])
        history.append(phase_rec.model_dump(mode="json"))

        # ---- Phase 5: build phase_asset_map entry ---------------------------
        all_ability_ids: list[str] = []
        all_stage_id_maps: dict[str, dict[str, str]] = {}
        ability_name_to_id: dict[str, str] = {}
        phase_adversary_id: str | None = None
        for sd in skills_buffer:
            if sd.get("adversary_id"):
                phase_adversary_id = sd["adversary_id"]
            for aid in sd.get("ability_ids", []):
                if aid not in all_ability_ids:
                    all_ability_ids.append(aid)
            all_stage_id_maps.update(sd.get("stage_id_map", {}))
            # Zip ability names with IDs — lengths should match since both
            # come from the same push, but guard against partial failures.
            names = sd.get("ability_names", [])
            ids = sd.get("ability_ids", [])
            for name, aid in zip(names, ids):
                ability_name_to_id[name] = aid

        phase_asset_map = dict(state.get("phase_asset_map") or {})
        phase_asset_map[phase] = {
            "adversary_id": phase_adversary_id,
            "ability_ids": all_ability_ids,
            "stage_id_map": all_stage_id_maps,
            "ability_name_to_id": ability_name_to_id,
        }

        # ---- Phase 5: write pending.* memory keys --------------------------
        pending_mem = CampaignMemory(new_memory)
        for sd in skills_buffer:
            for ab_name in sd.get("ability_names", []):
                pending_mem.add_pending(phase=phase, ability=ab_name)
        new_memory = pending_mem.data

        persist_state = dict(state)
        persist_state["memory"] = new_memory
        persist_state["phase_history"] = history
        persist_state["phase_asset_map"] = phase_asset_map
        persist_memory(persist_state, new_memory, label=f"push/{phase}/pending")

        # Do NOT add phase to completed_phases here — analyse_results owns
        # that decision after inspecting actual execution results.

        _log_step(
            "PUSH",
            f"→ PHASE COMPLETE  phase={phase!r}  "
            f"skills={phase_rec.skills_used}  "
            f"total_abilities={phase_rec.abilities_pushed}  "
            f"outcome={phase_rec.outcome!r}",
        )

        return {
            "stage_results": results,
            "completed_stages": completed_stages,
            "memory": new_memory,
            "phase_history": history,
            "phase_asset_map": phase_asset_map,
            "pending_operation_id": None,  # populated by analyse_results from backend result
            # retry flags consumed: the corrected plan was just pushed as a new op
            "retry_same_phase": False,
            "issues_to_fix": [],
            "retry_feedback": [],
            # reset all multi-skill tracking so next phase starts clean
            "phase_skills": [],
            "phase_skill_index": 0,
            "phase_skills_buffer": [],
            "current_plan": None,
            "current_plan_summary": [],
            "current_plan_error": None,
            "master_revision_feedback": "",
            "evaluator_action": "",
            "feedback": "",
            "log": log,
        }

    return push_node


# ---------------------------------------------------------------------------
# analyse_results node (Phase 4 — full implementation)
# ---------------------------------------------------------------------------


def _derive_phase_done(
    op_result: OperationResult,
    issues: list,
) -> bool:
    """Decide whether the phase is complete based on execution results.

    The phase is done when:
      1. The operation didn't fail at the infrastructure level.
      2. At least one ability passed.
        3. No detected issues. An exit code of 0 can still carry stdout/stderr
            failures from shell wrappers, missing types, unsupported parameters,
            or access denials.
        4. Every ability passed. Backend operation status alone is not enough;
            a completed operation can still contain failed ability stages.
    """
    from ..results import IssueKind

    total = len(op_result.abilities)
    if total == 0:
        return False

    if op_result.operation_status == "failed":
        return False

    passed_count = sum(1 for a in op_result.abilities if a.passed)
    if passed_count == 0:
        return False

    if passed_count != total:
        return False

    if issues:
        return False

    return True


def _build_retry_feedback(
    phase: str,
    op_result: OperationResult,
    issues: list,
) -> list[dict[str, Any]]:
    """Build structured retry guidance from deterministic issues and failures."""
    feedback: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    stage_lookup = {
        (ab.ability_id, st.stage_name): (ab, st)
        for ab in op_result.abilities
        for st in ab.stages
    }

    for iss in issues:
        ab, st = stage_lookup.get((iss.ability_id, iss.stage_name), (None, None))
        key = (iss.ability_id, iss.stage_name, iss.kind.value)
        if key in seen:
            continue
        seen.add(key)
        feedback.append({
            "kind": iss.kind.value,
            "phase": phase,
            "ability": iss.ability_name or (ab.name if ab else iss.ability_id),
            "stage": iss.stage_name,
            "exit_code": st.exit_code if st else None,
            "detail": (
                f"[{iss.kind.value}] ability={iss.ability_name!r} "
                f"stage={iss.stage_name!r}: {iss.detail}"
            ),
            "command": st.command_executed if st else "",
            "stdout_preview": (st.stdout if st else "")[:500],
            "stderr_preview": (st.stderr if st else "")[:500],
        })

    issue_stage_keys = {(item["ability"], item["stage"]) for item in feedback}
    for ab in op_result.abilities:
        for st in ab.stages:
            if st.exit_code == 0:
                continue
            if (ab.name, st.stage_name) in issue_stage_keys:
                continue
            feedback.append({
                "kind": "nonzero_exit",
                "phase": phase,
                "ability": ab.name or ab.ability_id,
                "stage": st.stage_name,
                "exit_code": st.exit_code,
                "detail": (
                    f"[nonzero_exit] ability={ab.name!r} stage={st.stage_name!r}: "
                    f"exit_code={st.exit_code}"
                ),
                "command": st.command_executed,
                "stdout_preview": st.stdout[:500],
                "stderr_preview": st.stderr[:500],
            })

    return feedback


def _make_analyse_results_node(master: MasterPolicy):
    """Build the analyse_results node closure that captures the master agent."""

    def analyse_results_node(state: SessionState) -> dict[str, Any]:
        """Pause for backend results, then parse + LLM-analyse execution output."""
        _log_step("ANALYSE", "pausing graph — awaiting backend results")
        result_data = interrupt("awaiting_results")

        # Safety: if checkpointer didn't restore memory, reload from disk.
        if not state.get("memory"):
            snapshot = load_memory_from_disk(state.get("results_dir"))
            if snapshot:
                state = dict(state)  # type: ignore[assignment]
                state["memory"] = snapshot.get("memory", {})
                progress = snapshot.get("campaign_progress", {})
                if not state.get("completed_phases") and progress.get("completed_phases"):
                    state["completed_phases"] = progress["completed_phases"]
                if not state.get("phase_history") and progress.get("phase_history"):
                    state["phase_history"] = progress["phase_history"]

        # Handle timeout resume — no results to analyse
        if isinstance(result_data, dict) and result_data.get("timeout"):
            log = list(state.get("log") or [])
            phase = state.get("current_phase") or ""

            # Clear pending.* keys — they can't be verified without results
            _mem = CampaignMemory.from_state(state)
            cleared_pending = len(_mem.pending_keys())
            new_memory = _mem.clear_pending().data

            # Set execution_summary so master_plan sees timeout context
            timeout_summary = (
                f"TIMEOUT: Phase '{phase}' — the backend did not return results "
                f"within the configured wait window. No abilities were confirmed. "
                f"Possible causes: agent offline, target unreachable, long-running "
                f"commands, or abilities blocked by security controls."
            )

            # Update phase_history with timeout outcome
            history = list(state.get("phase_history") or [])
            if history:
                last = dict(history[-1])
                last["execution_outcome"] = {
                    "operation_id": None,
                    "abilities_passed": 0,
                    "abilities_failed": 0,
                    "issues_detected": ["timeout"],
                    "timeout": True,
                }
                history[-1] = last

            retry_feedback = [{
                "kind": "timeout",
                "phase": phase,
                "ability": None,
                "stage": None,
                "exit_code": None,
                "detail": timeout_summary,
                "command": None,
                "stdout_preview": "",
                "stderr_preview": "",
            }]

            log.append(
                f"[analyse_results] TIMEOUT — cleared {cleared_pending} "
                f"pending keys, signalling retry for phase={phase}"
            )
            _log_step(
                "ANALYSE",
                f"→ TIMEOUT — cleared pending.* keys, "
                f"setting retry_same_phase=True for phase={phase!r}",
                level="warning",
            )

            persist_state = dict(state)
            persist_state["memory"] = new_memory
            persist_state["phase_history"] = history
            persist_memory(persist_state, new_memory, label=f"analyse/timeout/{phase}")

            return {
                "memory": new_memory,
                "phase_history": history,
                "execution_summary": timeout_summary,
                "pending_operation_id": None,
                "retry_same_phase": True,
                "issues_to_fix": [item["detail"] for item in retry_feedback],
                "retry_feedback": retry_feedback,
                "log": log,
            }

        # 1. Parse + issue detection
        op_result = parse_operation_result(result_data)
        phase = state.get("current_phase") or ""
        asset_map = (state.get("phase_asset_map") or {}).get(phase, {})
        stage_id_map = asset_map.get("stage_id_map", {})
        issues = detect_issues(op_result, stage_id_map)
        summary = build_structural_summary(op_result, issues)

        _log_step(
            "ANALYSE",
            f"resumed — op={op_result.operation_id} "
            f"abilities={len(op_result.abilities)} "
            f"passed={sum(1 for a in op_result.abilities if a.passed)} "
            f"issues={len(issues)}",
        )

        # 2. LLM analysis — extract confirmed facts from output
        try:
            mem_update = master.analyse_results(
                results_dir=state.get("results_dir"),
                operation_id=op_result.operation_id,
                structural_summary=summary,
                current_memory=project_for_prompt(state.get("memory", {}) or {}),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[analyse_results] LLM analysis failed: %s", exc)
            mem_update = MemoryUpdate(
                facts={}, narrative=f"analysis failed: {exc!r}"
            )

        # 3. Drop speculative pending.* keys, then deep-merge confirmed facts and
        # append the narrative.
        new_memory = (
            CampaignMemory.from_state(state)
            .clear_pending()
            .merge_facts(mem_update.facts)
            .add_narrative(phase=phase, text=mem_update.narrative, ts=_now())
            .data
        )

        # 4. Phase done decision
        phase_done = _derive_phase_done(op_result, issues)

        # 5. Store operation_id from backend into phase_asset_map (Phase 7)
        updated_asset_map = dict(state.get("phase_asset_map") or {})
        if phase and phase in updated_asset_map:
            phase_entry = dict(updated_asset_map[phase])
            phase_entry["operation_id"] = op_result.operation_id
            updated_asset_map[phase] = phase_entry

        # 6. Update phase_history entry with execution_outcome
        history = list(state.get("phase_history") or [])
        if history:
            last = dict(history[-1])
            retry_feedback = _build_retry_feedback(phase, op_result, issues)
            last["execution_outcome"] = {
                "operation_id": op_result.operation_id,
                "abilities_passed": sum(1 for a in op_result.abilities if a.passed),
                "abilities_failed": sum(1 for a in op_result.abilities if a.failed),
                "issues_detected": [i.kind.value for i in issues],
                "retry_feedback": retry_feedback,
            }
            history[-1] = last
        else:
            retry_feedback = _build_retry_feedback(phase, op_result, issues)

        completed_phases = list(state.get("completed_phases") or [])
        if phase_done and phase and phase not in completed_phases:
            completed_phases.append(phase)

        persisted_completed_phases = completed_phases if phase_done else list(state.get("completed_phases") or [])

        persist_state = dict(state)
        persist_state["memory"] = new_memory
        persist_state["phase_history"] = history
        persist_state["completed_phases"] = persisted_completed_phases
        persist_state["phase_asset_map"] = updated_asset_map
        persist_memory(
            persist_state,
            new_memory,
            label=f"analyse/{phase}/{op_result.operation_id[:8]}",
        )

        log = list(state.get("log") or [])
        log.append(
            f"[analyse_results] op={op_result.operation_id} "
            f"passed={sum(1 for a in op_result.abilities if a.passed)}/"
            f"{len(op_result.abilities)} issues={len(issues)} "
            f"phase_done={phase_done}"
        )

        _log_step(
            "ANALYSE",
            f"→ phase_done={phase_done}  "
            f"memory_keys={list((mem_update.facts or {}).keys())}  "
            f"issues={[i.kind.value for i in issues]}",
        )

        result: dict[str, Any] = {
            "memory": new_memory,
            "phase_history": history,
            "completed_phases": persisted_completed_phases,
            "execution_summary": summary,
            "phase_asset_map": updated_asset_map,
            "pending_operation_id": None,
            "retry_feedback": [] if phase_done else retry_feedback,
            "log": log,
        }

        # When the phase is NOT done and there are detected issues or failed
        # abilities, signal retry with specific actionable descriptions so the
        # master and planner know exactly what to fix.
        if not phase_done:
            failed_abilities = [a for a in op_result.abilities if a.failed]
            if issues or failed_abilities:
                result["retry_same_phase"] = True
                result["issues_to_fix"] = [item["detail"] for item in retry_feedback]
                result["retry_feedback"] = retry_feedback

        return result

    return analyse_results_node


# ---------------------------------------------------------------------------
# edge predicates
# ---------------------------------------------------------------------------


def _after_master_plan(state: SessionState) -> str:
    """Route out of the master node.

    * ``master_done`` -> END.
    * ``evaluator_action == 'commit'``     -> push the approved plan.
    * ``evaluator_action == 'master_revise'`` (revise mode) -> back to planner.
    * Otherwise (PICK mode emitted a briefing) -> planner crafts the plan.
    """
    if state.get("master_done"):
        return "done"
    if state.get("evaluator_action") == "commit":
        return "push"
    return "plan"


def _after_plan(state: SessionState) -> str:
    """The planner is the only node that talks to the master. After every plan
    invocation it either:
      * hands the plan up to the master when the evaluator finished
        (accept / escalate / retry-exhausted), or
      * sends the new draft down to the evaluator for critique.
    """
    eval_action = state.get("evaluator_action") or ""
    if eval_action in ("accept", "escalate", "retry-exhausted"):
        return "master"
    return "evaluate"


def _after_push(state: SessionState) -> str:
    """Route out of push — conditional on whether more skills remain in the phase.

    * Intermediate skill (more skills queued) → master_plan (plan the next skill).
    * Last skill in phase (phase complete) → analyse_results (wait for backend).

    Design note (Issue #1 fix): unconditional ``push → analyse_results`` would
    break multi-skill phases because intermediate pushes don't need to wait for
    backend results.
    """
    phase_skills = state.get("phase_skills") or []
    skill_idx = state.get("phase_skill_index", 0)
    if skill_idx + 1 < len(phase_skills):
        return "master_plan"
    return "analyse_results"


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def build_graph(
    *,
    master: MasterPolicy,
    skill_tool: SkillTool,
    planner: Planner,
    bas: BasClient,
    artifacts: ArtifactStore | None = None,
    evaluator: EvaluatorPolicy | None = None,
    checkpointer: Any = None,
):
    """Compile the master-driven orchestrator graph."""
    evaluator = evaluator or StaticAcceptEvaluator()

    g: StateGraph[SessionState] = StateGraph(SessionState)
    g.add_node("init", _init_node)
    g.add_node("master_plan", _make_master_plan_node(master, skill_tool))
    g.add_node("plan", _make_plan_node(planner, skill_tool))
    g.add_node("evaluate", _make_evaluate_node(evaluator, skill_tool))
    g.add_node("push", _make_push_node(master, skill_tool, bas, artifacts, planner=planner))
    g.add_node("analyse_results", _make_analyse_results_node(master))

    g.add_edge(START, "init")
    g.add_edge("init", "master_plan")
    g.add_conditional_edges(
        "master_plan",
        _after_master_plan,
        {"plan": "plan", "push": "push", "done": END},
    )
    g.add_conditional_edges(
        "plan",
        _after_plan,
        {"evaluate": "evaluate", "master": "master_plan"},
    )
    g.add_edge("evaluate", "plan")
    g.add_conditional_edges(
        "push",
        _after_push,
        {"analyse_results": "analyse_results", "master_plan": "master_plan"},
    )
    g.add_edge("analyse_results", "master_plan")
    return g.compile(checkpointer=checkpointer)


def _stream_graph(app, seed: Any, run_config: dict[str, Any]) -> dict[str, Any]:
    """Stream graph updates, log each step, raise GraphInterrupt if paused.

    Shared between ``_run_engagement_inner`` (initial run) and ``_resume_graph``
    (webhook resume) so both use the same logging/interrupt logic.

    ``seed`` may be a plain dict (initial run) or a ``Command(resume=...)``
    object (webhook resume).
    """
    is_resume = not isinstance(seed, dict)
    if is_resume:
        logger.info(
            "[GRAPH][RESUME         ]  thread_id=%s",
            run_config.get("configurable", {}).get("thread_id"),
        )
    else:
        logger.info(
            "[GRAPH][START          ]  phases=%s  foothold_keys=%s  thread_id=%s",
            seed.get("available_phases"),
            list((seed.get("foothold") or {}).keys()),
            run_config.get("configurable", {}).get("thread_id"),
        )

    state: dict[str, Any] = seed if isinstance(seed, dict) else {}
    step_count = 0
    _interrupted = False
    for chunk in app.stream(seed, config=run_config, stream_mode="updates"):
        for node_name, node_updates in chunk.items():
            if not isinstance(node_updates, dict):
                if node_name == "__interrupt__":
                    _interrupted = True
                continue
            step_count += 1
            phase = node_updates.get("current_phase") or state.get("current_phase") or ""
            visible_keys = [
                k for k in node_updates
                if k not in ("log", "proposal_log", "stage_results")
            ]
            logger.info(
                "[GRAPH][%-16s]  step=%-3d  phase=%-18s  keys=[%s]",
                node_name.upper(),
                step_count,
                f"{phase!r}" if phase else "(none)",
                ", ".join(visible_keys),
            )
            state.update(node_updates)
    if _interrupted:
        raise GraphInterrupt(())

    completed = state.get("completed_phases") or []
    history = state.get("phase_history") or []
    logger.info(
        "[GRAPH][DONE           ]  steps=%d  completed_phases=%s  phase_records=%d",
        step_count,
        completed,
        len(history),
    )
    return state


# NOTE: run_orchestrator() was removed — it created a private build_graph()
# with its own checkpointer, causing split-brain on interrupt/resume.
# All callers must use _get_compiled_graph() (the process singleton) and
# _stream_graph() directly. See bootstrap._get_compiled_graph().
