"""Process-global resources and lazy bootstrap.

Single responsibility: initialise and hold all shared singletons (config,
stores, BasClient, compiled graph). Every other module that needs these
imports from here — never the other way around.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

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
from .llm import get_provider
from ._logging import configure_logging
from .persistence import ArtifactStore, ResultStore, RunStore
from .tools import SkillTool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Process-global state dict (lazy — populated by _bootstrap)
# ---------------------------------------------------------------------------

_state: dict[str, Any] = {
    "cfg": None,
    "skills": None,
    "store": None,
    "artifacts": None,
    "results_store": None,
    "bas": None,             # process-global BasClient (Issue #3 fix)
    "compiled_graph": None,  # singleton compiled graph with checkpointer
    "llm_provider": None,    # shared LLM provider instance
}
_state_lock = threading.Lock()


def _bootstrap() -> tuple[AppConfig, SkillTool, RunStore, ArtifactStore]:
    """Lazily initialise all shared resources. Thread-safe."""
    with _state_lock:
        if _state["cfg"] is None:
            configure_logging()
            cfg = AppConfig.load()
            runs_dir = (
                os.environ.get("BAS_ENGAGEMENTS_DIR")
                or os.environ.get("BAS_RUNS_DIR")
                or cfg.run.output_dir
            )

            # Build all resources before committing to _state so a partial
            # failure leaves _state["cfg"] == None, allowing a retry on the
            # next call instead of silently returning half-initialised state.
            skills = SkillTool("skills").prime()
            store = RunStore(runs_dir)
            artifacts = ArtifactStore(runs_dir)
            results_store = ResultStore(runs_dir)
            bas = BasClient(
                cfg.bas.base_url,
                sleep_ms=cfg.bas.sleep_ms,
                timeout=cfg.bas.timeout,
                dry_run=cfg.bas.dry_run,
            )

            # Commit atomically — all or nothing.
            _state["skills"] = skills
            _state["store"] = store
            _state["artifacts"] = artifacts
            _state["results_store"] = results_store
            _state["bas"] = bas
            _state["cfg"] = cfg  # set LAST — this is the guard variable

            logger.info(
                "[boot] base_url=%s dry_run=%s engagements_dir=%s skills=%d",
                cfg.bas.base_url,
                cfg.bas.dry_run,
                runs_dir,
                len(skills.list_summaries()),
            )
        return (
            _state["cfg"],
            _state["skills"],
            _state["store"],
            _state["artifacts"],
        )


# ---------------------------------------------------------------------------
# Agent / component builders
# ---------------------------------------------------------------------------


def _get_provider(cfg: AppConfig):
    """Return the shared LLM provider instance (cached in _state)."""
    if _state.get("llm_provider") is None:
        _state["llm_provider"] = get_provider(cfg.llm)
    return _state["llm_provider"]


def _build_master(cfg: AppConfig, *, dry_run: bool) -> MasterPolicy:
    """Master router (campaign director). Falls back to StaticMasterRouter when
    no LLM key is configured (dry-run only)."""
    if dry_run:
        try:
            return LLMMasterRouter(_get_provider(cfg))
        except Exception:
            return StaticMasterRouter()
    return LLMMasterRouter(_get_provider(cfg))


def _build_evaluator(cfg: AppConfig, *, dry_run: bool):
    if dry_run:
        try:
            return LLMEvaluator(_get_provider(cfg))
        except Exception:
            return StaticAcceptEvaluator()
    return LLMEvaluator(_get_provider(cfg))


def _build_planner(cfg: AppConfig, *, dry_run: bool) -> Planner:
    if dry_run:
        try:
            return LLMPlanner(_get_provider(cfg))
        except Exception:
            from .worker import _DryRunStubPlanner
            return _DryRunStubPlanner()
    return LLMPlanner(_get_provider(cfg))


def _build_checkpointer(cfg: AppConfig) -> Any:
    """Build a LangGraph checkpointer from config.

    For sqlite we open the connection directly (rather than
    ``SqliteSaver.from_conn_string``, which is a context manager that would
    close the DB on ``__exit__``) so the saver stays usable for the whole
    process lifetime — resumes happen on a later request, long after build.
    """
    if cfg.execution.checkpointer == "sqlite":
        try:
            import sqlite3

            from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore[import-not-found]
        except ImportError:
            # Degrade rather than crash at boot: an in-memory saver still works,
            # it just won't survive a restart. Make the trade-off loud.
            logger.error(
                "[boot] checkpointer=sqlite but 'langgraph-checkpoint-sqlite' is "
                "not installed; falling back to in-memory checkpointer — paused "
                "engagements will NOT survive a restart. Install it with: "
                "uv add langgraph-checkpoint-sqlite"
            )
        else:
            db_path = Path(cfg.execution.checkpoint_db)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False: the worker may resume an engagement from a
            # different thread than the one that created the checkpoint.
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            return SqliteSaver(conn, serde=_build_serde())
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()


def _build_serde() -> Any:
    """Checkpoint serializer with our custom state types registered.

    LangGraph's msgpack serde warns on (and will soon BLOCK) deserialization of
    types it doesn't recognise. ``StageResult`` is the only custom pydantic model
    stored as an instance in SessionState (everything else is ``model_dump()``-ed
    to plain dicts), but we register ``PhaseRecord`` too as a defensive measure
    against any future code path that forgets to dump it. Registering an explicit
    allowlist also opts us into the future strict behaviour safely.
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from .orchestrator.state import PhaseRecord, StageResult

    return JsonPlusSerializer(allowed_msgpack_modules=[StageResult, PhaseRecord])


def _get_compiled_graph():
    """Return (or lazily build) the process-global compiled graph.

    Must be called AFTER ``_bootstrap()`` has populated ``_state``.
    """
    with _state_lock:
        if _state["compiled_graph"] is None:
            from .orchestrator.graph import build_graph

            cfg: AppConfig = _state["cfg"]
            dry_run = cfg.bas.dry_run
            _state["compiled_graph"] = build_graph(
                master=_build_master(cfg, dry_run=dry_run),
                skill_tool=_state["skills"],
                planner=_build_planner(cfg, dry_run=dry_run),
                bas=_state["bas"],
                artifacts=_state["artifacts"],
                evaluator=_build_evaluator(cfg, dry_run=dry_run),
                checkpointer=_build_checkpointer(cfg),
            )
        return _state["compiled_graph"]
