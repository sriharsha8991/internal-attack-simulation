"""FastAPI Router for UI skill-editing and audit trail tracking.

Strictly protects core prompts while enabling flexible manual Markdown playbooks editing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..tools.audit_logger import log_skill_modification, read_audit_log

logger = logging.getLogger(__name__)

ui_router = APIRouter(prefix="/api/ui", tags=["UI Management"])

CANONICAL_SKILLS = {
    "discovering-environment",
    "enumerating-active-directory",
    "escalating-privileges",
    "accessing-credentials",
    "moving-laterally",
    "establishing-persistence",
    "evading-defenses",
    "achieving-impact",
}

# Supported file type maps
FILE_MAPPINGS = {
    "skill": "SKILL.md",
    "commands": "reference/tool-commands.md",
    "techniques": "reference/techniques.md",
}


class EditSkillRequest(BaseModel):
    username: str = Field(..., min_length=1, description="Operator handle making the change")
    content: str = Field(..., description="Full raw Markdown file content")
    change_summary: str = Field(default="", description="Operator's description of what was changed")


def _get_skill_file_path(skill_name: str, file_type: str) -> Path:
    """Validate parameters and resolve the safe, absolute path to a skill file."""
    if skill_name not in CANONICAL_SKILLS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid skill name: {skill_name}. Must be one of canonical skills."
        )

    if file_type not in FILE_MAPPINGS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file type: {file_type}. Supported types: {list(FILE_MAPPINGS.keys())}"
        )

    # Strictly build path relative to the playbooks/skills directory
    skills_root = Path.cwd() / "skills"
    target_path = (skills_root / skill_name / FILE_MAPPINGS[file_type]).resolve()

    # Double check against path traversal attacks
    if not str(target_path).startswith(str(skills_root.resolve())):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Security error: Path traversal detected."
        )

    return target_path


@ui_router.get("/skills")
def list_skills() -> list[dict[str, Any]]:
    """List available skills with basic metadata."""
    from ..bootstrap import _bootstrap
    try:
        _, skill_tool, _, _ = _bootstrap()
        summaries = skill_tool.list_summaries()
        return [
            {
                "name": s.name,
                "stage": s.stage,
                "description": s.description,
                "mitre_tactics": s.mitre_tactics,
                "tool_count": s.tool_count,
            }
            for s in summaries
        ]
    except Exception as exc:
        # Fallback if bootstrap isn't primed yet
        return [{"name": name, "stage": name} for name in CANONICAL_SKILLS]


@ui_router.get("/skills/{skill_name}/files/{file_type}")
def get_skill_file(skill_name: str, file_type: str) -> dict[str, str]:
    """Retrieve raw Markdown content for a specific skill file."""
    path = _get_skill_file_path(skill_name, file_type)
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill file not found at expected path: {path.name}"
        )

    try:
        content = path.read_text(encoding="utf-8")
        return {"content": content, "file_path": str(path.relative_to(Path.cwd()))}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read file: {exc}"
        )


@ui_router.post("/skills/{skill_name}/files/{file_type}")
def edit_skill_file(skill_name: str, file_type: str, req: EditSkillRequest) -> dict[str, str]:
    """Overwrite the raw Markdown file contents and commit to the persistent audit log."""
    path = _get_skill_file_path(skill_name, file_type)
    
    try:
        # Backup original content or write file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(req.content, encoding="utf-8")

        # Log change to the skills audit log
        log_skill_modification(
            username=req.username,
            skill_name=skill_name,
            file_type=FILE_MAPPINGS[file_type],
            action="modify",
            details=req.change_summary
        )

        return {"status": "success", "message": f"Successfully updated {FILE_MAPPINGS[file_type]} on disk."}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save file: {exc}"
        )


@ui_router.get("/audit-log")
def get_audit_trail() -> list[dict[str, Any]]:
    """Retrieve the sorted manual modifications history."""
    return read_audit_log()
