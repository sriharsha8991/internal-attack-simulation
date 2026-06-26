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


class SafetyContext(BaseModel):
    """Human-provided safety approvals and scope hints for gated actions."""

    acks: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit human ACK tokens for high-risk/destructive actions. "
            "Examples: dcsync, byovd, persistence.tier0, impact.ransomware."
        ),
    )
    simulations: list[str] = Field(
        default_factory=list,
        description="Optional names of explicitly scoped simulation types.",
    )


class EngagementCreateRequest(BaseModel):
    """Minimum trigger payload — everything else is auto-resolved."""

    phases: list[Phase] | None = Field(
        default=None,
        description=(
            "Kill-chain phases to execute, in order. "
            "Accepted values: discovery, ad-enumeration, privesc, credaccess, lateral, "
            "persistence, defevasion, impact. "
            "Aliases like 'recon', 'priv-esc', 'credential-access' are "
            "also accepted and normalised automatically. "
            "When omitted or empty, the orchestrator runs all known phases "
            "in canonical kill-chain order."
        ),
        examples=[None, [], ["discovery"], ["discovery", "ad-enumeration", "privesc"]],
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
    safety: SafetyContext = Field(
        default_factory=SafetyContext,
        description="Safety approvals and scope hints enforced before high-risk push.",
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


class StageDefinition(BaseModel):
    """Stage definition as stored in the backend (no execution output)."""

    stage_id: str = Field(..., description="Stage UUID.")
    stage_name: str = Field(default="", description="Stage identifier.")
    stage_order: int = Field(default=0, description="Execution order within the ability.")
    executor: str | None = Field(default=None, description="Executor: cmd, psh, sh, bash.")
    command_template: str | None = Field(default=None, description="Command template for execution.")
    timeout_seconds: int | None = Field(default=None, description="Execution timeout.")
    payload: dict[str, Any] | None = Field(default=None, description="Optional payload metadata.")

    model_config = {"extra": "allow"}


class AbilityDefinition(BaseModel):
    """Ability definition with stage definitions (no execution output)."""

    ability_id: str = Field(..., description="The ability UUID.")
    name: str = Field(default="", description="Ability name.")
    description: str | None = Field(default=None, description="Ability description.")
    mitre_tactic: str | None = Field(default=None, description="MITRE ATT&CK tactic.")
    mitre_technique_id: str | None = Field(
        default=None, description="MITRE ATT&CK technique ID, e.g. T1016."
    )
    platform: str = Field(default="", description="Target platform: windows, linux, mac.")
    impact_type: str | None = Field(default=None)
    default_severity: str | None = Field(default=None)
    engagement_id: str | None = Field(default=None)
    requires_approval: bool | None = Field(default=None)
    tags: list[str] | None = Field(default=None)
    created_by: str | None = Field(default=None)
    stages: list[StageDefinition] = Field(
        default_factory=list, description="Ordered stage definitions."
    )

    model_config = {"extra": "allow"}


class ExecutionLogEntry(BaseModel):
    """A single execution log entry from the backend — the actual output."""

    ability_id: str | None = Field(default=None, description="Ability UUID.")
    stage_id: str | None = Field(default=None, description="Stage UUID.")
    command_executed: str = Field(default="", description="The actual command that was run.")
    stdout: str = Field(default="", description="Full standard output captured.")
    stderr: str = Field(default="", description="Full standard error captured.")
    exit_code: int = Field(default=-1, description="Exit code. 0 = success.")
    executor: str = Field(default="", description="Executor used: cmd, psh, sh, bash.")
    timestamp: datetime | None = Field(
        default=None, description="When this stage executed (ISO 8601)."
    )

    model_config = {"extra": "allow"}


class OperationInfo(BaseModel):
    """Nested operation metadata from the backend payload."""

    operation_id: str = Field(..., description="Operation UUID.")
    engagement_id: str | None = Field(default=None)
    name: str = Field(default="", description="Operation name.")
    status: str = Field(default="completed", description="Operation status.")
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)
    kill_switch_triggered: bool | None = Field(default=None)
    execution_mode: str | None = Field(default=None)

    model_config = {"extra": "allow"}


class OperationResultRequest(BaseModel):
    """Top-level result payload POSTed by the BAS backend after execution.

    The backend sends a full operation snapshot with:
    - ``operation``: nested operation metadata (contains operation_id, status, etc.)
    - ``engagement_id``: top-level engagement identifier
    - ``abilities``: stage definitions (command_template, stage_order, etc.)
    - ``execution_logs``: actual execution output (stdout, stderr, exit_code)
    """

    operation: OperationInfo = Field(
        ..., description="Nested operation metadata."
    )
    engagement_id: str = Field(
        ...,
        description="The engagement this result belongs to.",
        examples=["08c047ac0cba49b3ae85257406a5cc28"],
    )
    environment: dict[str, Any] = Field(
        default_factory=dict, description="Environment metadata."
    )
    adversary: dict[str, Any] = Field(
        default_factory=dict, description="Adversary metadata.",
    )
    platform_agents: dict[str, Any] = Field(
        default_factory=dict, description="Platform agent references.",
    )
    progress: dict[str, Any] = Field(
        default_factory=dict,
        description="Execution progress: {total_abilities, completed_abilities, progress_percent}.",
    )
    abilities: list[AbilityDefinition] = Field(
        default_factory=list, description="Ability definitions with stages."
    )
    execution_logs: list[ExecutionLogEntry] = Field(
        default_factory=list,
        description="Execution output: stdout, stderr, exit_code per stage.",
    )

    model_config = {"extra": "allow"}

    # Convenience accessors so downstream code can use flat names.
    @property
    def operation_id(self) -> str:
        return self.operation.operation_id

    @property
    def operation_status(self) -> str:
        return self.operation.status


class ResultAcceptedResponse(BaseModel):
    status: Literal["accepted", "already_received"]
    operation_id: str
