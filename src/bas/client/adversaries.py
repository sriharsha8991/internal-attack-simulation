"""Adversaries resource — POST /adversaries and link-to-ability endpoint."""

from __future__ import annotations

from uuid import UUID

from ..models import AdversaryCreate, AdversaryResponse
from . import dry_run as _dry
from .transport import HttpTransport


class AdversariesApi:
    def __init__(self, transport: HttpTransport, *, dry_run: bool = False) -> None:
        self._t = transport
        self._dry = dry_run

    def create(self, adversary: AdversaryCreate) -> AdversaryResponse:
        if self._dry:
            return _dry.fake_adversary(adversary)
        data = self._t.post_json("/adversaries", json=adversary.model_dump(exclude_none=True))
        self._t.pause()
        return AdversaryResponse.model_validate(data)

    def link_ability(self, adversary_id: UUID | str, ability_id: UUID | str) -> bool:
        """POST /adversaries/{aid}/abilities/{abid} — backend treats this as idempotent."""
        if self._dry:
            return True
        resp = self._t.request("POST", f"/adversaries/{adversary_id}/abilities/{ability_id}")
        self._t.pause()
        return 200 <= resp.status_code < 300
