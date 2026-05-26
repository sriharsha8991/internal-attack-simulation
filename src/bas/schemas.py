"""Request / response schemas for the BAS Orchestrator API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from .phases import Phase

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

    phases: list[Phase] | None = Field(
        default=None,
        description=(
            "Kill-chain phases to execute, in order. "
            "Accepted values: discovery, privesc, credaccess, lateral, "
            "persistence, defevasion, impact. "
            "Aliases like 'recon', 'priv-esc', 'credential-access' are "
            "also accepted and normalised automatically. "
            "When omitted, the LLM router decides each step."
        ),
        examples=[["discovery"], ["discovery", "privesc"]],
    )

    @field_validator("phases", mode="before")
    @classmethod
    def _normalise_phases(cls, v: list[str] | None) -> list[Phase] | None:
        """Accept aliases (e.g. 'recon') and normalise to canonical Phase enum."""
        if v is None:
            return v
        from .phases import _normalise_phase

        out: list[Phase] = []
        for raw in v:
            canonical = _normalise_phase(raw)
            try:
                out.append(Phase(canonical))
            except ValueError:
                raise ValueError(
                    f"Unknown phase {raw!r}. "
                    f"Accepted: {[p.value for p in Phase]}"
                ) from None
        return out
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
    status: Literal["queued", "running", "awaiting_results", "completed", "failed"]
    started_at: datetime
    finished_at: datetime | None
    awaiting_since: datetime | None = None
    iterations: int
    completed_stages: list[str]
    error: str | None = None


class EngagementDetail(EngagementSummary):
    phases: list[Phase] | None
    environment: EnvironmentSelector | None
    target: TargetHint | None
    foothold: dict[str, Any] | None
    skill_order: list[str] | None
    dry_run: bool
    state: dict[str, Any] | None
    log_tail: list[str]


class PhaseInfo(BaseModel):
    name: Phase
    skills: list[str]


class SkillInfo(BaseModel):
    name: str
    description: str
    stage: Phase | None
    mitre_tactics: list[str]
    tool_count: int


# ---------------------------------------------------------------------------
# Execution result schemas (webhook payload from BAS backend)
# ---------------------------------------------------------------------------


class StageExecutionRequest(BaseModel):
    """One executed stage within an ability."""

    stage_name: str = Field(..., description="Stage identifier.")
    executor: str = Field(default="", description="Executor used: cmd, psh, sh, bash.")
    command_executed: str = Field(default="", description="The actual command that was run.")
    execution_status: Literal["passed", "failed"] = Field(
        default="failed", description="Whether the stage passed or failed."
    )
    stdout: str = Field(default="", description="Full standard output captured.")
    stderr: str = Field(default="", description="Full standard error captured.")
    exit_code: int = Field(default=-1, description="Exit code. 0 = success.")
    timestamp: datetime | None = Field(
        default=None, description="When this stage executed (ISO 8601)."
    )

    model_config = {"extra": "allow"}


class AbilityResultRequest(BaseModel):
    """Execution outcome for a single ability (all its stages)."""

    ability_id: str = Field(
        ..., description="The ability UUID that was pushed to the BAS backend."
    )
    name: str = Field(default="", description="Ability name.")
    mitre_technique_id: str | None = Field(
        default=None, description="MITRE ATT&CK technique ID, e.g. T1016."
    )
    platform: str = Field(default="", description="Target platform: windows, linux, mac.")
    stages: list[StageExecutionRequest] = Field(
        default_factory=list, description="List of stage execution results."
    )

    model_config = {"extra": "allow"}


class OperationResultRequest(BaseModel):
    """Top-level result payload POSTed by the BAS backend after execution."""

    engagement_id: str = Field(
        ...,
        description="The engagement this result belongs to (returned by POST /engagements).",
        examples=["08c047ac0cba49b3ae85257406a5cc28"],
    )
    operation_id: str = Field(
        ...,
        description="Unique UUID for this execution run.",
        examples=["f9e8d7c6-b5a4-3210-fedc-ba0987654321"],
    )
    operation_name: str = Field(default="", description="Human-readable operation label.")
    operation_status: Literal["completed", "failed", "partial"] = Field(
        default="completed", description="Overall operation outcome."
    )
    completed_at: datetime | None = Field(
        default=None, description="When execution finished (ISO 8601)."
    )
    adversary: dict[str, Any] = Field(
        default_factory=dict,
        description="Adversary metadata: {adversary_id, name}.",
    )
    progress: dict[str, Any] = Field(
        default_factory=dict,
        description="Execution progress: {total_abilities, completed_abilities, progress_percent}.",
    )
    abilities: list[AbilityResultRequest] = Field(
        default_factory=list, description="List of ability execution results."
    )

    model_config = {"extra": "allow"}


class ResultAcceptedResponse(BaseModel):
    status: Literal["accepted", "already_received"]
    operation_id: str
