"""Router policies — decide which specialist to invoke next.

Two implementations:

  LinearRouterPolicy
    Deterministic: walks the supplied stage order, emits DONE at the end.
    No LLM call. Used for offline graph runs, tests, CI.

  LLMRouterPolicy
    Calls `LLMProvider.generate_structured` with the cheap router prompt.
    Sees skill *summaries* only (not full skill bodies) — the prompt stays
    tiny so this can be invoked many times per run.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ..llm.base import LLMMessage, LLMProvider
from ..tools.skill_tool import SkillSummary
from .state import DONE_SENTINEL, SessionState


class RouterDecision(BaseModel):
    """Structured output of every router invocation. Audited via the JSONL log."""

    next_stage: str = Field(
        description=f"Skill name to run next, or {DONE_SENTINEL!r} to terminate."
    )
    reason: str = Field(description="One-sentence rationale for the decision.")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class RouterPolicy(Protocol):
    def route(
        self, state: SessionState, skills: list[SkillSummary]
    ) -> RouterDecision: ...


# ---------------------------------------------------------------------------
# Deterministic policy — no LLM required
# ---------------------------------------------------------------------------


class LinearRouterPolicy:
    """Walks the supplied stage order; emits DONE once everything has run."""

    def __init__(self, order: list[str] | None = None) -> None:
        self._order = order or []

    def route(
        self, state: SessionState, skills: list[SkillSummary]
    ) -> RouterDecision:
        order = self._order or [s.name for s in skills]
        completed = set(state.get("completed_stages") or [])
        for name in order:
            if name not in completed:
                return RouterDecision(
                    next_stage=name,
                    reason=f"linear policy: {name} not yet completed",
                    confidence=1.0,
                )
        return RouterDecision(
            next_stage=DONE_SENTINEL,
            reason="linear policy: all stages completed",
            confidence=1.0,
        )


# ---------------------------------------------------------------------------
# LLM-driven policy
# ---------------------------------------------------------------------------

_ROUTER_SYSTEM_PROMPT = (
    "You are the routing brain of an internal-attack orchestrator. "
    "On each turn you pick exactly ONE skill name from the catalogue (or the "
    f"sentinel {DONE_SENTINEL!r}) based on session memory and the foothold. "
    "You never execute anything yourself; downstream specialists handle that. "
    "Prefer cheap reconnaissance before destructive stages. Stop with "
    f"{DONE_SENTINEL!r} once the kill-chain objective is met or no further "
    "progress is possible."
)


def _build_router_user_message(
    state: SessionState, skills: list[SkillSummary]
) -> str:
    catalogue = [
        {
            "name": s.name,
            "stage": s.stage,
            "description": s.description,
            "mitre_tactics": s.mitre_tactics,
        }
        for s in skills
    ]
    payload = {
        "foothold": state.get("foothold", {}),
        "memory": state.get("memory", {}),
        "completed_stages": state.get("completed_stages", []),
        "iteration": state.get("iteration", 0),
        "skills": catalogue,
        "done_sentinel": DONE_SENTINEL,
    }
    return (
        "Pick the next skill to run. Respond with JSON matching the schema "
        "{next_stage, reason, confidence}. Current state:\n\n"
        + json.dumps(payload, indent=2, default=str)
    )


class LLMRouterPolicy:
    """LLM-driven router. Uses structured-output mode so the response is typed."""

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    def route(
        self, state: SessionState, skills: list[SkillSummary]
    ) -> RouterDecision:
        messages = [
            LLMMessage(role="system", content=_ROUTER_SYSTEM_PROMPT),
            LLMMessage(role="user", content=_build_router_user_message(state, skills)),
        ]
        decision = self._llm.generate_structured(messages, RouterDecision)
        # Defensive: if the model invents a skill name, the dispatcher will
        # still validate against the catalogue.
        return decision


# ---------------------------------------------------------------------------
# ReAct router — reasons over the full history of stage results + memory
# ---------------------------------------------------------------------------


_REACT_ROUTER_SYSTEM_PROMPT = (
    "ROLE\n"
    "  You are the strategic brain of an authorised internal-attack simulation.\n"
    "  Think of yourself as a campaign lead picking the next move based on what\n"
    "  the team has already learned. You DO NOT execute or write commands; you\n"
    "  only choose the next skill (or the DONE sentinel) for the specialist\n"
    "  layer to execute.\n"
    "\n"
    "REACT LOOP\n"
    "  On each turn you receive:\n"
    "    * `foothold`        \u2014 the host we are operating from\n"
    "    * `memory`          \u2014 facts the team has confirmed so far\n"
    "    * `completed_stages` and `stage_results` \u2014 every prior skill +\n"
    "      whether the evaluator accepted it, plus the plan it produced\n"
    "    * `last_evaluator_action` \u2014 accept / retry-exhausted / escalate /\n"
    "      empty (first turn)\n"
    "    * `skills`          \u2014 the catalogue of skills available\n"
    "  Reason in three beats:\n"
    "    THOUGHT  : what does memory tell us is the next logical move?\n"
    "    OBSERVE  : did the last skill actually succeed and produce usable\n"
    "               output? If escalated, why did it fail and which skill is a\n"
    "               better fit?\n"
    "    ACTION   : pick exactly ONE skill name from the catalogue, or DONE.\n"
    "  Put the prose distilled from THOUGHT/OBSERVE into `reason`. Keep it\n"
    "  short (\u22641 sentence). Confidence is your own estimate.\n"
    "\n"
    "PRINCIPLES\n"
    "  * Reconnaissance before destruction. Don't pick credential-access or\n"
    "    lateral-movement skills before discovery has confirmed targets.\n"
    "  * Don't re-run a skill the evaluator already accepted unless memory\n"
    "    shows scope has expanded (e.g. new subnet discovered).\n"
    "  * If a skill was escalated and there is no recovery path, pick a\n"
    "    different but related skill or emit DONE \u2014 don't loop.\n"
    "  * Emit DONE when (a) every relevant phase is complete, (b) the\n"
    "    objective is met, or (c) no further progress is possible.\n"
)


def _summarise_stage_result_for_router(sr: Any) -> dict[str, Any]:
    if isinstance(sr, dict):
        skill = sr.get("skill")
        success = sr.get("success")
        adversary_id = sr.get("adversary_id")
        abilities_pushed = sr.get("abilities_pushed")
        notes = sr.get("notes", "") or ""
        extras = sr.get("extras") or {}
    else:
        skill = getattr(sr, "skill", None)
        success = getattr(sr, "success", None)
        adversary_id = getattr(sr, "adversary_id", None)
        abilities_pushed = getattr(sr, "abilities_pushed", None)
        notes = getattr(sr, "notes", "") or ""
        extras = getattr(sr, "extras", None) or {}

    plan_summary = extras.get("plan_summary") or []
    compact_plan: list[dict[str, Any]] = []
    for ab in plan_summary[:6]:
        first_cmd = ""
        if ab.get("stages"):
            first_cmd = (ab["stages"][0] or {}).get("command_template") or ""
        compact_plan.append(
            {
                "name": ab.get("name"),
                "mitre_technique_id": ab.get("mitre_technique_id"),
                "platform": ab.get("platform"),
                "stages": len(ab.get("stages") or []),
                "first_command": first_cmd[:240],
            }
        )
    return {
        "skill": skill,
        "success": success,
        "adversary_id": adversary_id,
        "abilities_pushed": abilities_pushed,
        "notes": notes,
        "plan": compact_plan,
    }


class ReactRouterPolicy:
    """History-aware LLM router that reasons in THOUGHT/OBSERVE/ACTION beats."""

    def __init__(self, llm: LLMProvider, *, temperature: float = 0.1) -> None:
        self._llm = llm
        self._temperature = temperature

    def route(
        self, state: SessionState, skills: list[SkillSummary]
    ) -> RouterDecision:
        catalogue = [
            {
                "name": s.name,
                "stage": s.stage,
                "description": s.description,
                "mitre_tactics": s.mitre_tactics,
            }
            for s in skills
        ]
        stage_results = state.get("stage_results") or []
        history = [
            _summarise_stage_result_for_router(sr) for sr in stage_results[-8:]
        ]
        payload = {
            "foothold": state.get("foothold", {}),
            "memory": state.get("memory", {}),
            "completed_stages": state.get("completed_stages", []),
            "last_evaluator_action": state.get("evaluator_action", ""),
            "iteration": state.get("iteration", 0),
            "history": history,
            "skills": catalogue,
            "done_sentinel": DONE_SENTINEL,
        }
        user = (
            "Pick the next skill to run. Respond with a RouterDecision JSON "
            "object (next_stage, reason, confidence). Current state:\n\n"
            + json.dumps(payload, indent=2, default=str)
        )
        msgs = [
            LLMMessage(role="system", content=_REACT_ROUTER_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user),
        ]
        return self._llm.generate_structured(
            msgs, RouterDecision, grounding="skip", temperature=self._temperature
        )