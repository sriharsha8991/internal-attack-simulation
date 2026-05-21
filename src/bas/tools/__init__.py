"""Tools available to the orchestrator (and, later, to specialist agents).

A "tool" here is a thin, well-typed Python object that an agent calls deliberately
when it needs information — never a blob of markdown dumped into a prompt.
"""

from .skill_tool import SkillSummary, SkillTool

__all__ = ["SkillSummary", "SkillTool"]
