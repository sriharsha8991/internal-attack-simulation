"""Agents resource — GET /environments/{environment_id}/agents."""

from __future__ import annotations

from uuid import UUID

from ..models import AgentResponse
from . import dry_run as _dry
from .transport import HttpTransport

_ONLINE_STATUSES = {"online", "active", "connected"}


class AgentsApi:
    def __init__(self, transport: HttpTransport, *, dry_run: bool = False) -> None:
        self._t = transport
        self._dry = dry_run

    def list(self, environment_id: UUID | str) -> list[AgentResponse]:
        if self._dry:
            return _dry.fake_agents()
        data = self._t.get_json(f"/environments/{environment_id}/agents")
        return [AgentResponse.model_validate(x) for x in self._t.unwrap_list(data)]

    @staticmethod
    def pick_by_platform(agents: list[AgentResponse]) -> dict[str, AgentResponse]:
        """Highest `last_seen` per platform among enabled + online agents."""
        best: dict[str, AgentResponse] = {}
        for a in agents:
            if not a.is_enabled:
                continue
            if a.display_status and a.display_status.lower() not in _ONLINE_STATUSES:
                continue
            plat = (a.platform or "unknown").lower()
            cur = best.get(plat)
            if cur is None or (a.last_seen or "") > (cur.last_seen or ""):
                best[plat] = a
        return best
