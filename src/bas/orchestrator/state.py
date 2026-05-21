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

from typing import Any

try:
    from typing_extensions import TypedDict  # pydantic needs this on Python < 3.12
except ImportError:  # pragma: no cover
    from typing import TypedDict  # type: ignore[assignment]

from pydantic import BaseModel, Field

DONE_SENTINEL = "DONE"


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


class SessionState(TypedDict, total=False):
    # ---- run identity --------------------------------------------------------
    run_id: str
    foothold: dict[str, Any]

    # ---- routing -------------------------------------------------------------
    next_stage: str          # skill name, or DONE_SENTINEL

    # ---- progress ------------------------------------------------------------
    completed_stages: list[str]
    stage_results: list[StageResult]

    # ---- memory --------------------------------------------------------------
    memory: dict[str, Any]   # cross-stage findings (orchestrator-write only)

    # ---- bookkeeping ---------------------------------------------------------
    iteration: int
    max_iterations: int
    log: list[str]           # human-readable trace

    # ---- master router (campaign director) ----------------------------------
    available_phases: list[str]      # phases requested by the API caller
    completed_phases: list[str]      # phases the master has committed
    current_phase: str               # active phase chosen by the master
    master_briefing: dict[str, Any] | None
    master_revisions_used: int       # 0..max_master_revisions (default cap 1)
    max_master_revisions: int        # default 1 (=> total master decisions = 2)
    master_revision_feedback: str    # router comments injected into next plan
    master_done: bool                # master said "no more phases"

    # ---- planner <-> evaluator inner loop -----------------------------------
    planner_attempts: int            # 0..max_planner_attempts
    max_planner_attempts: int        # default 3
    planner_tool_calls: int          # 0..max_planner_tool_calls (per phase)
    max_planner_tool_calls: int      # default 20

    # ---- proposal audit log -------------------------------------------------
    proposal_log: list[dict[str, Any]]

    # ---- phase history (structured campaign trail) --------------------------
    phase_history: list[dict[str, Any]]  # list of PhaseRecord dicts

    # ---- multi-skill per phase ----------------------------------------------
    phase_skills: list[str]       # all skills for the current phase
    phase_skill_index: int        # index into phase_skills (current skill)
    phase_skills_buffer: list[dict[str, Any]]
    # ^ per-skill push data accumulated while multi-skill phase is in progress.
    #   Each entry is built by _skill_data_from_push() and accumulated by
    #   push_node on every intermediate skill.  On the FINAL skill, all entries
    #   (buffer + current) are consolidated into ONE PhaseRecord before being
    #   appended to phase_history.  Cleared on phase completion or skip.

    # ---- plan/evaluate/push pipeline ----------------------------------------
    current_plan: dict[str, Any] | None
    current_plan_summary: list[dict[str, Any]]
    current_plan_error: str | None
    current_provider_id: str | None
    last_evaluator_verdict: dict[str, Any]
    phase_done: bool

    # ---- evaluator / retry ---------------------------------------------------
    feedback: str            # critique injected into the next specialist call
    evaluator_action: str    # routing marker: "" | retry | accept | escalate
                             # | retry-exhausted | master_revise | commit
