---
name: moving-laterally
description: Moves from the current host toward higher-value targets — the next hop on the BloodHound shortest path to DA, a host with an active DA session, an unconstrained-delegation host, or a Tier-0 asset (DC, CA, ADFS, Azure AD Connect). Use when accessing-credentials has produced a hash, ticket, certificate or password that grants access to a known target host, when the orchestrator selects the next hop, or when establishing-persistence requires positioning on a specific host. Covers PTH, PTT, WMI/WinRM execution, SMBexec, PsExec, DCOM, RDP hijack, Overpass-the-Hash, SSH pivoting, SOCKS / Ligolo-ng tunnelling, token impersonation, unconstrained delegation TGT capture via SpoolSample/PetitPotam, Shadow Credentials, RBCD, MSSQL link traversal, and WMI event-subscription lateral.
stage: lateral
agent: LateralAgent
mitre_tactics: ["TA0008"]
default_opsec: stealth
ambient: false
tool_allowlist:
  - impacket-wmiexec
  - impacket-psexec
  - impacket-smbexec
  - impacket-dcomexec
  - impacket-atexec
  - sharpwmi
  - crackmapexec
  - evil-winrm
  - rubeus
  - mimikatz
  - sharpcom
  - tscon
  - chisel
  - ligolo-ng
  - revsocks
  - gost
  - ssh
  - powerupsql
  - sqlcmd
  - whisker
  - pywhisker
  - certipy
  - spoolsample
  - petitpotam
  - impacket-addcomputer
  - impacket-rbcd
  - incognito
  - powerlurk
budget:
  max_tool_calls: 20
  max_wallclock_min: 20
---

# Moving laterally

Reach the next target host with the credential material in `credentials[]`.
Update `hosts[target].access_level`, append the hop to `attack_path[]`, and set
the next pivot per BloodHound shortest path.

## Quick start

1. Pick the **stealthiest method that works** — preferred order: **PTT > PTH
   via WMI > WinRM > DCOM > SMBexec > PsExec**. Never use PsExec first.
2. Validate the credential against the target first via `crackmapexec smb
   <ip> -u ... -H ... --json` — one packet, no exec.
3. Execute. On success, drop an implant if needed, then signal pivot.
4. Templated commands: [reference/tool-commands.md](reference/tool-commands.md).
   Full technique catalogue: [reference/techniques.md](reference/techniques.md).

## Preconditions

- ≥1 usable credential for the target in `credentials[]` (`usable_for`
  includes `pth` / `ptt` / `password` / `cert_pfx`).
- A specific `target.host` selected by the Orchestrator.
- Network reachability validated.
- AMSI / ETW patched on the source if PowerShell-based methods are used.

## Critical techniques

| # | Technique | MITRE | Tools | OPSEC |
|---|---|---|---|---|
| L1 | Pass-the-Hash | T1550.002 | `impacket-*`, `crackmapexec`, `evil-winrm` | moderate |
| L2 | Pass-the-Ticket | T1550.003 | `rubeus`, `mimikatz`, `impacket -k` | stealth |
| L3 | WMI remote execution | T1047 | `impacket-wmiexec`, `sharpwmi`, `crackmapexec -x` | moderate |
| L4 | WinRM / PowerShell Remoting | T1021.006 | `evil-winrm`, `crackmapexec` | moderate |

Important (L5-L15) and Optional (L16-L17) techniques and per-technique cards
in [reference/techniques.md](reference/techniques.md).

## Pivot conditions

- `target.host.tier == 0` and `access_level >= local_admin` →
  `accessing-credentials` (LSASS / DCSync from DC perspective).
- Active DA session on target (from D5) + SYSTEM there →
  `accessing-credentials`.
- Unconstrained-delegation host owned + `rubeus monitor` running →
  `accessing-credentials` (coerce DC, capture TGT, DCSync).
- Shadow Credential added on high-priv principal → `accessing-credentials`
  (`pkinit_to_tgt`).
- 3 consecutive lateral failures on the same target → mark `blocked`, ask
  Orchestrator for the next target on the BloodHound path.
- EDR alert on PsExec / SMBexec → `evading-defenses`; drop those tools from
  the allowlist; retry via DCOM / WMI.

## Self-critique

- "PTH `STATUS_LOGON_FAILURE` against a **local** admin account →
  `LocalAccountTokenFilterPolicy` / `FilterAdministratorToken`. Switch to a
  **domain** admin hash, or to Overpass-the-Hash for Kerberos auth."
- "PTT with TGS but auth fails for a different SPN than the ticket was issued
  for → tickets are SPN-bound; ask for the right TGS via S4U2Self/S4U2Proxy or
  request from the TGT."
- "Evil-WinRM hangs on connect → listener may be 5986 only; retry with `-S`
  (HTTPS) and `-c` to disable cert verification."
- "Many `validate_pth` rounds return zero `Pwn3d!` hits → the account is not a
  local admin anywhere. Stop spraying; try Overpass to find Kerberos-only
  resources it can touch."
- "PsExec failed twice with EDR alert → DO NOT retry. Switch tool **and** add
  `psexec` to `opsec_state.burned_tools`."
- "Coercion (SpoolSample/PetitPotam) succeeded but `rubeus monitor` captured
  no TGT → the listener must run **on** the unconstrained-delegation host AND
  DNS for the target SPN must resolve to its IP."

## Evidence to capture

- Per hop: source host, source principal, target host, target principal,
  technique, MITRE id, timestamp, sanitised tool command, parser result.
- `hostname` + `whoami` on the new host (mandatory for the report).
- Pivot session metadata (controller IP/port/transport) — reference only.
- Append to `attack_path[]` immediately so a mid-run crash does not lose the
  chain.
