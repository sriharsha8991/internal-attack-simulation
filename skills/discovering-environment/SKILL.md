---
name: discovering-environment
description: Domain-aware network discovery skill. Goal-driven — the AI decides what commands to run at runtime based on the platform, available tools, and what memory already contains. Covers Phase A (network mapping) and Phase B (full AD enumeration). Re-run on every new machine reached via lateral movement.
stage: discovery
agent: DiscoveryAgent
mitre_tactics: ["TA0007", "TA0006", "TA0008"]
default_opsec: moderate
ambient: false
tool_allowlist:
  - nmap
  - netexec
  - sharphound
  - bloodhound-python
  - certipy
  - ldapsearch
  - smbclient
  - rubeus
  - impacket-getuserspns
  - impacket-getnpusers
  - daclenum
  - adidnsdump
budget:
  max_tool_calls: 20
  max_wallclock_min: 15
---

# Discovering the environment

## Purpose

Build a complete, evidence-driven picture of the network and — when a domain is
present — the full Active Directory attack surface. The AI generates the right
commands at runtime based on what it finds in memory and on the target.

**Never hardcode commands. Generate them based on:**
- `host.platform` in memory (`windows` / `linux`)
- `recon.nmap_available` in memory (use PowerShell/bash fallback if false)
- `domain.base_dn` in memory (substitute into every LDAP query)
- `network.dc_ip` in memory (connect to the right DC)
- `creds.domain_user` / `creds.domain_pass` in memory (authenticate LDAP queries)
- What tools are confirmed present via `tool_allowlist`

---

## Step 0 — Machine context (every machine, every lateral-movement hop)

**Goal:** Know exactly where you are before doing anything else.
Use only pre-installed OS commands. No tools. No downloads.

**What to determine:**
- Current user identity and privilege level
- Whether the host is domain-joined and which domain
- All network adapters and their CIDRs (detect multi-homed hosts)
- AV/EDR products running
- Token privileges worth flagging

**What to save:**
- `host.platform` — windows / linux / darwin
- `host.domain_joined` — bool (not string)
- `host.domain_name` — FQDN if joined
- `host.current_user` — identity string
- `host.token_privileges` — list of privilege names, not raw text
- `host.av_edr` — list of product names found
- `host.integrity_level` — low / medium / high / system
- `network.cidr` — primary adapter CIDR
- `network.cidrs` — list of ALL adapter CIDRs (one per adapter)

**Success:** all keys above are populated with correct types.

**Routing after Step 0:**
- Already SYSTEM or Domain Admin → skip this phase, go to `accessing-credentials`
- High-integrity local admin → note, continue Phase A
- `SeImpersonatePrivilege` in token → flag for `escalating-privileges` after Phase B
- `host.av_edr` non-empty → note for `evading-defenses` before noisy tools

---

## Phase A — Network Discovery

### Step 1 — Network ranges

**Goal:** Know every subnet the foothold touches.

**What to determine:** CIDR for every non-loopback adapter.
Derive subnet from IP + prefix length. Multi-homed hosts = multiple CIDRs to scan.

**What to save:** `network.cidr` (primary), `network.cidrs` (full list).

**Step 4 must sweep every CIDR in `network.cidrs`.**

---

### Step 2 — Check nmap availability

**Goal:** Know which scan path to take before starting any sweep.

**What to determine:** Is nmap on the PATH?

**What to save:** `recon.nmap_available` = bool.

If false → set `recon.ps_scanner_mode = true`. All subsequent scan steps
use native OS commands (PowerShell TcpClient on Windows, bash /dev/tcp on Linux).

---

### Step 3 — Install nmap if missing and installation is possible

**Goal:** Get nmap working if the environment allows it.

**Run only if:** `recon.nmap_available = false` AND installation is feasible.

**If installation fails or is blocked:** set `recon.ps_scanner_mode = true` and
move forward — do not block Phase A on nmap availability.

**What to save:** updated `recon.nmap_available`.

---

### Step 4 — Host alive sweep (Stage 1 — who is alive)

**Goal:** Find every live host across all subnets. No port scanning here.

**Constraints:**
- Sweep every CIDR in `network.cidrs`, not just the primary one
- Host-alive check only — no port enumeration in this step
- If nmap available: ping sweep with TCP probes as backup
- If nmap not available: ARP cache first, then ICMP ping (500ms minimum timeout),
  then TCP/445 for hosts that block ICMP
- If a sweep returns zero hosts, retry with ARP-level probe before giving up

**What to save:**
- `network.live_hosts` — JSON array of IP strings, not text blob
- Write evidence to `temp/bas/live_hosts.txt`

**Success:** `network.live_hosts` is a non-empty JSON array.

---

### Step 5 — Port and service scan (Stage 2 — what is running)

**Goal:** Know what services every live host exposes.

**Constraints:**
- Scan only hosts in `network.live_hosts` — never re-sweep raw CIDR
- Skip host-alive re-check (Step 4 already confirmed them)
- Always include DC indicator ports: 88, 389, 445, 636, 3268, 3269
- Include common service ports: 21, 22, 25, 53, 80, 135, 443, 1433, 3389, 5985, 5986, 8080, 8443
- Include version detection when nmap is available
- If nmap not available: use TcpClient (Windows) or bash /dev/tcp (Linux) per port

**What to save:**
- `network.services` — JSON dict `{ip: [{port, proto, service}]}`, not raw text lines
- Write evidence to `temp/bas/services.json`

**Success:** `network.services` is populated for every host in `network.live_hosts`.

---

### Step 6 — Service fingerprinting (optional deepening)

**Goal:** Get version and OS details on high-value ports found in Step 5.

**When to run:** only for ports that matter for routing decisions:
- Port 445 → SMB OS version, signing required?
- Port 389/636 → LDAP rootDSE attributes
- Port 3389 → NLA required?
- Port 80/443 → what application is serving?

**Approach:** use the simplest native command that answers the question.
`curl -sI`, `net view`, `qwinsta`, raw LDAP rootDSE read via .NET or ldapsearch.
nmap scripts only as fallback if native commands are insufficient.

**What to save:** `network.fingerprints.<ip>.<port>` per host/port probed.

---

### Step 7 — DC gate (routing decision)

**Goal:** Determine whether a domain controller is in scope and route accordingly.

**DC confirmation criteria** (need at least two on the same host):
- TCP/88 open (Kerberos)
- TCP/389 or TCP/636 open (LDAP)
- TCP/445 open and server reports a DC-like hostname or Server OS

**What to save:**
- `network.has_domain_controller` — bool
- `recon.ad_present` — same value (evaluator checks both independently)
- `network.dc_ip` — IP of confirmed DC, or empty string
- `domain.base_dn` — derived from LDAP rootDSE if DC found
- `recommended_next` — `"ad-enumeration"` if DC found, `"escalating-privileges"` if not
- `phase_done` — false if DC found (Phase B must run), true if no DC found
- Write `temp/bas/dc_gate.json` always

**If DC found:** emit `recommended_next = "ad-enumeration"` immediately.
Do not generate more Phase A steps. The planner acts on this signal.

**If no DC found:** `phase_done = true`, `recommended_next = "escalating-privileges"`.

---

## Phase B — Active Directory Enumeration

**Triggers when:** `recommended_next = "ad-enumeration"` is in memory.
**Starts immediately. Do not wait for phase_done on Phase A.**

For every LDAP query in this phase:
- Substitute `domain.base_dn`, `network.dc_ip`, `creds.domain_user`,
  `creds.domain_pass` from memory — never hardcode these values
- Windows: use `[DirectorySearcher]` / `[ADSI]` as primary, anonymous bind as fallback,
  netexec as last resort
- Linux: use `ldapsearch` as primary, anonymous bind as fallback, netexec as last resort
- Every step: try/catch (Windows) or `|| fallback` (Linux)
- Write `ERROR: <step> <reason>` and continue — never abort on one failure

---

### Step 8 — Lightweight DC probe (D11)

**Goal:** Confirm DC identity and read domain metadata without credentials.

**What to determine:** domain name, DC FQDN, functional level, SMB signing status.
Use unauthenticated LDAP rootDSE read. Native .NET on Windows, ldapsearch on Linux.

**What to save:** `domain.name`, `domain.dc_hostname`, `domain.functional_level`,
`domain.smb_signing_required`.

---

### Step 9 — BloodHound collection (D1) — FIRST step of Phase B

**Goal:** Collect the complete AD relationship graph in one operation.
This feeds every subsequent Phase B step — run it first.

**Tool selection — determined by `host.platform` in memory:**
- `host.platform = windows` → `SharpHound.exe` on the target host
- `host.platform = linux` → `bloodhound-python` from the attacker machine
- SharpHound.exe blocked by AV → bloodhound-python as fallback from Linux pivot
- Azure/hybrid indicators in memory → azurehound in addition

**SharpHound.exe** = Windows collector that produces the zip.
**BloodHound** = GUI that ingests the zip. Never run BloodHound on the target.

**What to save:** `ad.bloodhound_zip` = full path to produced zip file.

**Routing after Step 9:**
- DA path found in graph → `moving-laterally` (skip remaining Phase B steps)
- Kerberoastable accounts in graph → continue to Step 16
- GenericAll / WriteDACL paths → continue to Step 15

---

### Step 10 — DC and FSMO discovery (D11)

**Goal:** Map all domain controllers and their roles.

**What to determine:** hostnames, IPs, and which FSMO roles each DC holds.
Use LDAP filter for computer objects with DC UAC bit. Complement with native
domain topology API on Windows or nltest.

**What to save:** `domain.dcs` — list with hostname, IP, and roles per DC.

---

### Step 11 — User and group enumeration (D6)

**Goal:** Know every domain user, their attributes, and group memberships.

**What to determine:**
- All users with: samaccountname, adminCount, UAC flags (disabled, no-preauth, SPN set)
- All privileged groups and their members
- Kerberoastable users (have SPN, not disabled, not krbtgt)
- AS-REP roastable users (DONT_REQ_PREAUTH UAC flag set)

**LDAP filters the AI should generate based on these goals — not hardcoded, but
the AI knows the relevant LDAP attributes and UAC bit values and uses them.**

**What to save:** `ad.users` count, `ad.groups` list, `ad.privileged_groups`,
`ad.kerberoastable_accounts`, `ad.asrep_roastable_accounts`.

---

### Step 12 — Password policy (D15)

**Goal:** Know lockout thresholds before any spray attempt.

**What to determine:** lockout threshold, observation window, minimum password length,
complexity requirements, fine-grained PSOs if any exist.

**Gate:** if lockout_threshold ≤ 3, record the value and abort any spray planning.

**What to save:** `domain.password_policy` with lockout_threshold,
observation_window_min, min_length, complexity.

---

### Step 13 — DNS enumeration (D13)

**Goal:** Find hidden hosts and new subnets not visible from network scan.

**What to determine:** all AD-integrated DNS records. New subnets discovered here
mean Phase A must restart for that segment.

**What to save:** `ad.dns_records` list. Cross-reference with `network.live_hosts`.
New subnet → add to `network.cidrs` and restart Step 4 for that range.

---

### Step 14 — Forest and trust mapping (D7)

**Goal:** Map all domain trusts and forest relationships.

**What to determine:** trust direction, type, SID filtering status.
Bidirectional trusts with SID filtering disabled = full cross-domain compromise path.

**What to save:** `domain.trusts` list with direction, type, sid_filtering per trust.

---

### Step 15 — ACL abuse discovery (D2)

**Goal:** Find principals the current account can abuse via ACL rights.

**What to determine:** objects where controlled principals have GenericAll, WriteDACL,
WriteOwner, ForceChangePassword, GenericWrite, AddMember, AllExtendedRights, or DCSync.

**Primary source:** BloodHound data from Step 9 (zero network traffic).
**Live LDAP supplement:** read nTSecurityDescriptor on high-value objects.

**What to save:** `ad.acl_abuse_paths` list (principal → object → right).

---

### Step 16 — Kerberoast and AS-REP discovery (D9)

**Goal:** Get hashes that can be cracked offline.

**What to determine:** service accounts with SPNs (Kerberoast), accounts with
DONT_REQ_PREAUTH set (AS-REP). Prioritize accounts with adminCount=1.

**Enumeration:** LDAP (native .NET / ldapsearch). No tool needed for discovery.
**Hash extraction:** Rubeus (Windows) or impacket-GetUserSPNs (Linux) — no native
equivalent for the actual TGS/AS-REP request.

**What to save:** `ad.kerberoastable_accounts`, `ad.asrep_roastable_accounts`.
Non-empty → immediately recommend `accessing-credentials`.

---

### Step 17 — Delegation discovery (D16)

**Goal:** Find hosts and accounts with delegation configured.

**What to determine:**
- Unconstrained delegation (non-DC computers) — coercion targets
- Constrained delegation (msDS-AllowedToDelegateTo set) — S4U abuse
- RBCD (msDS-AllowedToActOnBehalfOfOtherIdentity set) — RBCD abuse

**What to save:** `ad.unconstrained_delegation`, `ad.constrained_delegation`,
`ad.rbcd`.

---

### Step 18 — GPO enumeration (D10)

**Goal:** Find GPOs that control privileged access or contain credentials.

**What to determine:** all GPOs, their linked OUs, local-group mappings that
elevate accounts, GPP cpassword values in SYSVOL.

**SYSVOL cpassword:** read directly with native file traversal commands.
No tool needed — every domain member can read SYSVOL.

**What to save:** `ad.gpos` list. GPP cpassword found → immediately recommend
`accessing-credentials`.

---

### Step 19 — AD CS vulnerability discovery (D3)

**Goal:** Find certificate templates that allow privilege escalation.

**What to determine:** certificate templates with enrollee-supplied SAN (ESC1),
web enrollment endpoints (ESC8), template ACL abuse (ESC4), other ESC classes.

**Discovery:** LDAP query on `CN=Certificate Templates` for `msPKI-Certificate-Name-Flag`
and enrollment flag attributes — no Certify needed for enumeration.
**Full ESC check:** certipy (Linux) or Certify (Windows) — produces structured vuln list.

**What to save:** `ad.adcs_vulns` list with template name and ESC class.
Non-empty → immediately recommend `escalating-privileges`.

---

### Step 20 — SMB share enumeration (D4)

**Goal:** Find readable shares and credential-bearing files.

**What to determine:** readable shares on all live hosts, SYSVOL content,
credential files (web.config, unattend.xml, scripts with passwords).

**SYSVOL:** always readable by domain members — native file commands.
**Shares:** net view (Windows native) or smbclient/netexec (Linux).

**What to save:** `ad.shares` list. Credentials found → `accessing-credentials`.

---

### Step 21 — Session hunting (D5)

**Goal:** Find hosts where privileged users are logged on right now.

**What to determine:** active sessions for DA, EA, and other high-value accounts
across all reachable hosts.

**Primary:** native session query commands (query user, WMI).
**Supplement:** LDAP query for DA group members to know who to look for.

**What to save:** `ad.da_sessions` list. Session found → `moving-laterally`.

---

### Step 22 — Process and AV/EDR discovery (D12)

**Goal:** Know what security controls are running before using noisy tools.

**What to determine:** AV/EDR product names, Defender status, running processes,
token privileges, integrity level.

**Always use native OS commands** — WMI, Get-WmiObject, registry reads, sc query.
Offensive tools (Seatbelt, WinPEAS) only after AMSI bypass is confirmed.

**What to save:** `host.av_edr` list, `host.da_processes`.
EDR detected → flag for `evading-defenses` before continuing.

---

### Step 23 — Azure AD / hybrid enumeration (D14)

**Run only when:** Azure AD Connect, hybrid join indicators, or `.onmicrosoft.com`
UPN suffix detected in Steps 8, 11, or 13.

**Goal:** map the hybrid attack path from on-prem to cloud.

**What to save:** `ad.azure_tenant_id`, `ad.hybrid_join_hosts`,
`ad.aad_connect_server`.

---

### Step 24 — LAPS and gMSA (D20)

**Run only when:** Step 15 ACL data shows current account can read LAPS attributes.

**Goal:** read plaintext local admin passwords from LAPS-managed computers.

**What to determine:** which computers have readable `ms-Mcs-AdmPwd` for current user.
LDAP filter on computer objects where that attribute is readable.

**What to save:** `ad.laps_readable_hosts`, `ad.gmsa_accounts`.
Non-empty → `accessing-credentials`.

---

### Step 25 — MSSQL enumeration (D17)

**Run only when:** TCP/1433 or TCP/1434 found in Step 5.

**Goal:** find accessible SQL instances and linked-server chains.

**What to save:** `ad.mssql_servers`, `ad.mssql_links`.

---

### Step 26 — Exchange enumeration (D18)

**Run only when:** OWA / TCP/25 / TCP/587 found in Step 5.

**Goal:** enumerate Exchange and find mailbox credential paths.

**What to save:** `ad.exchange_hosts`, `ad.mailbox_secrets_found`.

---

### Step 27 — Spooler discovery (D19)

**Run only when:** TCP/445 + TCP/135 on non-DC hosts in Step 5.

**Goal:** identify hosts vulnerable to printer coercion (pair with Step 17).

**What to save:** `ad.spooler_enabled_hosts`.

---

## Preconditions

- `host.platform` known (`windows` / `linux` / `darwin`).
- For Phase B: `creds.domain_user` + `creds.domain_pass` in memory, OR foothold
  is domain-joined and running as a domain user.

## Hard rules

- **Generate commands at runtime from memory state** — never copy hardcoded scripts.
  Every command substitutes `domain.base_dn`, `network.dc_ip`, `creds.*` from memory.
- **Try/catch on every command block.** Write `ERROR: <step> <msg>` and continue.
  Partial data is better than aborting.
- **LDAP fallback chain:** authenticated → anonymous → netexec.
- **Scan order:** Step 4 (alive check on ALL CIDRs) then Step 5 (ports on alive hosts only).
  Never combine. Never port-scan raw CIDR.
- **Step 7 sets `recommended_next`** — planner acts on it immediately without waiting
  for `phase_done`.
- **Windows LDAP = `[DirectorySearcher]` / `[ADSI]` primary.** No PowerView required.
- **Linux LDAP = `ldapsearch` primary.** certipy/daclenum only for tasks with no
  ldapsearch path.
- **Step 9:** SharpHound.exe on Windows. bloodhound-python on Linux. Never swap.
- One step per ability. Do not fuse steps.
- Stay inside `network.cidrs`. No /16 sweeps.

## Stage goal

Phase A done when: `host.domain_joined`, `network.live_hosts`, `network.services`,
`network.has_domain_controller`, `recon.ad_present` all set with correct types.

Phase B done when: Critical steps (9, 12, 15, 16, 19) have run and their memory
keys are populated. Other steps may be partial or skipped if gated.

## Pivot conditions

- DC found → Phase B immediately (recommended_next = "ad-enumeration")
- No DC found → `escalating-privileges`
- DA path in BloodHound → `moving-laterally` (skip rest of Phase B)
- Kerberoastable / AS-REP accounts → `accessing-credentials`
- AD CS ESC vuln → `escalating-privileges`
- ACL abuse path to DA → `accessing-credentials`
- DA session on reachable host → `moving-laterally`
- LAPS readable → `accessing-credentials`
- GPP cpassword in SYSVOL → `accessing-credentials`
- EDR detected → `evading-defenses` before noisy tools
- SeImpersonatePrivilege → flag for `escalating-privileges` (Potato) after Phase B
