"""Abilities resource — POST /abilities, POST /abilities/{id}/stages."""

from __future__ import annotations

from uuid import UUID

from ..models import (
    AbilityCreate,
    AbilityResponse,
    AbilityStageCreate,
    AbilityStageResponse,
)
from . import dry_run as _dry
from .transport import HttpTransport


class AbilitiesApi:
    def __init__(self, transport: HttpTransport, *, dry_run: bool = False) -> None:
        self._t = transport
        self._dry = dry_run

    def create(self, ability: AbilityCreate) -> AbilityResponse:
        if self._dry:
            return _dry.fake_ability(ability)
        data = self._t.post_json("/abilities", json=ability.model_dump(exclude_none=True))
        self._t.pause()
        return AbilityResponse.model_validate(data)

    def create_stage(
        self, ability_id: UUID | str, stage: AbilityStageCreate
    ) -> AbilityStageResponse:
        if self._dry:
            return _dry.fake_stage(ability_id, stage)
        
        # Serialize UUID fields to string before passing to json encoder to avoid TypeError
        payload = stage.model_dump(exclude_none=True)
        if "payload_id" in payload and payload["payload_id"]:
            payload["payload_id"] = str(payload["payload_id"])

        data = self._t.post_json(
            f"/abilities/{ability_id}/stages",
            json=payload,
        )
        self._t.pause()
        return AbilityStageResponse.model_validate(data)
