"""LangGraph Studio / ``langgraph dev`` entry-point.

This module exposes a **compiled** LangGraph ``CompiledGraph`` as ``graph``
so that ``langgraph dev`` (the LangGraph local server CLI) can discover it
via the ``langgraph.json`` config and serve it for Studio visualisation /
interactive debugging.

Usage (after ``pip install "langgraph-cli[inmem]"``):
    langgraph dev          # reads langgraph.json at the repo root

Then open the Studio URL printed in the terminal output:
    https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024
"""

from __future__ import annotations

import logging
import os

from .agents import (
    LLMEvaluator,
    LLMMasterRouter,
    LLMPlanner,
    StaticAcceptEvaluator,
    StaticMasterRouter,
    StaticPlanner,
)
from ._logging import configure_logging
from .config import AppConfig
from .client import BasClient
from .llm import get_provider
from .orchestrator.graph import build_graph
from .tools import SkillTool

logger = logging.getLogger(__name__)

configure_logging()


def _build_studio_graph():
    """Build the orchestrator graph using the real config when available,
    falling back to safe stubs so Studio can always render the topology."""
    skills_dir = os.environ.get("BAS_SKILLS_DIR", "skills")
    skill_tool = SkillTool(skills_dir).prime()

    try:
        cfg = AppConfig.load()
        dry_run = cfg.bas.dry_run
        provider = get_provider(cfg.llm)
        master = LLMMasterRouter(provider)
        planner = LLMPlanner(provider)
        evaluator = LLMEvaluator(provider)
        bas = BasClient(
            cfg.bas.base_url,
            sleep_ms=cfg.bas.sleep_ms,
            timeout=cfg.bas.timeout,
            dry_run=dry_run,
        )
        logger.info("[studio] built graph with LLM-backed agents (dry_run=%s)", dry_run)
    except Exception:
        # Fallback: static stubs — Studio can still show the graph topology and
        # you can do dry-run invocations.
        logger.warning(
            "[studio] no valid config / LLM key; falling back to static stubs"
        )
        master = StaticMasterRouter()
        planner = StaticPlanner({})
        evaluator = StaticAcceptEvaluator()
        bas = BasClient("http://localhost:0", dry_run=True)

    return build_graph(
        master=master,
        skill_tool=skill_tool,
        planner=planner,
        bas=bas,
        evaluator=evaluator,
    )


# The compiled graph that ``langgraph dev`` / Studio discovers.
graph = _build_studio_graph()
