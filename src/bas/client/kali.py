"""Kali toolbox sidecar client.

Communicates with the lightweight FastAPI sidecar running inside the Kali
container over the Docker internal network.  Manages its own ``httpx.Client``
(separate from ``HttpTransport``) because it targets a different service with
a different timeout profile.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class KaliError(RuntimeError):
    """Raised when the Kali sidecar returns a non-2xx response or is unreachable."""

    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"Kali {method} {path} -> {status}: {body[:500]}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body


class KaliClient:
    """HTTP client for the Kali toolbox sidecar REST API."""

    def __init__(
        self,
        base_url: str = "http://kali-toolbox:9000",
        *,
        timeout: float = 300.0,
        connect_timeout: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            headers={"Accept": "application/json"},
        )

    # ── lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> "KaliClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── internal request helper ───────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise KaliError(
                method, path, 0,
                f"connection failed (cannot reach {self.base_url}): {exc}",
            ) from exc
        except httpx.TimeoutException as exc:
            raise KaliError(
                method, path, 0,
                f"request timed out ({self.base_url}): {exc}",
            ) from exc
        if resp.status_code >= 400:
            raise KaliError(method, path, resp.status_code, resp.text)
        return resp

    # ── health ────────────────────────────────────────────────────────────

    def is_healthy(self) -> bool:
        try:
            resp = self._request("GET", "/health")
            return resp.status_code == 200
        except (KaliError, Exception):
            return False

    # ── command execution ─────────────────────────────────────────────────

    def exec_command(
        self,
        command: str,
        *,
        timeout: int = 120,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"command": command, "timeout": timeout}
        if cwd:
            payload["cwd"] = cwd
        resp = self._request("POST", "/exec", json=payload)
        return resp.json()

    # ── file transfer ─────────────────────────────────────────────────────

    def upload(self, local_path: str | Path, dest_path: str) -> dict[str, Any]:
        local = Path(local_path)
        with open(local, "rb") as f:
            resp = self._request(
                "POST",
                "/upload",
                files={"file": (local.name, f)},
                data={"dest_path": dest_path},
            )
        return resp.json()

    def upload_bytes(
        self,
        content: bytes,
        filename: str,
        dest_path: str,
    ) -> dict[str, Any]:
        resp = self._request(
            "POST",
            "/upload",
            files={"file": (filename, content)},
            data={"dest_path": dest_path},
        )
        return resp.json()

    def download(self, remote_path: str) -> bytes:
        resp = self._request("GET", "/download", params={"path": remote_path})
        return resp.content

    def download_to(self, remote_path: str, local_path: str | Path) -> Path:
        data = self.download(remote_path)
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    # ── key / credential parsing ──────────────────────────────────────────

    def parse_keys(
        self,
        *,
        path: str | None = None,
        content: str | None = None,
        parse_type: str = "auto",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"parse_type": parse_type}
        if path:
            payload["path"] = path
        if content:
            payload["content"] = content
        resp = self._request("POST", "/parse/keys", json=payload)
        return resp.json()

    # ── BloodHound ingestion ──────────────────────────────────────────────

    def bloodhound_ingest(
        self,
        domain: str,
        dc_ip: str,
        *,
        username: str | None = None,
        password: str | None = None,
        hashes: str | None = None,
        collection_method: str = "Default",
        output_dir: str = "/tmp/bloodhound",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "domain": domain,
            "dc_ip": dc_ip,
            "collection_method": collection_method,
            "output_dir": output_dir,
        }
        if username:
            payload["username"] = username
        if password:
            payload["password"] = password
        if hashes:
            payload["hashes"] = hashes
        resp = self._request("POST", "/bloodhound/ingest", json=payload)
        return resp.json()

    # ── tool inventory ────────────────────────────────────────────────────

    def list_tools(self) -> list[dict[str, Any]]:
        resp = self._request("GET", "/tools")
        data = resp.json()
        return data.get("tools", []) if isinstance(data, dict) else data
