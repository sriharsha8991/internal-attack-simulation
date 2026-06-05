"""HTTP trigger surface for the orchestrator.

This module is the **assembly point** — it creates the FastAPI ``app``,
mounts all routers, and registers lifecycle hooks. No business logic lives
here; routes, bootstrap, schemas, and workers each have their own module.

Boot:
    uv run uvicorn bas.api:app --host 0.0.0.0 --port 8765 --reload

Endpoints:
    POST   /engagements                         start engagement (background)
    GET    /engagements                          list
    GET    /engagements/{id}                     detail
    GET    /engagements/{id}/log                 audit log
    DELETE /engagements/{id}                     drop from disk + registry
    POST   /results                              webhook receiver
    GET    /engagements/{id}/artifacts            saved ability/adversary specs
    GET    /phases                                kill-chain phases
    GET    /skills                                skill catalogue
    GET    /environments                          BAS environments (passthrough)
    GET    /healthz                               liveness
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from .bootstrap import _bootstrap, _state
from .foothold import FootholdResolutionError
from .persistence import now_iso
from .routes import engagements_router, results_router

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="BAS Orchestrator API",
    version="0.2.0",
    description="HTTP trigger surface for the BAS internal-attack orchestrator.",
)

app.include_router(engagements_router)
app.include_router(results_router)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@app.on_event("startup")
def _startup() -> None:
    """Mark orphaned engagements as failed on process restart and start timeout scanner."""
    cfg, _, store, _artifacts = _bootstrap()
    for rec in store.list_all():
        if rec.get("status") in ("queued", "running"):
            rec["status"] = "failed"
            rec["error"] = rec.get("error") or "process restarted before completion"
            rec["finished_at"] = now_iso()
            store.save(rec)

    from .worker import _start_timeout_scanner

    _start_timeout_scanner(
        cfg.execution.result_wait_timeout,
        cfg.execution.result_hard_timeout,
    )


@app.on_event("shutdown")
def _shutdown() -> None:
    """Gracefully close the process-global BasClient (httpx connection pool)."""
    bas = _state.get("bas")
    if bas is not None:
        bas.close()
        logger.info("[shutdown] BasClient closed")


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


@app.exception_handler(FootholdResolutionError)
def _on_foothold_error(_request: Any, exc: FootholdResolutionError) -> Any:
    """Foothold resolution failures map to 503 (platform-side issue)."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc)},
    )
