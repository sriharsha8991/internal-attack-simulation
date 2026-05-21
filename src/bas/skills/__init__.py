"""Skill catalogue loading.

Public surface:
    from bas.skills import Skill, load_skill, load_skills

A `Skill` is the parsed form of an Anthropic Agent Skill directory:

    skills/<name>/
        SKILL.md                  # YAML frontmatter + markdown body
        reference/
            techniques.md         # technique catalogue (optional)
            tool-commands.md      # command templates (optional)
"""

from .loader import (
    Skill,
    SkillLoadError,
    SkillFrontmatter,
    load_skill,
    load_skills,
)

__all__ = [
    "Skill",
    "SkillFrontmatter",
    "SkillLoadError",
    "load_skill",
    "load_skills",
]
