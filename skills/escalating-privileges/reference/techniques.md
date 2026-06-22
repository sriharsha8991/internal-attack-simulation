# Privilege Escalation — Techniques reference

Full catalogue for `escalating-privileges`.

## Contents
- Technique table P1–P6
- Per-technique cards (Critical first, Important after)
- OPSEC ladder quick-reference

---

## Technique table

| #  | Technique                               | Priority  | MITRE                  | Primary tools                                          | Notes                                              |
|----|-----------------------------------------|-----------|------------------------|--------------------------------------------------------|----------------------------------------------------|
| P0 | Current privilege state check           | Critical  | T1033                  | whoami (built-in only)                                 | No tools dropped; determines if phase needed at all |
| P1 | Local host enumeration                  | Critical  | T1057 / T1082          | seatbelt, winpeas, powerup, whoami, accesschk          | Run first; gates every other technique             |
| P2 | AD CS abuse (ESC1–ESC8)                 | Critical  | T1649                  | certify, certipy, rubeus, openssl, ntlmrelayx           | Most impactful; DA TGT from any domain user        |
| P3 | Token impersonation / Potato attacks    | Critical  | T1134.001              | godpotato, printspoofer, roguepotato, sweetpotato       | Instant SYSTEM if SeImpersonatePrivilege present   |
| P4 | UAC bypass                              | Important | T1548.002              | uacme, fodhelper (LOLBin)                               | Required if medium-integrity local admin           |
| P5 | Unquoted service path & weak perms      | Important | T1574.005 / T1574.010  | powerup, accesschk, sc, icacls, wmic                   | Service restart triggers SYSTEM exec               |
| P6 | DLL hijacking & sideloading             | Important | T1574.001 / T1574.002  | robber, procmon, msfvenom                               | Very common in legacy enterprise software          |

---

## OPSEC ladder quick-reference

### P0 — Privilege state decision tree

```
whoami /all + whoami /groups
         │
         ├─ SYSTEM ──────────────────────────────→ accessing-credentials (skip phase)
         ├─ Domain Admins / Enterprise Admins ──→ accessing-credentials (skip phase)
         ├─ Administrators + High integrity ────→ P4 (UAC bypass check)
         ├─ Administrators + Medium integrity ──→ P1 → P4
         └─ Standard user ───────────────────────→ P1 (full path)
```

### P2 — AD CS (ESC priority order)

| ESC  | Condition                                        | Impact            |
|------|--------------------------------------------------|-------------------|
| ESC1 | Enrollee-supplied SAN, no manager approval       | DA TGT directly   |
| ESC4 | Low-priv WriteDacl/WriteProperty on template     | Template takeover |
| ESC6 | EDITF_ATTRIBUTESUBJECTALTNAME2 set on CA         | DA TGT directly   |
| ESC8 | Web enrollment enabled, NTLM relay to CA         | DC cert via relay |

### P3 — Potato OPSEC ladder

| Order | Tool          | OS support                  | Why quieter                          |
|-------|---------------|-----------------------------|--------------------------------------|
| 1     | GodPotato     | Server 2012–2022, Win10–11  | CLSID abuse, no network listener     |
| 2     | PrintSpoofer  | Win10 / Server 2019+        | Named-pipe trick, no network         |
| 3     | RoguePotato   | Server 2016+                | Needs attacker listener on :9999     |
| 4     | SweetPotato   | Fallback                    | COM activation hijack                |

---

## P0 — Current privilege state check (Critical)

- MITRE: T1033
- Tools: `whoami` (OS built-in only — no binary drop, no disk write)
- Preconditions: any code execution on target host.
- **Run before every other technique. Two commands only. Never skip.**
- **Determines whether this entire phase is needed, and which path to take.**
- Commands: `whoami /all` and `whoami /groups` — nothing else in this step.
- Success indicators: integrity level confirmed; group memberships read; privilege list parsed.
- OPSEC: stealth (OS built-in, no network traffic, no disk write, no process spawn).
- Decision logic:

| whoami output shows | Memory saves | Next action |
|---|---|---|
| `NT AUTHORITY\SYSTEM` | `host.already_system=true`, `privesc.system_token=true` | → `accessing-credentials` immediately |
| `Domain Admins` or `Enterprise Admins` group | `host.is_domain_admin=true`, `privesc.local_admin=true` | → `accessing-credentials` immediately |
| `Administrators` group AND `High Mandatory Level` | `host.is_local_admin=true`, `host.current_integrity_level=high` | → P4 (UAC bypass check) then `accessing-credentials` |
| `Administrators` group AND `Medium Mandatory Level` | `host.is_local_admin=true`, `host.current_integrity_level=medium` | → P1 then P4 (UAC bypass needed) |
| No admin group AND `Medium Mandatory Level` | `host.is_local_admin=false`, `host.current_integrity_level=medium` | → P1 (full escalation path) |
| `Low Mandatory Level` | `host.current_integrity_level=low` | → P1 (most restricted — sandbox or limited token) |

- Save: `host.current_integrity_level`, `host.is_local_admin` (bool), `host.is_domain_admin` (bool), `host.already_system` (bool).
- Cycle / next:
  - `host.already_system=true` OR `host.is_domain_admin=true` → set `phase_done=true` → `accessing-credentials`
  - `host.is_local_admin=true` AND `high` integrity → P4 directly (skip P1–P3)
  - `host.is_local_admin=true` AND `medium` integrity → P1 then P4
  - Standard user → P1 (full escalation path)

---

## P1 — Local host enumeration (Critical)

- MITRE: T1057 / T1082
- Tools: `seatbelt`, `winpeas`, `powerup`, `whoami`, `accesschk`
- Preconditions: P0 has run and confirmed full escalation path is needed (not already SYSTEM/DA/high-admin).
- **Run before every exploitation technique. Never skip if P0 did not short-circuit.**
- Success indicators: `whoami /priv` output parsed; AV/EDR product name identified;
  integrity level confirmed; at least one escalation path identified.
- OPSEC: stealth (read-only queries; Seatbelt / WinPEAS touch disk but do not
  create services or modify registry).
- Expected output:
  - Seatbelt: AV products running, EDR hooks present, PS logging enabled/disabled
  - WinPEAS `[+]` = confirmed finding (exploitable), `[?]` = potential finding
  - `whoami /all`: SID, group memberships, token privileges
  - Token privileges of interest:
    - `SeImpersonatePrivilege` → P3 Potato attacks (instant SYSTEM)
    - `SeBackupPrivilege` → read NTDS.dit without DA (`accessing-credentials`)
    - `SeDebugPrivilege` → attach to LSASS (`accessing-credentials`)
    - `SeTakeOwnershipPrivilege` → take ownership of any object
    - `SeLoadDriverPrivilege` → BYOVD kernel driver load
  - Installed AV/EDR: determines which tools can run unmodified
  - Integrity level: Medium = UAC required; High/SYSTEM = proceed directly
- Cycle / next:
  - EDR present → `evading-defenses` FIRST (AMSI + ETW bypass)
  - `SeImpersonatePrivilege` → P3 immediately
  - Unquoted service / weak perms → P5
  - Missing DLL in writable path → P6
  - Medium-integrity local admin → P4 (UAC bypass) — P0 already confirmed this path

---

## P2 — AD CS abuse, ESC1–ESC8 (Critical)

- MITRE: T1649
- Tools: `certify` (Windows), `certipy` (Linux), `rubeus`, `openssl`, `ntlmrelayx`
- Preconditions: `ad.adcs_vulns` non-empty from discovery D3; any domain user.
- **Gate: do not run without confirmed vulnerable template from discovery.**
- Success indicators: `.pfx` file obtained; Rubeus `asktgt` returns base64 TGT;
  `klist` shows administrator@DOMAIN.LOCAL ticket.
- OPSEC: stealth (LDAP + DCOM CA query for discovery; one cert request for exploit).
- Expected output:
  - ESC1 full chain:
    - Certify: RSA private key + signed certificate (PEM format)
    - After openssl convert: `.pfx` file (cert + private key)
    - Rubeus asktgt: `[*] base64(ticket.kirbi)` for administrator
    - Rubeus: `[+] Ticket successfully imported`
    - `klist`: administrator@DOMAIN.LOCAL — 10 hr validity
    - Certipy: `administrator.pfx` + NTLM hash of administrator extracted from cert
  - ESC8 relay chain:
    - ntlmrelayx captures DC auth, forwards to CA HTTP endpoint
    - DC certificate issued for `DomainController` template
    - Certificate used to request DC TGT via PKINIT → DCSync
- Fallback: if ESC1 blocked (manager approval required), try ESC4 (modify template
  ACL if WriteDacl present), ESC6 (if EDITF_ATTRIBUTESUBJECTALTNAME2 set), then ESC8.
- Cycle / next:
  - DA TGT obtained → `achieving-impact` directly (skip remaining phases)
  - TGT obtained, need to move first → `moving-laterally` with PTT

---

## P3 — Token impersonation / Potato attacks (Critical)

- MITRE: T1134.001
- Tools: `godpotato`, `printspoofer`, `roguepotato`, `sweetpotato`
- Preconditions: `SeImpersonatePrivilege` present (confirmed in P1).
- **Always check `whoami /priv` first. If the privilege is not present, skip.**
- Success indicators: `whoami` returns `nt authority\system`; SYSTEM shell interactive.
- OPSEC: moderate. GodPotato and PrintSpoofer use CLSID/named-pipe tricks;
  no network listener required (quieter than RoguePotato).
- Follow OPSEC ladder — try GodPotato → PrintSpoofer → RoguePotato → SweetPotato;
  stop on first success.
- Expected output:
  - GodPotato: `[*] CreateProcessAsUser Success` → shell as `NT AUTHORITY\SYSTEM`
  - PrintSpoofer: `[+] Found privilege: SeImpersonatePrivilege` → `[+] Got token for SYSTEM`
  - `whoami` output: `nt authority\system`
  - Full SYSTEM shell: can now dump LSASS, read SAM, run Mimikatz
- Context where SeImpersonatePrivilege appears: IIS app pool accounts, MSSQL service
  accounts, WCF service accounts, any `Network Service` / `Local Service` principal.
- Cycle / next: SYSTEM achieved → `accessing-credentials` (LSASS + SAM + DPAPI)

---

## P4 — UAC bypass (Important)

- MITRE: T1548.002
- Tools: `fodhelper` (LOLBin, registry-only), `uacme` (60+ methods)
- Preconditions: user is in local Administrators; process is medium-integrity.
- Check: `whoami /groups | findstr "Mandatory Label"` → `Medium` = bypass needed.
- Success indicators: `whoami /groups` shows `Mandatory Label\High Mandatory Level`;
  can now read HKLM keys previously denied; Mimikatz / LSASS dump succeeds.
- OPSEC: fodhelper writes two registry keys then launches a process — moderate.
  UACME method 61 uses `consent.exe` COM activation — moderate.
- Try fodhelper first (no binary drop). If blocked, try UACME method 61 then 41.
- Expected output:
  - Process spawned with high integrity level (not medium)
  - `whoami /groups`: `Mandatory Label\High Mandatory Level`
  - UACME: `[#] Process created (PID: XXXX)`
  - Access to HKLM registry keys previously denied
- Cycle / next: high-integrity shell → `accessing-credentials` (LSASS dump)

---

## P5 — Unquoted service path & weak permissions (Important)

- MITRE: T1574.005 (unquoted path) / T1574.010 (weak binary perms)
- Tools: `powerup`, `accesschk`, `wmic`, `icacls`, `sc`
- Preconditions: any low-priv code execution; write access to at least one path segment
  or service binary.
- Two sub-paths:
  - **Unquoted path**: Windows splits the path at spaces and tries each prefix + `.exe`.
    If `C:\Program Files\My App\service.exe` is unquoted, Windows tries
    `C:\Program.exe` first. Drop payload at a writable prefix path.
  - **Weak binary perms**: service binary itself is writable. Replace it.
- Requires service restart: either `sc stop/start` (if stop/start perm granted) or
  wait for next reboot. PowerUp automates the check.
- Expected output:
  - PowerUp: `[*] Vulnerable path: C:\Program Files\Vuln App\vuln service.exe`
  - `accesschk` shows write permission for current user on path segment
  - After binary drop + service restart: `whoami` → `nt authority\system`
- Cycle / next: SYSTEM achieved → `accessing-credentials`

---

## P6 — DLL hijacking & sideloading (Important)

- MITRE: T1574.001 (DLL hijacking) / T1574.002 (DLL sideloading)
- Tools: `robber`, Process Monitor (manual), `msfvenom`
- Preconditions: any code execution; write access to a directory in a DLL search path
  that precedes the legitimate DLL location.
- Detection: Robber.exe automated scan for missing DLLs in writable paths.
  Manual: Process Monitor filter → `Result = NAME NOT FOUND` + extension `.dll`.
- Very common in legacy enterprise software that ships without `SafeDllSearchMode`.
- Expected output:
  - Robber: `[*] Vulnerable: C:\app\missing.dll` with write-permission confirmation
  - Procmon: `NAME NOT FOUND` for target DLL in a writable path
  - DLL load confirmed: beacon callback from service account context
  - If domain service account: creds available via LSASS after execution
- Cycle / next:
  - Execution as SYSTEM / high-priv service → `accessing-credentials`
  - Execution as domain service account → `accessing-credentials`
    (Kerberoast that account or dump LSASS for its ticket)
