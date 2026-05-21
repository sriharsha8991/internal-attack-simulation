---
name: discovering-environment
description: Network-first discovery. Starts from the foothold's own subnet (e.g. 192.168.1.0/24), confirms nmap is available (and installs it via the host's native package manager if not), sweeps the network for live assets, enumerates ports/services/versions on those assets, and ONLY then — and only if DC-indicative ports (88/389/445/636/3268) are observed — moves into Active Directory enumeration. Use as the very first stage after a fresh foothold, or whenever scope expands to a new subnet.
stage: discovery
agent: DiscoveryAgent
mitre_tactics: ["TA0007"]
default_opsec: moderate
ambient: false
tool_allowlist:
  # ---- recon backbone (always allowed) ----
  - nmap
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
  # ---- package managers (for nmap install only) ----
  - winget
  - choco
  - apt
  - apt-get
  - yum
  - dnf
  - brew
  # ---- conditional: only after a DC port is observed ----
  - crackmapexec
  - ldapsearch
  - rpcclient
  - smbclient
  - enum4linux-ng
budget:
  max_tool_calls: 20
  max_wallclock_min: 15
---

# Discovering the environment

Build a factual, evidence-driven inventory of the network the foothold sits in.
**Network reconnaissance comes first**; identity / Active Directory enumeration
is a *conditional* follow-up that only runs after the port scan proves there is
a domain controller in scope. Skipping the network sweep and jumping straight
to BloodHound or SharpHound is a misuse of this skill.

## Mandatory step ordering

Every ability emitted by this skill must implement exactly **one** of the
numbered steps below, in order. The orchestrator runs them sequentially; each
step's output gates the next.

### Step 1 — Determine the local network range

Goal: discover the CIDR the foothold is attached to so the rest of the skill
has a concrete scan target. Do **not** invent a range; read it from the host.

- Windows (`cmd`): `ipconfig /all` (parse the IPv4 + subnet mask of the
  primary adapter; `route print` as a cross-check for the default gateway's
  subnet).
- Linux / macOS (`sh`): `ip -o -4 addr show` (or `ifconfig -a` on
  BSD/macOS) + `ip route`.

Emit **one** ability with one stage that runs the platform-appropriate command.
Save the discovered CIDR into memory as `network.cidr` (e.g. `192.168.1.0/24`).

### Step 2 — Confirm nmap is installed

Goal: prove `nmap` is on PATH before any scan command is queued. A scan ability
that fails because nmap is missing wastes a tool budget slot and is forbidden.

- Windows (`cmd`): `nmap --version` (use `where nmap` as a fallback presence
  check).
- Linux / macOS (`sh`): `nmap --version` (use `command -v nmap` as a
  fallback presence check).

Emit **one** ability with one stage. Memory key: `recon.nmap_available` = bool.

### Step 3 — Install nmap if it is missing

Run **only** if Step 2 reported the binary missing. Pick the package manager
that exists on the host platform. Never download installers from arbitrary
URLs; only use the host's native package manager.

- Windows: `winget install -e --id Insecure.Nmap --silent --accept-package-agreements --accept-source-agreements`
  (fallback: `choco install nmap -y` if Chocolatey is present).
- Debian/Ubuntu: `sudo apt-get update && sudo apt-get install -y nmap`.
- RHEL/CentOS/Fedora: `sudo dnf install -y nmap` (or `yum`).
- macOS: `brew install nmap`.

Emit **one** ability with one stage. Re-verify with `nmap --version` as the
last command of the same stage (chained with `&&`).

### Step 4 — Host discovery / live-asset map

Goal: produce the list of live hosts inside `network.cidr`. Stay inside the
foothold's own subnet — do **not** widen to /16 or scan public ranges.

```
nmap -sn -PE -PP -PS21,22,80,443,445,3389 -PA80,443,3389 \
     -oA <artifacts>/nmap_hostsweep <network.cidr>
```

(`-sn` = ping sweep only, no port scan; `-PE/-PP` = ICMP echo + timestamp; the
`-PS/-PA` lists catch hosts that drop ICMP.)

Save into memory as `network.live_hosts` = list of IPs and (where resolvable)
hostnames. This is the input to Step 5.

### Step 5 — Service / port enumeration on live hosts

Goal: for each live host from Step 4, learn what services are listening. Use
the **top-1000-ports TCP scan with version detection** — broad enough to spot
DC ports, narrow enough to finish in budget.

```
nmap -sS -sV -Pn --top-ports 1000 --version-intensity 5 \
     -oA <artifacts>/nmap_services -iL <live_hosts_file>
```

`-sV` is mandatory — it is what produces the service+version data that drives
Step 6 and the AD-pivot decision. Save into memory as `network.services`
(per-host list of `{port, proto, service, product, version}`).

### Step 6 — Targeted version & banner deepening

Run **per high-value port** found in Step 5. Pick the smallest scan that still
answers the question:

- SMB on 445 → `nmap -p445 --script smb-os-discovery,smb2-security-mode <ip>`
- Kerberos on 88 → `nmap -p88 -sV <ip>` (and only if a user list is available,
  optionally `--script krb5-enum-users`).
- LDAP on 389/636 → `nmap -p389,636 --script ldap-rootdse <ip>`
- HTTP(S) on 80/443/8080/8443 → `nmap -p<port> --script http-title,http-server-header,http-methods <ip>`
- RDP on 3389 → `nmap -p3389 --script rdp-enum-encryption,rdp-ntlm-info <ip>`

Each scan is its own ability stage. Save findings under
`network.fingerprints.<ip>.<port>`.

### Step 7 — Pivot decision (Active Directory gate)

Inspect Step 5 / Step 6 output. Pivot into AD enumeration **only** if at least
two of the following are true on the same host:

- TCP/88 open (Kerberos)
- TCP/389 or TCP/636 open (LDAP / LDAPS)
- TCP/445 open (SMB) **and** that host's hostname looks like a DC
  (matches `*DC*`, `*dc0*`, ends in `$`) OR SMB OS discovery reports a server
  OS edition

If the gate passes, the *next* ability may run lightweight, unauthenticated AD
recon (e.g. `crackmapexec smb <dc_ip>`, `ldapsearch -x -H ldap://<dc_ip> -s base`,
`enum4linux-ng -A <dc_ip>`). Heavy collectors (BloodHound, SharpHound,
Certipy) are **not** to be queued by this skill — they belong to the
`accessing-credentials` / `escalating-privileges` skills once a usable
credential exists.

If the gate fails, set `network.has_domain_controller = false` and finish the
skill. The router will pick a different next phase.

## Preconditions

- `foothold.platform` is set (`windows`, `linux`, or `darwin`).
- `foothold.hostname` / `foothold.ip_address` populated by foothold resolution.
- Outbound ICMP / TCP to the foothold's own subnet is permitted by scope.

## Hard rules

- One step per ability. Don't fuse Steps 1 and 4 into a single 3-line stage.
- Never use a placeholder like `<target>` in `command_template` — substitute
  the actual CIDR / IP / hostname from memory or foothold.
- Match the host: Windows abilities use executor `cmd` or `psh`; Linux/macOS
  use `sh` or `bash`. Don't ship `apt-get` to a Windows host or `winget` to
  Linux.
- Stay inside `network.cidr`. Scanning a /16 you weren't given is out of scope.
- Do not queue BloodHound, SharpHound, Certipy, PowerView, or any other
  AD-heavy tooling from this skill. The pivot in Step 7 only allows
  lightweight, unauthenticated DC probes.

## Stage goal (commit criteria)

Signal `success` only when **all** hold:

- `network.cidr` populated (Step 1).
- `recon.nmap_available == true` (Step 2, possibly after Step 3).
- `network.live_hosts` is a non-empty list (Step 4).
- `network.services` populated for every live host (Step 5).
- Step 7 has either set `network.has_domain_controller = true` (with the DC's
  IP and detected ports) or set it explicitly to `false`.

## MITRE mapping for emitted abilities

| Step | MITRE technique                                  | tactic |
|------|--------------------------------------------------|--------|
| 1    | T1016 — System Network Configuration Discovery   | TA0007 |
| 2-3  | T1518 — Software Discovery / Install             | TA0007 |
| 4    | T1018 — Remote System Discovery                  | TA0007 |
| 5    | T1046 — Network Service Discovery                | TA0007 |
| 6    | T1046 — Network Service Discovery (deep)         | TA0007 |
| 7    | T1087.002 — Domain Account (only if pivot fires) | TA0007 |

Pick the row that matches the step you are emitting; never use a generic
parent technique when a specific one applies.

## Pivot conditions (set `recommended_next`)

- DC found + at least one user/computer enumerated → `accessing-credentials`
  (target SPN roasting / AS-REP roasting first, cheapest).
- No DC, but RDP/SMB open on a workstation with weak banner → `moving-laterally`.
- No DC, no obviously vulnerable service → `establishing-persistence` on the
  current foothold while waiting for more scope.
- Nmap genuinely cannot be installed (no admin, no package manager reachable)
  → `blocked`; escalate to human.

## Evidence to capture

- Raw nmap output: `-oA` to `<artifacts>/<step>/nmap_*.{nmap,gnmap,xml}`.
- Parsed live-host list and per-host service map merged into session memory.
- One-line `finding` per high-value service (e.g. "10.0.0.5:445 SMB Windows
  Server 2019 signing=disabled") with `raw_output_ref` pointer.

Full per-step command templates and parser hints:
[reference/tool-commands.md](reference/tool-commands.md).
