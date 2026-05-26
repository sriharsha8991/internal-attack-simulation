"""Background worker functions for engagement execution and graph resume.

Single responsibility: run and resume engagements in background threads/tasks.
All shared state access goes through ``bootstrap``.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from .bootstrap import (
    _bootstrap,
    _build_checkpointer,
    _build_evaluator,
    _build_master,
    _build_planner,
    _get_compiled_graph,
    _state,
)
from .config import AppConfig
from .foothold import resolve_foothold
from .orchestrator import run_orchestrator
from .persistence import RunStore, now_iso
from .phases import known_phases, resolve_phases_to_skills
from .schemas import EngagementCreateRequest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Completion notification — tells the BAS backend all phases are done
# ---------------------------------------------------------------------------


def _notify_completion(engagement_id: str, status: str) -> None:
    """Send a terminal ``POST /ai/operation-feedback`` with ``loop_status='done'``.

    This lets the backend know the orchestrator is finished and will not push
    any more abilities or feedback for this engagement.
    """
    try:
        bas = _state.get("bas")
        if bas is None or bas.feedback._dry:
            return

        from .client.feedback import AIFeedbackPayload

        payload = AIFeedbackPayload(
            source="operation-analyzer",
            loop_status="done",
            changes=[],
        )
        payload_dict = payload.model_dump(mode="json")
        payload_dict["engagement_id"] = engagement_id
        payload_dict["engagement_status"] = status

        bas.feedback._t.post_json(
            "/ai/operation-feedback",
            json=payload_dict,
        )
        logger.info(
            "[completion] notified backend: engagement=%s status=%s",
            engagement_id,
            status,
        )
    except Exception as exc:  # noqa: BLE001
        # Best-effort — don't let notification failure crash the worker.
        logger.warning(
            "[completion] failed to notify backend for engagement=%s: %s",
            engagement_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Dry-run stub planner (no LLM key needed)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# State serialisation
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Engagement runner
# ---------------------------------------------------------------------------


def _run_engagement(engagement_id: str) -> None:
    """Execute an engagement end-to-end in a background thread."""
    cfg, skill_tool, store, artifacts = _bootstrap()
    record = store.get(engagement_id)
    if record is None:
        return

    record["status"] = "running"
    store.save(record)

    request = EngagementCreateRequest.model_validate(record["request"])
    dry_run = request.dry_run if request.dry_run is not None else cfg.bas.dry_run
    bas = _state["bas"]

    logger.info(
        "[engagement %s] START phases=%s environment=%s target=%s dry_run=%s max_iter=%d",
        engagement_id,
        request.phases,
        request.environment.model_dump() if request.environment else None,
        request.target.model_dump() if request.target else None,
        dry_run,
        request.max_iterations,
    )

    from langgraph.errors import GraphInterrupt

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

        # 3. Build checkpointer, run the graph
        checkpointer = _build_checkpointer(cfg)

        state = run_orchestrator(
            master=_build_master(cfg, dry_run=dry_run),
            skill_tool=skill_tool,
            planner=_build_planner(cfg, dry_run=dry_run),
            bas=bas,
            foothold=foothold,
            available_phases=requested_phases,
            max_iterations=request.max_iterations,
            initial_state={
                "run_id": engagement_id,
                "results_dir": str(artifacts.root / engagement_id / "results"),
            },
            artifacts=artifacts,
            evaluator=_build_evaluator(cfg, dry_run=dry_run),
            checkpointer=checkpointer,
        )

        record["state"] = _serialise_state(state)
        record["artifacts_dir"] = str(artifacts.root / engagement_id)
        record["status"] = "completed"
        logger.info(
            "[engagement %s] COMPLETED stages=%s iterations=%s",
            engagement_id,
            (record.get("state") or {}).get("completed_stages"),
            (record.get("state") or {}).get("iteration"),
        )
        _notify_completion(engagement_id, "completed")
    except GraphInterrupt:
        # Graph paused at interrupt() — engagement is waiting for backend
        # results. Thread exits; webhook resume handles continuation.
        record["status"] = "awaiting_results"
        record["awaiting_since"] = now_iso()
        store.save(record)
        logger.info("[engagement %s] PAUSED — awaiting backend results", engagement_id)
        return
    except Exception as exc:  # noqa: BLE001
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        logger.exception("[engagement %s] FAILED", engagement_id)
        _notify_completion(engagement_id, "failed")
    finally:
        if record.get("status") != "awaiting_results":
            record["finished_at"] = now_iso()
        store.save(record)


# ---------------------------------------------------------------------------
# Graph resume (called from webhook via BackgroundTasks)
# ---------------------------------------------------------------------------


def _resume_graph(engagement_id: str, result_payload: dict) -> None:
    """Resume the paused graph in background after a result webhook arrives.

    Design note (Issue #4 fix): runs in a BackgroundTask so the webhook
    returns 202 immediately — the BAS backend caller is not blocked.
    """
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Command

    store: RunStore = _state["store"]
    record = store.get(engagement_id)
    if not record:
        logger.warning("[resume] engagement %s not found; skipping", engagement_id)
        return

    record["status"] = "running"
    record.pop("awaiting_since", None)
    store.save(record)

    try:
        compiled = _get_compiled_graph()
        run_config = {"configurable": {"thread_id": engagement_id}}
        compiled.invoke(Command(resume=result_payload), config=run_config)

        record["status"] = "completed"
        record["finished_at"] = now_iso()
        store.save(record)
        logger.info("[resume] engagement %s completed", engagement_id)
        _notify_completion(engagement_id, "completed")
    except GraphInterrupt:
        # Graph paused again (next phase push) — still awaiting results
        record["status"] = "awaiting_results"
        record["awaiting_since"] = now_iso()
        store.save(record)
        logger.info("[resume] engagement %s paused again — awaiting results", engagement_id)
    except Exception as exc:  # noqa: BLE001
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        record["finished_at"] = now_iso()
        store.save(record)
        logger.exception("[resume] engagement %s failed", engagement_id)
        _notify_completion(engagement_id, "failed")
