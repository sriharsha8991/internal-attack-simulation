# Defense Evasion — Techniques reference

Full catalogue for `evading-defenses`.

## Contents
- Technique table E1-E15
- Per-technique cards: E1 (AMSI), E2 (process injection), E5 (BYOVD)
- Mode glossary (preflight / rewrite / setup / post-action / build-time)

## Mode glossary

| Mode | Meaning |
|---|---|
| preflight | Runs before high-risk dispatch (ambient) |
| rewrite | Replaces an existing command with a stealthier equivalent |
| setup | Engagement-setup activity (C2 listeners, redirectors, payload build) |
| post-action | Runs after the action to remove forensic traces |
| build-time | Payload-build only; not live ops |

## Technique table

| # | Technique | Priority | MITRE | Primary tools | Mode |
|---|---|---|---|---|---|
| E1 | AMSI bypass | Critical | T1562.001 | amsi-bypass, AmsiScanBufferBypass, frida, invoke-obfuscation | preflight |
| E2 | Process injection | Critical | T1055 | cobaltstrike, sliver, mythic, havoc, donut, srdi | preflight |
| E3 | LOLBAS execution | Critical | T1218 | rundll32, mshta, regsvr32, wmic, cmstp, msbuild, installutil, regasm, certutil | rewrite |
| E4 | C2 traffic obfuscation | Critical | T1071.001 / T1090.004 | cobaltstrike malleable, sliver, havoc, domain fronting, CDN redirectors | setup |
| E5 | EDR disabling / BYOVD | Important | T1562.001 | edrsandblast, backstab, pplkiller, terminator, byovd drivers | last resort (destructive) |
| E6 | Reflective DLL / PE injection | Important | T1620 | srdi, donut, cobaltstrike, pe_to_shellcode | preflight |
| E7 | Obfuscation / packing | Important | T1027 | invoke-obfuscation, pyfuscation, confuser, sigflip, pezor | build-time |
| E8 | Parent process spoofing | Important | T1134.004 | selectmyparent, cobaltstrike | preflight |
| E9 | ETW patching | Important | T1562.006 | custom code, CS BOF, frida | preflight |
| E10 | Timestomping | Important | T1070.006 | cobaltstrike, metasploit, timestomp | post-action |
| E11 | Log clearing / tampering | Important | T1070.001 | wevtutil, Clear-EventLog, Invoke-Phant0m, mimikatz event::drop | post-action (constrained) |
| E12 | DNS-over-HTTPS / DNS C2 | Important | T1071.004 / T1572 | DoH providers, dnscat2, cobaltstrike DNS | setup |
| E13 | Disable / bypass PowerShell logging | Important | T1562.002 | PS patches, registry, AMSI bypass | preflight |
| E14 | Payload signing | Optional | T1553.002 | osslsigncode, sigthief, EV cert | build-time |
| E15 | Token manipulation | Optional | T1134 | cobaltstrike, incognito, metasploit | preflight |

## E1 — AMSI bypass (Critical, preflight gate)

- Tools: in-memory patch of `AmsiScanBuffer` / hardware-bp method; obfuscated
  bypass scripts.
- Success indicator: `[Ref].Assembly.GetType('System.Management.Automation.AmsiUtils')`
  patch returns expected handle, OR a known canary string is no longer
  scanned.
- OPSEC: invisible if in-memory; bypass strings are signatured — always
  obfuscate at build time.
- Failure handling: if 2 different bypass variants fail → mark
  `amsi_bypassed=false` and force subsequent stages to use Linux-side /
  Impacket tooling only.

## E2 — Process injection / parent PID spoof (Critical, preflight)

- Tools: C2-native (CS `inject`, Sliver `migrate`), donut, sRDI.
- Success: new beacon callback from injected PID; parent process spoofed;
  smoke-test `whoami` returns expected user.
- OPSEC: choose host process based on EDR profile (Teams.exe, OneDrive.exe,
  explorer.exe, svchost.exe). Avoid lsass / csrss / winlogon — auto-alerts.

## E5 — EDR disabling / BYOVD (Important, destructive last-resort)

- Tools: EDRSandBlast, Backstab, Terminator, PPLKiller, signed-but-vulnerable
  drivers.
- Destructive gate: YES — driver load is a high-signal event; explicit human
  ACK required.
- Cleanup: unload driver, restore EDR callbacks where possible — recorded in
  cleanup manifest.
