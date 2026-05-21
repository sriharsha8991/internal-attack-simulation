"""Auto-foothold resolver.

Asks the BAS platform "where can I run?" and returns a foothold dict the
specialist understands. M1 picks the most-recent environment and a
healthy agent within it; callers can pin to a specific environment by
id/name when needed (future engagements).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from .client import BasClient
from .client.agents import AgentsApi

logger = logging.getLogger(__name__)


class FootholdResolutionError(RuntimeError):
    """Raised when no environment+agent combination can be derived."""


_PLATFORM_PREFERENCE = ("windows", "linux", "mac")


def resolve_foothold(
    bas: BasClient,
    *,
    environment_id: UUID | str | None = None,
    environment_name: str | None = None,
    platform_hint: str | None = None,
) -> dict[str, Any]:
    """Pick an environment + an active agent inside it; return foothold dict.

    Precedence for environment selection:
        explicit id  >  explicit name  >  most-recent environment.
    The BasClient's `environments.resolve()` already honours these and the
    `BAS_ENVIRONMENT_*` env overrides.

    Precedence for agent selection:
        platform_hint  >  windows  >  linux  >  mac  >  whatever's online.
    """
    logger.info(
        "[bas] GET /environments  (selector: id=%s name=%s)",
        environment_id,
        environment_name,
    )
    env = bas.environments.resolve(environment_id, name=environment_name)
    if env.environment_id is None:
        raise FootholdResolutionError("environment has no id field")
    logger.info(
        "[bas] selected environment id=%s name=%r",
        env.environment_id,
        env.name,
    )

    logger.info("[bas] GET /environments/%s/agents", env.environment_id)
    agents = bas.agents.list(env.environment_id)
    logger.info(
        "[bas] received %d agent(s): %s",
        len(agents),
        ", ".join(
            f"{a.hostname or a.agent_id}({(a.platform or '?').lower()}/{a.display_status or '?'})"
            for a in agents
        )
        or "<none>",
    )
    by_platform = AgentsApi.pick_by_platform(agents)
    if not by_platform:
        raise FootholdResolutionError(
            f"no enabled agents in environment {env.name!r} ({env.environment_id}); "
            f"saw {len(agents)} agent record(s)"
        )

    chosen = None
    if platform_hint and platform_hint in by_platform:
        chosen = by_platform[platform_hint]
        pick_reason = f"platform_hint={platform_hint!r}"
    else:
        pick_reason = "preference-order"
        for p in _PLATFORM_PREFERENCE:
            if p in by_platform:
                chosen = by_platform[p]
                break
        if chosen is None:
            chosen = next(iter(by_platform.values()))
            pick_reason = "first-available"

    status = (chosen.display_status or "").lower()
    is_online = status in {"online", "active", "connected"}
    if not is_online:
        logger.warning(
            "[foothold] chosen agent %s is %s; proceeding because M1 only "
            "registers ability/adversary definitions (no execution).",
            chosen.agent_id,
            chosen.display_status or "unknown-status",
        )

    foothold = {
        "environment_id": str(env.environment_id),
        "environment_name": env.name,
        "agent_id": chosen.agent_id,
        "hostname": chosen.hostname,
        "platform": chosen.platform,
        "ip_address": chosen.ip_address,
        "display_status": chosen.display_status,
    }
    logger.info(
        "[foothold] picked agent=%s hostname=%s platform=%s ip=%s status=%s (%s)",
        chosen.agent_id,
        chosen.hostname,
        chosen.platform,
        chosen.ip_address,
        chosen.display_status,
        pick_reason,
    )
    return foothold
