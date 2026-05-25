"""Process-global resources and lazy bootstrap.

Single responsibility: initialise and hold all shared singletons (config,
stores, BasClient, compiled graph). Every other module that needs these
imports from here — never the other way around.
"""

from __future__ import annotations

import logging
import os
import threading
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
            _state["cfg"] = cfg
            _state["skills"] = SkillTool("skills").prime()
            _state["store"] = RunStore(runs_dir)
            _state["artifacts"] = ArtifactStore(runs_dir)
            _state["results_store"] = ResultStore(runs_dir)

            # Process-global BasClient (Issue #3 fix) — never closed until
            # process shutdown so graph resume via webhook uses a live client.
            _state["bas"] = BasClient(
                cfg.bas.base_url,
                sleep_ms=cfg.bas.sleep_ms,
                timeout=cfg.bas.timeout,
                dry_run=cfg.bas.dry_run,
            )

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
# Agent / component builders
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
            from .worker import _DryRunStubPlanner
            return _DryRunStubPlanner()
    return LLMPlanner(get_provider(cfg.llm))


def _build_checkpointer(cfg: AppConfig) -> Any:
    """Build a LangGraph checkpointer from config."""
    if cfg.execution.checkpointer == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore[import-not-found]
        except ImportError:
            raise ImportError(
                "checkpointer=sqlite requires 'langgraph-checkpoint-sqlite'; "
                "install it with: uv add langgraph-checkpoint-sqlite"
            ) from None
        return SqliteSaver.from_conn_string(cfg.execution.checkpoint_db)
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()


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
