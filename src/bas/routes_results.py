"""Webhook receiver for BAS backend execution results.

Single endpoint: ``POST /engagements/{engagement_id}/results``

Accepts the raw JSON the backend POSTs after an operation completes, validates
it, and stores it idempotently on disk. Status transitions and graph resume
are handled by later phases — this module only persists.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .persistence import ResultStore, RunStore

logger = logging.getLogger(__name__)

MAX_RESULT_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MB default

router = APIRouter(tags=["results"])


@router.post("/engagements/{engagement_id}/results")
async def receive_results(
    engagement_id: str,
    request: Request,
) -> Any:
    """Accept execution results POSTed by the BAS backend.

    Idempotent: duplicate ``operation_id`` returns 200 without re-processing.
    Status transitions are owned by graph nodes, not this endpoint.
    """
    # Resolve stores from process-global state.
    from .api import _bootstrap, _state
    _bootstrap()
    store: RunStore = _state["store"]
    results_store: ResultStore = _state["results_store"]

    # 1. Engagement must exist
    record = store.get(engagement_id)
    if record is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"engagement {engagement_id!r} not found",
        )

    # 2. Engagement must be waiting for results
    eng_status = record.get("status")
    if eng_status not in ("awaiting_results", "running"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"engagement status is {eng_status!r}; expected 'awaiting_results'",
        )

    # 3. Size guard
    body = await request.body()
    if len(body) > MAX_RESULT_PAYLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"payload exceeds {MAX_RESULT_PAYLOAD_BYTES // (1024 * 1024)} MB limit",
        )

    # 4. Parse JSON
    try:
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"invalid JSON: {exc}",
        )

    # 5. Extract and validate operation_id
    operation_id = payload.get("operation_id")
    if not operation_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "missing 'operation_id' in request body",
        )
    try:
        uuid.UUID(str(operation_id))
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"'operation_id' is not a valid UUID: {operation_id!r}",
        )
    operation_id = str(operation_id)

    # 6. Idempotency — already received this operation's result
    if results_store.exists(engagement_id, operation_id):
        return {"status": "already_received", "operation_id": operation_id}

    # 7. Save to disk
    results_store.save(engagement_id, operation_id, payload)
    logger.info(
        "[results] saved result for engagement=%s operation=%s (%d bytes)",
        engagement_id,
        operation_id,
        len(body),
    )

    # 8. Return accepted — graph resume is wired in Phase 3
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"status": "accepted", "operation_id": operation_id},
    )
