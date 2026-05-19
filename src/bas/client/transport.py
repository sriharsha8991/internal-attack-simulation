"""HTTP transport for the BAS Platform.

Single responsibility: take a (method, path, payload) and return a parsed body,
raising `BasClientError` on non-2xx. Carries an optional `sleep_ms` pause used
by callers between bulk POSTs.

The backend does not require authentication in M1, so this transport is purely
a thin wrapper around `httpx.Client`. This module knows nothing about dry-run,
resources, or domain models.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .errors import BasClientError


class HttpTransport:
    def __init__(
        self,
        base_url: str,
        *,
        sleep_ms: int = 250,
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.sleep_ms = sleep_ms
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    # ---- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> "HttpTransport":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---- request primitives -------------------------------------------------

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        resp = self._http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise BasClientError(method, path, resp.status_code, resp.text)
        return resp

    def get_json(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs).json()

    def post_json(self, path: str, *, json: Any | None = None, **kwargs: Any) -> Any:
        resp = self.request("POST", path, json=json, **kwargs)
        return resp.json() if resp.content else {}

    def pause(self) -> None:
        if self.sleep_ms > 0:
            time.sleep(self.sleep_ms / 1000.0)

    @staticmethod
    def unwrap_list(data: Any) -> list[Any]:
        """BAS endpoints sometimes return a bare list, sometimes {items: [...]}."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("items", [])
        return []
