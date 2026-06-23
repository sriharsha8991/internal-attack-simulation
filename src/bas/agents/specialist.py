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
from .payload_catalog import PayloadCatalog
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

    stage_id_map: dict[str, dict[str, str]] = Field(default_factory=dict)
    """Per-ability stage ID map: {ability_name -> {stage_name -> stage_id}}.
    Enables Phase 7 feedback loop to target specific stages by ID."""


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
    """Specialist planner driven by an LLM.

    The instance is stateless; context is passed in on every call via the
    agent class.
    """

    # We use LLM string matching to decide if evasion research is needed
    # because the EDR string in memory can be anything.
    _PHASE_RESEARCH_QUERIES = {
        "evasion": "Red team command evasion, AMSI bypass, EDR unhooking",
    }

    def __init__(
        self,
        llm: LLMProvider,
        *,
        temperature: float | None = None,
        catalog: PayloadCatalog | None = None,
    ) -> None:
        self._llm = llm
        self._temperature = temperature
        self._catalog = catalog

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
        "not recognized",
        "binary not found",
        "download",
        "github",
        "release",
        "pip install",
        "gem install",
        "no such file",
        "cannot find",
        "tool not available",
    )

    # Phase-specific research queries — grounded web search to get latest TTPs,
    # evasion techniques, and tool-specific guidance for each phase.
    _PHASE_RESEARCH_QUERIES: dict[str, str] = {
        "discovery": (
            "Latest network discovery techniques for internal penetration testing "
            "2024-2025. Best nmap scan profiles for stealth. Effective Windows/Linux "
            "native commands for host enumeration without installing tools."
        ),
        "credaccess": (
            "Latest credential access techniques 2024-2025. LSASS dump alternatives "
            "that bypass modern EDR. Native Windows credential harvesting without "
            "Mimikatz. DPAPI abuse techniques. Kerberoasting detection evasion."
        ),
        "privesc": (
            "Latest Windows/Linux privilege escalation techniques 2024-2025. "
            "Unpatched privilege escalation CVEs. LOLBins for privesc. "
            "Service permission abuse. Token impersonation techniques."
        ),
        "lateral": (
            "Latest lateral movement techniques 2024-2025 that evade EDR. "
            "WinRM and DCOM-based movement. Pass-the-hash and pass-the-ticket "
            "detection evasion. Living-off-the-land lateral movement."
        ),
        "persistence": (
            "Latest persistence techniques 2024-2025 that evade AV/EDR. "
            "Windows scheduled task and service-based persistence evasion. "
            "Registry-based persistence that survives detection. "
            "Linux persistence via systemd and cron evasion."
        ),
        "defevasion": (
            "Latest defense evasion techniques 2024-2025. AMSI bypass methods "
            "that still work. ETW patching techniques. Windows Defender exclusion "
            "abuse. PowerShell constrained language mode bypass. "
            "EDR unhooking and blinding techniques."
        ),
        "impact": (
            "Latest data staging and exfiltration simulation techniques "
            "2024-2025 for authorized red team exercises. Safe impact "
            "demonstration methods."
        ),
    }

    def plan(
        self,
        skill: Skill,
        state: SessionState,
        *,
        feedback: str | None = None,
    ) -> SpecialistPlan:
        skill_md = skill.render_for_prompt()
        profile: PromptProfile = get_profile(skill.frontmatter.stage)

        # ---- optional grounded preflight ------------------------------------
        # Gemini forbids combining `google_search` (grounding) with
        # `response_schema` (structured output) in a single call. So we run
        # the research step SEPARATELY as ungrounded-or-grounded `research()`
        # and splice the result into the structured planner prompt as
        # additional context. The structured call below always uses
        # grounding="skip".
        research_block = ""

        # Phase-specific research — always performed when a phase query exists.
        # The prior classifier-gated approach almost never triggered research on
        # first pass, leaving agents with stale knowledge. One grounded call per
        # phase is cheap compared to a failed phase due to outdated TTPs.
        phase_stage = getattr(skill.frontmatter, "stage", None) or ""
        issues_to_fix = state.get("issues_to_fix") or []
        is_retry = bool(feedback) or bool(issues_to_fix)
        phase_query = self._PHASE_RESEARCH_QUERIES.get(phase_stage.lower())
        needs_phase_research = bool(phase_query)
        if needs_phase_research and phase_query:
            platform = (state.get("foothold") or {}).get("platform") or "unknown"
            phase_query_full = (
                f"{phase_query}\n"
                f"Target platform: {platform}. "
                f"Focus on techniques that work with native OS tools."
            )
            try:
                rr = self._llm.research(phase_query_full, depth="light")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[plan] phase research failed: %s", exc)
                rr = None
            if rr and rr.text:
                research_block = (
                    "\n\n--- PHASE RESEARCH (latest TTPs) ---\n"
                    + rr.text.strip()
                )
                if rr.citations:
                    research_block += "\nSources: " + ", ".join(rr.citations[:6])

        # Tool acquisition research — triggers on retry when feedback mentions
        # install keywords, AND proactively on first pass when the skill's
        # tool_allowlist contains tools (likely non-native and need sourcing).
        tool_allowlist = skill.frontmatter.tool_allowlist or []
        _feedback_has_install_hint = feedback and any(
            kw in feedback.lower() for kw in self._INSTALL_KEYWORDS
        )
        _first_pass_with_tools = not is_retry and bool(tool_allowlist)
        if _feedback_has_install_hint or _first_pass_with_tools:
            platform = (state.get("foothold") or {}).get("platform") or "unknown"
            tool_allow = ", ".join(tool_allowlist) or "(none listed)"
            if feedback:
                context_line = f"Feedback that triggered this: {feedback[:500]}"
            else:
                context_line = (
                    "First-pass planning: proactively researching best tool "
                    "usage, deployment methods, and native alternatives."
                )
            query = (
                "Red-team tool acquisition research.\n"
                f"Foothold platform: {platform}.\n"
                f"Skill tool allowlist: {tool_allow}.\n"
                f"{context_line}\n\n"
                "Find the CURRENT best way to obtain and deploy the needed "
                "tool(s) on this platform. Consider:\n"
                "  - Latest working download sources\n"
                "  - Package manager availability\n"
                "  - In-memory execution alternatives\n"
                "  - Native OS commands that achieve the same goal\n"
                "  - Whether each tool is already built into the OS\n"
                "Provide actionable results. No hardcoded assumptions."
            )
            try:
                rr = self._llm.research(query, depth="light")
            except Exception as exc:  # noqa: BLE001 - research is best-effort
                logger.warning("[plan] preflight research failed: %s", exc)
                rr = None
            if rr and rr.text:
                research_block += (
                    "\n\n--- TOOL RESEARCH (grounded preflight) ---\n"
                    + rr.text.strip()
                )
                if rr.citations:
                    research_block += "\nCitations: " + ", ".join(rr.citations[:6])

        # Research — if prior attempt had issues (blocked, syntax, logic error, etc)
        if issues_to_fix:
            platform = (state.get("foothold") or {}).get("platform") or "unknown"
            block_query = (
                f"Red team command syntax, execution and evasion for {platform}. "
                f"Specific issues from previous run: {'; '.join(issues_to_fix[:3])}. "
                f"Provide correct LOLBin commands, encoded execution, or fixes "
                f"that achieve the goal without timing out or failing."
            )
            try:
                rr = self._llm.research(block_query, depth="light")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[plan] research failed: %s", exc)
                rr = None
            if rr and rr.text:
                research_block += (
                    "\n\n--- RESEARCH (anti-blocking & syntax fixes) ---\n"
                    + rr.text.strip()
                )
                if rr.citations:
                    research_block += "\nSources: " + ", ".join(rr.citations[:6])

        # Payload catalog block — list pre-uploaded binaries the on-target agent
        # can fetch. The LLM may set `payload_id` on a stage to reference one.
        payloads_block = ""
        if self._catalog is not None:
            platform_for_payloads = (state.get("foothold") or {}).get("platform")
            payloads_block = self._catalog.render_planner_block(
                phase_stage, platform_for_payloads
            )

        system = (
            f"{profile.specialist_system}\n\n--- SKILL PLAYBOOK ---\n{skill_md}"
            + research_block
            + payloads_block
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
            msgs, SpecialistPlan, grounding="skip", temperature=self._temperature,
            thinking="high",  # generating tradecraft needs deep reasoning
        )

        # ---- shell-native syntax validation (every plan) -------------------
        # Fast subprocess call (< 100ms per command) using real shell parsers.
        # Catches broken syntax that the LLM self-critique cannot detect.
        from ..tools.command_validator import format_errors, validate_plan

        shell_results = validate_plan(plan)
        shell_errors = [v for v in shell_results if not v.valid]
        if shell_errors:
            error_report = format_errors(shell_errors)
            logger.warning(
                "[plan/shell-validate] %d syntax error(s) found:\n%s",
                len(shell_errors),
                error_report,
            )
            # Feed shell errors back to the LLM for one re-generation attempt.
            import json as _json2

            user_payload2 = {
                "foothold": state.get("foothold", {}),
                "memory": state.get("memory", {}),
                "completed_stages": state.get("completed_stages", []),
                "run_id": state.get("run_id"),
            }
            fix_parts = [
                "Emit a SpecialistPlan as JSON matching the schema.",
                "Session context follows:",
                "",
                _json2.dumps(user_payload2, indent=2, default=str),
                "",
                "--- SHELL SYNTAX ERRORS (must fix all) ---",
                "The shell parser found these syntax errors in your commands.",
                "Fix every one. Do NOT change the plan structure, only fix the commands.",
                "",
                error_report,
            ]
            fix_msgs = [
                LLMMessage(role="system", content=system),
                LLMMessage(role="user", content="\n".join(fix_parts)),
            ]
            try:
                plan = self._llm.generate_structured(
                    fix_msgs, SpecialistPlan, grounding="skip",
                    temperature=self._temperature,
                    thinking="high",
                )
                logger.info("[plan/shell-validate] re-generated plan after syntax fix")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[plan/shell-validate] re-gen failed: %s; using plan as-is", exc
                )

        # ---- code-execution validation (LLM semantic check, retries only) --
        # Gemini code_execution validates logic (wrong flags, tool semantics).
        # Only on retries to save the LLM call on clean first passes.
        if is_retry:
            try:
                plan = self._validate_commands(plan, state, skill, system)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[plan/validate_commands] failed: %s; using plan as-is", exc)

        return plan

    def _validate_commands(
        self,
        plan: SpecialistPlan,
        state: "SessionState",
        skill: Skill,
        system_prompt: str,
    ) -> SpecialistPlan:
        """Use code execution to validate command syntax. Re-plans if issues found."""
        if not hasattr(self._llm, "validate_commands"):
            return plan

        platform = (state.get("foothold") or {}).get("platform") or "windows"
        commands: list[dict[str, str]] = []
        for gen in plan.abilities:
            for stage in gen.stages:
                commands.append({
                    "name": f"{gen.ability.name}/{stage.stage_name}",
                    "executor": stage.executor,
                    "command": stage.command_template,
                })

        if not commands:
            return plan

        result = self._llm.validate_commands(commands, platform=platform)
        if result.all_valid:
            logger.info("[plan/validate_commands] all commands passed validation")
            return plan

        # Build feedback from invalid commands and re-plan
        import json as _json
        issues_text = []
        for v in result.validations:
            if not v.valid:
                issues_text.append(f"  - {v.name}: {'; '.join(v.issues)}")
        if not issues_text:
            return plan

        logger.info(
            "[plan/validate_commands] %d commands have issues, re-generating",
            len(issues_text),
        )

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
            "--- COMMAND VALIDATION ISSUES (must fix all) ---",
            "Code execution validation found these command syntax issues:",
            *issues_text,
            "",
            "Fix every flagged command. Ensure no placeholder tokens, balanced",
            "quotes, valid shell syntax, and correct temp directory paths.",
        ]
        msgs = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content="\n".join(user_parts)),
        ]
        return self._llm.generate_structured(
            msgs, SpecialistPlan, grounding="skip", temperature=self._temperature
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
    catalog: PayloadCatalog | None = None,
) -> PushResult:
    """Push an already-approved `SpecialistPlan` to BAS and write artifacts."""
    skill_name = state.get("next_stage") or ""
    if not skill_name or skill_name == "DONE" or not skill_tool.has(skill_name):
        return PushResult(skill=skill_name, success=False, error="no skill to push")

    # Defense in depth against an LLM that emits a syntactically-valid UUID
    # that doesn't actually exist in the backend's payload catalog. Pydantic
    # only validates UUID *format*, not membership. Drop unknown IDs to null
    # rather than failing the push.
    if catalog is not None:
        known = catalog.known_ids()
        if known:  # only filter when we actually have a catalog to compare against
            for gen in plan.abilities:
                for stage in gen.stages:
                    if stage.payload_id is None:
                        continue
                    if str(stage.payload_id) not in known:
                        logger.warning(
                            "[push] dropping unknown payload_id %s on stage %s "
                            "(not in backend catalog)",
                            stage.payload_id,
                            stage.stage_name,
                        )
                        stage.payload_id = None

    # ---- 1b. Shell-native syntax validation (pre-push gate) -----------------
    # Parse every command_template through the real shell parser to catch
    # syntax errors before they reach the target agent.
    from ..tools.command_validator import format_errors, validate_plan

    validations = validate_plan(plan)
    syntax_errors = [v for v in validations if not v.valid]
    if syntax_errors:
        error_report = format_errors(syntax_errors)
        logger.warning(
            "[push] %d command(s) have syntax errors:\n%s",
            len(syntax_errors),
            error_report,
        )
    # Log warnings (placeholders, skipped checks) even for valid commands
    warnings = [v for v in validations if v.warnings]
    if warnings:
        warn_report = format_errors(warnings)
        if warn_report:
            logger.info("[push] command validation warnings:\n%s", warn_report)

    # ---- 2 & 3. push abilities + stages -------------------------------------
    ability_ids: list[str] = []
    stage_ids: list[str] = []
    stage_id_map: dict[str, dict[str, str]] = {}
    pushed_stages_by_ability: dict[str, list] = {}
    engagement_id = state.get("run_id")
    try:
        for gen in plan.abilities:
            # Stamp engagement_id so the backend can correlate this ability
            # back to the originating engagement (critical for multi-engagement).
            gen.ability.engagement_id = engagement_id
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
                st_id = _id_of(st_resp.stage_id)
                stage_ids.append(st_id)
                pushed_stages_by_ability[ab_id].append(stage)
                stage_id_map.setdefault(gen.ability.name, {})[stage.stage_name] = st_id
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
        plan.adversary.engagement_id = engagement_id
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
        stage_id_map=stage_id_map,
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
