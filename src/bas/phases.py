"""Kill-chain phase → skill name mapping.

The skills register a `stage` in their YAML frontmatter (discovery, privesc,
credaccess, lateral, persistence, defevasion, impact). Callers refer to phases
by those canonical names so the API never needs to know individual skill names.

Phase aliases are normalised in `_normalise_phase` so common synonyms work
without the agent having to memorise our internal names.
"""

from __future__ import annotations

from .tools.skill_tool import SkillTool

# Common alternative names → canonical stage. Lowercase, hyphen-normalised.
_ALIASES: dict[str, str] = {
    "recon": "discovery",
    "reconnaissance": "discovery",
    "discover": "discovery",
    "discovery": "discovery",
    "privilege-escalation": "privesc",
    "privilegeescalation": "privesc",
    "priv-esc": "privesc",
    "privesc": "privesc",
    "credentials": "credaccess",
    "credential-access": "credaccess",
    "credaccess": "credaccess",
    "lateral-movement": "lateral",
    "lateral": "lateral",
    "persistence": "persistence",
    "defense-evasion": "defevasion",
    "defence-evasion": "defevasion",
    "defevasion": "defevasion",
    "evasion": "defevasion",
    "impact": "impact",
}


def _normalise_phase(name: str) -> str:
    return _ALIASES.get(name.strip().lower(), name.strip().lower())


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
    return sorted(build_phase_index(skill_tool).keys())
