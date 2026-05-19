# Impact — Techniques reference

Full catalogue for `achieving-impact`.

## Contents
- Technique table I1-I12
- Per-technique cards: I1, I2, I3, I4

## Technique table

| # | Technique | Priority | MITRE | Primary tools |
|---|---|---|---|---|
| I1 | DCSync full-domain hash extraction | Critical | T1003.006 | mimikatz, impacket-secretsdump -just-dc, sharpkatz, crackmapexec |
| I2 | Domain Admin account compromise (path proof) | Critical | varies | bloodhound, rubeus, mimikatz, impacket, crackmapexec |
| I3 | Crown-jewel access | Critical | T1530 / T1213 | manual, cobaltstrike, crackmapexec, impacket |
| I4 | MITRE ATT&CK mapping of all TTPs used | Critical | reporting | attack-navigator, atomic-red-team, vectr, plextrac |
| I5 | Ransomware simulation (isolated host) | Important | T1486 | atomic-red-team, caldera, custom scripts |
| I6 | Business Email Compromise simulation | Important | T1114.002 / T1534 | mailsniper, ruler, manual |
| I7 | Business-process disruption demo | Important | T1489 | custom scripts, manual |
| I8 | Data exfiltration simulation (dummy data) | Important | T1041 / T1048 | cobaltstrike, dnscat2, impacket, custom |
| I9 | AD full-compromise proof (DC SYSTEM shell + krbtgt evidence) | Important | T1486 / T1078 | mimikatz, impacket, cobaltstrike, crackmapexec |
| I10 | Hybrid cloud lateral impact (on-prem DA → Cloud GA) | Optional | T1098.001 | aadinternals, roadtools, tokentactics, powerzure |
| I11 | Purple-team replay | Optional | reporting | caldera, atomic-red-team, attack-navigator, vectr |
| I12 | Detection & response gap analysis | Optional | reporting | SIEM review, manual, vectr |

## I1 — DCSync full-domain hash extraction (Critical)

- Note: while initially run as `accessing-credentials`, the *full-domain*
  extraction (`-just-dc`) is the proof artefact for this stage.
- Success indicator: output includes krbtgt (NTLM + AES) + ≥ 3
  protected-group account hashes. Redacted summary committed; raw stored
  only in vault.
- Destructive gate: YES (also true in accessing-credentials).
- Evidence: krbtgt hash hex + timestamp + DC hostname → report exhibit
  **DA-Proof-1**.

## I2 — Domain Admin account compromise (Critical)

- Proof: interactive shell or remote command as a confirmed DA member on a
  DC; `whoami /groups` shows `Domain Admins`; hostname is a DC.
- Method: usually result of L3/L4 (WMI/WinRM) using DA credentials harvested
  via C4 / Pe1 / P2.
- Evidence: console capture of `hostname`, `whoami /all`, `ipconfig`,
  `Get-ADUser -Server $env:LOGONSERVER` on the DC.

## I3 — Crown-jewel access (Critical, scope-driven)

- Objectives list comes from `context.objectives`
  (e.g. `access:CRM-DB`, `read:HR-shares`, `access:M365-admin-center`).
- Evidence per crown jewel: hostname, principal, listing of access (file
  names, table names — NOT contents), timestamps, file SHA256.
- **Hard rule**: do NOT exfiltrate or download actual sensitive content.
  List metadata; capture file hashes if needed for proof.

## I4 — MITRE ATT&CK mapping (Critical, reporting)

- Action: iterate `attack_path[]` and `findings[]`, ensure every step has a
  MITRE id; export ATT&CK Navigator JSON via `attack-navigator` exporter.
- Evidence: `report/attack_navigator.json`, `report/heatmap.svg`.
