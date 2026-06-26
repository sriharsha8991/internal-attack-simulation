"""Kill-chain phase → skill name mapping.

The skills register a `stage` in their YAML frontmatter (discovery, privesc,
ad-enumeration, credaccess, lateral, persistence, defevasion, impact). Callers refer to phases
by those canonical names so the API never needs to know individual skill names.

Phase aliases are normalised in `_normalise_phase` so common synonyms work
without the agent having to memorise our internal names.

The ``Phase`` enum is a ``str`` subclass, so every existing string comparison
(``phase == "discovery"``, ``phase in completed_list``, dict key lookups)
continues to work without changes across the codebase.
"""

from __future__ import annotations

from enum import Enum

from .tools.skill_tool import SkillTool


class Phase(str, Enum):
    """Canonical kill-chain phases — the contract between backend and orchestrator.

    Being a ``str`` enum, each member compares equal to its plain-string value:
    ``Phase.DISCOVERY == "discovery"`` is ``True``.
    """

    DISCOVERY = "discovery"
    AD_ENUMERATION = "ad-enumeration"
    PRIVESC = "privesc"
    CREDACCESS = "credaccess"
    LATERAL = "lateral"
    PERSISTENCE = "persistence"
    DEFEVASION = "defevasion"
    IMPACT = "impact"


# Common alternative names → canonical phase enum value.
_ALIASES: dict[str, Phase] = {
    "recon": Phase.DISCOVERY,
    "reconnaissance": Phase.DISCOVERY,
    "discover": Phase.DISCOVERY,
    "discovery": Phase.DISCOVERY,
    "ad": Phase.AD_ENUMERATION,
    "ad-enum": Phase.AD_ENUMERATION,
    "ad-enumeration": Phase.AD_ENUMERATION,
    "active-directory-enumeration": Phase.AD_ENUMERATION,
    "enumerating-active-directory": Phase.AD_ENUMERATION,
    "domain-enumeration": Phase.AD_ENUMERATION,
    "privilege-escalation": Phase.PRIVESC,
    "privilegeescalation": Phase.PRIVESC,
    "priv-esc": Phase.PRIVESC,
    "privesc": Phase.PRIVESC,
    "credentials": Phase.CREDACCESS,
    "credential-access": Phase.CREDACCESS,
    "credaccess": Phase.CREDACCESS,
    "lateral-movement": Phase.LATERAL,
    "lateral": Phase.LATERAL,
    "persistence": Phase.PERSISTENCE,
    "defense-evasion": Phase.DEFEVASION,
    "defence-evasion": Phase.DEFEVASION,
    "defevasion": Phase.DEFEVASION,
    "evasion": Phase.DEFEVASION,
    "impact": Phase.IMPACT,
}


def _normalise_phase(name: str) -> str:
    """Return the canonical phase name for *name* (lowercase, alias-resolved)."""
    key = name.strip().lower()
    return _ALIASES.get(key, key)  # type: ignore[return-value]  # Phase IS str


def build_phase_index(skill_tool: SkillTool) -> dict[str, list[str]]:
    """phase (canonical stage) → ordered list of skill names with that stage."""
    index: dict[str, list[str]] = {}
    for summary in skill_tool.list_summaries():
        if summary.stage:
            index.setdefault(summary.stage, []).append(summary.name)
    for skills in index.values():
        skills.sort()
    return index


def resolve_phases_to_skills(
    phases: list[str], skill_tool: SkillTool
) -> tuple[list[str], list[str]]:
    """Map a list of phase aliases to a flat ordered list of skill names.

    Returns `(resolved_skill_order, unknown_phases)`. Caller decides whether
    unknown phases are fatal.
    """
    index = build_phase_index(skill_tool)
    resolved: list[str] = []
    unknown: list[str] = []
    for raw in phases:
        canonical = _normalise_phase(raw)
        if canonical in index:
            resolved.extend(index[canonical])
        else:
            unknown.append(raw)
    return resolved, unknown


def known_phases(skill_tool: SkillTool) -> list[str]:
    index = build_phase_index(skill_tool)
    ordered = [phase.value for phase in Phase if phase.value in index]
    extras = sorted(phase for phase in index if phase not in ordered)
    return ordered + extras


def first_skill_for_phase(phase: str, skill_tool: SkillTool) -> str | None:
    """Return the canonical playbook (first skill) registered for a phase."""
    if not phase:
        return None
    resolved, _ = resolve_phases_to_skills([phase], skill_tool)
    return resolved[0] if resolved else None


def skills_for_phase(phase: str, skill_tool: SkillTool) -> list[str]:
    """Return ALL playbook skill names registered for a phase, in order."""
    if not phase:
        return []
    resolved, _ = resolve_phases_to_skills([phase], skill_tool)
    return resolved
