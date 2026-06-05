"""Background worker functions for engagement execution and graph resume.

Single responsibility: run and resume engagements in background threads/tasks.
All shared state access goes through ``bootstrap``.
"""

from __future__ import annotations

import logging
import threading
import traceback
from datetime import datetime, timezone
from typing import Any

from .bootstrap import (
    _bootstrap,
    _get_compiled_graph,
    _state,
)
from .foothold import resolve_foothold
from .persistence import RunStore, now_iso
from .phases import known_phases, resolve_phases_to_skills
from .schemas import EngagementCreateRequest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-engagement lock to prevent concurrent graph invocations on the same
# thread_id (timeout scanner vs. webhook resume, or multiple results arriving
# simultaneously).
# ---------------------------------------------------------------------------

_engagement_locks: dict[str, threading.Lock] = {}
_engagement_locks_guard = threading.Lock()


def _get_engagement_lock(engagement_id: str) -> threading.Lock:
    """Return (or create) a lock specific to this engagement."""
    with _engagement_locks_guard:
        lock = _engagement_locks.get(engagement_id)
        if lock is None:
            lock = threading.Lock()
            _engagement_locks[engagement_id] = lock
        return lock


# ---------------------------------------------------------------------------
# Timeout scanner — two-tier (soft = recoverable, hard = abandon)
#
# Lives here (not in api.py) because it is engagement business logic: it shares
# the per-engagement lock and the compiled graph with the webhook-resume path.
# api.py only wires it into the startup hook.
# ---------------------------------------------------------------------------

# Engagements we've already logged an "overdue" warning for (soft timeout
# passed but still parked). Keyed by (engagement_id, awaiting_since) so a fresh
# wait after a resume warns again. Cleaned up on hard-abandon.
_overdue_warned: set[tuple[str, str]] = set()


def _start_timeout_scanner(soft_timeout: int, hard_timeout: int) -> None:
    """Periodically handle engagements stuck in awaiting_results.

    Two tiers:
      * soft_timeout  — past this the engagement is 'overdue'; we keep it parked
        so a late result still resumes it (work is never wasted).
      * hard_timeout  — only past this do we force the timeout-resume (advance/
        retry), the backstop for genuinely dead agents. 0 disables it.
    """
    import time

    def _scan() -> None:
        while True:
            time.sleep(60)  # check every minute
            try:
                _expire_stale_engagements(soft_timeout, hard_timeout)
            except Exception:  # noqa: BLE001
                logger.exception("[timeout-scanner] error during scan")

    t = threading.Thread(target=_scan, daemon=True, name="timeout-scanner")
    t.start()
    logger.info(
        "[boot] timeout scanner started (soft=%ds, hard=%s)",
        soft_timeout,
        f"{hard_timeout}s" if hard_timeout else "disabled",
    )


def _expire_stale_engagements(soft_timeout: int, hard_timeout: int) -> None:
    """Force-resume only engagements past the HARD cap; keep overdue ones parked.

    Between the soft and hard timeouts the engagement stays in awaiting_results
    with its checkpoint and pending_operation_id intact, so a late result POSTed
    to /results resumes it normally instead of being discarded.
    """
    from langgraph.types import Command

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

        engagement_id = record["run_id"]

        # Within the recoverable window (under hard cap, or hard cap disabled):
        # leave it parked so a late result still resumes it. Warn once if overdue.
        if not hard_timeout or elapsed <= hard_timeout:
            if elapsed > soft_timeout:
                warn_key = (engagement_id, str(awaiting_since))
                if warn_key not in _overdue_warned:
                    _overdue_warned.add(warn_key)
                    logger.warning(
                        "[timeout-scanner] engagement %s overdue (%ds > soft %ds) "
                        "— still parked, will honour a late result%s",
                        engagement_id,
                        int(elapsed),
                        soft_timeout,
                        f" until hard cap {hard_timeout}s" if hard_timeout else "",
                    )
            continue

        # Past the hard cap: force-abandon — dead-agent backstop.
        _overdue_warned.discard((engagement_id, str(awaiting_since)))
        logger.warning(
            "[timeout-scanner] hard-expiring engagement %s after %ds (hard cap %ds)",
            engagement_id,
            int(elapsed),
            hard_timeout,
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


def _release_engagement_lock(engagement_id: str) -> None:
    """Remove the lock entry when an engagement finishes (cleanup)."""
    with _engagement_locks_guard:
        _engagement_locks.pop(engagement_id, None)


# ---------------------------------------------------------------------------
# Completion notification — tells the BAS backend all phases are done
# ---------------------------------------------------------------------------


def _notify_completion(engagement_id: str, status: str) -> None:
    """Notify the backend that the engagement is finished.

    Uses FeedbackApi.finalize() — the proper abstraction for terminal
    notifications. Best-effort: failures are logged but don't crash the worker.
    """
    try:
        bas = _state.get("bas")
        if bas is None:
            return
        bas.feedback.finalize(engagement_id, status)
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
        "master_done": state.get("master_done"),
        "master_briefing": state.get("master_briefing"),
        "master_revisions_used": state.get("master_revisions_used"),
        "planner_attempts": state.get("planner_attempts"),
        "planner_tool_calls": state.get("planner_tool_calls"),
        "proposal_log": list(state.get("proposal_log") or []),
        "phase_history": list(state.get("phase_history") or []),
        "completed_stages": state.get("completed_stages", []),
        "stage_results": [
            sr.model_dump(mode="json") if hasattr(sr, "model_dump") else sr
            for sr in state.get("stage_results", []) or []
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
    lock = _get_engagement_lock(engagement_id)
    lock.acquire()
    try:
        _run_engagement_inner(engagement_id)
    finally:
        lock.release()
        _release_engagement_lock(engagement_id)


def _run_engagement_inner(engagement_id: str) -> None:
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
        requested_phases = [p.value if hasattr(p, 'value') else str(p) for p in (request.phases or [])]
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

        # 3. Run the graph using the process-global singleton (shared
        #    checkpointer ensures resume uses the same state).
        compiled = _get_compiled_graph()

        seed = {
            "foothold": foothold,
            "available_phases": requested_phases,
            "max_iterations": request.max_iterations,
            "max_master_revisions": 1,
            "max_planner_attempts": 3,
            "max_planner_tool_calls": 20,
            "run_id": engagement_id,
            "results_dir": str(artifacts.root / engagement_id / "results"),
        }
        run_config = {"configurable": {"thread_id": engagement_id}}

        from .orchestrator.graph import _stream_graph
        state = _stream_graph(compiled, seed, run_config)

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

    Uses a per-engagement lock to prevent concurrent resumes (e.g. multiple
    results arriving simultaneously, or timeout scanner racing with a webhook).
    """
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Command

    lock = _get_engagement_lock(engagement_id)
    if not lock.acquire(timeout=120):
        logger.warning(
            "[resume] engagement %s lock timeout — another resume in progress; skipping",
            engagement_id,
        )
        return

    try:
        store: RunStore = _state["store"]
        record = store.get(engagement_id)
        if not record:
            logger.warning("[resume] engagement %s not found; skipping", engagement_id)
            return

        # Re-check status under the lock — may have been completed/failed
        # by the timeout scanner or a prior resume while we waited.
        if record.get("status") not in ("awaiting_results", "running"):
            logger.info(
                "[resume] engagement %s status is %r — skipping resume",
                engagement_id,
                record.get("status"),
            )
            return

        record["status"] = "running"
        record.pop("awaiting_since", None)
        store.save(record)

        compiled = _get_compiled_graph()
        run_config = {"configurable": {"thread_id": engagement_id}}

        from .orchestrator.graph import _stream_graph
        state = _stream_graph(
            compiled, Command(resume=result_payload), run_config
        )

        record["state"] = _serialise_state(state)
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
    finally:
        lock.release()
