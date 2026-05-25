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
from ..phases import resolve_phases_to_skills
from ..results import (
    OperationResult,
    build_structural_summary,
    detect_issues,
    parse_operation_result,
)
from ..tools.skill_tool import SkillTool
from .state import DONE_SENTINEL, PhaseRecord, SessionState, StageResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_skill_for_phase(phase: str, skill_tool: SkillTool) -> str | None:
    """Return the canonical playbook (first skill) registered for a phase."""
    if not phase:
        return None
    resolved, _ = resolve_phases_to_skills([phase], skill_tool)
    return resolved[0] if resolved else None


def _skills_for_phase(phase: str, skill_tool: SkillTool) -> list[str]:
    """Return ALL playbook skill names registered for a phase, in order."""
    if not phase:
        return []
    resolved, _ = resolve_phases_to_skills([phase], skill_tool)
    return resolved


def _log_step(tag: str, msg: str, *, level: str = "info") -> None:
    """Emit a visually distinct graph-step line.

    Format: ``[GRAPH][TAG          ]  message``
    The fixed-width tag makes it easy to grep / column-align in log viewers.
    """
    getattr(logger, level)("[GRAPH][%-16s]  %s", tag, msg)


def _phase_history_compact(
    phase_history: list[dict[str, Any]],
    *,
    max_entries: int = 5,
    max_commands_per_phase: int = 3,
    max_cmd_len: int = 120,
) -> list[dict[str, Any]]:
    """Return a trimmed view of phase_history for prompt injection.

    Keeps only the most recent ``max_entries`` phases and truncates the
    potentially long ``key_commands`` list so context usage stays bounded.
    """
    recent = (phase_history or [])[-max_entries:]
    result = []
    for rec in recent:
        result.append({
            "phase": rec.get("phase"),
            "objective": rec.get("objective"),
            "outcome": rec.get("outcome"),
            "skills_used": rec.get("skills_used"),
            "techniques_used": rec.get("techniques_used"),
            "ability_names": (rec.get("ability_names") or [])[:5],
            "key_commands": [
                cmd[:max_cmd_len]
                for cmd in (rec.get("key_commands") or [])[:max_commands_per_phase]
            ],
            "memory_delta_keys": rec.get("memory_delta_keys"),
        })
    return result


def _extract_prior_issues(state: "SessionState") -> list:
    """Pull StageIssue objects from the last execution for the current phase.

    Reconstructs issues from the stored result JSON via the parser. Falls back
    to an empty list if no results are available yet.
    """
    phase = state.get("current_phase") or ""
    asset_map = (state.get("phase_asset_map") or {}).get(phase, {})
    stage_id_map = asset_map.get("stage_id_map", {})

    # Get the last execution_outcome from phase_history
    history = state.get("phase_history") or []
    op_id: str | None = None
    for entry in reversed(history):
        outcome = entry.get("execution_outcome") or {}
        if outcome.get("operation_id"):
            op_id = outcome["operation_id"]
            break

    if not op_id:
        return []

    results_dir = state.get("results_dir")
    if not results_dir:
        return []

    from ..tools.master_tools import _load_and_parse

    _, op_result = _load_and_parse(results_dir, op_id)
    if op_result is None:
        return []

    return detect_issues(op_result, stage_id_map)


def _skill_data_from_push(
    skill_name: str,
    push: "PushResult",
    mem_update: "MemoryUpdate",
) -> dict[str, Any]:
    """Build a per-skill summary dict for accumulation in phase_skills_buffer."""
    ability_names = [ab.get("name", "") for ab in push.plan_summary]
    techniques = list(
        {ab.get("mitre_technique_id", "") for ab in push.plan_summary} - {"", None}
    )
    key_commands: list[str] = []
    for ab in push.plan_summary:
        for stg in ab.get("stages") or []:
            cmd = stg.get("command_template") or ""
            if cmd:
                key_commands.append(cmd[:200])
    return {
        "skill": skill_name,
        "abilities_pushed": len(push.ability_ids),
        "adversary_id": push.adversary_id,
        "ability_ids": push.ability_ids,
        "ability_names": ability_names,
        "techniques_used": techniques,
        "key_commands": key_commands[:15],
        "outcome": "committed" if push.success else "failed",
        "memory_delta_keys": list((mem_update.facts or {}).keys()),
        "stage_id_map": push.stage_id_map,
    }


def _consolidate_phase_record(
    phase: str,
    briefing_obj: "PhaseBriefing",
    all_skill_data: list[dict[str, Any]],
    state: "SessionState",
) -> "PhaseRecord":
    """Merge all per-skill push summaries into one PhaseRecord for the phase.

    This produces a single record regardless of how many skills the phase ran,
    so the master always sees exactly one entry per completed phase in
    ``phase_history``.
    """
    all_names: list[str] = []
    all_techs: set[str] = set()
    all_cmds: list[str] = []
    all_mem_keys: set[str] = set()
    total_abilities = 0
    adversary_id: str | None = None

    for sd in all_skill_data:
        all_names.extend(sd.get("ability_names") or [])
        all_techs.update(sd.get("techniques_used") or [])
        all_cmds.extend(sd.get("key_commands") or [])
        all_mem_keys.update(sd.get("memory_delta_keys") or [])
        total_abilities += sd.get("abilities_pushed") or 0
        if not adversary_id and sd.get("adversary_id"):
            adversary_id = sd["adversary_id"]

    any_committed = any(sd.get("outcome") == "committed" for sd in all_skill_data)
    return PhaseRecord(
        phase=phase,
        objective=briefing_obj.objective,
        skills_used=[sd["skill"] for sd in all_skill_data],
        abilities_pushed=total_abilities,
        adversary_id=adversary_id,
        ability_names=all_names,
        techniques_used=list(all_techs),
        key_commands=all_cmds[:15],
        outcome="committed" if any_committed else "failed",
        master_revisions=int(state.get("master_revisions_used", 0)),
        planner_attempts=int(state.get("planner_attempts", 0)),
        memory_delta_keys=list(all_mem_keys),
    )


def _log_proposal(
    state: SessionState,
    *,
    audience: str,
    phase: str,
    attempt: int,
    plan_summary: list[dict[str, Any]],
    note: str = "",
) -> list[dict[str, Any]]:
    """Append one proposal record to state['proposal_log']. Captures full
    command_template strings so the audit log is the source of truth on every
    revision the planner emitted."""
    proposals = list(state.get("proposal_log") or [])
    entry: dict[str, Any] = {
        "ts": _now(),
        "phase": phase,
        "audience": audience,  # 'evaluator' or 'master'
        "attempt": attempt,
        "note": note,
        "abilities": [
            {
                "name": ab.get("name"),
                "mitre_technique_id": ab.get("mitre_technique_id"),
                "platform": ab.get("platform"),
                "rationale": (ab.get("rationale") or "")[:400],
                "stages": [
                    {
                        "order": s.get("order"),
                        "executor": s.get("executor"),
                        "command_template": s.get("command_template"),
                    }
                    for s in ab.get("stages") or []
                ],
            }
            for ab in plan_summary
        ],
    }
    proposals.append(entry)
    # Also mirror a compact line into the human log so tail-N is informative.
    cmds = sum(len(ab.get("stages") or []) for ab in plan_summary)
    logger.info(
        "[proposal] -> %s phase=%s attempt=%d abilities=%d commands=%d note=%r",
        audience,
        phase,
        attempt,
        len(plan_summary),
        cmds,
        note,
    )
    return proposals


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------


def _init_node(state: SessionState) -> dict[str, Any]:
    """Seed defaults exactly once per run."""
    run_id = state.get("run_id") or uuid.uuid4().hex
    phases = list(state.get("available_phases") or [])
    foothold_keys = list((state.get("foothold") or {}).keys())
    _log_step("INIT", f"run_id={run_id} phases={phases} foothold_keys={foothold_keys}")
    return {
        "run_id": run_id,
        "foothold": state.get("foothold", {}) or {},
        "memory": state.get("memory", {}) or {},
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
        "completed_phases": list(state.get("completed_phases") or []),
        "current_phase": state.get("current_phase", "") or "",
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
        "phase_history": list(state.get("phase_history") or []),
        "phase_skills": list(state.get("phase_skills") or []),
        "phase_skill_index": int(state.get("phase_skill_index", 0) or 0),
        "phase_skills_buffer": [],
        "current_plan": None,
        "current_plan_summary": [],
        "current_plan_error": None,
        "current_provider_id": None,
        "last_evaluator_verdict": {},
        "phase_done": False,
        "feedback": "",
        "evaluator_action": "",
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

    proposals = _log_proposal(
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
            memory=state.get("memory", {}) or {},
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
            memory=state.get("memory", {}) or {},
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

    next_skill = _first_skill_for_phase(briefing.phase, skill_tool)
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

    all_skills = _skills_for_phase(briefing.phase, skill_tool)
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
        memory_with_brief = dict(state.get("memory") or {})
        memory_with_brief["_master_briefing"] = briefing
        memory_with_brief["_phase_history"] = _phase_history_compact(
            list(state.get("phase_history") or [])
        )

        plan_state: SessionState = dict(state)  # type: ignore[assignment]
        plan_state["memory"] = memory_with_brief

        planned: PlanResult = plan_specialist(
            plan_state, planner=planner, skill_tool=skill_tool, feedback=feedback
        )
        proposals = _log_proposal(
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
                memory=state.get("memory", {}) or {},
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
            "log": log,
        }

    return evaluate_node


def _make_push_node(
    master: MasterPolicy,
    skill_tool: SkillTool,
    bas: BasClient,
    artifacts: ArtifactStore | None,
):
    """Push the committed plan and update master memory."""

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

        # ---- retry detection (Phase 7) -------------------------------------
        # If the master flagged retry_same_phase AND we have prior asset_map
        # for this phase, send feedback to fix failed stages instead of
        # creating new abilities/adversaries.
        completed_phases = list(state.get("completed_phases") or [])
        prior_asset_map = (state.get("phase_asset_map") or {}).get(phase)
        is_retry = (
            prior_asset_map is not None
            and phase not in completed_phases
            and bool(state.get("retry_same_phase"))
        )

        if is_retry:
            _log_step("PUSH", f"→ RETRY PATH  phase={phase!r}", level="info")
            op_id = prior_asset_map.get("operation_id", "")
            prior_issues = _extract_prior_issues(state)

            changes = master.build_feedback_payload(
                issues=prior_issues,
                current_plan=plan,
                asset_map=prior_asset_map,
                operation_id=op_id,
            )
            if changes:
                bas.feedback.send(operation_id=op_id, changes=changes)
                log.append(
                    f"[push] RETRY phase={phase} sent {len(changes)} "
                    f"feedback corrections for op={op_id}"
                )
            else:
                log.append(
                    f"[push] RETRY phase={phase} no corrections to send — "
                    f"re-running same operation op={op_id}"
                )

            _log_step(
                "PUSH",
                f"→ RETRY feedback={len(changes)} changes  op={op_id}",
            )
            # Return minimal update — no new IDs, keep existing asset_map
            return {
                "stage_results": list(state.get("stage_results") or []),
                "retry_same_phase": False,  # consumed
                "issues_to_fix": [],
                # reset planning state so next cycle starts clean
                "current_plan": None,
                "current_plan_summary": [],
                "current_plan_error": None,
                "master_revision_feedback": "",
                "evaluator_action": "",
                "feedback": "",
                "log": log,
            }

        push: PushResult = push_specialist(
            state,
            plan=plan,
            skill_tool=skill_tool,
            bas=bas,
            artifacts=artifacts,
            provider_id=state.get("current_provider_id"),
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
        if skill_name and skill_name not in completed_stages:
            completed_stages.append(skill_name)

        # Master writes the memory delta.
        try:
            briefing = PhaseBriefing.model_validate(
                state.get("master_briefing") or {"phase": phase}
            )
            mem_update: MemoryUpdate = master.update_memory(
                memory=state.get("memory", {}) or {},
                briefing=briefing,
                plan_summary=push.plan_summary,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[push] memory update failed: %s", exc)
            mem_update = MemoryUpdate(facts={}, narrative=f"committed {phase}")

        new_memory = dict(state.get("memory") or {})
        # Merge facts (shallow); preserve prior keys unless overwritten.
        for k, v in (mem_update.facts or {}).items():
            new_memory[k] = v
        narratives = list(new_memory.get("narratives") or [])
        if mem_update.narrative:
            narratives.append({"phase": phase, "ts": _now(), "text": mem_update.narrative})
            new_memory["narratives"] = narratives

        msg = (
            f"[push] phase={phase} skill={skill_name} success={push.success} "
            f"adversary={push.adversary_id} abilities={len(push.ability_ids)} "
            f"narrative={(mem_update.narrative or '')[:120]!r}"
            + (f" error={push.error!r}" if push.error else "")
        )
        log.append(msg)
        _log_step(
            "PUSH",
            f"→ success={push.success}  adversary={push.adversary_id}  "
            f"abilities={len(push.ability_ids)}  "
            f"memory_keys={list((mem_update.facts or {}).keys())}"
            + (f"  error={push.error!r}" if push.error else ""),
            level="info" if push.success else "error",
        )

        # ---- accumulate per-skill data in buffer ----------------------------
        skill_data = _skill_data_from_push(skill_name, push, mem_update)
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
        phase_rec = _consolidate_phase_record(phase, briefing_obj, skills_buffer, state)
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
            for name, aid in zip(
                sd.get("ability_names", []),
                sd.get("ability_ids", []),
                strict=False,
            ):
                ability_name_to_id[name] = aid

        phase_asset_map = dict(state.get("phase_asset_map") or {})
        phase_asset_map[phase] = {
            "adversary_id": phase_adversary_id,
            "ability_ids": all_ability_ids,
            "stage_id_map": all_stage_id_maps,
            "ability_name_to_id": ability_name_to_id,
        }

        # ---- Phase 5: write pending.* memory keys --------------------------
        for sd in skills_buffer:
            for ab_name in sd.get("ability_names", []):
                new_memory[f"pending.{phase}.{ab_name}"] = "awaiting_results"

        completed_phases = list(state.get("completed_phases") or [])
        if phase and phase not in completed_phases:
            completed_phases.append(phase)

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
            "completed_phases": completed_phases,
            "memory": new_memory,
            "phase_history": history,
            "phase_asset_map": phase_asset_map,
            "pending_operation_id": push.adversary_id,  # set by backend on execution
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

    Heuristic: phase is done when at least one ability passed and no critical
    issues (placeholder tokens, tool-not-found) were detected in passing
    abilities. Timeout alone doesn't block completion.
    """
    from ..results import IssueKind

    passed_count = sum(1 for a in op_result.abilities if a.passed)
    if passed_count == 0:
        return False
    critical_kinds = {IssueKind.PLACEHOLDER_TOKEN, IssueKind.TOOL_NOT_FOUND}
    critical_issues = [i for i in issues if i.kind in critical_kinds]
    return len(critical_issues) == 0


def _make_analyse_results_node(master: MasterPolicy):
    """Build the analyse_results node closure that captures the master agent."""

    def analyse_results_node(state: SessionState) -> dict[str, Any]:
        """Pause for backend results, then parse + LLM-analyse execution output."""
        _log_step("ANALYSE", "pausing graph — awaiting backend results")
        result_data = interrupt("awaiting_results")

        # Handle timeout resume — no results to analyse
        if isinstance(result_data, dict) and result_data.get("timeout"):
            log = list(state.get("log") or [])
            log.append("[analyse_results] timeout — proceeding without results")
            _log_step("ANALYSE", "→ TIMEOUT — keeping pending.* as unverified")
            return {"log": log}

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
                current_memory=state.get("memory", {}),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[analyse_results] LLM analysis failed: %s", exc)
            mem_update = MemoryUpdate(
                facts={}, narrative=f"analysis failed: {exc!r}"
            )

        # 3. Promote pending.* → confirmed facts
        new_memory = dict(state.get("memory") or {})
        # Remove all pending.* keys — they were speculative
        pending_keys = [k for k in new_memory if k.startswith("pending.")]
        for pk in pending_keys:
            del new_memory[pk]
        # Merge confirmed facts from LLM
        for k, v in (mem_update.facts or {}).items():
            new_memory[k] = v
        if mem_update.narrative:
            narratives = list(new_memory.get("narratives") or [])
            narratives.append({
                "phase": phase, "ts": _now(), "text": mem_update.narrative
            })
            new_memory["narratives"] = narratives

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
            last["execution_outcome"] = {
                "operation_id": op_result.operation_id,
                "abilities_passed": sum(1 for a in op_result.abilities if a.passed),
                "abilities_failed": sum(1 for a in op_result.abilities if a.failed),
                "issues_detected": [i.kind.value for i in issues],
            }
            history[-1] = last

        completed_phases = list(state.get("completed_phases") or [])
        if phase_done and phase and phase not in completed_phases:
            completed_phases.append(phase)

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

        return {
            "memory": new_memory,
            "phase_history": history,
            "completed_phases": completed_phases if phase_done else state.get("completed_phases"),
            "execution_summary": summary,
            "phase_asset_map": updated_asset_map,
            "pending_operation_id": None,
            "log": log,
        }

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
    g.add_node("push", _make_push_node(master, skill_tool, bas, artifacts))
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


def run_orchestrator(
    *,
    master: MasterPolicy | None = None,
    skill_tool: SkillTool,
    planner: Planner,
    bas: BasClient,
    available_phases: list[str] | None = None,
    foothold: dict[str, Any] | None = None,
    max_iterations: int = 20,
    initial_state: SessionState | None = None,
    artifacts: ArtifactStore | None = None,
    evaluator: EvaluatorPolicy | None = None,
    max_master_revisions: int = 1,
    max_planner_attempts: int = 3,
    max_planner_tool_calls: int = 20,
    checkpointer: Any = None,
) -> SessionState:
    """Convenience runner. Returns the terminal `SessionState`.

    Uses ``app.stream(stream_mode="updates")`` instead of ``app.invoke`` so
    every graph step is logged as it completes — giving a clean real-time trace
    of node name + updated state keys without any extra overhead.
    Each node's internal ``_log_step`` calls provide the detail; this outer
    loop adds a structural "node completed" line.
    """
    master = master or StaticMasterRouter()
    app = build_graph(
        master=master,
        skill_tool=skill_tool,
        planner=planner,
        bas=bas,
        artifacts=artifacts,
        evaluator=evaluator,
        checkpointer=checkpointer,
    )
    seed: SessionState = {
        "foothold": foothold or {},
        "available_phases": list(available_phases or []),
        "max_iterations": max_iterations,
        "max_master_revisions": max_master_revisions,
        "max_planner_attempts": max_planner_attempts,
        "max_planner_tool_calls": max_planner_tool_calls,
    }
    if initial_state:
        seed.update(initial_state)

    # Thread ID for checkpointer — defaults to run_id so each engagement
    # gets its own checkpoint timeline.
    thread_id = seed.get("run_id") or "default"
    run_config = {"configurable": {"thread_id": thread_id}}

    logger.info(
        "[GRAPH][START          ]  phases=%s  foothold_keys=%s  thread_id=%s",
        seed.get("available_phases"),
        list((seed.get("foothold") or {}).keys()),
        thread_id,
    )

    # Stream updates to get per-step visibility; merge into full state so the
    # caller receives the same complete dict that invoke() would return.
    # Each node returns FULL lists (not diffs), so plain dict.update is correct.
    state: dict[str, Any] = dict(seed)
    step_count = 0
    for chunk in app.stream(seed, config=run_config, stream_mode="updates"):
        for node_name, node_updates in chunk.items():
            step_count += 1
            phase = node_updates.get("current_phase") or state.get("current_phase") or ""
            # Log all updated keys except verbose list fields that nodes already
            # logged internally (log, proposal_log stay in the audit trail but
            # are noisy at the stream level).
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

    completed = state.get("completed_phases") or []
    history = state.get("phase_history") or []
    logger.info(
        "[GRAPH][DONE           ]  steps=%d  completed_phases=%s  phase_records=%d",
        step_count,
        completed,
        len(history),
    )
    return state  # type: ignore[return-value]
