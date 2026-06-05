"""Tests for the CampaignMemory value object and prompt projection (P12 / P5).

Also includes a cross-phase memory-lifecycle test that runs the exact sequence
of mutations the push and analyse_results nodes perform, so the integrity
guarantees (deep-merge survival, pending lifecycle) are locked in end-to-end
without standing up the whole LangGraph runtime.
"""

from __future__ import annotations

import bas.orchestrator.graph as graph_mod
from bas.orchestrator.graph import _make_analyse_results_node
from bas.orchestrator.memory import (
    DEFAULT_PROMPT_NARRATIVES,
    CampaignMemory,
    project_for_prompt,
)


# ---- merge_facts -----------------------------------------------------------


def test_merge_facts_preserves_nested_siblings():
    mem = CampaignMemory({"network": {"cidr": "10.0.0.0/24", "live_hosts": ["a"]}})
    mem.merge_facts({"network": {"cidr": "10.0.0.0/16"}})
    assert mem.data["network"]["cidr"] == "10.0.0.0/16"
    assert mem.data["network"]["live_hosts"] == ["a"]


def test_merge_facts_unions_lists():
    mem = CampaignMemory({"network": {"live_hosts": ["a", "b"]}})
    mem.merge_facts({"network": {"live_hosts": ["b", "c"]}})
    assert mem.data["network"]["live_hosts"] == ["a", "b", "c"]


def test_merge_facts_none_is_noop():
    mem = CampaignMemory({"host": {"user": "x"}})
    mem.merge_facts(None).merge_facts({})
    assert mem.data == {"host": {"user": "x"}}


# ---- narratives ------------------------------------------------------------


def test_add_narrative_appends_and_skips_empty():
    mem = CampaignMemory()
    mem.add_narrative(phase="discovery", text="found a host", ts="t1")
    mem.add_narrative(phase="discovery", text="", ts="t2")  # skipped
    mem.add_narrative(phase="privesc", text=None, ts="t3")  # skipped
    assert mem.data["narratives"] == [
        {"phase": "discovery", "ts": "t1", "text": "found a host"}
    ]


# ---- pending lifecycle -----------------------------------------------------


def test_pending_add_and_clear():
    mem = CampaignMemory({"host": {"user": "x"}})
    mem.add_pending(phase="discovery", ability="nmap_scan")
    mem.add_pending(phase="discovery", ability="ad_enum")
    assert set(mem.pending_keys()) == {
        "pending.discovery.nmap_scan",
        "pending.discovery.ad_enum",
    }
    mem.clear_pending()
    assert mem.pending_keys() == []
    assert mem.data == {"host": {"user": "x"}}  # real facts untouched


def test_chaining_returns_self():
    mem = CampaignMemory()
    out = mem.merge_facts({"a": 1}).add_narrative(
        phase="p", text="n", ts="t"
    ).add_pending(phase="p", ability="x")
    assert out is mem


# ---- projection (P5) -------------------------------------------------------


def test_project_drops_pending_and_caps_narratives():
    narratives = [{"phase": "p", "ts": str(i), "text": f"n{i}"} for i in range(20)]
    mem = CampaignMemory(
        {
            "network": {"cidr": "10.0.0.0/24"},
            "narratives": narratives,
            "pending.discovery.scan": "awaiting_results",
        }
    )
    view = mem.project_for_prompt()
    # pending stripped
    assert not any(k.startswith("pending.") for k in view)
    # narratives capped to the most-recent N
    assert len(view["narratives"]) == DEFAULT_PROMPT_NARRATIVES
    assert view["narratives"][-1]["text"] == "n19"
    # structured facts preserved verbatim
    assert view["network"] == {"cidr": "10.0.0.0/24"}


def test_project_does_not_mutate_source():
    mem = CampaignMemory(
        {"narratives": [{"text": str(i)} for i in range(20)], "pending.x.y": "z"}
    )
    _ = mem.project_for_prompt()
    # original retains full narratives + pending key (canonical store untouched)
    assert len(mem.data["narratives"]) == 20
    assert "pending.x.y" in mem.data


def test_project_function_passthrough_small_memory():
    small = {"host": {"user": "x"}, "narratives": [{"text": "only one"}]}
    assert project_for_prompt(small) == small


# ---- cross-phase lifecycle (the integrity guarantee) -----------------------


def test_memory_survives_across_phases():
    """Reproduce the push→analyse mutation sequence across two phases.

    Phase 1 (discovery) confirms network facts; phase 2 (privesc) re-touches the
    `network` key with only a partial update. The discovery facts must survive,
    and pending markers must not leak between phases.
    """
    state = {"memory": {}}

    # --- Phase 1: discovery -------------------------------------------------
    # push: master writes intent + speculative pending markers
    mem = CampaignMemory.from_state(state)
    mem.merge_facts({"network": {"cidr": "10.0.0.0/24"}}).add_narrative(
        phase="discovery", text="pushing scan", ts="t1"
    )
    mem.add_pending(phase="discovery", ability="nmap_scan")
    state["memory"] = mem.data
    assert "pending.discovery.nmap_scan" in state["memory"]

    # analyse: clear pending, merge confirmed facts (adds live_hosts sibling)
    mem = CampaignMemory.from_state(state)
    mem.clear_pending().merge_facts(
        {"network": {"live_hosts": ["10.0.0.5", "10.0.0.9"]}}
    ).add_narrative(phase="discovery", text="found 2 hosts", ts="t2")
    state["memory"] = mem.data
    assert state["memory"]["network"] == {
        "cidr": "10.0.0.0/24",
        "live_hosts": ["10.0.0.5", "10.0.0.9"],
    }
    assert not any(k.startswith("pending.") for k in state["memory"])

    # --- Phase 2: privesc, partial network re-touch -------------------------
    mem = CampaignMemory.from_state(state)
    mem.clear_pending().merge_facts(
        {"network": {"gateway": "10.0.0.1"}, "privesc": {"technique": "T1068"}}
    ).add_narrative(phase="privesc", text="found gateway", ts="t3")
    state["memory"] = mem.data

    net = state["memory"]["network"]
    # discovery facts NOT clobbered by the privesc partial update
    assert net["cidr"] == "10.0.0.0/24"
    assert net["live_hosts"] == ["10.0.0.5", "10.0.0.9"]
    assert net["gateway"] == "10.0.0.1"
    assert state["memory"]["privesc"] == {"technique": "T1068"}
    # three narratives accumulated, no pending leakage
    assert [n["text"] for n in state["memory"]["narratives"]] == [
        "pushing scan",
        "found 2 hosts",
        "found gateway",
    ]
    assert not any(k.startswith("pending.") for k in state["memory"])


# ---- analyse_results timeout-resume path (regression) ----------------------


def test_analyse_results_timeout_path_clears_pending(monkeypatch):
    """The timeout-resume branch must clear pending.* and signal retry without
    raising. Regression for the NameError introduced when the inline pending-key
    loop was replaced by CampaignMemory.clear_pending() but a log line still
    referenced the removed `pending_keys` local.
    """
    # interrupt() returns the resume value; simulate a timeout resume.
    monkeypatch.setattr(graph_mod, "interrupt", lambda _: {"timeout": True})

    node = _make_analyse_results_node(master=object())  # master unused on timeout
    state = {
        "current_phase": "discovery",
        "results_dir": None,  # _persist_memory no-ops, no disk I/O
        "memory": {
            "network": {"cidr": "10.0.0.0/24"},
            "pending.discovery.nmap_scan": "awaiting_results",
            "pending.discovery.ad_enum": "awaiting_results",
        },
        "phase_history": [],
        "log": [],
    }

    out = node(state)  # must not raise

    assert out["retry_same_phase"] is True
    assert out["pending_operation_id"] is None
    # pending.* keys cleared, real facts preserved
    assert not any(k.startswith("pending.") for k in out["memory"])
    assert out["memory"]["network"] == {"cidr": "10.0.0.0/24"}
    # the log line that previously raised NameError now reports the count
    assert any("cleared 2 pending keys" in line for line in out["log"])
