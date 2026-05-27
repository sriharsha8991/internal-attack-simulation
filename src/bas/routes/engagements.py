"""Engagement CRUD, catalogue, and health routes.

Single responsibility: FastAPI route handlers for engagements, phases,
skills, environments, and artifacts. No business logic — delegates to
bootstrap and worker modules.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, status

from ..bootstrap import _bootstrap
from ..client import BasClient
from ..persistence import make_record, now_iso
from ..phases import build_phase_index, known_phases, resolve_phases_to_skills
from ..schemas import (
    EngagementCreateRequest,
    EngagementDetail,
    EngagementSubmitResponse,
    EngagementSummary,
    PhaseInfo,
    SkillInfo,
)
from ..worker import _run_engagement

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarise(record: dict[str, Any]) -> EngagementSummary:
    state = record.get("state") or {}
    return EngagementSummary(
        engagement_id=record["run_id"],
        status=record["status"],
        started_at=record["started_at"],
        finished_at=record.get("finished_at"),
        iterations=int(state.get("iteration") or 0),
        completed_stages=list(state.get("completed_stages") or []),
        error=record.get("error"),
    )


def _require(engagement_id: str):
    cfg, skills, store, _artifacts = _bootstrap()
    record = store.get(engagement_id)
    if record is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"engagement {engagement_id!r} not found"
        )
    return cfg, skills, store, record


# ---------------------------------------------------------------------------
# Catalogue + health
# ---------------------------------------------------------------------------


@router.get("/healthz")
def healthz() -> dict[str, Any]:
    cfg, _, store, artifacts = _bootstrap()
    return {
        "status": "ok",
        "base_url": cfg.bas.base_url,
        "dry_run_default": cfg.bas.dry_run,
        "engagements_dir": str(store.runs_dir),
        "artifacts_dir": str(artifacts.root),
    }


@router.get("/phases", response_model=list[PhaseInfo])
def list_phases() -> list[PhaseInfo]:
    _, skills, _, _ = _bootstrap()
    index = build_phase_index(skills)
    return [PhaseInfo(name=name, skills=members) for name, members in sorted(index.items())]


@router.get("/skills", response_model=list[SkillInfo])
def list_skills() -> list[SkillInfo]:
    _, skills, _, _ = _bootstrap()
    return [
        SkillInfo(
            name=s.name,
            description=s.description,
            stage=s.stage,
            mitre_tactics=s.mitre_tactics,
            tool_count=s.tool_count,
        )
        for s in skills.list_summaries()
    ]


@router.get("/environments")
def list_environments() -> list[dict[str, Any]]:
    cfg, _, _, _ = _bootstrap()
    bas = BasClient.from_config(cfg.bas)
    try:
        return [env.model_dump(mode="json") for env in bas.environments.list()]
    finally:
        bas.close()


# ---------------------------------------------------------------------------
# Engagements CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/engagements",
    response_model=EngagementSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_engagement(
    request: EngagementCreateRequest,
    background_tasks: BackgroundTasks,
) -> EngagementSubmitResponse:
    _, skills, store, _artifacts = _bootstrap()

    # Fail fast on bad phase names.
    if request.phases:
        _, unknown = resolve_phases_to_skills(request.phases, skills)
        if unknown:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"unknown phases: {unknown}; known: {known_phases(skills)}",
            )

    engagement_id = uuid.uuid4().hex
    record = make_record(
        run_id=engagement_id, request=request.model_dump(mode="json")
    )
    store.save(record)

    background_tasks.add_task(_run_engagement, engagement_id)

    return EngagementSubmitResponse(
        engagement_id=engagement_id,
        status_url=f"/engagements/{engagement_id}",
        status="queued",
    )


@router.get("/engagements", response_model=list[EngagementSummary])
def list_engagements(limit: int = 50) -> list[EngagementSummary]:
    _, _, store, _ = _bootstrap()
    return [_summarise(r) for r in store.list_all(limit=limit)]


@router.get("/engagements/{engagement_id}", response_model=EngagementDetail)
def get_engagement(engagement_id: str) -> EngagementDetail:
    cfg, _, _, record = _require(engagement_id)
    request = EngagementCreateRequest.model_validate(record["request"])
    summary = _summarise(record)
    state = record.get("state") or {}
    log = state.get("log") or []
    return EngagementDetail(
        **summary.model_dump(),
        phases=request.phases,
        environment=request.environment,
        target=request.target,
        foothold=record.get("foothold"),
        skill_order=record.get("skill_order"),
        dry_run=request.dry_run if request.dry_run is not None else cfg.bas.dry_run,
        state=record.get("state"),
        log_tail=list(log[-100:]),
    )


@router.get("/engagements/{engagement_id}/log", response_model=list[str])
def get_engagement_log(engagement_id: str) -> list[str]:
    _, _, _, record = _require(engagement_id)
    state = record.get("state") or {}
    return list(state.get("log") or [])


@router.delete("/engagements/{engagement_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_engagement(engagement_id: str) -> None:
    _, _, store, _ = _bootstrap()
    if not store.delete(engagement_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"engagement {engagement_id!r} not found"
        )


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


@router.get("/engagements/{engagement_id}/artifacts")
def list_artifacts(engagement_id: str) -> dict[str, list[dict[str, Any]]]:
    """Return the saved ability/adversary specs for one engagement."""
    _require(engagement_id)  # 404 if unknown
    _, _, _, artifacts = _bootstrap()
    base = artifacts.root / engagement_id
    out: dict[str, list[dict[str, Any]]] = {"abilities": [], "adversaries": []}
    for kind in ("abilities", "adversaries"):
        d = base / kind
        if not d.exists():
            continue
        for path in sorted(d.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as f:
                    out[kind].append(json.load(f))
            except (OSError, ValueError):
                continue
    return out
