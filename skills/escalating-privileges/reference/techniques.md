# Privilege Escalation — Techniques reference

Full catalogue for the `escalating-privileges` skill.

## Contents
- Technique table P1-P13 (Critical → Important → Optional)
- Per-technique cards: P1 (host enum), P2 (AD CS abuse), P3 (Potato)
- Selection guidance and OS-version matrix for the Potato family

## Technique table

| # | Technique | Priority | MITRE | Primary tools | Pivot hint |
|---|---|---|---|---|---|
| P1  | Local host enumeration | Critical | T1082 / T1518.001 | seatbelt, winpeas, privesccheck, powerup, jaws | gates every other technique |
| P2  | AD CS abuse (ESC1-ESC8) | Critical | T1649 | certify, certipy, adcspwn, pspkiaudit | accessing-credentials (DA cert → TGT) |
| P3  | Token impersonation / Potato | Critical | T1134.001 | printspoofer, godpotato, roguepotato, sweetpotato, juicypotato, efspotato | accessing-credentials (LSASS) |
| P4  | Unquoted service path | Important | T1574.009 | winpeas, powerup, beroot, accesschk | persistence + accessing-credentials |
| P5  | Weak service / file permissions | Important | T1574.010 | winpeas, powerup, accesschk, icacls | persistence + accessing-credentials |
| P6  | UAC bypass (medium → high integrity) | Important | T1548.002 | uacme (fodhelper, eventvwr, computerdefaults, cmstp, msconfig) | enables LSASS access |
| P7  | AlwaysInstallElevated (MSI as SYSTEM) | Important | T1548.002 | winpeas, powerup, msfvenom, msiexec | accessing-credentials |
| P8  | DLL search-order hijacking | Important | T1574.001 | procmon, winpeas, robber, dllspy | persistence-friendly |
| P9  | DLL sideloading | Important | T1574.002 | procmon, sigflip, custom dll | persistence-friendly, stealth |
| P10 | Named-pipe impersonation | Important | T1134.002 | pipeviewer, custom exploit | accessing-credentials |
| P11 | Sudo misconfig / SUID on Linux AD-joined host | Optional | T1548.003 | linpeas, gtfobins | accessing-credentials (steal /tmp/krb5cc_*) |
| P12 | Kernel exploit | Optional | T1068 | winpeas, metasploit, windows-kernel-exploits | last resort — BSOD risk |
| P13 | Container escape | Optional | T1611 | cdk, peirates, deepce, amicontained | host → AD path |

## Potato family — OS-version matrix

| Tool | Windows versions | Trigger |
|---|---|---|
| `printspoofer` | Win10, Server 2019, Server 2022 (Spooler reachable) | Spooler service enabled |
| `godpotato` | Server 2012R2 through Server 2022 | DCOM activation works |
| `roguepotato` / `sweetpotato` | Server 2016, Server 2019 | DCOM redirector available |
| `juicypotato` | Win7, Server 2008/2012/2016 (≤2016) | Patched on later versions |
| `efspotato` | Most versions, when Spooler patched | EFS RPC reachable |

Try in this order, stop on first success. Stop entirely if `whoami /priv` lacks
`SeImpersonatePrivilege` or `SeAssignPrimaryTokenPrivilege`.

## P1 — Local host enumeration (Critical)

- MITRE: T1082, T1518.001
- Tools: `seatbelt` (first, single beacon-friendly C# call), `winpeas` (depth,
  noisier), `privesccheck` (PS, in-memory).
- Success indicators: parser yields `os_build`, `patch_level`, `edr_product`,
  `uac_level`, `local_admins`, `services_with_unquoted_path`,
  `writable_services`, `interesting_files`.
- OPSEC: moderate. Seatbelt is preferred; WinPEAS touches disk.
- Fallback: individual Seatbelt commands (`TokenPrivileges`, `AMSIProviders`,
  `PowerShellSettings`, `Patches`) instead of `-group=all`.

## P2 — AD CS abuse (Critical)

- MITRE: T1649
- Tools: `certify` (Windows, in-memory), `certipy` (Linux).
- Preconditions: D3 result present in `findings[]`, or run Discovery first.
- Success indicators: issued PFX cert in `credentials[]` with
  `secret_type=cert_pfx`, `usable_for ⊇ ['ptt']`; UPN/SAN set to a privileged
  user (DA / DC computer).
- OPSEC: stealth — cert requests look legitimate.
- Fallback: if ESC1 blocked, try ESC4 (writable template), ESC8 (NTLM relay →
  web enrollment via PetitPotam → ntlmrelayx).
- Pivot hint: always → `accessing-credentials` (convert cert to TGT via PKINIT,
  then DCSync).

## P3 — Token impersonation / Potato (Critical)

- MITRE: T1134.001
- Preconditions: `whoami /priv` includes `SeImpersonatePrivilege` or
  `SeAssignPrimaryTokenPrivilege`.
- Success indicators: spawned process `whoami` returns `NT AUTHORITY\SYSTEM`;
  new session_id captured.
- OPSEC: moderate (Spooler / RPC traffic visible to EDR).
- Pivot hint: → `accessing-credentials` (LSASS dump now permitted).
