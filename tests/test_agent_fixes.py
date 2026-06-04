"""Regression tests for the agent resilience / integrity fixes.

Covers:
  * P6  — deep-merge of memory facts preserves sibling nested sub-keys.
  * P1  — retryable-vs-fatal classification of provider exceptions.
  * P14 — positional feedback matching is skipped when plan shapes diverge,
          so a corrected command is never attached to the wrong stage.
"""

from __future__ import annotations

from types import SimpleNamespace

from bas.agents.master import LLMMasterRouter
from bas.llm.base import GroundingBudgetExceeded
from bas.llm.gemini import _is_retryable
from bas.orchestrator.graph import _deep_merge, _merge_lists


# ---- P6: deep merge --------------------------------------------------------


def test_deep_merge_preserves_sibling_subkeys():
    base = {
        "network": {"cidr": "10.0.0.0/24", "live_hosts": ["a", "b"]},
        "host": {"user": "x"},
    }
    # A later phase re-emits only network.cidr.
    merged = _deep_merge(base, {"network": {"cidr": "10.0.0.0/16"}})
    assert merged["network"]["cidr"] == "10.0.0.0/16"
    assert merged["network"]["live_hosts"] == ["a", "b"]  # NOT clobbered
    assert merged["host"] == {"user": "x"}


def test_deep_merge_unions_lists_and_overwrites_scalars():
    assert _merge_lists(["a", "b"], ["b", "c"]) == ["a", "b", "c"]
    # Unhashable (dict) items still de-dup by equality.
    assert _merge_lists([{"ip": "1"}], [{"ip": "1"}, {"ip": "2"}]) == [
        {"ip": "1"},
        {"ip": "2"},
    ]
    # Type mismatch: delta wins.
    assert _deep_merge({"k": [1]}, {"k": "scalar"}) == {"k": "scalar"}
    assert _deep_merge({}, {"a": 1}) == {"a": 1}


# ---- P1: retry classification ---------------------------------------------


def test_is_retryable_transient_vs_fatal():
    assert _is_retryable(Exception("503 Service Unavailable"))
    assert _is_retryable(Exception("429 RESOURCE_EXHAUSTED rate limit"))
    assert _is_retryable(Exception("deadline exceeded / timed out"))
    # Deterministic failures must NOT be retried.
    assert not _is_retryable(Exception("400 invalid argument"))
    assert not _is_retryable(GroundingBudgetExceeded("budget"))


# ---- P14: positional feedback shape guard ---------------------------------


def _issue(ability_name, stage_name):
    return SimpleNamespace(
        ability_name=ability_name,
        stage_name=stage_name,
        ability_id="orig-ab",
        stage_id="orig-sid",
        kind=SimpleNamespace(value="blocked"),
        detail="command was blocked",
    )


def _plan(*abilities):
    """abilities = list of (ability_name, [(stage_name, command), ...])."""
    return SimpleNamespace(
        abilities=[
            SimpleNamespace(
                ability=SimpleNamespace(name=name),
                stages=[
                    SimpleNamespace(stage_name=sn, command_template=cmd)
                    for sn, cmd in stages
                ],
            )
            for name, stages in abilities
        ]
    )


def test_positional_match_skipped_when_shapes_diverge():
    master = LLMMasterRouter(llm=object())  # method doesn't touch the LLM
    asset_map = {
        "stage_id_map": {"recon": {"scan": "sid-1"}},
        "ability_name_to_id": {"recon": "ab-1"},
    }
    # New plan has 2 abilities with different names — shape diverges from the
    # single-ability original, so positional matching must be skipped.
    new_plan = _plan(
        ("recon_rewritten", [("scan_a", "nmap -sV")]),
        ("extra_ability", [("scan_b", "rustscan")]),
    )
    changes = master.build_feedback_payload(
        issues=[_issue("recon", "scan")],
        current_plan=new_plan,
        asset_map=asset_map,
        operation_id="op-1",
    )
    # No exact match (names differ) and positional disabled → nothing applied.
    assert changes == []


def test_positional_match_applies_when_shapes_align():
    master = LLMMasterRouter(llm=object())
    asset_map = {
        "stage_id_map": {"recon": {"scan": "sid-1"}},
        "ability_name_to_id": {"recon": "ab-1"},
    }
    # Same shape (1 ability, 1 stage) but renamed — positional should map it.
    new_plan = _plan(("recon_v2", [("scan_fixed", "nmap -sV -Pn")]))
    changes = master.build_feedback_payload(
        issues=[_issue("recon", "scan")],
        current_plan=new_plan,
        asset_map=asset_map,
        operation_id="op-1",
    )
    assert len(changes) == 1
    assert changes[0].suggested_command_template == "nmap -sV -Pn"
    assert changes[0].ability_id == "ab-1"
    assert changes[0].stage_id == "sid-1"
