"""Generic specialist — one function, parameterised by skill.

Pipeline:
    1. `skill_tool.read(state["next_stage"])`               — pull playbook
    2. `planner.plan(skill, state)`                          — LLM emits a SpecialistPlan
    3. For each ability in plan:
         POST /abilities           -> ability_id
         POST /abilities/{id}/stages (×N)
    4. POST /adversaries                                     -> adversary_id
    5. POST /adversaries/{adv}/abilities/{ab}  (link, ×N)
    6. Return PushResult                                     — IDs + provenance

The router (in graph.py) decides which skill runs next; this module never picks.

`Planner` is a Protocol so dry-run smoke tests can swap a `StaticPlanner` in
without an LLM round-trip.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from ..client import BasClient, BasClientError
from ..llm.base import LLMMessage, LLMProvider
from ..models import AdversaryCreate, GeneratedAbility
from ..persistence import ArtifactStore
from ..skills import Skill
from ..tools.skill_tool import SkillTool
from .prompt_profiles import PromptProfile, get_profile

if TYPE_CHECKING:
    from ..orchestrator.state import SessionState

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Plan + result shapes
# ----------------------------------------------------------------------------


class SpecialistPlan(BaseModel):
    """What the planner emits — one adversary + the abilities that compose it."""

    adversary: AdversaryCreate
    abilities: list[GeneratedAbility] = Field(min_length=1)


class PushResult(BaseModel):
    """Outcome of one specialist run. M2 feedback fields are reserved."""

    skill: str
    success: bool
    adversary_id: str | None = None
    ability_ids: list[str] = Field(default_factory=list)
    stage_ids: list[str] = Field(default_factory=list)
    linked_ability_ids: list[str] = Field(default_factory=list)
    provider_id: str | None = None
    rationale: str | None = None
    error: str | None = None
    plan_summary: list[dict] = Field(default_factory=list)
    """Compact view of what was emitted, fed to the evaluator. One entry per
    ability: {name, mitre_technique_id, platform, executor, command_templates}."""


# ----------------------------------------------------------------------------
# Planner protocol + implementations
# ----------------------------------------------------------------------------


class Planner(Protocol):
    def plan(
        self,
        skill: Skill,
        state: SessionState,
        *,
        feedback: str | None = None,
    ) -> SpecialistPlan: ...




class LLMPlanner:
    """Default planner — uses the configured LLM provider via structured output.

    The system prompt is composed from a phase-scoped `PromptProfile` looked up
    by the skill's `frontmatter.stage`, so different kill-chain phases can
    inject different requirements / completion criteria without changing the
    agent class.
    """

    def __init__(
        self,
        llm: LLMProvider,
        *,
        temperature: float = 0.2,
        profile_resolver=None,
    ) -> None:
        self._llm = llm
        self._temperature = temperature
        # Hook lets callers override the skill -> profile mapping for tests.
        self._profile_resolver = profile_resolver or (
            lambda skill: get_profile(getattr(skill.frontmatter, "stage", None))
        )

    # Keywords that hint the planner needs vendor-correct package ids /
    # install commands. Used to trigger an optional grounded preflight call.
    _INSTALL_KEYWORDS: tuple[str, ...] = (
        "install",
        "package",
        "winget",
        "choco",
        "apt",
        "apt-get",
        "dnf",
        "yum",
        "brew",
        "pacman",
        "missing tool",
        "not installed",
        "command not found",
    )

    def plan(
        self,
        skill: Skill,
        state: SessionState,
        *,
        feedback: str | None = None,
    ) -> SpecialistPlan:
        skill_md = skill.render_for_prompt()
        profile: PromptProfile = self._profile_resolver(skill)

        # ---- optional grounded preflight ------------------------------------
        # Gemini forbids combining `google_search` (grounding) with
        # `response_schema` (structured output) in a single call. So we run
        # the research step SEPARATELY as ungrounded-or-grounded `research()`
        # and splice the result into the structured planner prompt as
        # additional context. The structured call below always uses
        # grounding="skip".
        research_block = ""
        if feedback and any(
            kw in feedback.lower() for kw in self._INSTALL_KEYWORDS
        ):
            platform = (state.get("foothold") or {}).get("platform") or "unknown"
            tool_allow = ", ".join(skill.frontmatter.tool_allowlist or []) or "(none listed)"
            query = (
                "Operator-style red-team preflight research.\n"
                f"Foothold platform: {platform}.\n"
                f"Skill tool allowlist: {tool_allow}.\n"
                "List the CANONICAL native-package-manager IDs for the tools "
                "an operator would need on this platform. Use winget/choco "
                "for windows, apt-get/dnf for linux, brew for mac. Output one "
                "line per tool in the form `tool=<pkg-manager> <package-id>`. "
                "Stick to widely-shipped IDs; no commentary."
            )
            try:
                rr = self._llm.research(query, depth="light")
            except Exception as exc:  # noqa: BLE001 - research is best-effort
                logger.warning("[plan] preflight research failed: %s", exc)
                rr = None
            if rr and rr.text:
                research_block = (
                    "\n\n--- TOOL RESEARCH (grounded preflight) ---\n"
                    + rr.text.strip()
                )
                if rr.citations:
                    research_block += "\nCitations: " + ", ".join(rr.citations[:6])

        system = (
            f"{profile.specialist_system}\n\n--- SKILL PLAYBOOK ---\n{skill_md}"
            + research_block
        )
        user_payload = {
            "foothold": state.get("foothold", {}),
            "memory": state.get("memory", {}),
            "completed_stages": state.get("completed_stages", []),
            "run_id": state.get("run_id"),
        }
        import json
        user_parts = [
            "Emit a SpecialistPlan as JSON matching the schema.",
            "Session context follows:",
            "",
            json.dumps(user_payload, indent=2, default=str),
        ]
        if feedback:
            user_parts.extend(
                [
                    "",
                    "--- EVALUATOR / MASTER FEEDBACK (must be addressed) ---",
                    feedback.strip(),
                ]
            )
        msgs = [
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content="\n".join(user_parts)),
        ]
        # Structured output ALWAYS ungrounded (provider constraint).
        plan = self._llm.generate_structured(
            msgs, SpecialistPlan, grounding="skip", temperature=self._temperature
        )

        # ---- self-critique (lightweight internal review) --------------------
        # Before handing to the evaluator, the planner does ONE self-review
        # pass. If it finds issues, it re-generates the plan with the critique
        # spliced in. This catches obvious misses (wrong platform commands,
        # unreferenced variables, missing install steps) without burning an
        # evaluator round-trip.
        try:
            plan = self._self_critique(plan, skill, state, system)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[plan/self-critique] failed: %s; using original plan", exc)

        return plan

    # ---- self-critique helper -----------------------------------------------

    _SELF_CRITIQUE_PROMPT = (
        "You are the same red-team operator who just drafted the plan below.\n"
        "Review it against these checks and emit a SHORT numbered list of\n"
        "issues ONLY. If the plan is clean, respond with exactly: LGTM\n"
        "\n"
        "CHECKS:\n"
        "  1. PLACEHOLDER / TEMPLATE TOKENS: Scan every command_template for\n"
        "     tokens like #{...}, <TARGET>, <CIDR>, {{...}}. These are NEVER\n"
        "     substituted at runtime. If any exist, the command will fail.\n"
        "     Fix: compute the value inline or read from a file written by\n"
        "     an earlier ability.\n"
        "  2. CROSS-ABILITY VARIABLE LEAKAGE: Each ability runs in a FRESH\n"
        "     shell session. A variable set in ability A is empty in\n"
        "     ability B. Fix: pass data between abilities via files in the\n"
        "     temp directory.\n"
        "  3. Every non-builtin tool has a check+install ability before its\n"
        "     first use — a single inline if/else pattern (check → install\n"
        "     → verify). NOT two separate check and install abilities.\n"
        "  4. Output paths use the platform's standard temp directory under\n"
        "     a 'bas' subdirectory. Create the dir if it might not exist.\n"
        "  5. No unnecessary output filters on raw collection commands;\n"
        "     capture full output when gathering intel.\n"
        "  6. Platform consistency: all commands must be native to the\n"
        "     foothold's platform.\n"
    )

    def _self_critique(
        self,
        plan: SpecialistPlan,
        skill: Skill,
        state: "SessionState",
        system_prompt: str,
    ) -> SpecialistPlan:
        """One-shot self-review. Returns original plan if LGTM, else re-plans."""
        import json as _json

        summary = []
        for gen in plan.abilities:
            stages_compact = [
                {"order": s.stage_order, "executor": s.executor,
                 "cmd": s.command_template}
                for s in gen.stages
            ]
            summary.append({
                "name": gen.ability.name,
                "mitre_technique_id": gen.ability.mitre_technique_id,
                "platform": gen.ability.platform,
                "stages": stages_compact,
            })

        review_msg = (
            "Plan to review:\n"
            + _json.dumps(summary, indent=2, default=str)
            + "\n\nFoothold: "
            + _json.dumps(state.get("foothold", {}), indent=2, default=str)
            + "\n\nMemory keys: "
            + str(list((state.get("memory") or {}).keys()))
        )
        msgs = [
            LLMMessage(role="system", content=self._SELF_CRITIQUE_PROMPT),
            LLMMessage(role="user", content=review_msg),
        ]
        critique = self._llm.chat(msgs, grounding="skip", temperature=0.0)
        critique = critique.strip()

        if not critique or critique.upper().startswith("LGTM"):
            logger.info("[plan/self-critique] plan passed self-review")
            return plan

        logger.info(
            "[plan/self-critique] found issues, re-generating: %s",
            critique[:300],
        )
        # Re-plan with the critique as additional feedback.
        user_payload = {
            "foothold": state.get("foothold", {}),
            "memory": state.get("memory", {}),
            "completed_stages": state.get("completed_stages", []),
            "run_id": state.get("run_id"),
        }
        user_parts = [
            "Emit a SpecialistPlan as JSON matching the schema.",
            "Session context follows:",
            "",
            _json.dumps(user_payload, indent=2, default=str),
            "",
            "--- SELF-CRITIQUE (fix every issue below) ---",
            critique,
        ]
        msgs2 = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content="\n".join(user_parts)),
        ]
        return self._llm.generate_structured(
            msgs2, SpecialistPlan, grounding="skip", temperature=self._temperature
        )


class StaticPlanner:
    """Test / dry-run planner that returns a pre-built plan keyed by skill name."""

    def __init__(self, plans: dict[str, SpecialistPlan]) -> None:
        self._plans = plans

    def plan(
        self,
        skill: Skill,
        state: SessionState,
        *,
        feedback: str | None = None,
    ) -> SpecialistPlan:
        name = skill.frontmatter.name
        if name not in self._plans:
            raise KeyError(f"StaticPlanner has no plan for skill {name!r}")
        return self._plans[name]


# ----------------------------------------------------------------------------
# Specialist entry points — split: plan (no side effects) + push (BAS + disk)
# ----------------------------------------------------------------------------


class PlanResult(BaseModel):
    """Output of `plan_specialist`. No BAS calls have been made yet."""

    skill: str
    success: bool
    plan: SpecialistPlan | None = None
    plan_summary: list[dict] = Field(default_factory=list)
    provider_id: str | None = None
    error: str | None = None


def plan_specialist(
    state: SessionState,
    *,
    planner: Planner,
    skill_tool: SkillTool,
    feedback: str | None = None,
) -> PlanResult:
    """LLM-only step: ask the planner for a `SpecialistPlan`. No BAS, no disk."""
    skill_name = state.get("next_stage")
    if not skill_name or skill_name == "DONE":
        return PlanResult(skill=str(skill_name), success=False, error="no skill")
    if not skill_tool.has(skill_name):
        return PlanResult(
            skill=skill_name, success=False, error=f"unknown skill {skill_name!r}"
        )

    skill = skill_tool.read(skill_name)
    logger.info(
        "[plan] skill=%s stage=%s tactics=%s tools=%d foothold=%s/%s feedback=%s",
        skill_name,
        skill.frontmatter.stage,
        ",".join(skill.frontmatter.mitre_tactics or []),
        len(skill.frontmatter.tool_allowlist or []),
        state.get("foothold", {}).get("hostname"),
        state.get("foothold", {}).get("platform"),
        "yes" if feedback else "no",
    )
    try:
        plan = planner.plan(skill, state, feedback=feedback)
    except Exception as exc:  # noqa: BLE001 - planner failure is non-fatal
        logger.exception("[plan] planner failed for skill=%s", skill_name)
        return PlanResult(skill=skill_name, success=False, error=f"planner: {exc}")

    # Force created_by="ai" — the backend uses this flag to auto-route.
    plan.adversary.created_by = "ai"
    for gen in plan.abilities:
        gen.ability.created_by = "ai"

    provider_id = getattr(planner, "_llm", None)
    provider_id = provider_id.provider_id if provider_id is not None else None
    summary = _summarise_plan(plan)
    logger.info(
        "[plan] skill=%s planner=%s adversary=%r abilities=%d",
        skill_name,
        type(planner).__name__,
        plan.adversary.name,
        len(plan.abilities),
    )
    return PlanResult(
        skill=skill_name,
        success=True,
        plan=plan,
        plan_summary=summary,
        provider_id=provider_id,
    )


def push_specialist(
    state: SessionState,
    *,
    plan: SpecialistPlan,
    skill_tool: SkillTool,
    bas: BasClient,
    artifacts: ArtifactStore | None = None,
    provider_id: str | None = None,
) -> PushResult:
    """Push an already-approved `SpecialistPlan` to BAS and write artifacts."""
    skill_name = state.get("next_stage") or ""
    if not skill_name or skill_name == "DONE" or not skill_tool.has(skill_name):
        return PushResult(skill=skill_name, success=False, error="no skill to push")

    # ---- 2 & 3. push abilities + stages -------------------------------------
    ability_ids: list[str] = []
    stage_ids: list[str] = []
    pushed_stages_by_ability: dict[str, list] = {}
    engagement_id = state.get("run_id")
    try:
        for gen in plan.abilities:
            ab_resp = bas.abilities.create(gen.ability)
            ab_id = _id_of(ab_resp.ability_id)
            ability_ids.append(ab_id)
            pushed_stages_by_ability[ab_id] = []
            logger.info(
                "[bas] POST /abilities -> %s  (name=%r tactic=%s platform=%s)",
                ab_id,
                gen.ability.name,
                gen.ability.mitre_tactic,
                gen.ability.platform,
            )
            for stage in gen.stages:
                st_resp = bas.abilities.create_stage(ab_resp.ability_id, stage)
                stage_ids.append(_id_of(st_resp.stage_id))
                pushed_stages_by_ability[ab_id].append(stage)
                logger.info(
                    "[bas] POST /abilities/%s/stages -> %s  (order=%d executor=%s)",
                    ab_id,
                    _id_of(st_resp.stage_id),
                    stage.stage_order,
                    stage.executor,
                )
            if artifacts is not None and engagement_id:
                path = artifacts.write_ability(
                    engagement_id,
                    ability_id=ab_id,
                    skill=skill_name,
                    ability=gen.ability,
                    stages=pushed_stages_by_ability[ab_id],
                    rationale=gen.rationale,
                    provider=gen.provider or provider_id,
                    cited_urls=gen.cited_urls,
                )
                logger.info("[artifacts] wrote ability spec -> %s", path)
    except BasClientError as exc:
        logger.error("[bas] ability/stage push failed: %s", exc)
        return PushResult(
            skill=skill_name,
            success=False,
            ability_ids=ability_ids,
            stage_ids=stage_ids,
            provider_id=provider_id,
            error=f"push abilities/stages: {exc}",
        )

    # ---- 4. push adversary --------------------------------------------------
    try:
        adv_resp = bas.adversaries.create(plan.adversary)
        adversary_id = _id_of(adv_resp.adversary_id)
        logger.info(
            "[bas] POST /adversaries -> %s  (name=%r created_by=%s)",
            adversary_id,
            plan.adversary.name,
            plan.adversary.created_by,
        )
    except BasClientError as exc:
        logger.error("[bas] adversary push failed: %s", exc)
        return PushResult(
            skill=skill_name,
            success=False,
            ability_ids=ability_ids,
            stage_ids=stage_ids,
            provider_id=provider_id,
            error=f"push adversary: {exc}",
        )

    # ---- 5. link ------------------------------------------------------------
    linked: list[str] = []
    try:
        for ab_id in ability_ids:
            ok = bas.adversaries.link_ability(adversary_id, ab_id)
            if ok:
                linked.append(ab_id)
                logger.info(
                    "[bas] POST /adversaries/%s/abilities/%s linked",
                    adversary_id,
                    ab_id,
                )
            else:
                logger.warning(
                    "[bas] link adversary=%s ability=%s returned falsy",
                    adversary_id,
                    ab_id,
                )
    except BasClientError as exc:
        logger.error("[bas] link failed: %s", exc)
        return PushResult(
            skill=skill_name,
            success=False,
            adversary_id=adversary_id,
            ability_ids=ability_ids,
            stage_ids=stage_ids,
            linked_ability_ids=linked,
            provider_id=provider_id,
            error=f"link: {exc}",
        )

    rationale = "; ".join(g.rationale for g in plan.abilities if g.rationale)

    if artifacts is not None and engagement_id:
        path = artifacts.write_adversary(
            engagement_id,
            adversary_id=adversary_id,
            skill=skill_name,
            adversary=plan.adversary,
            linked_ability_ids=linked,
        )
        logger.info("[artifacts] wrote adversary spec -> %s", path)

    plan_summary = _summarise_plan(plan)

    return PushResult(
        skill=skill_name,
        success=len(linked) == len(ability_ids) and bool(ability_ids),
        adversary_id=adversary_id,
        ability_ids=ability_ids,
        stage_ids=stage_ids,
        linked_ability_ids=linked,
        provider_id=provider_id,
        rationale=rationale or None,
        plan_summary=plan_summary,
    )


def _summarise_plan(plan: SpecialistPlan) -> list[dict]:
    """Compact view of the emitted plan for the evaluator + audit log."""
    out: list[dict] = []
    for gen in plan.abilities:
        out.append(
            {
                "name": gen.ability.name,
                "mitre_tactic": gen.ability.mitre_tactic,
                "mitre_technique_id": gen.ability.mitre_technique_id,
                "platform": gen.ability.platform,
                "stages": [
                    {
                        "order": s.stage_order,
                        "executor": s.executor,
                        "command_template": s.command_template,
                    }
                    for s in gen.stages
                ],
                "rationale": gen.rationale,
            }
        )
    return out




def _id_of(uid: UUID | str) -> str:
    return str(uid)
