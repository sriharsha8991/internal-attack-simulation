"""Operations resource — drive + poll operations on the BAS backend.

This is the pull-based counterpart to the ``/results`` webhook: instead of
waiting for the backend to POST execution output to us, we can create/start an
operation and poll ``GET /operations/{id}`` ("detailed info with execution
logs") until it finishes. The detail payload has the same shape the webhook
delivers (``operation`` + ``abilities`` + ``execution_logs`` + ``progress``), so
the existing ``bas.results.parse_operation_result`` consumes it unchanged.

Endpoints (per the platform OpenAPI):
    GET  /operations                              list
    POST /operations                              create
    GET  /operations/{id}                         detail (with execution logs)
    GET  /operations/{id}/abilities-payloads      graph-ready ability data
    POST /operations/{id}/start|pause|resume|stop lifecycle
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from .transport import HttpTransport

logger = logging.getLogger(__name__)

# Operation statuses that mean "no more output is coming" — polling stops here.
# Kept lowercase; compared case-insensitively. `extra=allow` upstream means the
# backend may add more, so we treat anything matching these substrings as final.
_TERMINAL_STATUSES = frozenset(
    {"completed", "complete", "finished", "done", "stopped", "failed", "error", "cancelled", "canceled"}
)


def _status_of(detail: dict[str, Any]) -> str:
    """Pull the operation status from a detail payload (top-level or nested)."""
    op = detail.get("operation")
    if isinstance(op, dict) and op.get("status"):
        return str(op["status"])
    return str(detail.get("status") or "")


def _progress_percent(detail: dict[str, Any]) -> float | None:
    prog = detail.get("progress")
    if isinstance(prog, dict) and prog.get("progress_percent") is not None:
        try:
            return float(prog["progress_percent"])
        except (TypeError, ValueError):
            return None
    return None


def is_operation_complete(detail: dict[str, Any]) -> bool:
    """True when an operation-detail payload indicates execution has finished."""
    status = _status_of(detail).strip().lower()
    if any(term in status for term in _TERMINAL_STATUSES):
        return True
    pct = _progress_percent(detail)
    return pct is not None and pct >= 100.0


class OperationsApi:
    """Create, drive, and poll operations on the BAS backend."""

    def __init__(self, transport: HttpTransport, *, dry_run: bool = False) -> None:
        self._t = transport
        self._dry = dry_run

    # ---- read ---------------------------------------------------------------

    def list(self, **params: Any) -> list[dict[str, Any]]:
        """GET /operations (optionally filtered by query params)."""
        if self._dry:
            return []
        data = self._t.get_json("/operations", params=params or None)
        return self._t.unwrap_list(data)

    def get_detail(self, operation_id: str) -> dict[str, Any]:
        """GET /operations/{id} — full operation snapshot with execution logs."""
        if self._dry:
            return {"operation": {"operation_id": operation_id, "status": "completed"}}
        return self._t.get_json(f"/operations/{operation_id}")

    def get_abilities_payloads(self, operation_id: str) -> Any:
        """GET /operations/{id}/abilities-payloads — graph-ready ability data."""
        if self._dry:
            return {}
        return self._t.get_json(f"/operations/{operation_id}/abilities-payloads")

    # ---- lifecycle ----------------------------------------------------------

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /operations — create an operation. Returns the created record."""
        if self._dry:
            return {"operation_id": "dry-run-operation"}
        return self._t.post_json("/operations", json=payload) or {}

    def start(self, operation_id: str) -> dict[str, Any]:
        return self._lifecycle(operation_id, "start")

    def pause(self, operation_id: str) -> dict[str, Any]:
        return self._lifecycle(operation_id, "pause")

    def resume(self, operation_id: str) -> dict[str, Any]:
        return self._lifecycle(operation_id, "resume")

    def stop(self, operation_id: str) -> dict[str, Any]:
        """POST /operations/{id}/stop — kill switch."""
        return self._lifecycle(operation_id, "stop")

    def _lifecycle(self, operation_id: str, action: str) -> dict[str, Any]:
        if self._dry:
            logger.info("[operations] DRY-RUN: would %s op=%s", action, operation_id)
            return {}
        return self._t.post_json(f"/operations/{operation_id}/{action}") or {}

    # ---- polling ------------------------------------------------------------

    def poll_until_complete(
        self,
        operation_id: str,
        *,
        timeout_s: float,
        interval_s: float = 15.0,
        is_done: Callable[[dict[str, Any]], bool] = is_operation_complete,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any] | None:
        """Poll ``GET /operations/{id}`` until complete or ``timeout_s`` elapses.

        Returns the final detail payload once ``is_done`` is satisfied, or the
        last-seen payload if the timeout fires first (caller distinguishes via
        ``is_operation_complete``). Returns None only if no detail was readable.

        ``sleep``/``now`` are injectable for deterministic tests.
        """
        if self._dry:
            return self.get_detail(operation_id)

        deadline = now() + timeout_s
        last: dict[str, Any] | None = None
        while True:
            try:
                last = self.get_detail(operation_id)
            except Exception as exc:  # noqa: BLE001 - transient backend errors
                logger.warning("[operations] poll failed for op=%s: %s", operation_id, exc)
            if last is not None and is_done(last):
                logger.info(
                    "[operations] op=%s reached terminal state (%s)",
                    operation_id,
                    _status_of(last) or "complete",
                )
                return last
            if now() >= deadline:
                logger.warning(
                    "[operations] op=%s still not complete after %.0fs (status=%s)",
                    operation_id,
                    timeout_s,
                    _status_of(last) if last else "unknown",
                )
                return last
            sleep(interval_s)
