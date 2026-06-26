---
name: escalating-privileges
description: Escalates from any unprivileged or medium-integrity foothold to SYSTEM / local admin on the current host. Covers local host enumeration (Seatbelt / WinPEAS), AD CS abuse (ESC1–ESC8), token impersonation / Potato attacks (GodPotato / PrintSpoofer / RoguePotato), UAC bypass (fodhelper / UACME), unquoted service path and weak service-binary permissions, and DLL hijacking / sideloading. Use after discovering-environment surfaces SeImpersonatePrivilege, AD CS vulnerabilities, or service misconfigurations — and before accessing-credentials needs SYSTEM for LSASS dumping. REPEAT ON EVERY NEW MACHINE reached via lateral movement.
stage: privesc
agent: PrivEscAgent
mitre_tactics: ["TA0004", "TA0005"]
default_opsec: moderate
ambient: false
tool_allowlist:
  # ---- enumeration ----
  - seatbelt
  - winpeas
  - powerup
  - accesschk
  - robber
  # ---- AD CS abuse ----
  - certify
  - certipy
  - rubeus
  - openssl
  - impacket-ntlmrelayx
  # ---- token / potato ----
  - godpotato
  - printspoofer
  - roguepotato
  - sweetpotato
  - juicypotato
  - incognito
  - tokenvator
  # ---- UAC bypass ----
  - uacme
  # ---- service / DLL ----
  - msfvenom
  - sc
  - icacls
  - wmic
budget:
  max_tool_calls: 20
  max_wallclock_min: 15
---

# Escalating privileges

Escalate from any unprivileged or medium-integrity foothold to SYSTEM / local
admin on the current host. SYSTEM is required before `accessing-credentials`
can perform LSASS dumping, SAM extraction, or DPAPI decryption.

** Repeat this phase on every new machine reached via Phase 4 lateral movement.**

## Quick start

1. **P0 — Check current privilege state first (two commands, no tools).** `whoami /all`
   and `whoami /groups`. If already SYSTEM or DA → skip everything, go to `accessing-credentials`.
   If high-integrity local admin → skip to P4. Otherwise continue below.
2. **P1 — Local host enumeration.** Run Seatbelt + WinPEAS, read
   `whoami /priv`. Defines which path is safe and whether EDR bypass is needed.
3. If `SeImpersonatePrivilege` is present → **P3 Potato attacks** (fastest path
   to SYSTEM, no AD required).
4. If AD CS vulnerable templates found (from discovery D3) → **P2 AD CS abuse**
   (most impactful — yields DA TGT directly, skip straight to impact).
5. If medium-integrity local admin → **P4 UAC bypass** before LSASS.
6. Fallbacks: **P5 service misconfigs**, **P6 DLL hijacking**.
7. EDR blocking any tool → run `evading-defenses` ambient first, return here.

Per-step command templates: [reference/tool-commands.md](reference/tool-commands.md).
Full technique catalogue: [reference/techniques.md](reference/techniques.md).

---

## Phase — Privilege Escalation steps

### P0 — Current privilege state check (Critical — run BEFORE everything else)

**Goal: determine exactly what privileges we already have before running any tool.
If we are already SYSTEM or Domain Admin, skip this entire phase and go directly
to `accessing-credentials`. If we are high-integrity local admin, skip to P4 check.
Do not run WinPEAS or Seatbelt until P0 tells you it is needed.**

Run `whoami /all` and `whoami /groups` only — no disk writes, no tools dropped.

Check for these states in order:

| State detected | Action |
|---|---|
| `NT AUTHORITY\SYSTEM` in whoami | Skip entire phase → `accessing-credentials` immediately |
| Domain Admin / Enterprise Admin group membership | Skip entire phase → `accessing-credentials` immediately |
| `High Mandatory Level` + local Administrators member | Skip P1–P3 → go to P4 (UAC bypass) then `accessing-credentials` |
| `High Mandatory Level` + NOT local admin | Continue to P1 enumeration |
| `Medium Mandatory Level` + local Administrators member | Continue to P1 then P4 (UAC bypass needed) |
| `Medium Mandatory Level` + standard user | Continue to P1 (full escalation path needed) |
| `Low Mandatory Level` | Continue to P1 (most constrained — sandbox or restricted token) |

Save: `host.current_integrity_level`, `host.is_local_admin` (bool), `host.is_domain_admin` (bool), `host.already_system` (bool).

Cycle / next:
- Already SYSTEM → skip ALL of P1–P6 → `accessing-credentials`
- Already DA / EA → skip ALL of P1–P6 → `accessing-credentials`
- High-integrity local admin → skip P1–P3 → P4 then `accessing-credentials`
- Anything else → P1 (local host enumeration)

### P1 — Local host enumeration (Critical — run FIRST on every machine)

**Goal: define which escalation path is safe before running any noisy tool.
Check EDR before anything else.**

Run Seatbelt and WinPEAS. Read `whoami /all` for token privileges. Check AV/EDR
processes. Identifies the fastest safe path and gates all subsequent steps.

See `tool-commands.md § P1` for all commands.

Save: `host.av_edr`, `host.token_privileges`, `host.integrity_level`,
`host.local_admins`, `host.listening_ports`.

Cycle / next:
- EDR present → `evading-defenses` (AMSI + EDR bypass) BEFORE any further step
- `SeImpersonatePrivilege` found → P3 (Potato attacks) immediately
- `SeBackupPrivilege` found → flag for `accessing-credentials` (NTDS.dit read)
- `SeDebugPrivilege` found → flag for `accessing-credentials` (LSASS attach)
- Unquoted service path or weak binary perms found → P5
- Missing DLL in writable path found → P6

### P2 — AD CS abuse (ESC1–ESC8) (Critical)

**ESC1 = any domain user gets DA TGT in 3 commands. Most impactful single
privesc in modern AD. Run if D3 from discovery found vulnerable templates.**

Gate: only run if `ad.adcs_vulns` is non-empty (from discovery Phase B Step 19).

ESC1 (enrollee-supplied SAN): request cert with `/altname:administrator`,
convert to PFX, use Rubeus to get DA TGT. ESC8 (web enrollment): NTLM relay
to AD CS HTTP endpoint. See `tool-commands.md § P2`.

Save: `host.privesc_method = adcs`, `credentials.da_tgt`, `privesc.local_admin = true` (DA-equivalent access obtained).

Cycle / next:
- DA TGT obtained → skip to `achieving-impact` (Phase 7) directly
- Or inject TGT and use in `moving-laterally` (Phase 4)

### P3 — Token impersonation / Potato attacks (Critical)

**IIS, MSSQL, and WCF service accounts always have SeImpersonatePrivilege.
Instant SYSTEM with no AD interaction.**

Gate: `SeImpersonatePrivilege` confirmed in P1 `whoami /priv`.

OPSEC ladder — try in order, stop on first success:
1. **GodPotato** — widest OS support (Server 2012–2022, Win10–11)
2. **PrintSpoofer** — Win10 / Server 2019+
3. **RoguePotato** — requires attacker-controlled listener
4. **SweetPotato** — fallback

See `tool-commands.md § P3`.

Save: `host.privesc_method = potato`, `host.access_level = system`, `privesc.system_token = true`.

Cycle / next: SYSTEM achieved → `accessing-credentials` (LSASS dump + SAM + DPAPI)

### P4 — UAC bypass (Important)

**Required if running as medium-integrity local admin before LSASS dump.
Try fodhelper first (no binary drop); fall back to UACME method 61.**

Gate: `whoami /groups` shows `Medium Mandatory Level` AND user is in local
Administrators group.

Check UAC level via registry before attempting. fodhelper bypass uses only
registry writes (no binary drop, stealthy). UACME has 60+ methods.
See `tool-commands.md § P4`.

Save: `host.privesc_method = uac_bypass`, `host.integrity_level = high`, `privesc.local_admin = true`.

Cycle / next: high-integrity shell → `accessing-credentials` (LSASS dump)

### P5 — Unquoted service path & weak permissions (Important)

**Drop payload in a writable path segment or replace a writable service
binary. Service restart triggers execution as SYSTEM.**

Gate: only run if P1 enumeration found at least one of:
- PowerUp reported a vulnerable unquoted service path with a writable segment, OR
- `icacls` / `accesschk` confirmed the current user can write to a service binary.

If P1 found neither → skip P5, try P6.

Two sub-paths:
- **Unquoted path**: detect services with unquoted paths containing spaces;
  identify which path segment is writable; drop beacon binary there.
- **Weak binary permissions**: detect services whose binary is writable by
  current user; replace binary; restart service.

Requires service restart — either wait for next reboot or `sc stop/start` if
permissions allow. See `tool-commands.md § P5`.

Save: `host.privesc_method = service_abuse`, `host.access_level = system`, `privesc.system_token = true`.

Cycle / next: SYSTEM achieved → `accessing-credentials`

### P6 — DLL hijacking & sideloading (Important)

**Very common with legacy enterprise software. Process Monitor reveals exactly
which path Windows checks first.**

Gate: only run if P1 enumeration found at least one of:
- Robber.exe reported a missing DLL in a writable path, OR
- A writable directory appears earlier in `%PATH%` than the legitimate DLL location, OR
- Manual Process Monitor (`Result = NAME NOT FOUND` + `.dll`) confirmed a candidate.

If P1 found no DLL hijack candidate → skip P6; signal `partial` if P2–P5 also found nothing.

Detection: Robber.exe automated scan, or manual Process Monitor filter
(`Result = NAME NOT FOUND` + `.dll` extension). Verify writable path comes
before legitimate DLL location in search order. Drop crafted DLL.

See `tool-commands.md § P6`.

Save: `host.privesc_method = dll_hijack`, `host.access_level = service_account`, `privesc.local_admin = true` (if service runs as SYSTEM or local admin).

Cycle / next:
- Execution as SYSTEM / high-priv service account → `accessing-credentials`
- Domain service account obtained → `accessing-credentials` (Kerberoast that account)

---

## Preconditions

- `foothold.platform` is set (`windows` or `linux`). P2–P6 are Windows-specific. On Linux, run `sudo -l`, SUID scan, and cron checks instead.
- **P0 runs before anything else — no exceptions. P0 determines whether this phase is needed at all.**
- If `host.already_system=true` OR `host.is_domain_admin=true` → skip entire phase, go to `accessing-credentials`.
- If `host.is_local_admin=true` AND `host.current_integrity_level=high` → skip P1–P3, go directly to P4.
- Evaluator expects `privesc.local_admin=true` or `privesc.system_token=true` to mark phase done.
- `host.access_level` is below `local_admin` or `system` (otherwise skip this skill — P0 should have caught this).
- For P2: `ad.adcs_vulns` non-empty from `discovering-environment` Phase B Step 19.
- For P3: `host.token_privileges` contains `SeImpersonatePrivilege`.
- For P4: current process is medium-integrity AND user is in local Administrators — confirmed by P0.
- AMSI bypass + ETW patch loaded if PowerShell-based tools are used (ambient `evading-defenses`).

## Hard rules

- Always run P0 first. P0 is a read-only whoami check — two commands, no tools dropped.
  P0 determines whether this phase is needed at all and which path to take.
- If P0 shows SYSTEM or Domain Admin — STOP. Do not run P1. Go to `accessing-credentials`.
- If P0 shows high-integrity local admin — SKIP P1, P2, P3. Go directly to P4.
- Always run P1 before any exploitation step (unless P0 short-circuits above).
  Do not skip to a specific technique without the enumeration baseline.
- Follow the OPSEC ladder in P3 — never jump to a louder Potato variant
  without trying the quieter one first.
- P2 requires discovery D3 gate — never run Certify/certipy without confirmed
  vulnerable template from `ad.adcs_vulns`.
- Do not run EDR-triggering tools (Mimikatz-style) without `evading-defenses`
  completing first.
- One step per ability. Do not fuse P0 check with P1 enumeration, or P1 with any exploitation step.

## Stage goal (commit criteria)

**Evaluator checks:** `privesc.local_admin=true` OR `privesc.system_token=true`.
Both keys are set alongside the existing `host.access_level` / `host.integrity_level` keys.

Signal `success` when ANY of the following is true:

- `host.already_system = true` — set by P0 (already running as SYSTEM, no escalation needed).
- `host.is_domain_admin = true` — set by P0 (already DA/EA, no escalation needed).
- `privesc.system_token = true` — set by P3 (Potato), P5 (service abuse), P6 (DLL hijack).
- `privesc.local_admin = true` — set by P4 (UAC bypass to high integrity) or P2 (DA TGT obtained).

**P0 short-circuit:** if P0 sets `host.already_system=true` or `host.is_domain_admin=true`,
phase_done is set to `true` immediately and the evaluator routes to `accessing-credentials`
without running any other step. This applies whether we just landed here from discovery,
from lateral movement, or from any other phase.

These are the two keys the evaluator checks. Both map to the existing
`host.access_level` and `host.integrity_level` values — they are set at the
same time as those keys so nothing already in memory is replaced.

Signal `partial` if all techniques were attempted and blocked by EDR — pivot
to `evading-defenses`, then retry once.

## MITRE mapping for emitted abilities

| Step | Technique ID | Technique name                          | Tactic |
|------|--------------|-----------------------------------------|--------|
| P0   | T1033        | System Owner / User Discovery           | TA0007 |
| P1   | T1057 / T1082 | Process / System Info Discovery        | TA0007 |
| P2   | T1649        | AD CS Abuse (ESC1–ESC8)                 | TA0004 |
| P3   | T1134.001    | Token Impersonation / Potato Attacks    | TA0004 |
| P4   | T1548.002    | UAC Bypass                              | TA0004 |
| P5   | T1574.005 / T1574.010 | Service Path / Binary Hijack  | TA0004 |
| P6   | T1574.001 / T1574.002 | DLL Hijacking / Sideloading   | TA0004 |

## Pivot conditions (set `recommended_next`)

- P0: `host.already_system=true` OR `host.is_domain_admin=true` → `accessing-credentials` immediately (skip P1–P6).
- P0: `host.is_local_admin=true` AND `host.current_integrity_level=high` → P4 directly (skip P1–P3).
- P0: `host.is_local_admin=true` AND `host.current_integrity_level=medium` → P1 then P4.
- P0: standard user → P1 (full escalation path needed).
- P1: EDR detected → `evading-defenses` FIRST before any other step.
- P1: `SeImpersonatePrivilege` → P3 (skip P2 if AD CS not applicable).
- P1: `SeBackupPrivilege` → complete privesc, then `accessing-credentials` (NTDS read path).
- P2: DA TGT obtained → `achieving-impact` (skip `accessing-credentials` and `moving-laterally`).
- P2: TGT obtained, further pivoting needed → `moving-laterally`.
- P3 / P5: SYSTEM achieved → sets `privesc.system_token=true` → `accessing-credentials`.
- P4: high-integrity local admin achieved → sets `privesc.local_admin=true` → `accessing-credentials`.
- P6: service account obtained → sets `privesc.local_admin=true` (if SYSTEM/admin level) → `accessing-credentials`.
- All techniques blocked → `evading-defenses`; return here; if still blocked → `blocked`.
- After credential dump in `accessing-credentials` → `moving-laterally` toward DC.
- On every new machine from lateral movement → restart from P1.

## Evidence to capture

- Seatbelt / WinPEAS output → `<artifacts>/privesc/p1_enum.txt`.
- `whoami /all` → `<artifacts>/privesc/p1_whoami.txt`.
- AD CS cert + PFX → vault (`vault://P2-cert-####`); never plain artifact.
- Rubeus TGT (base64) → vault.
- Potato attack output → `<artifacts>/privesc/p3_potato.txt`.
- UACME output → `<artifacts>/privesc/p4_uac.txt`.
- One-line `finding` per successful technique, e.g.:
  - `"WORKSTATION07 — SeImpersonatePrivilege → GodPotato → SYSTEM"`
  - `"ESC1 template 'UserCert' → DA TGT for administrator@corp.local"`
  - `"SVC_BACKUP unquoted path writable → SYSTEM on DB01"`
