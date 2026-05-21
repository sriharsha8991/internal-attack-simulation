"""SkillTool — the orchestrator's interface to the skill catalogue.

Design rules:
  - The orchestrator's router prompt sees ONLY `list_summaries()` output:
    short, structured metadata so the LLM can pick the next stage cheaply.
  - The full skill markdown is fetched on demand via `read(name)`. This is
    intended for specialists (M2) — the orchestrator should not need it.
  - All lookups are O(1) once `prime()` has cached the directory.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..skills import Skill, SkillLoadError, load_skills


class SkillSummary(BaseModel):
    """Compact skill record — what the router LLM sees about each skill."""

    name: str
    description: str
    stage: str | None = None
    agent: str | None = None
    mitre_tactics: list[str] = Field(default_factory=list)
    tool_count: int = 0


def _summarize(skill: Skill) -> SkillSummary:
    fm = skill.frontmatter
    return SkillSummary(
        name=fm.name,
        description=fm.description,
        stage=fm.stage,
        agent=fm.agent,
        mitre_tactics=list(fm.mitre_tactics),
        tool_count=len(fm.tool_allowlist),
    )


class SkillTool:
    """A handle the orchestrator (and later specialists) call into."""

    def __init__(self, root: str | Path = "skills") -> None:
        self._root = Path(root)
        self._cache: dict[str, Skill] | None = None

    # ---- lifecycle ----------------------------------------------------------

    def prime(self) -> "SkillTool":
        """Eagerly load + validate the catalogue. Returns self for chaining."""
        self._cache = load_skills(self._root)
        return self

    def _ensure(self) -> dict[str, Skill]:
        if self._cache is None:
            self._cache = load_skills(self._root)
        return self._cache

    # ---- tool surface -------------------------------------------------------

    def list_summaries(self) -> list[SkillSummary]:
        """Cheap metadata listing — safe to embed in router prompts."""
        return [_summarize(s) for s in self._ensure().values()]

    def names(self) -> list[str]:
        return list(self._ensure().keys())

    def has(self, name: str) -> bool:
        return name in self._ensure()

    def read(self, name: str) -> Skill:
        """Full skill (body + reference files). Used by specialists, not the router."""
        cache = self._ensure()
        if name not in cache:
            raise SkillLoadError(f"unknown skill {name!r}; known: {sorted(cache)}")
        return cache[name]
