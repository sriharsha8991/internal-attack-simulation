"""Parse `skills/<name>/SKILL.md` (+ reference files) into typed objects.

A skill dir has the shape:

    skills/<name>/
        SKILL.md             YAML frontmatter + markdown body  (required)
        reference/
            techniques.md    raw markdown                       (optional)
            tool-commands.md raw markdown                       (optional)

Frontmatter must satisfy the Anthropic spec:
  - `name`        lowercase-hyphen, ≤ 64 chars, must NOT contain 'anthropic'
                  or 'claude', must match the directory name.
  - `description` ≤ 1024 chars, third-person, what+when.

Unknown frontmatter keys are captured in `Skill.extras` (warning, not error).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------


class SkillLoadError(RuntimeError):
    """Raised when a skill directory is missing required files or invalid."""


# ----------------------------------------------------------------------------
# Frontmatter schema
# ----------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_BANNED_TOKENS = ("anthropic", "claude")
_KNOWN_OPSEC = {"stealth", "moderate", "loud", None}


class SkillBudget(BaseModel):
    model_config = ConfigDict(extra="allow")
    max_tool_calls: int | None = None
    max_wallclock_min: int | None = None


class SkillFrontmatter(BaseModel):
    """Validated YAML frontmatter. Unknown keys are accepted and surfaced via `extras`."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str
    stage: str | None = None
    agent: str | None = None
    mitre_tactics: list[str] = Field(default_factory=list)
    default_opsec: str | None = None
    ambient: bool = False
    tool_allowlist: list[str] = Field(default_factory=list)
    budget: SkillBudget | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"skill name {v!r} must be lowercase-hyphen, start with a letter, "
                "and be at most 64 characters"
            )
        lo = v.lower()
        for bad in _BANNED_TOKENS:
            if bad in lo:
                raise ValueError(f"skill name {v!r} must not contain {bad!r}")
        return v

    @field_validator("description")
    @classmethod
    def _check_description(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("skill description must not be empty")
        if len(v) > 1024:
            raise ValueError(f"skill description is {len(v)} chars; max 1024")
        return v

    @field_validator("default_opsec")
    @classmethod
    def _check_opsec(cls, v: str | None) -> str | None:
        if v is not None and v not in _KNOWN_OPSEC:
            # Not a hard error; just preserve the value but record it as nonstandard.
            return v
        return v


# ----------------------------------------------------------------------------
# Loaded skill
# ----------------------------------------------------------------------------


class Skill(BaseModel):
    """Fully-parsed skill, ready to be injected into an agent's context."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    frontmatter: SkillFrontmatter
    body: str = ""
    techniques: str | None = None
    tool_commands: str | None = None
    path: Path

    # ---- shortcuts used by agents -------------------------------------------

    @property
    def name(self) -> str:
        return self.frontmatter.name

    @property
    def description(self) -> str:
        return self.frontmatter.description

    @property
    def mitre_tactics(self) -> list[str]:
        return self.frontmatter.mitre_tactics

    @property
    def tool_allowlist(self) -> list[str]:
        return self.frontmatter.tool_allowlist

    def render_for_prompt(self) -> str:
        """Render the skill as a single prompt block — body + references appended.

        Agents pass this to the LLM so the model sees the same content a human
        operator would when reading the skill directory in a code editor.
        """
        parts: list[str] = [
            f"# Skill: {self.name}",
            f"_{self.description}_",
            "",
            self.body.strip(),
        ]
        if self.techniques:
            parts.extend(["", "---", "## reference/techniques.md", self.techniques.strip()])
        if self.tool_commands:
            parts.extend(["", "---", "## reference/tool-commands.md", self.tool_commands.strip()])
        return "\n".join(parts)


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SkillLoadError("SKILL.md is missing the leading `---` YAML frontmatter block")
    raw_yaml, body = m.group(1), m.group(2)
    data = yaml.safe_load(raw_yaml) or {}
    if not isinstance(data, dict):
        raise SkillLoadError("SKILL.md frontmatter must be a YAML mapping")
    return data, body


def _read_optional(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


# ----------------------------------------------------------------------------
# Public loaders
# ----------------------------------------------------------------------------


def load_skill(path: str | Path) -> Skill:
    """Load a single skill directory."""
    dir_path = Path(path)
    if not dir_path.is_dir():
        raise SkillLoadError(f"not a directory: {dir_path}")

    skill_md = dir_path / "SKILL.md"
    if not skill_md.is_file():
        raise SkillLoadError(f"missing SKILL.md in {dir_path}")

    raw = skill_md.read_text(encoding="utf-8")
    fm_data, body = _split_frontmatter(raw)

    try:
        fm = SkillFrontmatter.model_validate(fm_data)
    except ValidationError as e:
        raise SkillLoadError(f"invalid frontmatter in {skill_md}: {e}") from e

    if fm.name != dir_path.name:
        raise SkillLoadError(
            f"frontmatter `name: {fm.name}` does not match directory name {dir_path.name!r}"
        )

    return Skill(
        frontmatter=fm,
        body=body,
        techniques=_read_optional(dir_path / "reference" / "techniques.md"),
        tool_commands=_read_optional(dir_path / "reference" / "tool-commands.md"),
        path=dir_path,
    )


def load_skills(root: str | Path = "skills") -> dict[str, Skill]:
    """Load every skill directory under `root`, keyed by skill name.

    Skipped silently: hidden dirs, dirs without a SKILL.md (with a warning).
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise SkillLoadError(f"skills root not found: {root_path}")

    out: dict[str, Skill] = {}
    for entry in sorted(root_path.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not (entry / "SKILL.md").is_file():
            continue
        skill = load_skill(entry)
        if skill.name in out:
            raise SkillLoadError(
                f"duplicate skill name {skill.name!r}: {out[skill.name].path} vs {skill.path}"
            )
        out[skill.name] = skill
    return out
