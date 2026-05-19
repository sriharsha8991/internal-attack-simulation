"""Pydantic models mirroring the BAS Platform OpenAPI schema (only the bits M1 needs).

Schemas labelled (defined) come from the spec verbatim. Schemas labelled (inferred)
are best-guess shapes for endpoints whose response schema is `{}` in the spec; we
keep them permissive via `model_config = ConfigDict(extra="allow")` so unknown fields
don't break parsing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Environment  (inferred; OpenAPI exposes only Create/Update bodies)
# ---------------------------------------------------------------------------


class EnvironmentResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    environment_id: UUID | None = Field(default=None, alias="id")
    name: str | None = None
    environment_type: str | None = None
    risk_tolerance: str | None = None
    network_ranges: list[str] | None = None
    allowed_tactics: list[str] | None = None
    blocked_techniques: list[str] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Agent  (defined)
# ---------------------------------------------------------------------------


class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_id: str
    hostname: str
    ip_address: str | None = None
    platform: str | None = None
    environment_id: UUID | None = None
    is_enabled: bool
    last_seen: datetime | None = None
    registered_at: datetime | None = None
    display_status: str


# ---------------------------------------------------------------------------
# Abilities  (defined)
# ---------------------------------------------------------------------------


class AbilityCreate(BaseModel):
    name: str
    description: str | None = None
    mitre_tactic: str | None = None
    mitre_technique_id: str | None = None
    platform: str | None = None
    impact_type: str | None = None
    default_severity: str | None = None
    requires_approval: bool | None = False
    tags: list[str] | None = None
    created_by: str = "ai"


class AbilityResponse(AbilityCreate):
    model_config = ConfigDict(extra="allow")

    ability_id: UUID = Field(alias="id")


# ---------------------------------------------------------------------------
# Ability Stages  (defined)
# ---------------------------------------------------------------------------


class AbilityStageCreate(BaseModel):
    stage_name: str
    stage_order: int
    executor: str | None = None
    command_template: str | None = None
    timeout_seconds: int | None = None
    payload_id: UUID | None = None


class AbilityStageResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    stage_id: UUID
    ability_id: UUID
    stage_name: str | None = None
    stage_order: int
    executor: str | None = None
    command_template: str | None = None
    timeout_seconds: int | None = None
    payload_id: UUID | None = None


# ---------------------------------------------------------------------------
# Adversary  (defined)
# ---------------------------------------------------------------------------


class AdversaryCreate(BaseModel):
    name: str
    description: str | None = None
    profile: str | None = None
    execution_strategy: str | None = None
    requires_approval: bool | None = False
    is_tested: bool | None = False
    created_by: str = "ai"


class AdversaryResponse(AdversaryCreate):
    model_config = ConfigDict(extra="allow")
    adversary_id: UUID = Field(alias="id")


# ---------------------------------------------------------------------------
# Payload  (M2; metadata only — backend stores binaries on its own disk)
# ---------------------------------------------------------------------------


class PayloadMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    payload_id: UUID | None = Field(default=None, alias="id")
    name: str
    platform: str | None = None
    type: str | None = None
    risk_classification: str | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Internal (not from OpenAPI): the artefact our agents emit per technique
# ---------------------------------------------------------------------------


class GeneratedAbility(BaseModel):
    """One Ability + its ordered stages, as emitted by the LLM before push.

    Carries provenance fields so `runs/<ts>/abilities/<id>.json` is auditable.
    """

    ability: AbilityCreate
    stages: list[AbilityStageCreate]
    rationale: str
    grounding_depth: str  # "skip" | "light" | "deep"
    cited_urls: list[str] = Field(default_factory=list)
    provider: str  # e.g. "gemini:gemini-2.5-pro"
    extras: dict[str, Any] = Field(default_factory=dict)
