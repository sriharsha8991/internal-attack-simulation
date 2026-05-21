"""HTTP trigger surface for the orchestrator.

Design rules:
  - The caller declares **intent**, not implementation: which kill-chain
    phases to run (`discovery`, `privesc`, ...). Skill names are internal.
  - Environment + foothold are auto-resolved against the BAS platform. The
    caller can optionally pin to a specific environment by id or name.
  - The agent picks the foothold (online agent in the chosen environment)
    using `platform` as an optional hint.
  - Persistence is one JSON file per engagement under `BAS_ENGAGEMENTS_DIR`
    (defaults to `cfg.run.output_dir`).

Boot:
    uv run uvicorn bas.api:app --host 0.0.0.0 --port 8765 --reload

Endpoints:
    POST   /engagements                 start engagement (background)
    GET    /engagements                 list
    GET    /engagements/{id}            detail
    GET    /engagements/{id}/log        audit log
    DELETE /engagements/{id}            drop from disk + registry
    GET    /phases                      kill-chain phases the catalogue exposes
    GET    /skills                      skill catalogue (low-level)
    GET    /environments                BAS environments (passthrough)
    GET    /healthz                     liveness
"""

from __future__ import annotations

import logging
import os
import threading
import traceback
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .agents import (
    LLMEvaluator,
    LLMMasterRouter,
    LLMPlanner,
    MasterPolicy,
    Planner,
    StaticAcceptEvaluator,
    StaticMasterRouter,
)
from .client import BasClient
from .config import AppConfig
from .foothold import FootholdResolutionError, resolve_foothold
from .llm import get_provider
from ._logging import configure_logging
from .orchestrator import run_orchestrator
from .persistence import ArtifactStore, RunStore, make_record, now_iso
from .phases import build_phase_index, known_phases, resolve_phases_to_skills
from .tools import SkillTool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


PlatformLiteral = Literal["windows", "linux", "mac"]


class EnvironmentSelector(BaseModel):
    """Optional pin for which BAS environment to use. Defaults to most-recent."""

    id: UUID | None = Field(default=None, description="BAS environment_id (UUID).")
    name: str | None = Field(default=None, description="Exact environment name.")


class TargetHint(BaseModel):
    """Optional preferences when selecting the foothold agent."""

    platform: PlatformLiteral | None = None


class EngagementCreateRequest(BaseModel):
    """Minimum trigger payload — everything else is auto-resolved."""

    phases: list[str] | None = Field(
        default=None,
        description=(
            "Kill-chain phases to execute, in order. "
            "Accepted: discovery, privesc, credaccess, lateral, persistence, "
            "defevasion, impact (aliases like 'recon', 'priv-esc', "
            "'credential-access' also work). "
            "When omitted, the LLM router decides each step."
        ),
        examples=[["discovery"], ["discovery", "privesc"]],
    )
    environment: EnvironmentSelector | None = Field(
        default=None,
        description="Which BAS environment to use. Defaults to most-recent.",
    )
    target: TargetHint | None = Field(
        default=None,
        description="Preferences for picking the foothold agent.",
    )
    max_iterations: int = Field(
        default=20,
        ge=1,
        le=50,
        description=(
            "Hard cap on master-router iterations across the engagement. "
            "Each iteration is one master phase decision (briefing or commit); "
            "internal planner attempts (\u22643 per phase) and master revisions "
            "(\u22641 per phase) are capped separately."
        ),
    )
    dry_run: bool | None = Field(
        default=None,
        description="Override `bas.dry_run` from config when set.",
    )


class EngagementSubmitResponse(BaseModel):
    engagement_id: str
    status_url: str
    status: Literal["queued"]


class EngagementSummary(BaseModel):
    engagement_id: str
    status: Literal["queued", "running", "completed", "failed"]
    started_at: datetime
    finished_at: datetime | None
    iterations: int
    completed_stages: list[str]
    error: str | None = None


class EngagementDetail(EngagementSummary):
    phases: list[str] | None
    environment: EnvironmentSelector | None
    target: TargetHint | None
    foothold: dict[str, Any] | None
    skill_order: list[str] | None
    dry_run: bool
    state: dict[str, Any] | None
    log_tail: list[str]


class PhaseInfo(BaseModel):
    name: str
    skills: list[str]


class SkillInfo(BaseModel):
    name: str
    description: str
    stage: str | None
    mitre_tactics: list[str]
    tool_count: int


# ---------------------------------------------------------------------------
# Process-global resources (lazy)
# ---------------------------------------------------------------------------


_state: dict[str, Any] = {"cfg": None, "skills": None, "store": None, "artifacts": None}
_state_lock = threading.Lock()


def _bootstrap() -> tuple[AppConfig, SkillTool, RunStore, ArtifactStore]:
    with _state_lock:
        if _state["cfg"] is None:
            configure_logging()
            cfg = AppConfig.load()
            runs_dir = (
                os.environ.get("BAS_ENGAGEMENTS_DIR")
                or os.environ.get("BAS_RUNS_DIR")
                or cfg.run.output_dir
            )
            _state["cfg"] = cfg
            _state["skills"] = SkillTool("skills").prime()
            _state["store"] = RunStore(runs_dir)
            _state["artifacts"] = ArtifactStore(runs_dir)
            logger.info(
                "[boot] base_url=%s dry_run=%s engagements_dir=%s skills=%d",
                cfg.bas.base_url,
                cfg.bas.dry_run,
                runs_dir,
                len(_state["skills"].list_summaries()),
            )
        return (
            _state["cfg"],
            _state["skills"],
            _state["store"],
            _state["artifacts"],
        )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _build_master(cfg: AppConfig, *, dry_run: bool) -> MasterPolicy:
    """Master router (campaign director). Falls back to StaticMasterRouter when
    no LLM key is configured (dry-run only)."""
    if dry_run:
        try:
            return LLMMasterRouter(get_provider(cfg.llm))
        except Exception:
            return StaticMasterRouter()
    return LLMMasterRouter(get_provider(cfg.llm))


def _build_evaluator(cfg: AppConfig, *, dry_run: bool):
    if dry_run:
        try:
            return LLMEvaluator(get_provider(cfg.llm))
        except Exception:
            return StaticAcceptEvaluator()
    return LLMEvaluator(get_provider(cfg.llm))


def _build_planner(cfg: AppConfig, *, dry_run: bool) -> Planner:
    if dry_run:
        try:
            return LLMPlanner(get_provider(cfg.llm))
        except Exception:
            return _DryRunStubPlanner()
    return LLMPlanner(get_provider(cfg.llm))


class _DryRunStubPlanner:
    """Offline plan generator used only when dry_run=True and no LLM key."""

    def plan(self, skill, state):  # type: ignore[override]
        from .agents import SpecialistPlan
        from .models import (
            AbilityCreate,
            AbilityStageCreate,
            AdversaryCreate,
            GeneratedAbility,
        )

        fm = skill.frontmatter
        foothold = state.get("foothold") or {}
        platform = foothold.get("platform") or "linux"
        executor = "cmd" if platform == "windows" else "sh"
        return SpecialistPlan(
            adversary=AdversaryCreate(
                name=f"{fm.name}-adversary",
                description=f"dry-run stub for {fm.name}",
                profile="ai-stub",
            ),
            abilities=[
                GeneratedAbility(
                    ability=AbilityCreate(
                        name=f"{fm.name}-ability",
                        description=fm.description[:200],
                        mitre_tactic=(fm.mitre_tactics or [None])[0],
                        platform=platform,
                        default_severity="low",
                    ),
                    stages=[
                        AbilityStageCreate(
                            stage_name="identify",
                            stage_order=1,
                            executor=executor,
                            command_template="whoami",
                        ),
                        AbilityStageCreate(
                            stage_name="enumerate",
                            stage_order=2,
                            executor=executor,
                            command_template="hostname",
                        ),
                    ],
                    rationale=f"dry-run stub plan for {fm.name}",
                    grounding_depth="skip",
                    provider="dry-run-stub",
                )
            ],
        )


def _serialise_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "engagement_id": state.get("run_id"),
        "iteration": state.get("iteration"),
        "next_stage": state.get("next_stage"),
        "current_phase": state.get("current_phase"),
        "available_phases": state.get("available_phases", []),
        "completed_phases": state.get("completed_phases", []),
        "master_briefing": state.get("master_briefing"),
        "master_revisions_used": state.get("master_revisions_used"),
        "planner_attempts": state.get("planner_attempts"),
        "planner_tool_calls": state.get("planner_tool_calls"),
        "proposal_log": list(state.get("proposal_log") or []),
        "phase_history": list(state.get("phase_history") or []),
        "completed_stages": state.get("completed_stages", []),
        "stage_results": [
            sr.model_dump(mode="json") for sr in state.get("stage_results", []) or []
        ],
        "memory": state.get("memory", {}),
        "foothold": state.get("foothold", {}),
        "max_iterations": state.get("max_iterations"),
        "log": list(state.get("log") or []),
    }


def _run_engagement(engagement_id: str) -> None:
    cfg, skill_tool, store, artifacts = _bootstrap()
    record = store.get(engagement_id)
    if record is None:
        return

    record["status"] = "running"
    store.save(record)

    request = EngagementCreateRequest.model_validate(record["request"])
    logger.info(
        "[engagement %s] START phases=%s environment=%s target=%s dry_run=%s max_iter=%d",
        engagement_id,
        request.phases,
        request.environment.model_dump() if request.environment else None,
        request.target.model_dump() if request.target else None,
        request.dry_run if request.dry_run is not None else cfg.bas.dry_run,
        request.max_iterations,
    )
    try:
        dry_run = request.dry_run if request.dry_run is not None else cfg.bas.dry_run
        bas = BasClient(
            cfg.bas.base_url,
            sleep_ms=cfg.bas.sleep_ms,
            timeout=cfg.bas.timeout,
            dry_run=dry_run,
        )
        try:
            # 1. Validate phases against catalogue (the master picks the order).
            requested_phases = list(request.phases or [])
            if requested_phases:
                _, unknown = resolve_phases_to_skills(requested_phases, skill_tool)
                if unknown:
                    raise ValueError(
                        f"unknown phases: {unknown}; known: {known_phases(skill_tool)}"
                    )
            else:
                requested_phases = known_phases(skill_tool)
            record["skill_order"] = requested_phases
            logger.info(
                "[engagement %s] available_phases=%s",
                engagement_id,
                requested_phases,
            )

            # 2. Resolve foothold against BAS
            foothold = resolve_foothold(
                bas,
                environment_id=request.environment.id if request.environment else None,
                environment_name=request.environment.name if request.environment else None,
                platform_hint=request.target.platform if request.target else None,
            )
            record["foothold"] = foothold
            store.save(record)  # checkpoint so caller can poll mid-flight

            # 3. Build master + planner + evaluator, run the graph
            master = _build_master(cfg, dry_run=dry_run)
            planner = _build_planner(cfg, dry_run=dry_run)
            evaluator = _build_evaluator(cfg, dry_run=dry_run)

            state = run_orchestrator(
                master=master,
                skill_tool=skill_tool,
                planner=planner,
                bas=bas,
                foothold=foothold,
                available_phases=requested_phases,
                max_iterations=request.max_iterations,
                initial_state={"run_id": engagement_id},
                artifacts=artifacts,
                evaluator=evaluator,
            )
            record["state"] = _serialise_state(state)
            record["artifacts_dir"] = str(artifacts.root / engagement_id)
        finally:
            bas.close()
        record["status"] = "completed"
        logger.info(
            "[engagement %s] COMPLETED stages=%s iterations=%s",
            engagement_id,
            (record.get("state") or {}).get("completed_stages"),
            (record.get("state") or {}).get("iteration"),
        )
    except Exception as exc:  # noqa: BLE001
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        logger.exception("[engagement %s] FAILED", engagement_id)
    finally:
        record["finished_at"] = now_iso()
        store.save(record)


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


def _require(engagement_id: str) -> tuple[AppConfig, SkillTool, RunStore, dict[str, Any]]:
    cfg, skills, store, _artifacts = _bootstrap()
    record = store.get(engagement_id)
    if record is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"engagement {engagement_id!r} not found"
        )
    return cfg, skills, store, record


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


app = FastAPI(
    title="BAS Orchestrator API",
    version="0.2.0",
    description="HTTP trigger surface for the BAS internal-attack orchestrator.",
)


@app.on_event("startup")
def _startup() -> None:
    _, _, store, _artifacts = _bootstrap()
    for rec in store.list_all():
        if rec.get("status") in ("queued", "running"):
            rec["status"] = "failed"
            rec["error"] = rec.get("error") or "process restarted before completion"
            rec["finished_at"] = now_iso()
            store.save(rec)


# ---- catalogue + health ----------------------------------------------------


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    cfg, _, store, artifacts = _bootstrap()
    return {
        "status": "ok",
        "base_url": cfg.bas.base_url,
        "dry_run_default": cfg.bas.dry_run,
        "engagements_dir": str(store.runs_dir),
        "artifacts_dir": str(artifacts.root),
    }


@app.get("/phases", response_model=list[PhaseInfo])
def list_phases() -> list[PhaseInfo]:
    _, skills, _, _ = _bootstrap()
    index = build_phase_index(skills)
    return [PhaseInfo(name=name, skills=members) for name, members in sorted(index.items())]


@app.get("/skills", response_model=list[SkillInfo])
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


@app.get("/environments")
def list_environments() -> list[dict[str, Any]]:
    cfg, _, _, _ = _bootstrap()
    bas = BasClient.from_config(cfg.bas)
    try:
        return [env.model_dump(mode="json") for env in bas.environments.list()]
    finally:
        bas.close()


# ---- engagements -----------------------------------------------------------


@app.post(
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


@app.get("/engagements", response_model=list[EngagementSummary])
def list_engagements(limit: int = 50) -> list[EngagementSummary]:
    _, _, store, _ = _bootstrap()
    return [_summarise(r) for r in store.list_all(limit=limit)]


@app.get("/engagements/{engagement_id}", response_model=EngagementDetail)
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


@app.get("/engagements/{engagement_id}/log", response_model=list[str])
def get_engagement_log(engagement_id: str) -> list[str]:
    _, _, _, record = _require(engagement_id)
    state = record.get("state") or {}
    return list(state.get("log") or [])


@app.delete("/engagements/{engagement_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_engagement(engagement_id: str) -> None:
    _, _, store, _ = _bootstrap()
    if not store.delete(engagement_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"engagement {engagement_id!r} not found"
        )


# ---- artifacts (locally-saved ability + adversary specs) -------------------


@app.get("/engagements/{engagement_id}/artifacts")
def list_artifacts(engagement_id: str) -> dict[str, list[dict[str, Any]]]:
    """Return the saved ability/adversary specs for one engagement."""
    _, _, _, _ = _require(engagement_id)  # 404 if unknown
    _, _, _, artifacts = _bootstrap()
    base = artifacts.root / engagement_id
    out: dict[str, list[dict[str, Any]]] = {"abilities": [], "adversaries": []}
    for kind in ("abilities", "adversaries"):
        d = base / kind
        if not d.exists():
            continue
        for path in sorted(d.glob("*.json")):
            try:
                import json as _json
                with path.open("r", encoding="utf-8") as f:
                    out[kind].append(_json.load(f))
            except (OSError, ValueError):
                continue
    return out


# Foothold resolution failures map to 503 (platform-side issue, not a bad request).
@app.exception_handler(FootholdResolutionError)
def _on_foothold_error(_request: Any, exc: FootholdResolutionError) -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": str(exc)},
    )
