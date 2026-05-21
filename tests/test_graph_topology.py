"""Build the orchestrator graph and dump its diagram as a PNG."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from bas.agents import StaticAcceptEvaluator, StaticMasterRouter, StaticPlanner
from bas.orchestrator.graph import build_graph
from bas.tools import SkillTool

REPO = Path(__file__).resolve().parents[1]
ARTIFACT = Path(__file__).resolve().parent / "artifacts" / "workflow_graph.png"


def test_dump_graph_png() -> None:
    skill_tool = SkillTool(str(REPO / "skills")).prime()
    app = build_graph(
        master=StaticMasterRouter(),
        skill_tool=skill_tool,
        planner=StaticPlanner({}),
        bas=MagicMock(),
        artifacts=None,
        evaluator=StaticAcceptEvaluator(),
    )
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_bytes(app.get_graph().draw_mermaid_png())
    assert ARTIFACT.exists() and ARTIFACT.stat().st_size > 0

test_dump_graph_png()