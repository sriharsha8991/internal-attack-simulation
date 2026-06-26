"""Session state shared across orchestrator nodes.

The diagram's "Session memory.json" persists as `state["memory"]`. Only the
master/push nodes update memory; the planner reads it (with the active phase
briefing merged in) and the evaluator inspects it without mutating it.

``phase_history`` is a structured audit trail: one entry per committed phase
carrying the objective, skills used, abilities pushed, and a compact summary.
The master injects it into its prompts so each subsequent phase has full
campaign context without re-parsing raw memory.

LangGraph treats the dict as the unit of inter-node communication. Each node
returns a partial dict — keys present in the return value overwrite, keys
absent are preserved.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field

DONE_SENTINEL = "DONE"


class SessionState(TypedDict, total=False):
    """LangGraph state schema for the orchestrator.
    
    Uses TypedDict with total=False so all fields are optional - nodes only
    need to return the subset they want to update.
    """
    # Core identifiers
    run_id: str
    results_dir: str
    
    # Foothold and environment
    foothold: dict[str, Any]
    kali_sidecar: dict[str, Any]
    memory: dict[str, Any]
    safety: dict[str, Any]
    
    # Stage tracking
    completed_stages: list[str]
    stage_results: list[dict[str, Any]]
    
    # Iteration control
    iteration: int
    max_iterations: int
    log: list[str]
    
    # Master/phase control
    available_phases: list[str]
    completed_phases: list[str]
    current_phase: str
    next_stage: str
    master_briefing: dict[str, Any]
    master_revisions_used: int
    max_master_revisions: int
    master_revision_feedback: str
    master_done: bool
    
    # Planner inner loop
    planner_attempts: int
    max_planner_attempts: int
    planner_tool_calls: int
    max_planner_tool_calls: int
    
    # Plan state
    proposal_log: list[dict[str, Any]]
    phase_history: list[dict[str, Any]]
    phase_skills: list[str]
    phase_skill_index: int
    phase_skills_buffer: list[dict[str, Any]]
    current_plan: dict[str, Any] | None
    current_plan_summary: list[dict[str, Any]]
    current_plan_error: str | None
    current_provider_id: str | None
    route_hint: str | None
    blocked_reason: str | None
    required_ack: str | None
    risk_level: str
    
    # Evaluator state
    last_evaluator_verdict: dict[str, Any]
    phase_done: bool
    feedback: str
    evaluator_action: str
    
    # Execution tracking
    retry_same_phase: bool
    issues_to_fix: list[str]
    retry_feedback: list[dict[str, Any]]
    execution_summary: str | None
    phase_asset_map: dict[str, Any]
    pending_operation_id: str | None


class PhaseRecord(BaseModel):
    """Structured record of what happened in one committed phase."""

    phase: str
    objective: str = ""
    skills_used: list[str] = Field(default_factory=list)
    abilities_pushed: int = 0
    adversary_id: str | None = None
    ability_names: list[str] = Field(default_factory=list)
    techniques_used: list[str] = Field(default_factory=list)
    key_commands: list[str] = Field(default_factory=list)
    outcome: str = ""   # "committed" | "skipped" | "escalated"
    master_revisions: int = 0
    planner_attempts: int = 0
    memory_delta_keys: list[str] = Field(default_factory=list)


class StageResult(BaseModel):
    """Outcome reported by a specialist back to the orchestrator."""

    skill: str
    success: bool = True
    abilities_pushed: int = 0
    adversary_id: str | None = None
    notes: str = ""
    extras: dict[str, Any] = Field(default_factory=dict)


def phase_history_compact(
    phase_history: list[dict[str, Any]],
    *,
    max_entries: int = 5,
    max_commands_per_phase: int = 3,
    max_cmd_len: int = 120,
) -> list[dict[str, Any]]:
    """Return a trimmed view of phase_history for prompt injection."""
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


def skill_data_from_push(
    skill_name: str,
    push: Any,
    mem_update: Any,
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


def consolidate_phase_record(
    phase: str,
    briefing_obj: Any,
    all_skill_data: list[dict[str, Any]],
    state: dict[str, Any],
) -> PhaseRecord:
    """Merge all per-skill push summaries into one PhaseRecord for the phase."""
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


def log_proposal(
    state: dict[str, Any],
    *,
    audience: str,
    phase: str,
    attempt: int,
    plan_summary: list[dict[str, Any]],
    note: str = "",
) -> list[dict[str, Any]]:
    """Append one proposal record to state['proposal_log']."""
    import logging
    from .utils import _now
    logger = logging.getLogger(__name__)
    
    proposals = list(state.get("proposal_log") or [])
    entry: dict[str, Any] = {
        "ts": _now(),
        "phase": phase,
        "audience": audience,
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
    cmds = sum(len(ab.get("stages") or []) for ab in plan_summary)
    logger.info(
        "[proposal] -> %s phase=%s attempt=%d abilities=%d commands=%d note=%r",
        audience, phase, attempt, len(plan_summary), cmds, note,
    )
    return proposals


def build_phase_asset_map(
    phase: str,
    skills_buffer: list[dict[str, Any]],
    current_map: dict[str, Any]
) -> dict[str, Any]:
    """Rebuilds the asset map for a phase given the accumulated skill pushes."""
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
        
        names = sd.get("ability_names", [])
        ids = sd.get("ability_ids", [])
        for name, aid in zip(names, ids):
            ability_name_to_id[name] = aid

    updated_map = dict(current_map)
    updated_map[phase] = {
        "adversary_id": phase_adversary_id,
        "ability_ids": all_ability_ids,
        "stage_id_map": all_stage_id_maps,
        "ability_name_to_id": ability_name_to_id,
    }
    return updated_map


def _merge_lists(a: list, b: list) -> list:
    """Merge two lists into a sorted list."""
    result = []
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i] <= b[j]:
            result.append(a[i])
            i += 1
        else:
            result.append(b[j])
            j += 1
    result.extend(a[i:])
    result.extend(b[j:])
    return sorted(result)
