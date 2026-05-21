# Discovery — tool commands reference

Concrete, copy-pasteable commands for each step of `SKILL.md`. The planner
must produce `command_template` strings that look like these — same flags,
same outputs, just with the placeholders substituted from `foothold` or
session memory.

## Step 1 — Determine local network range

### Windows (`cmd`)

```
ipconfig /all
```

```
route print -4
```

### Linux / macOS (`sh`)

```
ip -o -4 addr show
```

```
ip route
```

Parse the primary adapter's `IPv4 Address` + `Subnet Mask` (Windows) or
`inet a.b.c.d/NN` (Linux) into a CIDR. Store as `network.cidr`.

## Step 2 — Confirm nmap is installed

### Windows (`cmd`)

```
nmap --version
```

Fallback presence check (in the same stage, chained with `||`):

```
where nmap
```

### Linux / macOS (`sh`)

```
nmap --version
```

Fallback:

```
command -v nmap
```

A non-zero exit on both means `recon.nmap_available = false` → trigger Step 3.

## Step 3 — Install nmap (only if missing)

### Windows (`cmd`, admin)

```
winget install -e --id Insecure.Nmap --silent --accept-package-agreements --accept-source-agreements && nmap --version
```

Chocolatey fallback:

```
choco install nmap -y && nmap --version
```

### Debian / Ubuntu (`sh`)

```
sudo apt-get update && sudo apt-get install -y nmap && nmap --version
```

### RHEL / CentOS / Fedora (`sh`)

```
sudo dnf install -y nmap && nmap --version
```

### macOS (`sh`)

```
brew install nmap && nmap --version
```

If none of the package managers is reachable, fail this ability and emit
`recommended_next = blocked`.

## Step 4 — Host discovery sweep

Substitute `<CIDR>` with `network.cidr` from Step 1 (e.g. `192.168.1.0/24`)
and `<ARTIFACTS>` with the per-stage artifact directory.

### Windows (`cmd`) and Linux/macOS (`sh`) — identical nmap invocation

```
nmap -sn -PE -PP -PS21,22,80,443,445,3389 -PA80,443,3389 -oA <ARTIFACTS>/nmap_hostsweep <CIDR>
```

Parse the resulting `.gnmap` for `Status: Up` lines. Persist each IP (plus
reverse-DNS hostname where present) into `network.live_hosts`.

If the sweep returns zero hosts, re-run **once** with `-PR` (ARP) on the
same subnet before giving up; many environments drop ICMP at the host
firewall.

## Step 5 — Service / port enumeration

Substitute `<LIVE_HOSTS_FILE>` with a one-IP-per-line file built from
`network.live_hosts` (a small `printf` / `Set-Content` stage just before this
one is acceptable if needed).

```
nmap -sS -sV -Pn --top-ports 1000 --version-intensity 5 -oA <ARTIFACTS>/nmap_services -iL <LIVE_HOSTS_FILE>
```

Flag breakdown — keep all of them:

- `-sS` SYN scan (fast, low footprint when run as root/Administrator).
  Non-privileged shells should substitute `-sT` (TCP connect).
- `-sV` service + version detection (mandatory).
- `-Pn` don't re-ping (Step 4 already filtered to live hosts).
- `--top-ports 1000` good coverage without scanning all 65535.
- `--version-intensity 5` default; raise to 7 if you specifically need
  banners on a stubborn service, do **not** go to 9 on a full sweep.

Persist each `(host, port, proto, service, product, version)` row into
`network.services`.

## Step 6 — Targeted deepening

Run one ability per high-value port. Pick the matching template; never
broaden the port list.

| Port | Service     | Template                                                                  |
|------|-------------|---------------------------------------------------------------------------|
| 445  | SMB         | `nmap -p445 --script smb-os-discovery,smb2-security-mode <IP>`            |
| 88   | Kerberos    | `nmap -p88 -sV <IP>`                                                       |
| 389  | LDAP        | `nmap -p389 --script ldap-rootdse <IP>`                                    |
| 636  | LDAPS       | `nmap -p636 --script ldap-rootdse,ssl-cert <IP>`                           |
| 3389 | RDP         | `nmap -p3389 --script rdp-enum-encryption,rdp-ntlm-info <IP>`              |
| 80   | HTTP        | `nmap -p80 --script http-title,http-server-header,http-methods <IP>`       |
| 443  | HTTPS       | `nmap -p443 --script http-title,http-server-header,ssl-cert <IP>`          |
| 22   | SSH         | `nmap -p22 --script ssh2-enum-algos,ssh-hostkey <IP>`                      |
| 3306 | MySQL       | `nmap -p3306 --script mysql-info <IP>`                                     |
| 1433 | MSSQL       | `nmap -p1433 --script ms-sql-info,ms-sql-ntlm-info <IP>`                   |

Persist the script output under `network.fingerprints.<IP>.<PORT>`.

## Step 7 — AD pivot probes (gated)

Only emit these abilities if Step 7's gate condition (see SKILL.md) is met.
They are unauthenticated and lightweight; deeper enumeration is the next
skill's job.

```
crackmapexec smb <DC_IP>
```

```
ldapsearch -x -H ldap://<DC_IP> -s base -b "" namingContexts defaultNamingContext
```

```
enum4linux-ng -A <DC_IP>
```

Persist the discovered domain name, NetBIOS name, naming contexts, and
`signing required` flag under `identity.domain`. Then set
`network.has_domain_controller = true` and emit `recommended_next` per the
SKILL.md pivot table.
