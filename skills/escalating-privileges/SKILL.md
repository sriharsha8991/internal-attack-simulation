---
name: escalating-privileges
description: Escalates from low-privilege domain user to SYSTEM / high-integrity local admin on the current beachhead host before any noisy AD attack (LSASS dump, NTDS, golden ticket). Use when the beachhead host has access_level=user, when context.initial_access.integrity is MEDIUM or LOW, after the discovering-environment skill returns AD CS ESC1-ESC8 findings, or whenever credential-access actions are blocked by insufficient local privilege. Covers Seatbelt/WinPEAS triage, AD CS template abuse, the Potato family (PrintSpoofer/GodPotato/RoguePotato/JuicyPotato/EfsPotato/SweetPotato), UAC bypass via UACME, unquoted service paths, weak service ACLs, AlwaysInstallElevated, DLL search-order hijack and sideload, named-pipe impersonation, Linux SUID/sudo abuse on AD-joined hosts, and container escape.
stage: privesc
agent: PrivEscAgent
mitre_tactics: ["TA0004"]
default_opsec: moderate
ambient: false
tool_allowlist:
  - seatbelt
  - winpeas
  - privesccheck
  - powerup
  - certify
  - certipy
  - printspoofer
  - roguepotato
  - godpotato
  - sweetpotato
  - juicypotato
  - efspotato
  - accesschk
  - uacme
  - msfvenom
  - msiexec
  - procmon
  - linpeas
  - cdk
  - peirates
budget:
  max_tool_calls: 20
  max_wallclock_min: 15
---

# Escalating local privileges

Reach SYSTEM / local admin on the beachhead host **before** running noisy AD
attacks. On success, update `hosts[<beachhead>].access_level` to `local_admin`
or `system`, set `context.initial_access.integrity = HIGH`, and capture any
side-channel credential (issued PFX, harvested hash) into `credentials[]`.

## Quick start

1. Run **P1 host enumeration** (Seatbelt → WinPEAS) — this gates every other
   technique by revealing EDR, UAC, patch level, and writable surfaces.
2. If Discovery already flagged AD CS ESC1/4/6/8, jump to **P2 AD CS abuse**
   (cleanest path; one cert request and you have a DA-context TGT).
3. If `whoami /priv` includes `SeImpersonatePrivilege`, run **P3 Potato**.
4. Otherwise walk the Important list in
   [reference/techniques.md](reference/techniques.md).
5. Templated commands: [reference/tool-commands.md](reference/tool-commands.md).

## Preconditions

- Beachhead host present in `hosts[]` with `access_level=user`.
- Local enum is missing or older than 30 minutes.
- AMSI bypass loaded by ambient `evading-defenses` skill before any PS tool.

## Critical techniques

| # | Technique | MITRE | Tools | Trigger |
|---|---|---|---|---|
| P1 | Local host enumeration | T1082 / T1518.001 | `seatbelt`, `winpeas`, `privesccheck`, `powerup` | always first |
| P2 | AD CS abuse (ESC1-ESC8) | T1649 | `certify`, `certipy` | ESC finding in `findings[]` |
| P3 | Token impersonation / Potato | T1134.001 | `printspoofer`, `godpotato`, `roguepotato`, `efspotato` | `SeImpersonatePrivilege` present |

Important and Optional techniques (10 more) and per-technique cards are in
[reference/techniques.md](reference/techniques.md).

## Pivot conditions

- SYSTEM achieved on beachhead → `accessing-credentials` (LSASS/DCSync planning now safe).
- ESC cert issued for a high-priv user → `accessing-credentials` (`pkinit_to_tgt`).
- 3 consecutive escalation attempts blocked → signal `blocked`, recommend
  `moving-laterally` to a different host on the BloodHound path.
- EDR alert at any point → `evading-defenses` (ambient interlude), retry with
  stealth-tagged commands only.

## Self-critique

- "PrintSpoofer fails with 'Could not connect to spooler' → Spooler is disabled
  or patched. Switch to GodPotato; do not retry PrintSpoofer."
- "If `whoami /priv` does not list `SeImpersonatePrivilege`, skip ALL Potato
  techniques. Do not try them speculatively."
- "If WinPEAS is killed by AV mid-run, do not re-run. Switch to PrivescCheck
  (PowerShell, in-memory). Mark WinPEAS as burned in `opsec_state.burned_tools`
  for this host."
- "Kernel-exploit techniques carry BSOD risk → require explicit human ACK via
  the destructive-action gate. Never auto-run."

## Evidence to capture

- Seatbelt / WinPEAS / PowerUp JSON outputs → `artifacts/<step>/`.
- Issued PFX → vault (`vault://C-####`), not raw artifact.
- `whoami /all` snapshots before and after privilege gain.
- New session/PID + `whoami` confirming SYSTEM for the report timeline.
