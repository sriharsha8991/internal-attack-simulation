"""Central orchestrator \u2014 master-router LangGraph state machine.

Public surface:

    from bas.orchestrator import (
        SessionState, StageResult, DONE_SENTINEL,
        build_graph,
    )

The graph is driven by a ``MasterPolicy`` (campaign director) defined in
``bas.agents.master``. It picks phases, reviews every approved plan before
commit, and updates session memory after each phase.
"""

from .graph import build_graph
from .state import DONE_SENTINEL, PhaseRecord, SessionState, StageResult

__all__ = [
    "SessionState",
    "StageResult",
    "PhaseRecord",
    "DONE_SENTINEL",
    "build_graph",
]
