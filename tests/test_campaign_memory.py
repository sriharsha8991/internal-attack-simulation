"""Tests for the CampaignMemory value object and prompt projection (P12 / P5).

Also includes a cross-phase memory-lifecycle test that runs the exact sequence
of mutations the push and analyse_results nodes perform, so the integrity
guarantees (deep-merge survival, pending lifecycle) are locked in end-to-end
without standing up the whole LangGraph runtime.
"""

from __future__ import annotations

import json

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


def test_analyse_results_persists_execution_outcome_after_result(monkeypatch):
    persisted: list[tuple[dict, dict, str]] = []

    monkeypatch.setattr(
        graph_mod,
        "interrupt",
        lambda _: {
            "operation_id": "op-partial-success",
            "name": "credaccess-op",
            "status": "completed",
            "execution_logs": [
                {
                    "ability_id": "ab-ticket-cache",
                    "stage_id": "st-ticket-cache",
                    "command_executed": "klist",
                    "stdout": "Cached Tickets: (5)",
                    "stderr": "",
                    "exit_code": 0,
                    "executor": "powershell",
                },
                {
                    "ability_id": "ab-kerberoast",
                    "stage_id": "st-kerberoast",
                    "command_executed": 'Write-Output "Failed for $spn: $_"',
                    "stdout": "",
                    "stderr": (
                        "Variable reference is not valid. ':' was not followed by "
                        "a valid variable name character.\n"
                        "FullyQualifiedErrorId : InvalidVariableReferenceWithDrive"
                    ),
                    "exit_code": 1,
                    "executor": "powershell",
                },
            ],
        },
    )
    monkeypatch.setattr(
        graph_mod,
        "persist_memory",
        lambda state, memory, label: persisted.append((state, memory, label)),
    )

    class Master:
        def analyse_results(self, **_kwargs):
            return graph_mod.MemoryUpdate(facts={}, narrative="analysed")

    node = _make_analyse_results_node(master=Master())
    state = {
        "current_phase": "credaccess",
        "results_dir": "engagements/test/results",
        "memory": {},
        "completed_phases": ["discovery"],
        "phase_history": [
            {
                "phase": "credaccess",
                "objective": "collect credentials",
                "outcome": "committed",
            }
        ],
        "log": [],
    }

    out = node(state)

    assert out["retry_same_phase"] is True
    assert out["completed_phases"] == ["discovery"]
    execution_outcome = out["phase_history"][-1]["execution_outcome"]
    assert execution_outcome["operation_id"] == "op-partial-success"
    assert execution_outcome["abilities_passed"] == 1
    assert execution_outcome["abilities_failed"] == 1
    assert execution_outcome["issues_detected"] == ["psh_parse_error"]
    assert execution_outcome["retry_feedback"][0]["kind"] == "psh_parse_error"
    assert out["retry_feedback"][0]["command"] == 'Write-Output "Failed for $spn: $_"'
    assert {
        "operation_id": "op-partial-success",
        "abilities_passed": 1,
        "abilities_failed": 1,
        "issues_detected": ["psh_parse_error"],
    }.items() <= execution_outcome.items()
    assert persisted
    persisted_state = persisted[-1][0]
    assert persisted_state["phase_history"][-1]["execution_outcome"] == out["phase_history"][-1]["execution_outcome"]
    assert persisted_state["completed_phases"] == ["discovery"]


def test_analyse_results_exit_zero_error_marker_retries(monkeypatch):
    monkeypatch.setattr(
        graph_mod,
        "interrupt",
        lambda _: {
            "operation_id": "op-error-marker",
            "name": "ad-enum-op",
            "status": "completed",
            "execution_logs": [
                {
                    "ability_id": "ab-ad-enum",
                    "stage_id": "st-ad-enum",
                    "command_executed": "powershell ad enum",
                    "stdout": "Users failed: Unable to find type [DirectorySearcher].\n",
                    "stderr": "",
                    "exit_code": 0,
                    "executor": "powershell",
                },
            ],
        },
    )

    class Master:
        def analyse_results(self, **_kwargs):
            return graph_mod.MemoryUpdate(facts={}, narrative="analysed")

    node = _make_analyse_results_node(master=Master())
    out = node(
        {
            "current_phase": "ad-enumeration",
            "results_dir": None,
            "memory": {},
            "completed_phases": ["discovery"],
            "phase_history": [{"phase": "ad-enumeration", "outcome": "committed"}],
            "log": [],
        }
    )

    assert out["retry_same_phase"] is True
    assert out["completed_phases"] == ["discovery"]
    assert "ad-enumeration" not in out["completed_phases"]
    kinds = {item["kind"] for item in out["retry_feedback"]}
    assert {"error_marker", "type_not_found"} <= kinds


def test_push_memory_is_pending_only(monkeypatch):
    persisted: list[tuple[dict, dict, str]] = []

    def fake_push_specialist(*_args, **_kwargs):
        return graph_mod.PushResult(
            skill="accessing-credentials",
            success=True,
            adversary_id="adv-1",
            ability_ids=["ab-1"],
            stage_ids=["st-1"],
            linked_ability_ids=["ab-1"],
            plan_summary=[
                {
                    "name": "Harvest Local Kerberos Tickets",
                    "mitre_technique_id": "T1558.004",
                    "stages": [
                        {"command_template": "klist"},
                    ],
                }
            ],
        )

    class Master:
        def update_memory(self, **_kwargs):
            raise AssertionError("push must not call master.update_memory")

    class SkillTool:
        def has(self, _name):
            return False

    monkeypatch.setattr(graph_mod, "push_specialist", fake_push_specialist)
    monkeypatch.setattr(
        graph_mod,
        "persist_memory",
        lambda state, memory, label: persisted.append((state, memory, label)),
    )

    node = graph_mod._make_push_node(
        Master(), SkillTool(), bas=object(), artifacts=None, planner=None
    )
    out = node(
        {
            "run_id": "run-1",
            "current_phase": "credaccess",
            "next_stage": "accessing-credentials",
            "phase_skills": ["accessing-credentials"],
            "phase_skill_index": 0,
            "memory": {"host": {"user": "north\\test"}},
            "master_briefing": {"phase": "credaccess", "objective": "collect creds"},
            "current_plan": {
                "adversary": {"name": "adv"},
                "abilities": [
                    {
                        "ability": {"name": "placeholder", "platform": "windows"},
                        "stages": [
                            {"stage_name": "stage", "stage_order": 1, "executor": "powershell", "command_template": "klist"}
                        ],
                        "rationale": "test",
                        "grounding_depth": "skip",
                        "provider": "test",
                    }
                ],
            },
            "log": [],
        }
    )

    assert out["memory"]["host"] == {"user": "north\\test"}
    assert "narratives" not in out["memory"]
    assert out["memory"]["pending.credaccess.Harvest Local Kerberos Tickets"] == "awaiting_results"
    assert out["completed_stages"] == []
    assert persisted[-1][2] == "push/credaccess/pending"


def test_master_analysis_includes_selected_stage_outputs(monkeypatch):
    import bas.agents.master as master_mod

    captured_payloads: list[dict] = []

    class FakeResponse:
        def __init__(self, text):
            self._text = text

        def strip(self):
            return self._text.strip()

    class FakeLlm:
        def chat(self, *_args, **_kwargs):
            return FakeResponse("Harvest Local Kerberos Tickets")

        def generate_structured(self, msgs, _schema, **_kwargs):
            payload_text = msgs[-1].content.split("\n\n", 1)[1]
            captured_payloads.append(json.loads(payload_text))
            return master_mod.MemoryUpdate(facts={}, narrative="ok")

    monkeypatch.setattr(
        "bas.tools.master_tools.read_stage_output",
        lambda *_args, **_kwargs: "STDOUT:\nCached Tickets: (5)",
    )

    master = master_mod.LLMMasterRouter(FakeLlm())
    update = master.analyse_results(
        results_dir="engagements/test/results",
        operation_id="op-1",
        structural_summary="[PASS] Harvest Local Kerberos Tickets",
        current_memory={},
    )

    assert update.narrative == "ok"
    assert captured_payloads
    assert captured_payloads[-1]["selected_ability_outputs"] == {
        "Harvest Local Kerberos Tickets": "STDOUT:\nCached Tickets: (5)"
    }


def test_init_preserves_kali_sidecar_context():
    out = graph_mod._init_node(
        {
            "run_id": "run-1",
            "available_phases": ["discovery"],
            "foothold": {"platform": "windows"},
            "kali_sidecar": {"enabled": True, "healthy": True},
        }
    )

    assert out["kali_sidecar"] == {"enabled": True, "healthy": True}
