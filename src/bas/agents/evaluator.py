"""Evaluator agent — critiques a freshly-emitted SpecialistPlan.

The orchestrator runs the evaluator *after* the specialist has pushed its
abilities + adversary to the BAS backend. The evaluator does not undo the
push; it inspects the `PushResult.plan_summary` (which is a compact view of
what the specialist actually emitted) against the skill contract and decides:

  - ``accept``   the plan satisfies the skill — proceed to the router.
  - ``retry``    the plan has fixable defects — re-dispatch the same skill,
                 injecting the evaluator's `feedback` string into the
                 specialist prompt.
  - ``escalate`` the plan is fundamentally wrong and retrying won't help
                 (e.g. wrong platform, wrong tactic). Skip to the router and
                 let it choose a different next stage.

The evaluator runs locally (no BAS calls) and burns one LLM round-trip per
critique.
"""

from __future__ import annotations

import json
import logging
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from ..llm.base import LLMMessage, LLMProvider
from ..skills import Skill
from .prompt_profiles import PromptProfile, get_profile

logger = logging.getLogger(__name__)

EvaluatorAction = Literal["accept", "retry", "escalate"]


class EvaluatorVerdict(BaseModel):
    """Structured output the evaluator emits per critique."""

    action: EvaluatorAction = Field(
        description=(
            "accept = plan satisfies the skill; retry = fixable defects, re-dispatch "
            "the same skill with `feedback`; escalate = abandon this skill."
        )
    )
    feedback: str = Field(
        default="",
        description=(
            "Operator-voice critique. When action=retry, MUST list every fix the "
            "specialist needs to apply, in numbered form."
        ),
    )
    mismatches: list[str] = Field(
        default_factory=list,
        description="Concise list of contract violations found (one per item).",
    )
    phase_done: bool = Field(
        default=False,
        description=(
            "True iff, after applying this plan, the phase's completion criteria "
            "are satisfied (see COMPLETION CRITERIA in the prompt). The router "
            "uses this hint to stop pestering the same phase."
        ),
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)




class EvaluatorPolicy(Protocol):
    def evaluate(
        self,
        *,
        skill: Skill,
        foothold: dict,
        memory: dict,
        completed_stages: list[str],
        plan_summary: list[dict],
        push_success: bool,
        push_error: str | None,
        attempt: int,
    ) -> EvaluatorVerdict: ...


class LLMEvaluator:
    """Default evaluator backed by the configured LLM provider.

    System prompt comes from the phase-scoped `PromptProfile` so each phase
    can plug in its own completion criteria + checklist.
    """

    def __init__(
        self,
        llm: LLMProvider,
        *,
        temperature: float | None = None,
        profile_resolver=None,
    ) -> None:
        self._llm = llm
        self._temperature = temperature
        self._profile_resolver = profile_resolver or (
            lambda skill: get_profile(getattr(skill.frontmatter, "stage", None))
        )

    def evaluate(
        self,
        *,
        skill: Skill,
        foothold: dict,
        memory: dict,
        completed_stages: list[str],
        plan_summary: list[dict],
        push_success: bool,
        push_error: str | None,
        attempt: int,
    ) -> EvaluatorVerdict:
        profile: PromptProfile = self._profile_resolver(skill)
        criteria_block = (
            f"\n\n--- COMPLETION CRITERIA ({profile.phase}) ---\n"
            f"{profile.completion_criteria}"
            if profile.completion_criteria
            else ""
        )
        system = (
            f"{profile.evaluator_system}{criteria_block}\n\n"
            f"--- SKILL PLAYBOOK ---\n{skill.render_for_prompt()}"
        )
        user_payload = {
            "foothold": foothold,
            "memory": memory,
            "completed_stages": completed_stages,
            "attempt": attempt,
            "push_success": push_success,
            "push_error": push_error,
            "plan_summary": plan_summary,
        }
        user = (
            "Grade the following plan against the skill contract and emit an "
            "EvaluatorVerdict as JSON.\n\n"
            + json.dumps(user_payload, indent=2, default=str)
        )
        msgs = [
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content=user),
        ]
        return self._llm.generate_structured(
            msgs, EvaluatorVerdict, grounding="skip", temperature=self._temperature,
            thinking="high",  # grading against criteria, not generating a plan
        )


class StaticAcceptEvaluator:
    """No-LLM evaluator used in dry-run / tests — always accepts."""

    def evaluate(self, **_: object) -> EvaluatorVerdict:
        return EvaluatorVerdict(action="accept", confidence=1.0)
