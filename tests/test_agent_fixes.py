"""Regression tests for the agent resilience / integrity fixes.

Covers:
  * P6 — deep-merge of memory facts preserves sibling nested sub-keys.
  * P1 — retryable-vs-fatal classification of provider exceptions.

(The former P14 positional-feedback-matching tests were removed when the
/ai/operation-feedback retry path was retired in favour of re-pushing a
corrected operation — see test_operations_client.py / the poller.)
"""

from __future__ import annotations

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
