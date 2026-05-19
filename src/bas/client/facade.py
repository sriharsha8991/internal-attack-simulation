"""High-level facade composing the resource clients.

`BasClient` is intentionally thin — it constructs the shared `HttpTransport`,
instantiates one of each resource API, and exposes both:

  - Resource handles (preferred for new code):
        client.environments, client.agents, client.payloads,
        client.abilities, client.adversaries

  - Flat methods (back-compat with the original monolithic surface):
        client.list_environments(), client.pick_latest_environment(),
        client.resolve_environment(...), client.list_agents(env_id),
        client.pick_agents_by_platform(agents), client.list_payloads(),
        client.create_ability(ab), client.create_ability_stage(ab_id, stage),
        client.create_adversary(adv), client.link_ability_to_adversary(adv_id, ab_id)

Note: the BAS Platform requires no authentication in M1, so there is no
`AuthApi` and no `login()` method.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import httpx

from ..models import (
    AbilityCreate,
    AbilityResponse,
    AbilityStageCreate,
    AbilityStageResponse,
    AdversaryCreate,
    AdversaryResponse,
    AgentResponse,
    EnvironmentResponse,
    PayloadMetadata,
)
from .abilities import AbilitiesApi
from .adversaries import AdversariesApi
from .agents import AgentsApi
from .environments import EnvironmentsApi
from .payloads import PayloadsApi
from .transport import HttpTransport

if TYPE_CHECKING:
    from ..config import BasConfig


class BasClient:
    def __init__(
        self,
        base_url: str,
        *,
        sleep_ms: int = 250,
        timeout: float = 30.0,
        dry_run: bool = False,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.dry_run = dry_run
        self._transport = HttpTransport(
            base_url,
            sleep_ms=sleep_ms,
            timeout=timeout,
            http_client=http_client,
        )
        self.environments = EnvironmentsApi(self._transport, dry_run=dry_run)
        self.agents = AgentsApi(self._transport, dry_run=dry_run)
        self.payloads = PayloadsApi(self._transport, dry_run=dry_run)
        self.abilities = AbilitiesApi(self._transport, dry_run=dry_run)
        self.adversaries = AdversariesApi(self._transport, dry_run=dry_run)

    # ---- alternative constructors ------------------------------------------

    @classmethod
    def from_config(
        cls,
        cfg: "BasConfig",
        *,
        http_client: httpx.Client | None = None,
    ) -> "BasClient":
        """Build a client straight from the central `BasConfig`."""
        return cls(
            cfg.base_url,
            sleep_ms=cfg.sleep_ms,
            timeout=cfg.timeout,
            dry_run=cfg.dry_run,
            http_client=http_client,
        )

    # ---- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> "BasClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def base_url(self) -> str:
        return self._transport.base_url

    @property
    def sleep_ms(self) -> int:
        return self._transport.sleep_ms

    # ---- flat back-compat surface ------------------------------------------

    def list_environments(self) -> list[EnvironmentResponse]:
        return self.environments.list()

    def pick_latest_environment(self) -> EnvironmentResponse:
        """Back-compat: honors `BAS_ENVIRONMENT_ID` / `BAS_ENVIRONMENT_NAME` overrides."""
        return self.environments.resolve()

    def resolve_environment(
        self,
        environment_id: UUID | str | None = None,
        *,
        name: str | None = None,
    ) -> EnvironmentResponse:
        return self.environments.resolve(environment_id, name=name)

    def list_agents(self, environment_id: UUID | str) -> list[AgentResponse]:
        return self.agents.list(environment_id)

    @staticmethod
    def pick_agents_by_platform(agents: list[AgentResponse]) -> dict[str, AgentResponse]:
        return AgentsApi.pick_by_platform(agents)

    def list_payloads(self) -> list[PayloadMetadata]:
        return self.payloads.list()

    def create_ability(self, ability: AbilityCreate) -> AbilityResponse:
        return self.abilities.create(ability)

    def create_ability_stage(
        self, ability_id: UUID | str, stage: AbilityStageCreate
    ) -> AbilityStageResponse:
        return self.abilities.create_stage(ability_id, stage)

    def create_adversary(self, adversary: AdversaryCreate) -> AdversaryResponse:
        return self.adversaries.create(adversary)

    def link_ability_to_adversary(
        self, adversary_id: UUID | str, ability_id: UUID | str
    ) -> bool:
        return self.adversaries.link_ability(adversary_id, ability_id)
