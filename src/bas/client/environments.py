"""Environments resource — GET /environments.

Environment selection precedence (highest wins):
    1. Explicit `environment_id` / `name` argument to `resolve()`.
    2. Env var `BAS_ENVIRONMENT_ID`.
    3. Env var `BAS_ENVIRONMENT_NAME` (case-insensitive match).
    4. Most recent environment by `updated_at` / `created_at`.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from ..models import EnvironmentResponse
from . import dry_run as _dry
from .errors import BasClientError
from .transport import HttpTransport

ENV_ID_VAR = "BAS_ENVIRONMENT_ID"
ENV_NAME_VAR = "BAS_ENVIRONMENT_NAME"


class EnvironmentsApi:
    def __init__(self, transport: HttpTransport, *, dry_run: bool = False) -> None:
        self._t = transport
        self._dry = dry_run

    # ---- raw -----------------------------------------------------------------

    def list(self) -> list[EnvironmentResponse]:
        if self._dry:
            return _dry.fake_environments()
        data = self._t.get_json("/environments")
        return [EnvironmentResponse.model_validate(x) for x in self._t.unwrap_list(data)]

    # ---- selection -----------------------------------------------------------

    def resolve(
        self,
        environment_id: UUID | str | None = None,
        *,
        name: str | None = None,
    ) -> EnvironmentResponse:
        """Pick the environment to operate against, honoring overrides."""
        envs = self.list()
        if not envs:
            raise BasClientError("GET", "/environments", 200, "no environments returned")

        # 1. explicit args
        if environment_id is not None:
            return _find_by_id(envs, environment_id)
        if name is not None:
            return _find_by_name(envs, name)

        # 2 + 3. environment-variable overrides
        env_id = os.getenv(ENV_ID_VAR)
        if env_id:
            return _find_by_id(envs, env_id)
        env_name = os.getenv(ENV_NAME_VAR)
        if env_name:
            return _find_by_name(envs, env_name)

        # 4. fallback: most recent
        return self._most_recent(envs)

    def pick_latest(self) -> EnvironmentResponse:
        """Most recent environment, ignoring overrides — kept for completeness."""
        envs = self.list()
        if not envs:
            raise BasClientError("GET", "/environments", 200, "no environments returned")
        return self._most_recent(envs)

    # ---- helpers -------------------------------------------------------------

    @staticmethod
    def _most_recent(envs: list[EnvironmentResponse]) -> EnvironmentResponse:
        def keyfn(e: EnvironmentResponse) -> Any:
            return e.updated_at or e.created_at or ""

        return sorted(envs, key=keyfn, reverse=True)[0]


def _find_by_id(envs: list[EnvironmentResponse], wanted: UUID | str) -> EnvironmentResponse:
    target = str(wanted)
    for e in envs:
        if e.environment_id is not None and str(e.environment_id) == target:
            return e
    raise BasClientError(
        "GET", "/environments", 200, f"environment id not found: {target}"
    )


def _find_by_name(envs: list[EnvironmentResponse], wanted: str) -> EnvironmentResponse:
    target = wanted.strip().lower()
    matches = [e for e in envs if (e.name or "").strip().lower() == target]
    if not matches:
        raise BasClientError(
            "GET", "/environments", 200, f"environment name not found: {wanted!r}"
        )
    if len(matches) > 1:
        raise BasClientError(
            "GET",
            "/environments",
            200,
            f"environment name {wanted!r} is ambiguous ({len(matches)} matches)",
        )
    return matches[0]
