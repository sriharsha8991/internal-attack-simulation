---
name: discovering-environment
description: Builds a complete inventory of the victim environment from any foothold — host fingerprint, network sweep, service / banner enumeration, identity context (workgroup / domain / AAD / cloud), file shares, defenses, and exposure surface — without assuming Active Directory is present. Use when the agent has just landed on a victim host, when typed memory is empty for host_self / network / identity / defenses, when the next phase needs a fresh asset inventory, or when scope expands to a new subnet. Covers all 34 MITRE TA0007 techniques across four sub-tracks (host fingerprint, network, identity / AD-conditional, cloud-conditional) plus defense-aware command substitution; the AD-specific catalogue (BloodHound, ADSearch, AD CS ESC1-13, Kerberoast targets, delegation, GPO / SYSVOL) runs only when domain-joined signals fire.
stage: discovery
agent: DiscoveryAgent
mitre_tactics: ["TA0007"]
default_opsec: moderate
ambient: false
tool_allowlist:
  - sharphound
  - bloodhound-python
  - powerview
  - certipy
  - certify
  - crackmapexec
  - kerbrute
  - impacket-getuserspns
  - snaffler
  - adidnsdump
  - group3r
  - lapstoolkit
  - powerupsql
  - ldapdomaindump
  - windapsearch
  - nmap
  - masscan
  - pingcastle
  - roadtools
  - aadinternals
  - azurehound
  - spoolsample
  - petitpotam
budget:
  max_tool_calls: 30
  max_wallclock_min: 25
---

# Discovering Active Directory

Map AD structure, trust paths, attack surface, and the shortest path to Domain
Admin from any valid domain user. Output populates session memory
(`ad_graph`, `hosts[]`, `findings[]`) and drives stage selection for every
later stage.

## Quick start

1. Verify preconditions (below).
2. Run **BloodHound full collection** (Critical) — this is the most important
   single action of the stage and gates everything else.
3. Run **AD CS vulnerability discovery** in parallel (Critical, stealth).
4. Walk the Critical → Important → Optional list in
   [reference/techniques.md](reference/techniques.md) until at least one
   Critical-priority finding is committed, then signal `success`.
5. Set `recommended_next` per the rules in §Pivot conditions below.

Full technique catalogue, success indicators, and OPSEC notes:
[reference/techniques.md](reference/techniques.md).
Templated tool commands the agent dispatches via the Execution Layer:
[reference/tool-commands.md](reference/tool-commands.md).

## Preconditions

- `context.initial_access.user` set and validated (any domain user).
- DC reachable on 88/389/445 from the beachhead (verify with one `crackmapexec smb` sweep before anything else).
- If a PowerShell tool is selected, `opsec_state.amsi_bypassed == true`
  (handled by the ambient `evading-defenses` skill before dispatch).
- BloodHound / Neo4j sink reachable, or local zip ingestion path configured.

## Stage goal (commit criteria)

Signal `success` only when **all** of these hold:

- `ad_graph` populated with ≥ 10 user nodes, ≥ 1 computer, ≥ 1 group, and
  `shortest_path_to_DA` computed (length ≥ 1).
- `hosts[]` includes every DC discovered, with `role`, `ip`, `os`, signing flag.
- AD CS scan result attached: either a list of vulnerable templates
  (ESC1-ESC13) or an explicit "no findings" marker so later stages do not
  re-run.
- Password policy captured (gates any future spraying).
- At least one Critical-priority `finding` committed.

## Critical techniques (do not skip)

| # | Technique | MITRE | Tools |
|---|---|---|---|
| D1 | BloodHound full collection | T1087.002 | `sharphound`, `bloodhound-python` |
| D2 | ACL / permission abuse discovery | T1069.002 | `bloodhound`, `powerview` |
| D3 | AD CS vulnerability discovery (ESC1-13) | T1649 | `certipy`, `certify` |

Important and Optional techniques (16 more) and full per-technique cards are in
[reference/techniques.md](reference/techniques.md). Always run D1 first.

## Pivot conditions

The agent fills `agent_result.recommended_next` from the first rule that
matches. The Orchestrator may override.

- ESC1 / ESC4 / ESC6 / ESC8 found → `escalating-privileges`
- Kerberoastable SPN account found → `accessing-credentials`
- AS-REP-roastable user found → `accessing-credentials`
- ACL abuse edge with `WriteDacl` / `GenericAll on group` / `ForceChangePassword` → `moving-laterally` (cheap takeover)
- Unconstrained delegation host found (not a DC) → `accessing-credentials` (coerce + capture TGT)
- `ad_graph.shortest_path_to_DA.length == 0` and stage complete → `blocked`, escalate to human
- `opsec_state.edr_alert_observed` at any point → `evading-defenses` (ambient interlude)

## Self-critique (run every 5 tool calls)

- "Am I making progress against the stage goal? If the last two tool calls
  produced zero new findings, switch technique or signal `blocked`."
- "Did SharpHound `-c All` fail twice? Switch to `-c DCOnly --stealth` or to
  `bloodhound-python` from the Linux side. Do not retry `-c All` a third time."
- "Three consecutive `STATUS_LOGON_FAILURE` on the same credential → set
  `burned=true` and stop using it. Do not consume the lockout threshold."
- "If `crackmapexec --json` returns empty arrays for live hosts in
  `subnets_allow`, fall back to `nmap -sn` inside the same allow-list. Never
  widen scope."

## Evidence to capture

- BloodHound zip(s) → `artifacts/<step>/sh*.zip`; parsed graph delta committed
  to `ad_graph`.
- Certipy / Certify output → `artifacts/<step>/adcs.json`.
- PowerView JSON outputs → `artifacts/<step>/pv_<query>.json`.
- One-line summary per finding with `raw_output_ref` pointer.
- LAPS / GPP secrets, if any, go to the vault (`vault://C-####`) — never to
  plain artifact files.
