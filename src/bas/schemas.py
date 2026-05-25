"""Request / response schemas for the BAS Orchestrator API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

PlatformLiteral = Literal["windows", "linux", "mac"]


class EnvironmentSelector(BaseModel):
    """Optional pin for which BAS environment to use. Defaults to most-recent."""

    id: UUID | None = Field(default=None, description="BAS environment_id (UUID).")
    name: str | None = Field(default=None, description="Exact environment name.")


class TargetHint(BaseModel):
    """Optional preferences when selecting the foothold agent."""

    platform: PlatformLiteral | None = None


class EngagementCreateRequest(BaseModel):
    """Minimum trigger payload — everything else is auto-resolved."""

    phases: list[str] | None = Field(
        default=None,
        description=(
            "Kill-chain phases to execute, in order. "
            "Accepted: discovery, privesc, credaccess, lateral, persistence, "
            "defevasion, impact (aliases like 'recon', 'priv-esc', "
            "'credential-access' also work). "
            "When omitted, the LLM router decides each step."
        ),
        examples=[["discovery"], ["discovery", "privesc"]],
    )
    environment: EnvironmentSelector | None = Field(
        default=None,
        description="Which BAS environment to use. Defaults to most-recent.",
    )
    target: TargetHint | None = Field(
        default=None,
        description="Preferences for picking the foothold agent.",
    )
    max_iterations: int = Field(
        default=20,
        ge=1,
        le=50,
        description=(
            "Hard cap on master-router iterations across the engagement. "
            "Each iteration is one master phase decision (briefing or commit); "
            "internal planner attempts (\u2264 3 per phase) and master revisions "
            "(\u2264 1 per phase) are capped separately."
        ),
    )
    dry_run: bool | None = Field(
        default=None,
        description="Override `bas.dry_run` from config when set.",
    )


class EngagementSubmitResponse(BaseModel):
    engagement_id: str
    status_url: str
    status: Literal["queued"]


class EngagementSummary(BaseModel):
    engagement_id: str
    status: Literal["queued", "running", "completed", "failed"]
    started_at: datetime
    finished_at: datetime | None
    iterations: int
    completed_stages: list[str]
    error: str | None = None


class EngagementDetail(EngagementSummary):
    phases: list[str] | None
    environment: EnvironmentSelector | None
    target: TargetHint | None
    foothold: dict[str, Any] | None
    skill_order: list[str] | None
    dry_run: bool
    state: dict[str, Any] | None
    log_tail: list[str]


class PhaseInfo(BaseModel):
    name: str
    skills: list[str]


class SkillInfo(BaseModel):
    name: str
    description: str
    stage: str | None
    mitre_tactics: list[str]
    tool_count: int
