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
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from .bootstrap import _bootstrap, _get_compiled_graph, _state
from .foothold import FootholdResolutionError
from .persistence import RunStore, now_iso
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

    _start_timeout_scanner(cfg.execution.result_wait_timeout)


def _start_timeout_scanner(timeout_seconds: int) -> None:
    """Periodically resume engagements stuck in awaiting_results past timeout."""
    import time

    def _scan() -> None:
        while True:
            time.sleep(60)  # check every minute
            try:
                _expire_stale_engagements(timeout_seconds)
            except Exception:  # noqa: BLE001
                logger.exception("[timeout-scanner] error during scan")

    t = threading.Thread(target=_scan, daemon=True, name="timeout-scanner")
    t.start()
    logger.info("[boot] timeout scanner started (timeout=%ds)", timeout_seconds)


def _expire_stale_engagements(timeout_seconds: int) -> None:
    """Resume graphs that have been waiting longer than the configured timeout."""
    from langgraph.types import Command

    from .worker import _get_engagement_lock

    store: RunStore = _state["store"]
    compiled = _get_compiled_graph()
    now = datetime.now(timezone.utc)

    for record in store.list_all():
        if record.get("status") != "awaiting_results":
            continue
        awaiting_since = record.get("awaiting_since")
        if not awaiting_since:
            continue
        elapsed = (now - datetime.fromisoformat(awaiting_since)).total_seconds()
        if elapsed <= timeout_seconds:
            continue

        engagement_id = record["run_id"]
        logger.warning(
            "[timeout-scanner] expiring engagement %s after %ds",
            engagement_id,
            int(elapsed),
        )

        # Acquire per-engagement lock to prevent racing with _resume_graph.
        lock = _get_engagement_lock(engagement_id)
        if not lock.acquire(timeout=10):
            logger.info(
                "[timeout-scanner] engagement %s locked by resume; skipping this cycle",
                engagement_id,
            )
            continue

        try:
            # Re-read under lock — a webhook resume may have already changed status.
            fresh = store.get(engagement_id)
            if not fresh or fresh.get("status") != "awaiting_results":
                continue

            from langgraph.errors import GraphInterrupt

            fresh["status"] = "running"
            fresh.pop("awaiting_since", None)
            store.save(fresh)

            compiled.invoke(
                Command(resume={"timeout": True, "engagement_id": engagement_id}),
                config={"configurable": {"thread_id": engagement_id}},
            )
            fresh["status"] = "completed"
            fresh["finished_at"] = now_iso()
            store.save(fresh)
        except GraphInterrupt:
            fresh["status"] = "awaiting_results"
            fresh["awaiting_since"] = now_iso()
            store.save(fresh)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[timeout-scanner] failed to resume engagement %s",
                engagement_id,
            )
            fresh["status"] = "failed"
            fresh["error"] = "timeout-scanner resume failed"
            fresh["finished_at"] = now_iso()
            store.save(fresh)
        finally:
            lock.release()


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
