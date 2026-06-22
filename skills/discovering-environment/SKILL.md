---
name: discovering-environment
description: Domain-aware network discovery skill. Error-safe — every step has try/catch with fallback chain, never aborts on single failure. Starts from the foothold's subnet, sweeps for live assets via nmap, enumerates open ports and services, then — if DC-indicative ports (88/389/445/636/3268) are confirmed — launches a full progressive AD enumeration chain covering BloodHound collection, ACL/permission abuse, AD CS vulnerabilities, SMB shares, user/group/trust/GPO/delegation/DNS/Kerberoast/AS-REP, LAPS, MSSQL, Exchange, and printer/spooler discovery. Also handles non-domain hosts with standard port/service enumeration. Use as the primary discovery stage after a fresh foothold or whenever scope expands to a new subnet. Re-run Phase B on every new machine reached via lateral movement.
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

**Goal: determine platform, domain membership, current identity, and privileges
using only pre-installed OS commands. No tools, no downloads. This shapes
every decision in Phase A and Phase B.**

Mandatory on the initial foothold AND on every machine reached via lateral
movement. Run the correct platform branch below, then follow the decision tree.

---

### 0-A  Windows foothold

#### 0-A.1 Identity and privileges

```cmd
whoami
whoami /all
whoami /groups
whoami /priv
```

Parse:
- User format `DOMAIN\user` → domain account. `HOSTNAME\user` → local account.
- Groups: note `Domain Admins`, `Enterprise Admins`, `Administrators`.
- Privileges: `SeImpersonatePrivilege`, `SeDebugPrivilege`, `SeBackupPrivilege`
  are immediate escalation signals → flag for Phase 2.

#### 0-A.2 Domain membership check

```cmd
systeminfo | findstr /B /C:"Domain"
wmic computersystem get Name,Domain,Workgroup,PartOfDomain /value
```

PowerShell alternative:
```powershell
(Get-WmiObject Win32_ComputerSystem).PartOfDomain
(Get-WmiObject Win32_ComputerSystem).Domain
```

**Parse `PartOfDomain`:**

| Value | Meaning | Next action |
|-------|---------|-------------|
| `TRUE` | Windows host is domain-joined | Save `host.domain_joined=true`, `host.domain=<Domain>`. Run 0-A.3. |
| `FALSE` | Windows host is in a workgroup | Save `host.domain_joined=false`. Skip 0-A.3. Run Phase A — scan may find domain hosts on the wire. |

#### 0-A.3 DC and domain detail (domain-joined Windows only)

Run only when `PartOfDomain=TRUE`.

```cmd
echo %USERDOMAIN%
echo %LOGONSERVER%
nltest /dsgetdc:%USERDOMAIN%
net config workstation | findstr /i "domain\|logon"
```

PowerShell:
```powershell
[System.DirectoryServices.ActiveDirectory.Domain]::GetCurrentDomain() | select Name,DomainControllers
```

Save: `host.dc_hostname` (from `%LOGONSERVER%`), `host.domain_name`.

#### 0-A.4 OS, hostname, interfaces

```cmd
hostname
systeminfo | findstr /B /C:"OS Name" /C:"OS Version" /C:"System Type"
ipconfig /all
net localgroup administrators
```

---

### 0-B  Linux foothold

#### 0-B.1 Identity

```bash
id
whoami
```

#### 0-B.2 Domain membership check

```bash
# Primary check — realmd / sssd
realm list 2>/dev/null

# Fallback 1 — Kerberos config
grep -E "default_realm|kdc" /etc/krb5.conf 2>/dev/null

# Fallback 2 — SSSD config
grep -E "^domains|^ad_domain" /etc/sssd/sssd.conf 2>/dev/null

# Fallback 3 — hostname hint
hostname -f 2>/dev/null
```

**Parse:**

| Condition | Meaning | Next action |
|-----------|---------|-------------|
| `realm list` shows enrolled domain | Linux host is domain-joined | Save `host.domain_joined=true`, `host.domain=<realm>`. Run 0-B.3. |
| All checks return empty | Linux host is NOT domain-joined | Save `host.domain_joined=false`. Skip 0-B.3. Run Phase A — scan may find domain hosts on the wire. |

> **Key point:** a Linux host that is NOT domain-joined still runs the full
> Phase A network scan. Domain-joined Windows hosts may be on the same subnet
> and can be probed via anonymous LDAP / SMB / RPC in Step 7 even without
> credentials.

#### 0-B.3 DC and domain detail (domain-joined Linux only)

Run only when `realm list` confirmed enrollment.

```bash
cat /etc/krb5.conf 2>/dev/null | grep -E "default_realm|kdc"
cat /etc/sssd/sssd.conf 2>/dev/null | grep -E "^domains|^ad_domain"
```

Save: `host.domain_name`, `host.dc_hostname` (from kdc= line if present).

#### 0-B.4 OS, hostname, interfaces

```bash
uname -a
hostname -f
ip -o -4 addr show
cat /etc/os-release
```

---

### Decision tree after Step 0

```
Windows host
│
├── PartOfDomain = TRUE
│     └── Same domain as previous machine?
│           YES → skip Step 8 (DC probe done); jump to Step 9 (SharpHound.exe on Windows)
│           NO  → new domain; run full Phase A + Phase B from scratch
│
└── PartOfDomain = FALSE (workgroup)
      └── Phase A scan will find any domain hosts on the wire
            DC ports found in Step 7? → Phase B with anonymous + any creds
            No DC found?             → non-domain pivot conditions (bottom of file)

Linux host
│
├── domain_joined = TRUE
│     └── Same domain as previous machine?
│           YES → skip Step 8; jump to Step 9 (bloodhound-python from Linux)
│           NO  → new domain; run full Phase A + Phase B from scratch
│
└── domain_joined = FALSE
      └── Phase A scan finds live hosts
            Any host with ports 88+389/636? → Step 7 gate passes
                Domain-joined Windows hosts on subnet:
                  probe via anonymous LDAP, SMB null session, RPC,
                  enum4linux-ng → collect domain name, DC IP, basic info
                  → Phase B with whatever is available
            No DC ports found? → non-domain pivot conditions
```

Save: `host.platform` (`windows`/`linux`), `host.domain_joined` (bool),
`host.domain_name`, `host.dc_hostname`, `host.current_user`,
`host.current_user_groups`, `host.token_privileges`.

Emit **one** ability with one stage using the platform-appropriate commands above.

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

**Two paths — the agent MUST choose one based on `recon.nmap_available`:**

- `recon.nmap_available = true` → use nmap sweep (see `tool-commands.md § Step 4 — nmap`).
  Flags: `-sn -PE -PS21,22,80,443,445,3389 -oA`. Target MUST be the full CIDR (e.g. `192.168.1.0/24`),
  never a single IP. If sweep returns zero hosts, re-run once with `-PR` (ARP) before giving up.

- `recon.nmap_available = false` → use PowerShell fallback (see `tool-commands.md § Step 4 — PS-fallback`).
  Uses `arp -a` cache + `Test-Connection` ICMP (500ms timeout) + TCP/445 fallback for ICMP-blocked hosts.
  Covers the full /24 range. Writes `live_hosts.json` (JSON array) to `temp/bas/`.

Both paths MUST write `network.live_hosts` as a JSON array of IP strings.
Never save as raw text or `Out-String` blob — downstream steps iterate this list.

Save: `network.live_hosts` = JSON array of IP strings. Write `temp/bas/live_hosts.txt` (one IP per line).

### Step 5 — Service / port enumeration on live hosts

Goal: learn what services are listening on every live host.
`network.live_hosts` MUST be a non-empty list before this step runs.

**Two paths — the agent MUST choose one based on `recon.nmap_available`:**

- `recon.nmap_available = true` → nmap TCP connect scan with version detection
  (see `tool-commands.md § Step 5 — nmap`).
  Use `-sT` (TCP connect, works without root) + `-sV` + `-Pn` + `-p` covering all DC indicator ports
  plus common services. Target MUST be `-iL <LIVE_HOSTS_FILE>`, not a raw CIDR.
  `-sV` is mandatory — it produces service+version data that feeds the DC gate in Step 7.

- `recon.nmap_available = false` → PowerShell TcpClient scan
  (see `tool-commands.md § Step 5 — PS-fallback`).
  Covers all DC indicator ports (88, 389, 445, 636, 3268, 3269) plus common services
  (80, 443, 8080, 135, 1433, 5985, 5986, 22, 3389). Writes `services.json` to `temp/bas/`.

Both paths MUST write `network.services` as a JSON dict `{ip: [{port, proto, service}]}`.
Never save as raw `IP:PORT:OPEN` text lines — downstream routing iterates this structure.

Save: `network.services` = JSON dict `{ip: [{port, proto, service}]}`.

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
**This is the FIRST step of Phase B. It runs immediately after the DC gate passes.**

**Collector selection — MANDATORY, choose based on `host.platform`:**

| `host.platform` | Tool | Binary name | Why |
|---|---|---|---|
| `windows` | SharpHound.exe | `SharpHound.exe` | Windows-native collector. Runs on the domain-joined target via agent.exe. |
| `linux` | bloodhound-python | `bloodhound-python` | Linux collector. Runs from attacker machine. Requires domain creds + DC reachable. |
| `windows` (AV blocks SharpHound) | bloodhound-python | `bloodhound-python` | Fallback only. Run from Linux pivot or attacker box. |
| Azure / hybrid | azurehound | `azurehound` | Only when D14 confirmed Azure AD Connect. |

**CRITICAL — never confuse these:**
- `SharpHound.exe` is the collector binary that produces the zip file.
- `BloodHound` is the GUI server that ingests the zip. It does NOT run on the target.
- On a Windows target: run `SharpHound.exe`. NEVER run `bloodhound` or `bloodhound.py` on Windows.

See `tool-commands.md § Step 9` for all commands.

Run BloodHound GUI queries after ingest. See `tool-commands.md § Step 9`.

Save: `ad.bloodhound_zip` path. Feeds D2, D5, D7, D9, D10, D16.

Cycle / next:
- DA path found → `moving-laterally` (skip remaining steps)
- Kerberoastable accounts found → Step 16, then `accessing-credentials`
- GenericAll / WriteDACL found → Step 15, then `accessing-credentials`
- After lateral move to new machine → re-run from this step on that machine

### Step 10 — Domain Controller / FSMO role discovery (D11)

Enumerate all DCs and their FSMO roles.
Windows: native .NET `[DirectorySearcher]` + `[System.DirectoryServices.ActiveDirectory.Domain]`.
Linux: `ldapsearch`. See `tool-commands.md § Step 10`.

Save: `domain.dcs` list with IPs and FSMO roles.

### Step 11 — Domain user & group enumeration (D6)

Dump all users, groups, and memberships. Flag privileged groups:
Domain Admins, Enterprise Admins, Schema Admins, Backup Operators, Account Operators.
Windows: `[DirectorySearcher]` with LDAP filters — no AD module or PowerView needed.
Linux: `ldapsearch`. See `tool-commands.md § Step 11`.

Save: `ad.users` count, `ad.groups` list, `ad.privileged_groups`.

### Step 12 — Password policy discovery (D15)

**Run BEFORE any password spraying. Abort spray if lockout threshold ≤ 3.**

Windows: read `lockoutThreshold` and `lockoutObservationWindow` directly from domain
object via `[DirectoryEntry]` — no AD module needed. Linux: `ldapsearch` on domain root.
Compute safe spray rate: 1 attempt per (ObservationWindow + 5 min buffer).
See `tool-commands.md § Step 12`.

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

Query BloodHound data from Step 9 first (zero network traffic).
Windows: read `nTSecurityDescriptor` via `[DirectoryEntry].ObjectSecurity.Access` —
no PowerView or AD module needed. Linux: `ldapsearch` + `daclenum.py`.
Target rights: GenericAll, WriteDACL, WriteOwner, ForceChangePassword, AllExtendedRights.
See `tool-commands.md § Step 15`.

Save: `ad.acl_abuse_paths` list (source principal → target object → right).

Cycle / next:
- WriteDACL on domain object → `accessing-credentials` (grant self DCSync)
- GenericAll on group → add self to elevated group
- GenericWrite on computer → `moving-laterally` (RBCD)

### Step 16 — Kerberoastable & AS-REP roastable discovery (D9)

**Kerberoast requires one domain user. AS-REP roast requires zero credentials.**

Windows enumeration: `[DirectorySearcher]` with LDAP filters
`(servicePrincipalName=*)` and `(userAccountControl:1.2.840.113556.1.4.803:=4194304)`.
Hash extraction: Rubeus (Windows) or impacket-GetUserSPNs (Linux) — no native equivalent.
Linux enumeration: `ldapsearch`. See `tool-commands.md § Step 16`.

Save: `ad.kerberoastable_accounts`, `ad.asrep_roastable_accounts`.
Non-empty → immediately recommend `accessing-credentials`.

### Step 17 — Delegation discovery (D16)

Find unconstrained, constrained, and RBCD objects.
Windows: `[DirectorySearcher]` with UAC bit filters
`userAccountControl:1.2.840.113556.1.4.803:=524288` (unconstrained)
and `msDS-AllowedToDelegateTo=*` (constrained).
Linux: `ldapsearch`. See `tool-commands.md § Step 17`.

Save: `ad.unconstrained_delegation`, `ad.constrained_delegation`, `ad.rbcd`.

### Step 18 — GPO enumeration (D10)

Enumerate all GPOs and local-group mappings via LDAP query on
`CN=Policies,CN=System,<BASE_DN>`. Check SYSVOL with native `findstr` /
`Get-ChildItem` for GPP cpassword values — no third-party tool needed.
Linux: `ldapsearch`. See `tool-commands.md § Step 18`.

Save: `ad.gpos` list.

### Step 19 — AD CS vulnerability discovery (D3)

**ESC1 = any domain user can request a cert as DA. Most impactful single privesc in modern AD.**

Windows: `[DirectorySearcher]` on `CN=Certificate Templates` to enumerate
`msPKI-Certificate-Name-Flag` — identifies ESC1 candidates without Certify.
Certify/certipy used for full ESC check and actual exploitation (no native equivalent).
Linux: `ldapsearch` + certipy. See `tool-commands.md § Step 19`.

Save: `ad.adcs_vulns` list of template names + ESC class.
Non-empty → immediately recommend `escalating-privileges`
(ESC1/4/6/8) or `accessing-credentials` (ESC9/10/13).

### Step 20 — SMB & network share enumeration (D4)

**SYSVOL always readable. GPP cpassword = instant credential.**

Windows: native `net view` + `findstr /S /I cpassword` on SYSVOL (no tools needed).
PowerShell `Get-ChildItem` + `Select-String` for credential files.
Snaffler used only if AMSI/EDR bypassed. Linux: `smbclient` + `netexec`.
See `tool-commands.md § Step 20`.

Save: `ad.shares` list with UNC paths. Flag credential-bearing extensions.
GPP cpassword found → `accessing-credentials` (GPP decrypt).

### Step 21 — Logged-on user & admin session hunting (D5)

**DA session on reachable host = steal token without cracking anything.**

Windows: native `query user /server`, `wmic computersystem get username`,
and `[DirectorySearcher]` for DA members — no PowerView.
Linux: `netexec smb --loggedon-users`. See `tool-commands.md § Step 21`.

Save: `ad.da_sessions` (hosts where DA or high-value accounts have active sessions).
DA session found → `moving-laterally` (token impersonation / PTT).

### Step 22 — Process & service discovery (D12)

Run on foothold and any host where Step 21 found a privileged session.
Windows: `whoami /all`, `Get-WmiObject AntiVirusProduct`, `Get-MpComputerStatus`,
`Get-Process`, `Get-Service`, registry read — all native, zero EDR alerts.
Seatbelt/WinPEAS only after AMSI bypass. See `tool-commands.md § Step 22`.

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

- **Every command wraps in try/catch** — write `ERROR: <step> <reason>` and continue.
  Never abort a phase on a single step failure. Partial data beats no data.
- **Fallback chain for every Windows LDAP step:**
  1. Authenticated `[DirectorySearcher]` / `[DirectoryEntry]`
  2. Anonymous bind of same query
  3. netexec equivalent
  If all three fail → write `ERROR:` and move to next step.
- **Fallback chain for every Linux LDAP step:**
  1. Authenticated `ldapsearch`
  2. Anonymous `ldapsearch`
  3. netexec equivalent
  If all three fail → write `ERROR:` and move to next step.
- **Step 4 scans ALL CIDRs** in `network.cidrs` — multi-homed hosts have multiple subnets.
- **Step 4 is alive-check only** — no port scanning. Port scan is Step 5 on confirmed hosts.
- **nmap Step 4 target = full CIDR** (e.g. `192.168.1.0/24`) never a single IP.
- **nmap Step 5 = `-sT -Pn -iL live_hosts.txt`** — never raw CIDR, never skip `-Pn`.
- **PowerShell ping timeout = 500ms** — 100ms misses most hosts.
- **Windows LDAP primary = `[ADSI]` / `[DirectorySearcher]`** (built-in .NET, no install).
  PowerView only as last fallback when no LDAP filter can express the query.
- **Linux LDAP primary = `ldapsearch`** (built-in on Kali).
  certipy / daclenum / bloodhound-python only for tasks with no ldapsearch path.
- **Step 9 on Windows = `SharpHound.exe`**. Step 9 on Linux = `bloodhound-python`.
- Every step writes evidence to `<ARTIFACTS>/` even on partial success.
- One step per ability. Do not fuse steps.
- Stay inside `network.cidr` for Phase A. Do not widen to /16.
- Phase B Steps 9–27 must not run until Step 7 sets `network.has_domain_controller=true`.

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
- EDR detected in Step 22 → `defense-evasion` (AMSI + EDR bypass) before continuing Phase B.

## Evidence to capture

- Raw nmap output: `-oA` to `<artifacts>/<step>/nmap_*.{nmap,gnmap,xml}`.
- SharpHound zip (Windows): `<artifacts>/bloodhound/<timestamp>_BloodHound.zip` — produced by SharpHound.exe.
- bloodhound-python output (Linux fallback): `<artifacts>/bloodhound/*.json` files — ingest manually into BloodHound GUI.
- All tool output files: stored under `<artifacts>/<step_name>/`.
- Parsed live-host list and per-host service map merged into session memory.
- One-line `finding` per high-value discovery with `raw_output_ref` pointer, e.g.:
  - `"10.0.0.5 — ESC1 vulnerable template 'UserCert' — certipy"`
  - `"10.0.0.12:445 SMB Windows Server 2019 signing=disabled"`
  - `"svc_sql — Kerberoastable SPN: MSSQLSvc/db01.corp.local:1433 — RC4"`
  - `"da_user — active session on WORKSTATION07 — Invoke-UserHunter"`
  - `"SYSVOL Groups.xml — cpassword found — domain-wide GPO"`
