# Discovery Phase — Research Dossier

Status: research only (no code). Last updated 2026-05-19.

## 0. Recap of clarified backend behaviour

From the BAS OpenAPI + your answers:

1. `AbilityStage.executor` is free-form — psh, pwsh, cmd, sh, bash, python, donut, bof, anything the BAS agent shells out to.
2. Our LLM-generated Abilities/Adversaries write `created_by: "ai"`.
3. AI-generated abilities skip the HITL gate (`requires_approval: false` even on otherwise loud commands — risk control becomes *our* job pre-push).
4. `/ai/operation-feedback` always sends `loop_status: "continue"`.
5. Backend auto-routes a stage to the correct platform agent by reading `Ability.platform`. We can mix `windows`/`linux`/`mac` abilities in one Adversary and only set the relevant `*_agent_id` on the Operation.
6. Cleanup commands are modelled as their own Abilities, bundled into a separate `*-rollback` Adversary that runs at the end of the engagement.
7. Discovery is **NOT just AD**. The goal is a complete inventory of the victim environment regardless of whether it's a single Linux box, a Windows workstation, a Kubernetes node, or a domain-joined member server.

## 1. Discovery phase — primary goal

> Build a **complete inventory of all reachable digital assets**, map data flows
> and trust relationships, and identify exposure points — so subsequent phases
> (privesc, credential access, lateral) have a populated typed memory to plan
> against.

Concretely, by end of Discovery the typed session memory should contain:

- `host_self`: full system fingerprint of the foothold (OS, patches, user, groups, processes, services, scheduled tasks, security software).
- `network.local`: interfaces, routes, ARP cache, listening sockets, DNS suffix.
- `network.remote`: live hosts in scope, open TCP/UDP ports, service banners, OS guesses, web app fingerprints.
- `identity`: whether the host is domain-joined, workgroup, AAD-joined, or standalone. If AD: domain controllers, forest trusts, users, groups, GPOs, SPNs, ACLs. If cloud: tenant id, attached managed identity, cloud roles.
- `assets`: file shares, databases, web apps, mail systems, CI/CD runners, container runtimes, hypervisors.
- `defenses`: AV/EDR vendor + version, AppLocker/WDAC policy, logging agents (Splunk UF, Wazuh, Sysmon), patch level vs known CVE feed.
- `attack_surface`: ranked list of exposures (kerberoastable accounts, ESC1-13 templates, writable shares, unpatched MSxx, exposed RDP/WinRM/SSH, default creds).

## 2. Renaming proposal

`skills/discovering-environment/` is too narrow. Proposed rename:

```
skills/discovering-environment/        (broad — what we run FIRST)
    SKILL.md
    reference/
        techniques.md                  (covers 34 MITRE TA0007 techniques)
        tool-commands.md
```

The AD-specific catalogue stays — it becomes a **conditional sub-track** inside
`discovering-environment` that the agent enters only when one of these signals
fires from the foothold fingerprint:

- `host_self.domain != "WORKGROUP"` on Windows
- Presence of `realmd`/`sssd`/keytab on Linux
- DNS resolution of `_ldap._tcp.dc._msdcs.<domain>` succeeds
- A reachable host has port 88 (Kerberos) open in the subnet sweep

I will not rename until you confirm. Either:
- (a) rename `discovering-environment` → `discovering-environment` and absorb the AD content as a sub-track, **or**
- (b) keep `discovering-environment` and add a new `discovering-environment` sibling that pivots into it.

My recommendation: **(a)**. Less duplication, matches the "Discovery = inventory" mental model.

## 3. Full technique catalogue for Discovery (MITRE TA0007, 34 techniques)

Grouped by sub-track. Bold = Critical for v1. Italic = Important. Plain = Optional / context-dependent.

### 3.1 Foothold fingerprint (host_self) — Windows + Linux

| # | Technique | MITRE | Notes |
|---|---|---|---|
| **D1** | **System Information Discovery** | T1082 | OS, build, patches, hostname, arch, uptime |
| **D2** | **System Owner/User Discovery** | T1033 | whoami, id, current user SID, integrity level |
| **D3** | **System Network Configuration Discovery** | T1016 | ipconfig/all, ip a, route, arp, DNS suffix |
| **D4** | **System Network Connections Discovery** | T1049 | netstat / ss / Get-NetTCPConnection |
| *D5* | *Process Discovery* | T1057 | tasklist/v, ps auxf, parent/child tree |
| *D6* | *System Service Discovery* | T1007 | sc query, systemctl, launchctl |
| *D7* | *Software Discovery* | T1518 | wmic product, Get-Package, dpkg-l, rpm-qa |
| *D8* | *Security Software Discovery* | T1518.001 | EDR/AV vendor, Sysmon, Defender state |
| D9 | Backup Software Discovery | T1518.002 | Veeam/CommVault agents — feeds Impact phase |
| D10 | Device Driver Discovery | T1652 | driverquery — feeds vuln driver / BYOVD |
| *D11* | *Query Registry (Windows)* | T1012 | uptime, install root, AV exclusions, RDP enabled |
| D12 | Application Window Discovery | T1010 | only useful in interactive sessions |
| D13 | Browser Information Discovery | T1217 | bookmarks → internal URLs, saved creds (also CredAccess) |
| D14 | Log Enumeration | T1654 | wevtutil, journalctl — feeds future tactics |
| D15 | Peripheral Device Discovery | T1120 | smart cards, removable media |
| D16 | System Time Discovery | T1124 | needed before Kerberoast (clock skew) |
| D17 | System Location Discovery + System Language | T1614 / .001 | geolocation / locale |
| D18 | Virtual Machine Discovery | T1673 | esxcli / vim-cmd / vboxmanage if on hypervisor |
| D19 | Virtualization/Sandbox Evasion checks | T1497 | run AS A DETECTION, not evasion: tells us if we're in a honeypot |
| D20 | File and Directory Discovery (local) | T1083 | C:\Users, /home, /opt, /srv — interesting files |
| D21 | Local Storage Discovery | T1680 | drives, volumes, BitLocker state |

### 3.2 Network sub-track (the nmap-driven slice from your prompt)

| # | Technique | MITRE | Tools / commands |
|---|---|---|---|
| **D22** | **Remote System Discovery (ping sweep)** | T1018 | `nmap -sn {{ env.network_ranges }}`, `arp -a`, `Get-NetNeighbor`, `net view /domain` |
| **D23** | **Network Service Discovery (port scan)** | T1046 | `nmap -sS -p- --min-rate 1000`, `nmap -sU --top-ports 200`, `masscan` |
| **D24** | **Service version + banner** | T1046 | `nmap -sV -sC` (default NSE), `nmap --script vuln,banner` |
| **D25** | **OS fingerprint** | T1018 | `nmap -O`, passive (`p0f`), TTL/window heuristics |
| *D26* | *Web app discovery* | T1595.002-ish | `whatweb`, `httpx`, `nuclei -t exposures/`, `gobuster vhost` |
| *D27* | *Network Share Discovery* | T1135 | `net view \\host`, `smbclient -L`, `nxc smb -M spider_plus`, `enum4linux-ng` |
| D28 | Network Sniffing (passive) | T1040 | only on hosts where we already have root/SYSTEM; PktMon / tcpdump |

### 3.3 Identity / AD sub-track (gated by D3 + D22 results)

| # | Technique | MITRE | Tools |
|---|---|---|---|
| **D29** | **Account Discovery — Domain** | T1087.002 | `nxc ldap -M users`, `ldapdomaindump`, ADSearch, SharpHound |
| *D30* | *Account Discovery — Local* | T1087.001 | `net user`, `Get-LocalUser`, `/etc/passwd` |
| **D31** | **Permission Groups Discovery — Domain** | T1069.002 | SharpHound, `Get-DomainGroup`, BloodHound |
| *D32* | *Domain Trust Discovery* | T1482 | `nltest /domain_trusts`, `Get-DomainTrust` |
| *D33* | *Group Policy Discovery* | T1615 | `gpresult /r`, SYSVOL parsing |
| *D34* | *Password Policy Discovery* | T1201 | `net accounts /domain`, ldap rootDSE |

### 3.4 Cloud sub-track (gated by managed-identity / IMDS reachability)

| # | Technique | MITRE | Tools |
|---|---|---|---|
| D35 | Cloud Account Discovery | T1087.004 | az ad user/group list, aws iam list-users, gcloud iam |
| D36 | Cloud Infrastructure Discovery | T1580 | `pacu`, `ScoutSuite`, `roadtools` (AAD) |
| D37 | Cloud Service Discovery | T1526 | service catalogue per provider |
| D38 | Cloud Storage Object Discovery | T1619 | s3 ls, az storage blob list |
| D39 | Container & Resource Discovery | T1613 | `kubectl get all -A`, `crictl`, `docker ps` |

(Cloud sub-track is **scaffolded** in v1 but not implemented — needs a separate Adversary later.)

## 4. Tooling matrix

### 4.1 Always-on (push or verify on every foothold)

| Tool | Platform | Used for | Source of binary |
|---|---|---|---|
| nmap | win/lin/mac | D22-D25, D27 NSE scripts | Payload (upload once) or `apt/choco/brew install` via prereq Ability |
| Native `cmd` + `powershell` | win | D1-D21 | Built-in |
| Native `sh`/`bash`+ coreutils | lin/mac | D1-D21 | Built-in |
| NetExec (`nxc`, ex CrackMapExec) | lin (drives smb/ldap/winrm/mssql) | D27, D29, D31 | Payload |
| SharpHound (`.exe`, BOF, .NET) | win | D29-D32 | Payload |
| ADSearch | win | D29-D34 | Payload |
| ldapdomaindump | lin | D29-D34 (off-box) | Payload (python single-file build) |
| BloodHound CE ingest | controller-side | Graph build | Our orchestrator, not pushed |

### 4.2 Conditional (push only if signal triggers)

| Tool | Trigger | Used for |
|---|---|---|
| masscan | Subnet > /16 | Fast pre-sweep before nmap -sV |
| nuclei | Web port discovered | Exposure templates |
| whatweb / httpx | Web port discovered | Fingerprint + screenshot |
| enum4linux-ng | SMB null session allowed | Share + user enum |
| Certify | AD-CS CES endpoint discovered | ESC1-13 templates (also PrivEsc) |
| roadrecon (AAD) | AAD-joined detected | Cloud identity graph |
| kube-hunter / kubectl-who-can | k8s API reachable | Container sub-track |

### 4.3 Defense-aware substitutions

- Defender + ASR enabled → swap `wmic` → `Get-CimInstance`, `tasklist` → `Get-Process`.
- AMSI hot → call SharpHound via BOF through Sliver inproc loader instead of `.NET Assembly.Load`.
- Sysmon present → drop `-sS` SYN scans (loud), prefer `-sT` connect with `--max-retries 1 --max-rate 200`.

## 5. Branching decision tree (post-foothold)

```
[FOOTHOLD]
    │
    ├── D1-D4 (host fingerprint)      ALWAYS (cheap, ~2s)
    │
    ├── D8 / D11 → defenses           ALWAYS  ─┐  feeds ambient `evading-defenses`
    │                                          │
    ├── domain-joined?                          │
    │     ├── yes → run identity sub-track (D29-D34) in parallel with network sub-track
    │     └── no  → skip identity, stay on network + cloud
    │
    ├── network sub-track (D22-D27) — gated on env.network_ranges populated
    │     ├── D22 ping sweep  (cheap, broad)
    │     ├── D23 service scan (filtered: only live hosts from D22)
    │     ├── D24 banner/vuln NSE on services found
    │     └── D27 share enum on SMB hosts
    │
    ├── cloud sub-track (D35-D39) — gated on IMDS reachable / managed identity present
    │
    └── EMIT PIVOT
          ├── kerberoastable users  → privesc + credaccess
          ├── ESC1-13 templates     → privesc
          ├── writable share        → lateral
          ├── unpatched MS17-010    → lateral
          ├── exposed RDP/SSH       → credaccess (spray)
          └── crown-jewel asset hit → impact (mark target)
```

## 6. Mapping to backend objects (concrete shape)

### 6.1 One Adversary per skill

```jsonc
POST /adversaries
{
  "name": "Discovery — Environment (v1)",
  "description": "Autonomous environment inventory; spans 34 MITRE TA0007 techniques across host, network, identity, and cloud sub-tracks.",
  "profile": "discovery",
  "execution_strategy": "branching",   // see Q-7 below
  "requires_approval": false,
  "is_tested": false,
  "created_by": "ai"
}
→ { adversary_id }
```

### 6.2 One Ability per technique

```jsonc
POST /abilities
{
  "name": "D22 — Remote System Discovery (ping sweep)",
  "description": "Identify live hosts in the in-scope subnet using ICMP + ARP fallback.",
  "mitre_tactic": "TA0007",
  "mitre_technique_id": "T1018",
  "platform": "linux",                 // duplicate-create per platform; backend routes
  "impact_type": "recon",
  "default_severity": "info",
  "requires_approval": false,
  "tags": ["discovery", "network", "nmap", "subtrack:network"],
  "created_by": "ai"
}
→ { ability_id }
```

Then link:

```jsonc
POST /adversaries/{adversary_id}/abilities/{ability_id}
```

### 6.3 AbilityStages = the actual commands (ordered, multi-step)

For D22 on Linux:

```jsonc
POST /abilities/{D22_linux_ability_id}/stages
[
  { "stage_name": "verify_nmap_present", "stage_order": 1,
    "executor": "sh",
    "command_template": "command -v nmap >/dev/null 2>&1 && nmap --version || echo MISSING",
    "timeout_seconds": 10 },
  { "stage_name": "install_nmap_if_missing", "stage_order": 2,
    "executor": "sh",
    "command_template": "if ! command -v nmap >/dev/null; then (apt-get update -y && apt-get install -y nmap) || (yum install -y nmap) || (apk add nmap); fi",
    "timeout_seconds": 120 },
  { "stage_name": "icmp_arp_sweep", "stage_order": 3,
    "executor": "sh",
    "command_template": "nmap -sn -PE -PP -PM -PR -oX - {{ env.network_ranges[0] }}",
    "timeout_seconds": 600 },
  { "stage_name": "tcp_probe_fallback", "stage_order": 4,
    "executor": "sh",
    "command_template": "nmap -sn -PS22,80,135,139,443,445,3389 -PA80,443,3389 -oX - {{ env.network_ranges[0] }}",
    "timeout_seconds": 600 }
]
```

Note `{{ env.network_ranges[0] }}` — the **orchestrator renders templates before pushing**. The backend never sees Jinja-style placeholders; it gets fully-resolved commands.

The Windows counterpart of D22 has a separate `ability_id` with `platform: "windows"` and `executor: "psh"`, body uses `Test-Connection` + `arp -a` + chocolatey-install fallback for nmap.

### 6.4 Payloads we'll upload up-front

| Payload | Why up-front | Stages that reference it |
|---|---|---|
| `nmap-portable-win.zip` | Avoids triggering `choco install` (loud, requires admin) | D22-D25 Windows variants |
| `SharpHound-v2.4.exe` | AD identity sub-track | D29, D31, D32 |
| `ADSearch.exe` | Quieter than SharpHound for read-only LDAP | D29, D33, D34 |
| `nxc-linux-musl` (statically linked) | Lateral-adjacent recon from Linux foothold | D27, D29 |
| `ldapdomaindump.pyz` | AD enum from Linux foothold | D29, D33 |

Uploaded once with `POST /payloads` (multipart), then each Stage references the returned `payload_id`. The BAS agent downloads via `/payloads/{id}/download` (base64) before executing.

## 7. Push-order plan (the "happy path")

This is what our orchestrator code will do in v1, before we even start the agent task loop:

```
1.  Resolve environment_id from /environments
2.  Resolve windows/linux/mac agent_ids from /environments/{env}/agents
3.  Upload payloads (idempotent: list, dedupe by hash, upload missing)
4.  For each technique row in skills/discovering-environment/reference/techniques.md:
       for each platform the technique supports:
           POST /abilities                          → ability_id
           For each command in tool-commands.md:
               render template against env memory   (Jinja2)
               POST /abilities/{id}/stages          → stage_id
5.  POST /adversaries  "Discovery — Environment (v1)"   → adversary_id
6.  For each created ability_id:
       POST /adversaries/{adversary_id}/abilities/{ability_id}
7.  POST /operations { adversary_id, environment_id, *_agent_id, execution_mode:"lab" } → operation_id
8.  POST /operations/{operation_id}/start
9.  Poll loop:
     a. GET  /operations/{operation_id}/abilities-payloads     (every 5s)
     b. parse execution_log entries → feed parsers/* → update typed memory
     c. on hot signals (kerberoast targets, ESC templates, etc.):
          POST /ai/operation-feedback  (mutate next stage's command_template
                                        OR queue a new branching Ability)
     d. on stage budget exhaustion / commit-criteria met:
          POST /operations/{operation_id}/stop
10. Emit pivot recommendation → next Adversary in next phase.
```

## 8. Open questions (please answer before I start the wiring code)

1. **Branching execution** — `Adversary.execution_strategy` is a free-form string in the schema. Do you have an existing enum (`sequential` / `parallel` / `branching` / `bandit`)? The discovery tree (§5) needs at minimum "skip subtree on condition". If the backend only supports linear execution, we'll have to encode branching by *not pushing* later abilities and instead reacting via `/ai/operation-feedback` to add abilities mid-flight. Confirm which one.

2. **Mid-flight ability creation** — `AIOperationFeedbackRequest` only mutates `suggested_command_template` on existing `stage_id`s. Can we add NEW abilities/stages while an operation is running? If not, our v1 has to push the *full superset* (all 34 techniques) up-front and skip-via-no-op the irrelevant ones at runtime.

3. **`execution_log` shape** — still empty in the OpenAPI spec. Can you paste one real example (e.g. from a hello-world nmap operation in lab)? Without it I can't write the parser layer or define what "success" looks like per stage.

4. **`/environments/{id}/agents` semantics** — does it return only currently-online agents (last_seen < N seconds) or all ever-registered? Affects whether we fail-fast or pick the highest-`last_seen` agent per platform.

5. **Payload dedup** — does `POST /payloads` accept a `sha256` or `name` uniqueness key, or do repeat uploads create duplicates? Need this to make our push idempotent across re-runs.

6. **Per-stage timeout vs. per-operation budget** — backend has `timeout_seconds` per stage. Is there an operation-wide budget I should respect, or do I just sum stage timeouts? Our skill files declare `budget.max_wallclock_min: 25`.

7. **Stage failure semantics** — if `verify_nmap_present` returns "MISSING" (exit 0, non-error stdout) does the operation continue to the install stage? Or does the backend treat any non-empty stderr / non-zero exit as terminal? We need a way to chain "if A then B".

8. **`/ai/operation-feedback` write-rate** — any rate limit? If we want self-critique every N=5 stages, we'll hit it ~30 times per Discovery operation.

9. **Concurrent abilities** — can two abilities run in parallel on the same agent (e.g. host fingerprint + network sweep simultaneously)? Or does the agent task loop serialize?

10. **Conditional stage skip** — is there a primitive to mark a stage `skip_if: "{{ memory.host_self.domain == 'WORKGROUP' }}"`, or is gating purely our job via `/ai/operation-feedback` interventions?

Items **1, 2, 3, 7, 10** are blockers — they decide whether v1 ships as a "push everything, gate at runtime" or "push minimal, grow via feedback" architecture. The other items I can defer with safe defaults.

## 9. References

- MITRE ATT&CK Enterprise — Discovery (TA0007), v19: https://attack.mitre.org/tactics/TA0007/
- Larbi Ouiyzme, "Mastering Network Discovery: A Comprehensive Guide to Nmap Commands and Scanning Techniques": https://larbi-ouiyzme.medium.com/mastering-network-discovery-a-comprehensive-guide-to-nmap-commands-and-scanning-techniques-541e99466e9c
- BloodHound CE documentation (graph schema): https://bloodhound.specterops.io/
- NetExec wiki: https://www.netexec.wiki/
- Nuclei templates: https://github.com/projectdiscovery/nuclei-templates
- Pacu (AWS): https://github.com/RhinoSecurityLabs/pacu
- ROADtools (Entra ID): https://github.com/dirkjanm/ROADtools
