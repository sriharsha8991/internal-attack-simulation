"""Deterministic fixtures for offline / dry-run flows.

A single namespaced UUID5 derivation gives the orchestrator stable IDs so that
re-running the graph produces identical artefacts under `runs/<ts>/`.
"""

from __future__ import annotations

import uuid
from uuid import UUID

from ..models import (
    AbilityCreate,
    AbilityResponse,
    AbilityStageCreate,
    AbilityStageResponse,
    AdversaryCreate,
    AdversaryResponse,
    AgentResponse,
    EnvironmentResponse,
)

# Stable namespace: hex of "bas".
DRY_RUN_NS = uuid.UUID("00000000-0000-0000-0000-000000626173")


def dry_uuid(*parts: str) -> UUID:
    """UUID5 over a colon-joined key — same input always yields the same UUID."""
    return uuid.uuid5(DRY_RUN_NS, ":".join(str(p) for p in parts))


# ---- canned resource fixtures -------------------------------------------------


def fake_environments() -> list[EnvironmentResponse]:
    """Two labs so override-by-id / override-by-name flows can be exercised offline."""
    return [
        EnvironmentResponse.model_validate(
            {
                "id": str(dry_uuid("env", "lab", "alpha")),
                "name": "dry-run-lab-alpha",
                "environment_type": "lab",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ),
        EnvironmentResponse.model_validate(
            {
                "id": str(dry_uuid("env", "lab", "beta")),
                "name": "dry-run-lab-beta",
                "environment_type": "lab",
                "created_at": "2026-05-01T00:00:00Z",
                "updated_at": "2026-05-01T00:00:00Z",
            }
        ),
    ]


def fake_agents() -> list[AgentResponse]:
    return [
        AgentResponse(
            agent_id="dry-agent-linux",
            hostname="dry-linux",
            platform="linux",
            is_enabled=True,
            display_status="online",
        ),
        AgentResponse(
            agent_id="dry-agent-windows",
            hostname="dry-windows",
            platform="windows",
            is_enabled=True,
            display_status="online",
        ),
    ]


def fake_ability(ability: AbilityCreate) -> AbilityResponse:
    return AbilityResponse.model_validate(
        {"id": str(dry_uuid("ability", ability.name)), **ability.model_dump()}
    )


def fake_stage(ability_id: UUID | str, stage: AbilityStageCreate) -> AbilityStageResponse:
    return AbilityStageResponse(
        stage_id=dry_uuid("stage", str(ability_id), str(stage.stage_order), stage.stage_name),
        ability_id=UUID(str(ability_id)),
        **stage.model_dump(),
    )


def fake_adversary(adv: AdversaryCreate) -> AdversaryResponse:
    return AdversaryResponse.model_validate(
        {"id": str(dry_uuid("adversary", adv.name)), **adv.model_dump()}
    )
