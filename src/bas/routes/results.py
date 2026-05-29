"""Webhook receiver for BAS backend execution results.

Single endpoint: ``POST /results``

Accepts the raw JSON the backend POSTs after an operation completes, validates
it, and stores it idempotently on disk. Status transitions and graph resume
are handled by later phases — this module only persists.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import JSONResponse

from ..persistence import ResultStore, RunStore
from ..schemas import OperationResultRequest, ResultAcceptedResponse

logger = logging.getLogger(__name__)

MAX_RESULT_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MB default

router = APIRouter(tags=["results"])


@router.post(
    "/results",
    response_model=ResultAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"description": "Duplicate result (already received)", "model": ResultAcceptedResponse},
        404: {"description": "Engagement not found"},
        409: {"description": "Engagement not in awaiting_results or running status"},
        422: {"description": "Invalid JSON or missing/invalid fields"},
    },
    summary="Receive execution results from BAS backend",
    description=(
        "Accept execution results POSTed by the BAS backend after abilities are "
        "executed on the target agent. The `engagement_id` (from `POST /engagements`) "
        "and `operation_id` are both in the request body.\n\n"
        "**Idempotent**: duplicate `operation_id` returns 200 without re-processing."
    ),
)
async def receive_results(
    payload: OperationResultRequest,
    background_tasks: BackgroundTasks,
) -> Any:
    # Resolve stores from process-global state.
    from ..bootstrap import _bootstrap, _state
    _bootstrap()
    store: RunStore = _state["store"]
    results_store: ResultStore = _state["results_store"]

    # 1. Extract engagement_id from body and normalise format.
    #    Our store keys are 32-char hex (no dashes). The backend may send
    #    the dashed UUID form (e.g. "b3b3bdc6-1926-42f9-8311-55ce1107bd33").
    engagement_id = payload.engagement_id.replace("-", "")

    # 2. Engagement must exist
    record = store.get(engagement_id)
    if record is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"engagement {engagement_id!r} not found",
        )

    # 3. Engagement must be in a state that can accept results.
    #    If already completed/failed (e.g. timeout scanner expired it),
    #    we still persist the result for audit but skip the graph resume.
    eng_status = record.get("status")
    _expired = eng_status not in ("awaiting_results", "running")

    # 4. Validate operation_id format (nested inside operation object)
    operation_id = payload.operation.operation_id
    try:
        uuid.UUID(str(operation_id))
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"'operation.operation_id' is not a valid UUID: {operation_id!r}",
        )
    operation_id = str(operation_id)

    # 5. Serialize payload to dict for storage (preserves full backend snapshot)
    payload_dict = payload.model_dump(mode="json")

    # 6. Idempotency — already received this operation's result
    if results_store.exists(engagement_id, operation_id):
        return {"status": "already_received", "operation_id": operation_id}

    # 7. Save to disk (always persisted for audit, even if expired)
    results_store.save(engagement_id, operation_id, payload_dict)
    logger.info(
        "[results] saved result for engagement=%s operation=%s (expired=%s)",
        engagement_id,
        operation_id,
        _expired,
    )

    # 8. Resume graph only if engagement is still active.
    if _expired:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "accepted_late",
                "operation_id": operation_id,
                "detail": f"engagement already {eng_status!r}; result stored but graph not resumed",
            },
        )

    # Schedule graph resume in the background so this response returns 202
    # immediately without blocking on LLM calls.
    from ..worker import _resume_graph
    background_tasks.add_task(_resume_graph, engagement_id, payload_dict)

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"status": "accepted", "operation_id": operation_id},
    )
