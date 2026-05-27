---
name: discovering-environment
description: Domain-aware network discovery skill. Starts from the foothold's subnet, sweeps for live assets via nmap, enumerates open ports and services, then — if DC-indicative ports (88/389/445/636/3268) are confirmed — launches a full progressive AD enumeration chain covering BloodHound collection, ACL/permission abuse, AD CS vulnerabilities, SMB shares, user/group/trust/GPO/delegation/DNS/Kerberoast/AS-REP, LAPS, MSSQL, Exchange, and printer/spooler discovery. Also handles non-domain hosts with standard port/service enumeration. Use as the primary discovery stage after a fresh foothold or whenever scope expands to a new subnet. Re-run Phase B on every new machine reached via lateral movement.
stage: discovery
agent: DiscoveryAgent
mitre_tactics: ["TA0007", "TA0006", "TA0008"]
default_opsec: moderate
ambient: false
tool_allowlist:
  # ---- recon backbone ----
  - nmap
  - masscan
  - ping
  - tracert
  - traceroute
  - ipconfig
  - ifconfig
  - ip
  - route
  - arp
  - netstat
  - ss
  # ---- package managers (nmap install only) ----
  - winget
  - choco
  - apt
  - apt-get
  - yum
  - dnf
  - brew
  # ---- non-domain service fingerprinting ----
  - netexec
  - smbclient
  # ---- domain enumeration (D1–D20, gate: DC confirmed) ----
  - sharphound
  - bloodhound-python
  - azurehound
  - powerview
  - adexplorer
  - adaclscanner
  - daclenum
  - certipy
  - certify
  - pspkiaudit
  - locksmith
  - snaffler
  - sauroneye
  - sharpshares
  - ldapsearch
  - rpcclient
  - enum4linux-ng
  - ldapdomaindump
  - windapsearch
  - adidnsdump
  - dnsx
  - nslookup
  - nltest
  - dig
  - rubeus
  - impacket-getuserspns
  - impacket-getnpusers
  - impacket-finddelegation
  - kerbrute
  - group3r
  - gpoddity
  - sharpgpo
  - roadtools
  - aadinternals
  - stormspotter
  - seatbelt
  - winpeas
  - powerupsql
  - sqlcmd
  - mailsniper
  - ruler
  - spoolsample
  - petitpotam
  - lapstoolkit
  - pingcastle
budget:
  max_tool_calls: 20
  max_wallclock_min: 15
---

# Discovering the AD / Environment

Build a factual, evidence-driven inventory of the network and — when a domain
controller is confirmed in scope — a complete Active Directory attack-surface
map covering all D1–D20 techniques from `reference/techniques.md`.

** START HERE every time you land on a new machine. Map before you attack.**

**Step 0 is mandatory on every machine — first foothold and every lateral-movement hop.**
**Phase A (Steps 1–7) runs after Step 0 confirms the network range.** Phase B (Steps 8–27) is
conditional: it fires only after Step 7 confirms a domain controller. Non-domain
hosts discovered in Step 5 receive the standard service-fingerprinting path
(Step 6) and are then routed to the appropriate lateral-movement skill.

**Core cycle from XMind Phase 4:**
Each new machine → **Step 0** (context check) → **Phase A** (network scan) → **Phase B**
(AD enumeration) → lateral move → repeat Step 0 on next machine.

Per-step command templates and parser hints: [reference/tool-commands.md](reference/tool-commands.md).
Full technique catalogue with tool selection guidance: [reference/techniques.md](reference/techniques.md).

---

## Step 0 — Machine context check (runs on EVERY machine, including lateral-movement hops)

**Goal: determine within seconds whether the new machine is domain-joined, which domain
it belongs to, who you are, and what privileges you hold — using only pre-installed
OS commands. No tools, no downloads.**

This is the mandatory first step on the initial foothold AND on every machine reached
via lateral movement (PTH, PTT, WMI, WinRM, etc.). The result decides which path to
take next.

### 0.1 — Identity & privilege

#### Windows (`cmd` or `psh`)

```
whoami
whoami /all
whoami /groups
whoami /priv
```

Parse:
- Current user and domain prefix (`DOMAIN\user` vs `HOSTNAME\user` vs `NT AUTHORITY\SYSTEM`).
- Group memberships: `Domain Admins`, `Enterprise Admins`, `Administrators`.
- Token privileges: `SeImpersonatePrivilege`, `SeDebugPrivilege`, `SeBackupPrivilege` —
  all are immediate escalation signals (see Phase 2).

#### Linux (`sh`)

```
id
```

### 0.2 — Domain membership check

#### Windows — quick domain check (`cmd`)

```
systeminfo | findstr /B /C:"Domain"
wmic computersystem get Name,Domain,Workgroup,PartOfDomain /value
```

Parse `PartOfDomain`:
- `TRUE` → machine is domain-joined; `Domain` field shows the FQDN. Continue to 0.3.
- `FALSE` → machine is in a workgroup (`WORKGROUP`). Save `host.domain_joined = false`;
  skip Phase B; apply workgroup pivot conditions at the bottom of this file.

#### Windows — alternate (PowerShell)

```
(Get-WmiObject Win32_ComputerSystem).PartOfDomain
(Get-WmiObject Win32_ComputerSystem).Domain
[System.DirectoryServices.ActiveDirectory.Domain]::GetCurrentDomain()
```

`GetCurrentDomain()` throws if not domain-joined — treat exception as `PartOfDomain = false`.

#### Linux (`sh`)

```
hostname
cat /etc/hosts
realm list 2>/dev/null || echo "realm not available"
```

`realm list` shows the domain name and enrollment status. If realm is absent, check
`/etc/sssd/sssd.conf` or `/etc/krb5.conf` for domain name.

### 0.3 — DC and domain detail (domain-joined only)

Run **only** if 0.2 confirmed `PartOfDomain = true`.

#### Windows (`cmd`)

```
echo %USERDOMAIN%
echo %LOGONSERVER%
nltest /dsgetdc:%USERDOMAIN%
net config workstation | findstr /i "domain\|logon"
```

`%LOGONSERVER%` gives the authenticating DC hostname directly.
`nltest /dsgetdc` returns the DC FQDN, site, and IP.

#### Windows (PowerShell)

```
[System.DirectoryServices.ActiveDirectory.Domain]::GetCurrentDomain() | select Name,DomainControllers
```

#### Linux (`sh`)

```
cat /etc/krb5.conf 2>/dev/null | grep -E "default_realm|kdc"
cat /etc/sssd/sssd.conf 2>/dev/null | grep -E "^domains|^ad_domain"
```

### 0.4 — OS, hostname, and network interfaces

#### Windows (`cmd`)

```
hostname
systeminfo | findstr /B /C:"OS Name" /C:"OS Version" /C:"System Type"
ipconfig /all
net localgroup administrators
```

#### Linux (`sh`)

```
uname -a
hostname -f
ip -o -4 addr show
cat /etc/os-release
```

### Decision tree after Step 0

```
PartOfDomain = TRUE
    └── Same domain as previous machine?
            YES → skip Step 8 (DC probe already done); proceed to Step 9 (BloodHound)
            NO  → new domain found → run full Phase A + Phase B from scratch
                  save new domain to session as additional scope

PartOfDomain = FALSE  (workgroup)
    └── Any domain hosts visible on network? (check ipconfig /all for DNS suffix)
            YES → scan for DCs in that domain (Phase A → Step 7 gate)
            NO  → establish-persistence on this host; wait for scope expansion
```

Save: `host.domain_joined` (bool), `host.domain_name`, `host.dc_hostname`,
`host.current_user`, `host.current_user_groups`, `host.token_privileges`.

Emit **one** ability with one stage containing the platform-appropriate commands above.

---

## Phase A — Network Discovery (always runs)

### Step 1 — Determine the local network range

Goal: discover the CIDR the foothold is attached to. Do **not** invent a range;
read it from the host.

- Windows (`cmd`): `ipconfig /all` + `route print` as cross-check.
- Linux / macOS (`sh`): `ip -o -4 addr show` + `ip route` (or `ifconfig -a` on BSD/macOS).

Emit **one** ability with one stage. Save: `network.cidr` (e.g. `192.168.1.0/24`).

### Step 2 — Confirm nmap is installed

Goal: prove `nmap` is on PATH before any scan is queued.

- Windows: `nmap --version` (fallback: `where nmap`).
- Linux / macOS: `nmap --version` (fallback: `command -v nmap`).

Emit **one** ability. Memory key: `recon.nmap_available` = bool.

### Step 3 — Install nmap if missing

Run **only** if Step 2 reported the binary absent. Use the host's native package
manager; never download from arbitrary URLs.

- Windows: `winget install -e --id Insecure.Nmap` (fallback: `choco install nmap -y`).
- Debian/Ubuntu: `sudo apt-get update && sudo apt-get install -y nmap`.
- RHEL/CentOS/Fedora: `sudo dnf install -y nmap` (or `yum`).
- macOS: `brew install nmap`.

Chain `nmap --version` at the end of the same stage to re-verify. Emit **one** ability.

### Step 4 — Host discovery / live-asset map

Goal: list live hosts in `network.cidr`. Stay inside the foothold's own subnet.
If the sweep returns zero hosts, re-run once with `-PR` (ARP) before giving up.

See `tool-commands.md § Step 4` for the exact nmap invocation and `.gnmap` parse logic.

Save: `network.live_hosts` = list of IPs + hostnames (where resolvable).

### Step 5 — Service / port enumeration on live hosts

Goal: learn what services are listening on every live host. `-sV` is mandatory —
it produces the service+version data that drives the DC gate in Step 7.
Non-privileged shells substitute `-sT` (TCP connect) for `-sS`.

See `tool-commands.md § Step 5` for the full nmap invocation and flag rationale.

Save: `network.services` (per-host `{port, proto, service, product, version}`).

### Step 6 — Targeted version & banner deepening

Run **per high-value port** found in Step 5. One ability stage per port type.
Pick the script template from `tool-commands.md § Step 6` that matches the port.

Save: `network.fingerprints.<ip>.<port>`.

### Step 7 — DC gate (pivot decision)

Inspect Step 5 / Step 6 output. A domain controller is confirmed when **at
least two** of the following hold on the same host:

- TCP/88 open (Kerberos)
- TCP/389 or TCP/636 open (LDAP / LDAPS)
- TCP/445 open **and** hostname matches `*DC*` / `*dc0*` / ends in `$`, OR SMB
  OS discovery reports a Server edition

**Gate passes** → set `network.has_domain_controller = true`, record
`network.dc_ip` and `network.dc_ports`; continue to Phase B.

**Gate fails** → set `network.has_domain_controller = false`; skip Phase B and
apply the non-domain pivot conditions below.

---

## Phase B — Active Directory Enumeration (runs only if DC gate passes)

**Re-run this entire phase from Step 8 on every new machine reached via lateral movement.**
Each step maps to one or more D-numbered techniques from `techniques.md`.
Emit **one** ability per step; steps run sequentially unless marked as parallelisable.

### Step 8 — Lightweight unauthenticated DC probe (D11)

Confirm DC identity, domain name, and SMB signing before any authenticated tool.
See `tool-commands.md § Step 8`.

Save: `domain.name`, `domain.dc_hostname`, `domain.functional_level`,
`domain.smb_signing_required`.

### Step 9 — BloodHound full collection (D1)

**Run on EVERY new machine. Reveals DA paths, ACL abuses, sessions, Kerberoastable accounts.**

Select the appropriate collector (SharpHound / bloodhound-python / azurehound)
based on foothold platform and credential state. Run BloodHound GUI queries after
ingest. See `tool-commands.md § Step 9`.

Save: `ad.bloodhound_zip` path. Feeds D2, D5, D7, D9, D10, D16.

Cycle / next:
- DA path found → `moving-laterally` (skip remaining steps)
- Kerberoastable accounts found → Step 16, then `accessing-credentials`
- GenericAll / WriteDACL found → Step 15, then `accessing-credentials`
- After lateral move to new machine → re-run from this step on that machine

### Step 10 — Domain Controller / FSMO role discovery (D11)

Enumerate all DCs and their FSMO roles. See `tool-commands.md § Step 10`.

Save: `domain.dcs` list with IPs and FSMO roles.

### Step 11 — Domain user & group enumeration (D6)

Dump all users, groups, and memberships. Flag privileged groups:
Domain Admins, Enterprise Admins, Schema Admins, Backup Operators, Account Operators.
See `tool-commands.md § Step 11`.

Save: `ad.users` count, `ad.groups` list, `ad.privileged_groups`.

### Step 12 — Password policy discovery (D15)

**Run BEFORE any password spraying. Abort spray if lockout threshold ≤ 3.**

Retrieve default and fine-grained password policies. Compute safe spray rate:
1 attempt per (ObservationWindow + 5 min buffer). See `tool-commands.md § Step 12`.

Save: `domain.password_policy` (lockout_threshold, observation_window_min,
min_length, complexity).

### Step 13 — DNS enumeration (D13)

Dump all AD-integrated DNS records to find hidden hosts and new subnets.
New segment found → pivot (tunneling) → restart Phase A in new segment.
See `tool-commands.md § Step 13`.

Save: `ad.dns_records`. Cross-reference with `network.live_hosts`.

### Step 14 — Forest & trust relationship mapping (D7)

Map all domain trusts and forest relationships. Flag bi-directional external trusts
as high-value cross-domain pivot paths. See `tool-commands.md § Step 14`.

Save: `domain.trusts` list with direction and type.

### Step 15 — ACL / permission abuse discovery (D2)

Query BloodHound data from Step 9 first; supplement with direct PowerView / daclenum
queries. Target rights: GenericAll, WriteDACL, WriteOwner, ForceChangePassword,
GenericWrite, AddMember, AllExtendedRights. See `tool-commands.md § Step 15`.

Save: `ad.acl_abuse_paths` list (source principal → target object → right).

Cycle / next:
- WriteDACL on domain object → `accessing-credentials` (grant self DCSync)
- GenericAll on group → add self to elevated group
- GenericWrite on computer → `moving-laterally` (RBCD)

### Step 16 — Kerberoastable & AS-REP roastable discovery (D9)

**Kerberoast requires one domain user. AS-REP roast requires zero credentials.**

Enumerate SPN accounts and DONT_REQ_PREAUTH accounts. Write hashes to artifact files
ready for Hashcat (mode 13100 for RC4 Kerberoast; mode 18200 for AS-REP).
See `tool-commands.md § Step 16`.

Save: `ad.kerberoastable_accounts`, `ad.asrep_roastable_accounts`.
Non-empty → immediately recommend `accessing-credentials`.

### Step 17 — Delegation discovery (D16)

Find all unconstrained, constrained, and RBCD-configured computer objects.
See `tool-commands.md § Step 17`.

Save: `ad.unconstrained_delegation`, `ad.constrained_delegation`, `ad.rbcd`.

### Step 18 — GPO enumeration (D10)

Enumerate all GPOs and their local-group mappings. Flag GPOs linked to OUs
containing DCs or high-value servers. See `tool-commands.md § Step 18`.

Save: `ad.gpos` list.

### Step 19 — AD CS vulnerability discovery (D3)

**ESC1 = any domain user can request a cert as DA. Most impactful single privesc in modern AD.**

Run certipy (Linux) or Certify (Windows). Check for ESC8 web enrollment.
See `tool-commands.md § Step 19`.

Save: `ad.adcs_vulns` list of template names + ESC class.
Non-empty → immediately recommend `escalating-privileges`
(ESC1/4/6/8) or `accessing-credentials` (ESC9/10/13).

### Step 20 — SMB & network share enumeration (D4)

**SYSVOL always readable. GPP cpassword = instant credential. Snaffler auto-classifies findings.**

Enumerate readable shares and spider for credential-bearing files.
Check SYSVOL for GPP cpassword values. See `tool-commands.md § Step 20`.

Save: `ad.shares` list with UNC paths. Flag credential-bearing extensions.
GPP cpassword found → `accessing-credentials` (GPP decrypt).

### Step 21 — Logged-on user & admin session hunting (D5)

**DA session on reachable host = steal token or ticket without cracking anything.**

Hunt for privileged sessions across the domain. Use `-Stealth` to reduce noise.
See `tool-commands.md § Step 21`.

Save: `ad.da_sessions` (hosts where DA or high-value accounts have active sessions).
DA session found → `moving-laterally` (token impersonation / PTT).

### Step 22 — Process & service discovery (D12)

Run on the foothold host and any host where Step 21 found a privileged session.
Check for AV/EDR products before running any further noisy tools.
See `tool-commands.md § Step 22`.

Save: `host.av_edr`, `host.da_processes`.
EDR detected → flag for `defense-evasion` (AMSI + EDR bypass) before continuing.

### Step 23 — Azure AD / hybrid enumeration (D14)

Run **only** if Step 8 or DNS records reveal an Azure AD Connect server, hybrid
join indicators, or an `*.onmicrosoft.com` UPN suffix. See `tool-commands.md § Step 23`.

Save: `ad.azure_tenant_id`, `ad.hybrid_join_hosts`, `ad.aad_connect_server`.

### Step 24 — LAPS & gMSA reader discovery (D20)

Find computers where the current user can read the LAPS password attribute and
identify gMSA accounts with readable managed passwords.
See `tool-commands.md § Step 24`.

Save: `ad.laps_readable_hosts`, `ad.gmsa_accounts`.
Non-empty → `accessing-credentials`.

### Step 25 — MSSQL enumeration (D17)

Run **only** if Step 5 / Step 6 found TCP/1433 or TCP/1434 on any host.
Discover accessible instances and crawl linked-server chains.
See `tool-commands.md § Step 25`.

Save: `ad.mssql_servers`, `ad.mssql_links`.
Linked server with `sysadmin` on remote instance → `moving-laterally`.

### Step 26 — Exchange / mail enumeration (D18)

Run **only** if Step 5 found TCP/443 with OWA banner, TCP/25, or TCP/587.
See `tool-commands.md § Step 26`.

Save: `ad.exchange_hosts`, `ad.mailbox_secrets_found` (bool).

### Step 27 — Printer & spooler discovery (D19)

Run **only** if Step 5 found TCP/445 + TCP/135 on non-DC hosts.
Hosts with spooler + unconstrained delegation (Step 17) → coercion targets.
See `tool-commands.md § Step 27`.

Save: `ad.spooler_enabled_hosts`.

---

## Preconditions

- `foothold.platform` is set (`windows`, `linux`, or `darwin`).
- `foothold.hostname` / `foothold.ip_address` populated by foothold resolution.
- Outbound ICMP / TCP to the foothold's own subnet is permitted by scope.
- For Phase B Steps 9–27: at least one set of domain credentials available in
  memory (`creds.domain_user` + `creds.domain_pass`), or the foothold is already
  domain-joined and running as a domain user.

## Hard rules

- One step per ability. Do not fuse steps.
- Never use a placeholder like `<target>` in `command_template` — substitute
  the actual CIDR / IP / hostname from memory or foothold context.
- Match the host: Windows abilities use executor `cmd` or `psh`; Linux/macOS
  use `sh` or `bash`. Do not ship `apt-get` to a Windows host or `winget` to Linux.
- Stay inside `network.cidr` for Phase A scanning. Do not widen to /16 or scan public ranges.
- Phase B Steps 9–27 must not run until Step 7 sets `network.has_domain_controller = true`.
- Do not queue Phase B tools during Phase A. The DC gate is not optional.
- Optional-priority techniques (D17, D18, D19, D20) only emit if the relevant
  service or condition is confirmed in earlier steps.
- Run AMSI bypass (`defense-evasion`) before any PowerShell-based tools in Phase B.

## Stage goal (commit criteria)

Signal `success` only when **all** hold:

- `host.domain_joined` populated and `host.current_user` / `host.token_privileges` saved (Step 0).
- `network.cidr` populated (Step 1).
- `recon.nmap_available == true` (Step 2, possibly after Step 3).
- `network.live_hosts` is a non-empty list (Step 4).
- `network.services` populated for every live host (Step 5).
- Step 7 has set `network.has_domain_controller` to `true` or `false`.
- If `true`: all Critical-priority Phase B steps (8, 9, 12, 15, 16, 19) have
  run and their memory keys are populated.

## MITRE mapping for emitted abilities

| Step | Technique ID          | Technique name                            | Tactic |
|------|-----------------------|-------------------------------------------|--------|
| 0    | T1033 / T1016         | Machine Context Check (identity + domain) | TA0007 |
| 1    | T1016                 | System Network Configuration Discovery    | TA0007 |
| 2–3  | T1518                 | Software Discovery / Install              | TA0007 |
| 4    | T1018                 | Remote System Discovery                   | TA0007 |
| 5    | T1046                 | Network Service Discovery                 | TA0007 |
| 6    | T1046                 | Network Service Discovery (deep)          | TA0007 |
| 7    | —                     | Gate / router (no MITRE action)           | —      |
| 8    | T1018                 | Domain Controller Discovery               | TA0007 |
| 9    | T1087.002             | BloodHound Collection                     | TA0007 |
| 10   | T1018                 | FSMO Role Discovery                       | TA0007 |
| 11   | T1087.002             | Domain User / Group Enumeration           | TA0007 |
| 12   | T1201                 | Password Policy Discovery                 | TA0007 |
| 13   | T1590.002             | DNS Enumeration                           | TA0007 |
| 14   | T1482                 | Forest & Trust Mapping                    | TA0007 |
| 15   | T1069.002             | ACL / Permission Abuse Discovery          | TA0007 |
| 16   | T1558.003 / T1558.004 | Kerberoast / AS-REP Roast Discovery       | TA0006 |
| 17   | T1558                 | Delegation Discovery                      | TA0007 |
| 18   | T1615                 | GPO Enumeration                           | TA0007 |
| 19   | T1649                 | AD CS Vulnerability Discovery             | TA0007 |
| 20   | T1135                 | SMB & Share Enumeration                   | TA0007 |
| 21   | T1033                 | Logged-on User / Admin Session Hunting    | TA0007 |
| 22   | T1057 / T1007         | Process / Service Discovery               | TA0007 |
| 23   | T1538                 | Azure AD / Hybrid Enumeration             | TA0007 |
| 24   | T1555                 | LAPS / gMSA Reader Discovery              | TA0006 |
| 25   | T1046                 | MSSQL Enumeration                         | TA0007 |
| 26   | T1087                 | Exchange / Mail Enumeration               | TA0007 |
| 27   | T1187                 | Printer / Spooler Discovery               | TA0007 |

## Pivot conditions (set `recommended_next`)

- Step 0: `PartOfDomain = FALSE` (workgroup) + domain hosts visible on network → Phase A to find DCs.
- Step 0: `PartOfDomain = FALSE` + no domain visible → `establishing-persistence` on this host.
- Step 0: `PartOfDomain = TRUE` + new domain (different from previous machine) → full Phase A + Phase B from scratch for new domain.
- Step 0: `PartOfDomain = TRUE` + same domain + DC already known → skip to Step 9 (BloodHound).
- Step 0: `SeImpersonatePrivilege` in token → flag for `escalating-privileges` (Potato attacks) before Phase B.
- BloodHound collected + DA path found → `moving-laterally` (skip remaining steps).
- Kerberoastable / AS-REP accounts found → `accessing-credentials` (offline crack first).
- AD CS ESC vulnerabilities found → `escalating-privileges` (certipy / certify abuse).
- ACL abuse paths to DA found → `accessing-credentials` (DCSync / ForceChangePassword).
- DA session found on reachable host (Step 21) → `moving-laterally` (token impersonation / PTT).
- Unconstrained delegation + spooler enabled (Steps 17 + 27) → `moving-laterally` (coercion).
- LAPS / gMSA readable by current user → `accessing-credentials`.
- GPP cpassword found in SYSVOL (Step 20) → `accessing-credentials` (GPP decrypt).
- Azure AD Connect / MSOL account found (Step 23) → `moving-laterally` (cloud).
- No DC confirmed, RDP / SMB open on workstation → `moving-laterally`.
- No DC, no obviously vulnerable service → `establishing-persistence` on current foothold.
- nmap cannot be installed (no admin, no package manager) → `blocked`; escalate to human.
- EDR detected in Step 22 → `defense-evasion` (AMSI + EDR bypass) before continuing Phase B.

## Evidence to capture

- Raw nmap output: `-oA` to `<artifacts>/<step>/nmap_*.{nmap,gnmap,xml}`.
- BloodHound zip: `<artifacts>/bloodhound/<timestamp>_BloodHound.zip`.
- All tool output files: stored under `<artifacts>/<step_name>/`.
- Parsed live-host list and per-host service map merged into session memory.
- One-line `finding` per high-value discovery with `raw_output_ref` pointer, e.g.:
  - `"10.0.0.5 — ESC1 vulnerable template 'UserCert' — certipy"`
  - `"10.0.0.12:445 SMB Windows Server 2019 signing=disabled"`
  - `"svc_sql — Kerberoastable SPN: MSSQLSvc/db01.corp.local:1433 — RC4"`
  - `"da_user — active session on WORKSTATION07 — Invoke-UserHunter"`
  - `"SYSVOL Groups.xml — cpassword found — domain-wide GPO"`
