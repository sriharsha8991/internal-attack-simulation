---
name: evading-defenses
description: Keeps the engagement alive by bypassing or blinding AV, EDR, AMSI, ETW, ScriptBlock logging, and SIEM where feasible. Operates in two modes — ambient preflight (loaded by every other stage before any high-risk tool dispatch to inject AMSI / ETW bypass and OPSEC-score commands) and on-demand stage invocation (when opsec_state.edr_hot becomes true the orchestrator routes here to sanitise burned tools, restore cover, verify cold state, then resume the previous stage). Covers AMSI bypass, process injection / parent PID spoofing, LOLBAS rewriting, C2 traffic obfuscation, reflective DLL / PE injection, EDR disabling / BYOVD, packer/obfuscation, ETW patching, timestomping, log clearing/tampering, DNS C2, PowerShell logging bypass, payload signing, and token manipulation.
stage: defevasion
agent: DefEvasionAgent
mitre_tactics: ["TA0005"]
default_opsec: stealth
ambient: true
tool_allowlist:
  - amsi-bypass-scripts
  - invoke-obfuscation
  - amsiscanbufferbypass
  - frida
  - cobaltstrike
  - sliver
  - mythic
  - havoc
  - metasploit
  - srdi
  - donut
  - pe_to_shellcode
  - edrsandblast
  - backstab
  - pplkiller
  - terminator
  - byovd-drivers
  - selectmyparent
  - wevtutil
  - invoke-phant0m
  - mimikatz
  - timestomp
  - sigflip
  - pezor
  - confuser
  - pyfuscation
  - osslsigncode
  - sigthief
  - incognito
  - dnscat2
budget:
  max_tool_calls: 15
  max_wallclock_min: 10
---

# Evading defenses (ambient + on-demand)

Two modes:

- **Ambient** — loaded by every other stage. Runs the preflight checklist
  before any high-risk dispatch (AMSI status, EDR product detection, ETW
  patch state), and OPSEC-scores the pending command.
- **On-demand** — invoked as a stage by the Orchestrator when
  `opsec_state.edr_hot == true`. Restore cold state, prune burned tools,
  signal `success` so the previous stage resumes.

## Quick start (on-demand stage call)

1. **Identify the signal** — which tool produced the alert? which event
   source? — set `opsec_state.alert_source`.
2. **Sanitise** — add the offending tool to `opsec_state.burned_tools`;
   rewrite the previous stage's allowlist to its stealth-tagged equivalents.
3. **Restore cover** — re-apply AMSI/ETW patches; migrate into a fresh host
   process (E2); rotate beacon sleep + jitter; switch C2 channel if available
   (HTTPS → DoH/DNS).
4. **Verify** — rerun the ambient preflight; require `edr_hot == false` for
   ≥ 1 minute.
5. **Return** — `agent_result.recommended_next = <previous_stage>` so the
   Orchestrator resumes where it was.

Ambient preflight + on-demand commands:
[reference/tool-commands.md](reference/tool-commands.md).
Full technique catalogue: [reference/techniques.md](reference/techniques.md).

## Ambient preflight rules (applied to every dispatch)

- `cmd.opsec == 'loud'` and `opsec_state.edr_hot == true` → **drop**; request
  rewrite from the calling skill (use that skill's `fallback`).
- Tool in `opsec_state.burned_tools` → **drop**; force fallback.
- PS-based tool requested but `opsec_state.amsi_bypassed == false` → inject
  AMSI bypass first, then proceed.
- Network destination outside `scope.subnets_allow` → **hard drop + alert**
  (scope filter, never bypassed).

## Tool acquisition

Defense evasion should primarily use BUILT-IN methods that require NO
downloads. The agent should figure out the right approach based on what
security controls it detects in the environment.

**Strategy:**
1. **Built-in first**: Most evasion techniques (AMSI bypass, ETW
   patching, process manipulation) can be done with native PowerShell,
   .NET reflection, or OS APIs. No downloads needed.
2. **Environment-driven**: Detect what security products are present
   (AV vendor, EDR product, logging configuration) and tailor the
   evasion approach specifically to that product.
3. **In-memory only**: If evasion code is needed, prefer loading it
   directly into memory via download-cradles or reflection. Never
   drop suspicious binaries to disk for evasion purposes.
4. **Search for latest bypasses**: Security products update signatures
   frequently. If a bypass technique is blocked, use web search to
   find the latest working variant for that specific product.
5. **Minimal footprint**: Defense evasion should NOT require downloading
   large or suspicious binaries. If a binary-based approach is the
   only option (e.g., driver-based EDR disabling), it should be a
   last resort with immediate cleanup.

## Critical techniques

| # | Technique | MITRE | Tools | Mode |
|---|---|---|---|---|
| E1 | AMSI bypass | T1562.001 | amsi-bypass, AmsiScanBufferBypass, frida | preflight |
| E2 | Process injection / parent PID spoof | T1055 | cobaltstrike, sliver, mythic, havoc, donut, srdi | preflight |
| E3 | LOLBAS execution | T1218 | rundll32, mshta, regsvr32, wmic, cmstp, msbuild, installutil, regasm, certutil | rewrite |
| E4 | C2 traffic obfuscation | T1071.001 / T1090.004 | cobaltstrike malleable, sliver, havoc, CDN fronting | setup |

Important (E5-E13) and Optional (E14-E15) techniques and per-technique cards
in [reference/techniques.md](reference/techniques.md).

## Pivot conditions

- Three consecutive evasion attempts fail to bring `edr_hot` back to false →
  signal `blocked` and escalate to human (engagement may be burned).
- After `edr_hot=false` for ≥ 1 minute → return to `<previous_stage>`.

## Self-critique

- "AMSI-bypass string itself was signatured (Defender blocks before
  execution) → DO NOT retry variants of the same script. Switch *method*
  (AmsiScanBuffer hardware-breakpoint, or move to Impacket on the Linux
  side)."
- "`wevtutil cl Security` triggered EventID 1102 visible to defenders.
  Prefer `Invoke-Phant0m` or per-event drop. If 1102 was already logged → do
  NOT clear again; accept and continue, log the OPSEC cost in
  `opsec_state.observable_evidence`."
- "BYOVD driver load → very high signal. Use only when explicitly authorised
  AND no other evasion path remains. Always unload + restore on cleanup."
- "Process-injection target chosen poorly (e.g. into a process the user just
  closed) → migrate to a long-running host process (explorer.exe, svchost.exe
  with the right service). Never lsass, csrss, winlogon."

## Evidence to capture

- Every evasion action is itself an `attack_path[]` step. Defenders should
  see what we did and when, even if they did not detect it live.
- AMSI / ETW patch state probed and recorded before and after each major op.
- Cleanup-manifest entries for driver loads, log tampering, process spoofing,
  C2 channel changes — same rollback discipline as `establishing-persistence`.
