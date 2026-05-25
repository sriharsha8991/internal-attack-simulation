---
name: establishing-persistence
description: Maintains access through reboots, password resets, and partial blue-team response. Every action must be reversible, human-acknowledged via the destructive-action gate, and recorded with an exact rollback command. Use when accessing-credentials has produced the krbtgt hash (forge Golden Ticket before rotation), when DCSync or DA-equivalent control is held and the engagement scope authorises persistence, when host-level persistence is required for long-dwell scenarios, or when the engagement scope explicitly requests hybrid Azure AD persistence. Covers Golden Ticket, Silver Ticket, Skeleton Key, AdminSDHolder ACL backdoor, DCSync rights grant, WMI event subscription, scheduled tasks, registry Run keys, service creation/hijack, account manipulation, SSH authorized_keys, DSRM account abuse, DLL sideloading persistence, and Azure AD service-principal backdoors.
stage: persistence
agent: PersistenceAgent
mitre_tactics: ["TA0003"]
default_opsec: stealth
ambient: false
tool_allowlist:
  - mimikatz
  - rubeus
  - impacket-ticketer
  - impacket-addcomputer
  - powerview
  - adexplorer
  - damp
  - sharptask
  - schtasks
  - powershell
  - sharpwmi
  - powerlurk
  - sc
  - reg
  - net
  - aadinternals
  - roadtools
  - tokentactics
budget:
  max_tool_calls: 15
  max_wallclock_min: 15
  destructive_default: ask
---

# Establishing persistence

Plant durable access. Every action MUST be (a) reversible — cleanup recorded
in `artifacts/<step>/cleanup.json`, (b) human-acknowledged via the destructive
gate, (c) appended to `attack_path[]` so the final report hands the client an
exact list to revert.

## Quick start

1. **Always** verify the destructive-gate ACK token is present in
   `context.acks[]` for the chosen technique. No ACK → `blocked`, do not
   attempt.
2. If `vault.has('krbtgt_nt')` and `stage_status.impact != 'success'`, forge
   the **Golden Ticket** (Pe1) **before krbtgt rotation**.
3. Add a stealth ACL persistence (Pe2 AdminSDHolder, or Pe3 DCSync grant) only
   if the engagement scope authorises tier-0 resilience.
4. For host-level dwell, prefer **WMI event subscription** (Pe6) > scheduled
   task (Pe7) > registry Run (Pe8) > service (Pe9, loudest).
5. Templated commands: [reference/tool-commands.md](reference/tool-commands.md).
   Full technique catalogue: [reference/techniques.md](reference/techniques.md).

## Preconditions

- For Tier-0 persistence (Golden Ticket, AdminSDHolder, DSRM, DCSync grant):
  `findings.any(technique in ['DCSync','Domain Admin Account Compromise'])`.
- For host-scoped persistence: local admin / SYSTEM on the target.
- Human ACK token in `context.acks[]` for the specific technique attempted.

## Tool acquisition

Most persistence techniques use NATIVE OS capabilities — scheduled tasks,
registry keys, services, WMI subscriptions, cron jobs, SSH keys, etc.
These require NO downloads.

**Strategy:**
1. **Native first**: The majority of host-level persistence can be
   achieved with built-in OS commands. Always prefer these.
2. **AD-level persistence**: Some AD persistence techniques (ticket
   forging, ACL manipulation) require specific tools. The agent should
   check if any were already acquired in prior phases before downloading
   again. Use web search to find the right tool and source if needed.
3. **Reuse prior tools**: Tools acquired during credential access or
   privilege escalation phases may still be available in the temp
   directory. Check before re-downloading.
4. **In-memory for scripts**: PowerShell-based AD modules can often be
   loaded directly into memory without writing to disk.
5. **Cleanup awareness**: Every persistence mechanism must have an exact
   rollback command recorded — this includes cleaning up any tools
   downloaded for this phase.

## Critical techniques

| # | Technique | MITRE | Tools | Notes |
|---|---|---|---|---|
| Pe1 | Golden Ticket | T1558.001 | `rubeus golden`, `impacket-ticketer`, `mimikatz` | Forge offline; ~10y default |
| Pe2 | AdminSDHolder ACL modification | T1098 | `powerview`, `damp` | SDProp re-applies every ~60 min |
| Pe3 | DCSync rights grant to controlled account | T1098.002 | `powerview`, AD module | Pseudo-DA without group membership |

Important (Pe4-Pe12) and Optional (Pe13-Pe15) techniques and per-technique
cards in [reference/techniques.md](reference/techniques.md).

## Pivot conditions

- `vault.has('krbtgt_nt')` and `stage_status.impact != 'success'` → forge
  Golden Ticket NOW → `achieving-impact`.
- Golden Ticket forged + injected + DC access verified →
  `evading-defenses` (log clear) → `achieving-impact` (report).
- AdminSDHolder / DCSync grant successful → `achieving-impact` (proof). Do
  not stack multiple Tier-0 persistences unless resilience testing is in
  scope.
- Engagement scope says "ephemeral" → skip stage; signal `success` with
  `status_note: 'skipped_by_scope'`.

## Self-critique

- "Golden Ticket forged but `klist` shows `KDC_ERR_C_PRINCIPAL_UNKNOWN` →
  the forged principal must exist OR `/id:500` must be set; verify `/sid:`
  matches domain SID exactly (case-sensitive on Linux tools)."
- "AdminSDHolder write succeeded but new privileges not visible after 1 h →
  SDProp interval may be customised; check `dsHeuristics`. Wait one full
  interval before re-attempting."
- "Service creation returned access denied with SYSTEM → service name
  conflict; pick a different `persist.service_name`."
- "Skeleton Key 'works' but only on the patched DC → by design; per-DC,
  non-replicated. Do NOT patch every DC."
- "ACK token missing → STOP, signal `blocked`,
  `reason='needs_human_ack'`."

## Evidence to capture

- For every action, write `artifacts/<step>/cleanup.json` with the exact
  reverse command, host, principal, timestamps. This is the rollback manifest
  handed to the client at engagement end.
- Golden / Silver tickets → vault; only metadata (validity window, target
  user, SID, embedded group memberships) in the report.
- At engagement close, re-run each cleanup command and record the result. Any
  failure escalates to human.
