"""
simulate_all_phases.py
======================
Simulate the entire 7-phase autonomous campaign locally.
Uses live Gemini LLM reasoning, validates all commands locally via native shell syntax checkers,
and feeds back high-fidelity Active Directory and environment mocks to campaign memory.
"""

from __future__ import annotations

import sys
import os
import uuid
import logging
import json
from pathlib import Path

# Add src/ folder to Python import search path
REPO = Path.cwd()
if (REPO / "src").is_dir():
    sys.path.insert(0, str(REPO / "src"))

from bas.config import AppConfig
from bas.tools import SkillTool
from bas.client import BasClient
from bas.bootstrap import _bootstrap, _get_compiled_graph
from bas.tools.command_validator import validate_plan, format_errors
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("simulation_test")

# 1. High-Fidelity Mock Outputs for Campaign Memory Extraction
MOCK_DOMESTICS = {
    # Discovery Phase
    "wmic computersystem get domain": "Domain\nCASTELBLACK.local\n",
    "wmic computersystem get domainjoined": "DomainJoined\nTRUE\n",
    "nltest /dclist:": "Get list of DCs from '\\\\CASTELBLACK.local':\n   CASTELBLACK-DC.CASTELBLACK.local [DS] Site: Default-First-Site-Name\nThe command completed successfully\n",
    "whoami": "castelblack\\domain_admin",
    "hostname": "CASTELBLACK-WORKSTATION-01",
    "net user /domain": "The user accounts for \\\\CASTELBLACK-DC are:\n\nAdministrator            guest                    domain_user\nThe command completed successfully.",
    "ipconfig /all": "Windows IP Configuration\n   Host Name . . . . . . . . . . . . : CASTELBLACK-WORKSTATION-01\n   Primary Dns Suffix  . . . . . . . : CASTELBLACK.local\n   Ethernet adapter Ethernet0:\n   IPv4 Address. . . . . . . . . . . : 192.168.187.141\n   Subnet Mask . . . . . . . . . . . : 255.255.255.0\n   Default Gateway . . . . . . . . . : 192.168.187.2\n   DNS Servers . . . . . . . . . . . : 192.168.187.100\n",
    # Privilege Escalation Phase
    "whoami /priv": "PRIVILEGES INFORMATION\n----------------------\nSeDebugPrivilege             Enabled\nSeImpersonatePrivilege       Enabled\n",
}

def get_high_fidelity_stdout(command_template: str) -> str:
    """Check if we have a realistic response for a known discovery/enumeration command."""
    for keyword, response in MOCK_DOMESTICS.items():
        if keyword in command_template.lower():
            return response
    return f"Success mock output for command: {command_template}"


def main():
    print("\n" + "="*80)
    print("      🚀 STARTING AUTONOMOUS CAMPAIGN LOCAL SIMULATION (7 PHASES) 🚀")
    print("="*80 + "\n")

    # 2. Boot up configuration and assets
    cfg, skill_tool, store, artifacts = _bootstrap()
    cfg.bas.dry_run = True  # force dry run so we don't trigger real network breaches

    # Instantiate BasClient in dry run mode
    bas = BasClient(
        cfg.bas.base_url,
        sleep_ms=0,
        timeout=cfg.bas.timeout,
        dry_run=True,
    )

    # 3. Define Foothold Target (Windows with PowerShell)
    foothold = {
        "environment_id": "00000000-0000-0000-0000-000000000001",
        "environment_name": "Dry-Run-Local-Test-Lab",
        "agent_id": "local-dry-run-agent",
        "hostname": "CASTELBLACK-WORKSTATION-01",
        "platform": "windows",
        "ip_address": "192.168.187.141",
        "display_status": "online",
    }

    # All 7 Attack Chain phases to evaluate sequentially
    all_phases = [
        "discovery",
        "privesc",
        "credaccess",
        "lateral",
        "persistence",
        "defevasion",
        "impact",
    ]

    run_id = uuid.uuid4().hex
    seed = {
        "foothold": foothold,
        "available_phases": all_phases,
        "max_iterations": 25,
        "max_master_revisions": 1,
        "max_planner_attempts": 3,
        "max_planner_tool_calls": 20,
        "run_id": run_id,
        "results_dir": str(artifacts.root / run_id / "results"),
    }
    run_config = {"configurable": {"thread_id": run_id}}
    compiled = _get_compiled_graph()

    # Create dummy parent directory to host artifacts
    (artifacts.root / run_id / "results").mkdir(parents=True, exist_ok=True)

    current_seed = seed
    total_abilities_evaluated = 0
    phase_syntax_status = {}

    # 4. Campaign Orchestration Loop
    while True:
        try:
            from bas.orchestrator.graph import _stream_graph
            state = _stream_graph(compiled, current_seed, run_config)
            print("\n" + "="*80)
            print("🎉 AUTONOMOUS CAMPAIGN FULLY COMPLETED! 🎉")
            print("="*80 + "\n")
            break
        except GraphInterrupt:
            # We reached the interrupt gate at analyse_results_node
            state = compiled.get_state(run_config).values
            phase = state.get("current_phase")
            skill_name = state.get("next_stage")
            raw_plan = state.get("current_plan")

            print(f"\n⚡ [SIMULATION INTERCEPT] Phase: {phase.upper()} | Skill: {skill_name}")

            if raw_plan:
                from bas.agents.specialist import SpecialistPlan
                plan = SpecialistPlan.model_validate(raw_plan)
                print(f"👉 Adversary: {plan.adversary.name}")

                # Command Syntax Validation (PowerShell / CMD / Bash)
                validations = validate_plan(plan)
                syntax_errors = [v for v in validations if not v.valid]
                warnings = [v for v in validations if v.warnings]

                if syntax_errors:
                    print(f"❌ SYNTAX ERRORS ENCOUNTERED:\n{format_errors(syntax_errors)}")
                    phase_syntax_status[phase] = "Failed"
                else:
                    print("✅ Local shell syntax validations: PASSED")
                    if phase not in phase_syntax_status or phase_syntax_status[phase] != "Failed":
                        phase_syntax_status[phase] = "Passed"

                if warnings:
                    print(f"⚠️ Validation warnings:\n{format_errors(warnings)}")

                # Print proposed abilities & command details
                for ab in plan.abilities:
                    total_abilities_evaluated += 1
                    print(f"  • Ability: {ab.ability.name} ({ab.ability.mitre_tactic or 'Unknown Tactic'})")
                    for st in ab.stages:
                        print(f"    [{st.executor}] Command: {st.command_template}")

                # Build Simulated Result payload to Resume the Orchestrator Graph
                asset_map = state.get("phase_asset_map", {}).get(phase, {})
                stage_id_map = asset_map.get("stage_id_map", {})
                ability_name_to_id = asset_map.get("ability_name_to_id", {})

                abilities_payload = []
                execution_logs = []

                for ab_name, stg_map in stage_id_map.items():
                    ab_id = ability_name_to_id.get(ab_name)
                    # Locate planned template command for high-fidelity responses
                    matching_plan_ab = next((a for a in plan.abilities if a.ability.name == ab_name), None)
                    stages_payload = []
                    for stg_name, stg_id in stg_map.items():
                        cmd_template = ""
                        executor = "psh"
                        if matching_plan_ab:
                            matching_stg = next((s for s in matching_plan_ab.stages if s.stage_name == stg_name), None)
                            if matching_stg:
                                cmd_template = matching_stg.command_template or ""
                                executor = matching_stg.executor

                        stages_payload.append({
                            "stage_id": stg_id,
                            "stage_name": stg_name,
                            "executor": executor,
                            "command_template": cmd_template,
                        })

                        # Fetch realistic simulated outputs so subsequent phases are grounded
                        simulated_stdout = get_high_fidelity_stdout(cmd_template)

                        execution_logs.append({
                            "ability_id": ab_id,
                            "stage_id": stg_id,
                            "command_executed": cmd_template,
                            "exit_code": 0,
                            "stdout": simulated_stdout,
                            "stderr": "",
                        })

                    abilities_payload.append({
                        "ability_id": ab_id,
                        "name": ab_name,
                        "stages": stages_payload,
                    })

                result_payload = {
                    "operation": {
                        "operation_id": f"dry_run_op_{phase}",
                        "name": f"dry_run_operation_for_{phase}",
                        "status": "completed",
                    },
                    "abilities": abilities_payload,
                    "execution_logs": execution_logs,
                }

                # Instruct the checkpointer to resume the thread with the mock result payload
                current_seed = Command(resume=result_payload)
            else:
                # No plan was generated (skipped / error)
                print("⚠️ No plan was generated in this step.")
                current_seed = Command(resume={"operation": {"status": "completed"}})

    # 5. Compile Executive Evaluation Summary
    print("\n" + "="*80)
    print("      📊 SIMULATION TESTING REPORT 📊")
    print("="*80)
    print(f"Total Abilities Generated & Tested: {total_abilities_evaluated}")
    print("\nPhase Syntax Validation Breakdown:")
    for ph, status in phase_syntax_status.items():
        icon = "✅" if status == "Passed" else "❌"
        print(f"  {icon} {ph:<15}: {status}")
    print("="*80 + "\n")


if __name__ == "__main__":
    main()
