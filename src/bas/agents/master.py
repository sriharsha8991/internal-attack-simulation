"""Master router agent — the campaign director that holds memory of record.

Two LLM-backed entry points:

  plan_phase(memory, phases, completed, foothold)
      Inspect the master memory + the remaining phase list (from the API
      caller). Emit a `PhaseBriefing` that tells the planner WHICH phase to
      tackle next and what context / constraints / open questions the planner
      should address. This is the entry point on a fresh phase or after a
      previous phase commits.

  review_plan(briefing, plan_summary, memory, evaluator_verdict, attempt)
      The evaluator has already accepted (or escalated) the plan. The master
      gets one final look. It can either:
        * `commit` -> approve push + memory update
        * `revise` -> reject with comments; the planner must restart this phase
                      addressing every point. Only one revise per phase
                      (master_revisions cap = 2: original brief + 1 revise).

  update_memory(memory, briefing, plan_summary, commit)
      Synthesises an updated master memory dict after a successful commit:
      structured keys for machine consumption + a `narrative` line of prose.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from ..llm.base import LLMMessage, LLMProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PhaseBriefing(BaseModel):
    """What the master router hands to the planner at the start of a phase."""

    phase: str = Field(
        description=(
            "Canonical kill-chain phase to plan for (e.g. 'discovery'). MUST be "
            "drawn from the `available_phases` list provided in the prompt."
        )
    )
    objective: str = Field(
        description="One-sentence statement of what success looks like for this phase."
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="Hard constraints (platform, scope, OPSEC, tool allowlist).",
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="Specific intel gaps the planner should address in this phase.",
    )
    rationale: str = Field(
        default="",
        description="Why this phase is next given current master memory.",
    )
    done: bool = Field(
        default=False,
        description=(
            "True iff there is nothing left to do (every requested phase complete "
            "or no productive next phase). When true, the orchestrator halts."
        ),
    )


CommitAction = Literal["commit", "revise"]


class MasterDecision(BaseModel):
    """Verdict the master returns after reviewing an evaluator-approved plan."""

    action: CommitAction = Field(
        description=(
            "commit = push abilities + adversary to BAS and update memory; "
            "revise = reject and force the planner to re-plan addressing the "
            "comments below. Only ONE revise per phase is allowed."
        )
    )
    comments: str = Field(
        default="",
        description=(
            "When action=revise, list (numbered) every missing detail, every "
            "command that needs to change, and every datum the planner failed "
            "to incorporate from master memory. The planner MUST obey these on "
            "the next pass."
        ),
    )
    missing: list[str] = Field(
        default_factory=list,
        description="Concrete data items the planner failed to use or collect.",
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class MemoryUpdate(BaseModel):
    """Structured + narrative memory delta the master writes after each commit."""

    facts: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Phase-scoped findings keyed by canonical names: e.g. "
            "{'network': {'cidr': '192.168.1.0/24', 'live_hosts': [...]}}."
        ),
    )
    narrative: str = Field(
        default="",
        description="One-paragraph campaign-log entry for the next phase's planner.",
    )


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------


_MASTER_PLAN_PROMPT = (
    "ROLE\n"
    "  You are the master campaign director of an authorised internal-attack\n"
    "  simulation. You hold the only authoritative copy of master memory and\n"
    "  you decide which kill-chain phase the planner tackles next.\n"
    "\n"
    "TASK\n"
    "  Pick exactly ONE phase from `available_phases`. Emit a PhaseBriefing\n"
    "  that the planner will use as its mission statement. Be specific about\n"
    "  what is already known, what is missing, and what constraints apply.\n"
    "\n"
    "  You will receive a `phase_history` array: a structured record of every\n"
    "  previously completed phase. Use it to:\n"
    "    * Summarise what was accomplished so far — the planner reads your\n"
    "      briefing, NOT the raw history.\n"
    "    * Identify intel gaps the next phase should fill.\n"
    "    * Avoid duplicating work already done.\n"
    "    * Reference concrete findings (CIDRs, hosts, services, creds) from\n"
    "      prior phases in your `constraints` and `open_questions`.\n"
    "\n"
    "RULES\n"
    "  * `phase` MUST be one of `available_phases` and MUST NOT be in\n"
    "    `completed_phases`.\n"
    "  * If `available_phases` is empty OR every phase the caller requested is\n"
    "    completed, set `done=true` and leave `phase` as the empty string.\n"
    "  * The objective is one sentence. Constraints reference the foothold\n"
    "    (platform, IP) and known scope.\n"
    "  * Open questions are concrete intel gaps phrased as questions the\n"
    "    planner can answer with commands.\n"
    "  * When writing constraints, include relevant data from phase_history\n"
    "    (e.g. discovered CIDRs, live hosts, creds obtained) so the planner\n"
    "    can reference them directly in commands.\n"
)


_MASTER_REVIEW_PROMPT = (
    "ROLE\n"
    "  Still the master campaign director. The planner has produced a plan\n"
    "  that the evaluator already approved (or escalated). You are the FINAL\n"
    "  gate before the abilities + adversary are pushed to the platform.\n"
    "\n"
    "TASK\n"
    "  Decide commit or revise.\n"
    "\n"
    "RULES\n"
    "  * `commit` if the plan satisfies the briefing AND fills in every\n"
    "    relevant `open_question` AND is consistent with master memory.\n"
    "  * `revise` ONLY when the plan is missing something the briefing asked\n"
    "    for, or it ignored data from master memory, or it would write\n"
    "    commands that contradict the constraints. Numbered, prescriptive\n"
    "    `comments`. List every concrete `missing` item.\n"
    "  * IMPORTANT: this is the LAST chance to revise. If `revise_budget` is 0,\n"
    "    you MUST commit (return action='commit'), even if you have minor\n"
    "    nits \u2014 put them in `comments` for the audit log only.\n"
)


_MEMORY_UPDATE_PROMPT = (
    "ROLE\n"
    "  Master memory keeper. The planner's plan has been committed. Distil the\n"
    "  plan's intent and the briefing's objective into a memory delta the next\n"
    "  phase's planner will read.\n"
    "\n"
    "TASK\n"
    "  Emit a MemoryUpdate. `facts` carries STRUCTURED keys (e.g. network,\n"
    "  ad, creds, lateral) with sub-keys. `narrative` is one paragraph in the\n"
    "  voice of an operator briefing a teammate.\n"
    "\n"
    "RULES\n"
    "  * Do not invent findings. Only record what the COMMANDS in the plan\n"
    "    would actually produce if executed successfully. If a command was\n"
    "    aspirational (e.g. 'discover live hosts'), record the EXPECTATION\n"
    "    keyed under `pending.<area>` rather than as a confirmed fact.\n"
    "  * Merge with existing memory: preserve prior keys unless this phase\n"
    "    explicitly supersedes them.\n"
)

_MASTER_ANALYSE_TRIAGE_PROMPT = (
    "ROLE\n"
    "  Master campaign director reviewing EXECUTION RESULTS from the BAS\n"
    "  backend. You have the structural summary of what each ability/stage\n"
    "  produced when run on the target.\n"
    "\n"
    "TASK\n"
    "  Identify which abilities deserve DEEPER inspection of their raw\n"
    "  stdout/stderr. Name each ability by its exact `name` field from the\n"
    "  summary. Focus on abilities that:\n"
    "    * Passed and likely produced extractable intel (IPs, hosts, creds,\n"
    "      service names, paths).\n"
    "    * Failed with interesting stderr (permission denied, partial data).\n"
    "  Skip abilities that clearly timed out or had placeholder errors.\n"
    "\n"
    "OUTPUT\n"
    "  A plain-text list of ability names, one per line. Nothing else.\n"
)

_MASTER_ANALYSE_EXTRACT_PROMPT = (
    "ROLE\n"
    "  Master memory keeper. You are reviewing ACTUAL execution output (stdout\n"
    "  and stderr) from completed abilities. Your job is to extract confirmed\n"
    "  facts from the output and record them in structured memory.\n"
    "\n"
    "TASK\n"
    "  Emit a MemoryUpdate. `facts` carries STRUCTURED keys (e.g. network,\n"
    "  ad, creds, lateral, services) with concrete values extracted from the\n"
    "  output. `narrative` is one paragraph summarising what was confirmed.\n"
    "\n"
    "RULES\n"
    "  * Only record facts that are CONFIRMED by output. Do not speculate.\n"
    "  * If exit_code != 0, note failures under `issues.{phase}` rather\n"
    "    than as confirmed facts.\n"
    "  * If stdout contains IPs, CIDRs, hostnames, service names, usernames,\n"
    "    or file paths, extract them into structured keys.\n"
    "  * Merge with existing memory: preserve prior keys unless this output\n"
    "    explicitly supersedes them.\n"
    "  * Delete any `pending.*` keys that are now confirmed or contradicted.\n"
)


# ---------------------------------------------------------------------------
# Protocol + LLM-backed implementation
# ---------------------------------------------------------------------------


class MasterPolicy(Protocol):
    def plan_phase(
        self,
        *,
        foothold: dict,
        memory: dict,
        available_phases: list[str],
        completed_phases: list[str],
        phase_history: list[dict],
        attempt: int,
    ) -> PhaseBriefing: ...

    def review_plan(
        self,
        *,
        briefing: PhaseBriefing,
        plan_summary: list[dict],
        memory: dict,
        evaluator_verdict: dict,
        revise_budget: int,
    ) -> MasterDecision: ...

    def update_memory(
        self,
        *,
        memory: dict,
        briefing: PhaseBriefing,
        plan_summary: list[dict],
    ) -> MemoryUpdate: ...

    def analyse_results(
        self,
        *,
        results_dir: str | None,
        operation_id: str,
        structural_summary: str,
        current_memory: dict,
    ) -> MemoryUpdate: ...


class LLMMasterRouter:
    """Default master router. Three LLM round-trips per phase (plan, review,
    memory update). Each call uses structured output to keep parsing safe."""

    def __init__(self, llm: LLMProvider, *, temperature: float = 0.1) -> None:
        self._llm = llm
        self._temperature = temperature

    def plan_phase(
        self,
        *,
        foothold: dict,
        memory: dict,
        available_phases: list[str],
        completed_phases: list[str],
        phase_history: list[dict],
        attempt: int,
    ) -> PhaseBriefing:
        payload = {
            "foothold": foothold,
            "memory": memory,
            "available_phases": available_phases,
            "completed_phases": completed_phases,
            "phase_history": phase_history,
            "attempt": attempt,
        }
        msgs = [
            LLMMessage(role="system", content=_MASTER_PLAN_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    "Emit a PhaseBriefing JSON. Current campaign state:\n\n"
                    + json.dumps(payload, indent=2, default=str)
                ),
            ),
        ]
        return self._llm.generate_structured(
            msgs, PhaseBriefing, grounding="skip", temperature=self._temperature
        )

    def review_plan(
        self,
        *,
        briefing: PhaseBriefing,
        plan_summary: list[dict],
        memory: dict,
        evaluator_verdict: dict,
        revise_budget: int,
    ) -> MasterDecision:
        payload = {
            "briefing": briefing.model_dump(),
            "plan_summary": plan_summary,
            "memory": memory,
            "evaluator_verdict": evaluator_verdict,
            "revise_budget": revise_budget,
        }
        msgs = [
            LLMMessage(role="system", content=_MASTER_REVIEW_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    "Emit a MasterDecision JSON. revise_budget tells you how "
                    "many more revises you may still spend (0 means commit).\n\n"
                    + json.dumps(payload, indent=2, default=str)
                ),
            ),
        ]
        verdict = self._llm.generate_structured(
            msgs, MasterDecision, grounding="skip", temperature=self._temperature
        )
        # Hard guard: if budget is exhausted the master MUST commit.
        if revise_budget <= 0 and verdict.action == "revise":
            logger.warning(
                "[master] revise_budget exhausted; forcing commit despite "
                "model returning revise with comments=%r",
                verdict.comments[:200],
            )
            verdict = MasterDecision(
                action="commit",
                comments=verdict.comments,
                missing=verdict.missing,
                confidence=verdict.confidence,
            )
        return verdict

    def update_memory(
        self,
        *,
        memory: dict,
        briefing: PhaseBriefing,
        plan_summary: list[dict],
    ) -> MemoryUpdate:
        payload = {
            "current_memory": memory,
            "briefing": briefing.model_dump(),
            "plan_summary": plan_summary,
        }
        msgs = [
            LLMMessage(role="system", content=_MEMORY_UPDATE_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    "Emit a MemoryUpdate JSON merging this phase's intent "
                    "into master memory.\n\n"
                    + json.dumps(payload, indent=2, default=str)
                ),
            ),
        ]
        return self._llm.generate_structured(
            msgs, MemoryUpdate, grounding="skip", temperature=0.0
        )

    def analyse_results(
        self,
        *,
        results_dir: str | None,
        operation_id: str,
        structural_summary: str,
        current_memory: dict,
    ) -> MemoryUpdate:
        """Two-step LLM analysis of execution results.

        Step A (unstructured): triage which abilities need deeper inspection.
        Step B (structured): extract confirmed facts from selected stage output.
        """
        from ..tools.master_tools import read_stage_output

        # Step A — triage: identify abilities worth deeper inspection
        triage_msgs = [
            LLMMessage(role="system", content=_MASTER_ANALYSE_TRIAGE_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    "Structural summary of execution results:\n\n"
                    + structural_summary
                ),
            ),
        ]
        try:
            triage_text = self._llm.generate(
                triage_msgs, grounding="skip", temperature=0.0
            )
            ability_names = [
                line.strip().lstrip("- •*")
                for line in triage_text.strip().splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
        except Exception as exc:
            logger.warning("[master/analyse] triage step failed: %s; inspecting all", exc)
            ability_names = []

        # Collect detailed output for selected (or all) abilities
        detailed_output = ""
        if results_dir and ability_names:
            parts: list[str] = []
            for name in ability_names[:10]:  # cap to prevent context blowup
                out = read_stage_output(results_dir, operation_id, name.strip())
                if not out.startswith("[no matching"):
                    parts.append(out)
            detailed_output = "\n".join(parts)

        if not detailed_output and results_dir:
            # Fallback: grab output for ALL abilities (first 5)
            from ..results import parse_operation_result
            from ..tools.master_tools import _load_result

            raw = _load_result(results_dir, operation_id)
            if raw:
                op = parse_operation_result(raw)
                parts = []
                for ab in op.abilities[:5]:
                    out = read_stage_output(results_dir, operation_id, ab.name)
                    if not out.startswith("[no matching"):
                        parts.append(out)
                detailed_output = "\n".join(parts)

        # Step B — extract confirmed facts from output
        extract_payload = (
            f"Structural summary:\n{structural_summary}\n\n"
            f"Current memory:\n{json.dumps(current_memory, indent=2, default=str)}\n"
        )
        if detailed_output:
            extract_payload += f"\nDetailed stage output:\n{detailed_output}\n"

        extract_msgs = [
            LLMMessage(role="system", content=_MASTER_ANALYSE_EXTRACT_PROMPT),
            LLMMessage(role="user", content=extract_payload),
        ]
        return self._llm.generate_structured(
            extract_msgs, MemoryUpdate, grounding="skip", temperature=0.0
        )


class StaticMasterRouter:
    """Deterministic master used in tests/dry-run.

    Picks phases in the order they appear in `available_phases`, commits every
    plan unconditionally, and stores plan rationales in memory.narrative.
    """

    def plan_phase(
        self,
        *,
        foothold: dict,
        memory: dict,
        available_phases: list[str],
        completed_phases: list[str],
        phase_history: list[dict],
        attempt: int,
    ) -> PhaseBriefing:
        remaining = [p for p in available_phases if p not in completed_phases]
        if not remaining:
            return PhaseBriefing(
                phase="", objective="all phases complete", done=True
            )
        phase = remaining[0]
        return PhaseBriefing(
            phase=phase,
            objective=f"static brief: execute {phase}",
            rationale="static master picks the next requested phase",
        )

    def review_plan(self, **_: object) -> MasterDecision:
        return MasterDecision(action="commit", confidence=1.0)

    def update_memory(
        self,
        *,
        memory: dict,
        briefing: PhaseBriefing,
        plan_summary: list[dict],
    ) -> MemoryUpdate:
        return MemoryUpdate(
            facts={},
            narrative=f"static commit of phase {briefing.phase} ({len(plan_summary)} abilities)",
        )

    def analyse_results(
        self,
        *,
        results_dir: str | None,
        operation_id: str,
        structural_summary: str,
        current_memory: dict,
    ) -> MemoryUpdate:
        return MemoryUpdate(
            facts={},
            narrative=f"static analysis of operation {operation_id}",
        )
