# Discovery — tool command reference

This file is a **reference for the AI**, not a script library.
The AI generates actual commands at runtime by substituting values from memory.

**Core substitution principle:**
Every command the AI generates must pull values from memory:
- `<DC_IP>` → `network.dc_ip`
- `<BASE_DN>` → `domain.base_dn`
- `<USER>` → `creds.domain_user`
- `<PASS>` → `creds.domain_pass`
- `<DOMAIN>` → `domain.name`
- `<ARTIFACTS>` → `temp/bas`

If a memory key is missing, try to derive it or query it before running the step.

---

## Phase A reference

### Step 0 — Machine context

**Windows:** `whoami /all` + `wmic computersystem get PartOfDomain,Domain /value`
+ `Get-NetIPAddress -AddressFamily IPv4` for all adapters.

**Linux:** `id` + `realm list` + `ip -o -4 addr show`.

Parse output into typed memory keys. `host.domain_joined` must be a bool, not a string.
`host.token_privileges` must be a list, not raw text.

---

### Step 4 — Host alive sweep

**nmap available:** `nmap -sn` with ICMP + TCP probes on full CIDR.
ARP fallback (`-PR`) if ICMP returns zero hosts.

**nmap not available (Windows):** ARP cache → ICMP ping (500ms timeout) → TCP/445.
Use `System.Net.Sockets.TcpClient` for TCP check.

**nmap not available (Linux):** `ping -c1 -W1` per host or bash `/dev/tcp`.

Run for every CIDR in `network.cidrs`.
Save result as JSON array to `temp/bas/live_hosts.json`.

---

### Step 5 — Port scan

**nmap available:** `-sT -sV -Pn` reading from `live_hosts.txt` with `-iL`.
Never scan raw CIDR in this step.

**nmap not available (Windows):** `System.Net.Sockets.TcpClient.BeginConnect`
per port per host. 150ms timeout per port.

**nmap not available (Linux):** bash `/dev/tcp/$ip/$port` or `nc -zw1`.

Save result as JSON dict to `temp/bas/services.json`.

---

### Step 6 — Fingerprinting

Choose the simplest command that answers the question:
- SMB: `net view \\<IP>` (Windows) or `smbclient -N -L //<IP>` (Linux)
- LDAP rootDSE: `.NET DirectoryEntry("LDAP://<DC_IP>")` (Windows) or
  `ldapsearch -x -H ldap://<DC_IP> -s base -b ""` (Linux)
- HTTP: `curl -sI -m5 http://<IP>/`
- RDP: `qwinsta /server:<IP>` (Windows)

Use nmap scripts only if native commands return insufficient data.

---

### Step 7 — DC gate

Probe port 389 unauthenticated:
- Windows: `[ADSI]"LDAP://<DC_IP>"` → read `defaultNamingContext`
- Linux: `ldapsearch -x -H ldap://<DC_IP> -s base -b "" defaultNamingContext`

Always write `temp/bas/dc_gate.json` with `dc_found`, `dc_ip`, `base_dn`,
`recommended_next`, `phase_done`.

---

## Phase B reference

### LDAP query pattern

Every Phase B LDAP step follows this pattern. The AI generates the filter
and attributes based on the step goal — not from a hardcoded template.

**Windows pattern:**
```
[DirectorySearcher] on LDAP://<network.dc_ip>/<domain.base_dn>
Credentials: <creds.domain_user>@<domain.name> / <creds.domain_pass>
Fallback 1: anonymous bind (same query, no credentials)
Fallback 2: netexec ldap <network.dc_ip> -u <USER> -p <PASS> [module]
```

**Linux pattern:**
```
ldapsearch -x -H ldap://<network.dc_ip> -D "<USER>@<DOMAIN>" -w '<PASS>'
           -b "<domain.base_dn>" "<filter>" [attributes]
Fallback 1: ldapsearch without -D / -w (anonymous)
Fallback 2: netexec ldap <network.dc_ip> -u <USER> -p <PASS> [module]
```

---

### Step 9 — BloodHound / SharpHound

**Windows:** `SharpHound.exe -c All --stealth --outputdirectory <ARTIFACTS>/bloodhound`

**Linux:** `bloodhound-python -u <USER> -p '<PASS>' -d <DOMAIN> -ns <DC_IP> -c all --zip`

Output zip → `ad.bloodhound_zip`.

---

### Step 11 — User enumeration LDAP filters

The AI constructs filters from knowledge of AD schema:
- All users: `(&(objectCategory=person)(objectClass=user))`
- Kerberoastable: add `(servicePrincipalName=*)` and filter out disabled (UAC bit 2)
  and krbtgt
- AS-REP roastable: `(userAccountControl:1.2.840.113556.1.4.803:=4194304)`
- Admin accounts: `(adminCount=1)`

Attributes to request: samAccountName, displayName, adminCount,
userAccountControl, servicePrincipalName, memberOf, lastLogonTimestamp.

---

### Step 12 — Password policy attributes

Read from domain root object: `lockoutThreshold`, `lockoutObservationWindow`,
`minPwdLength`, `pwdProperties`.

PSOs: filter `(objectClass=msDS-PasswordSettings)` in
`CN=Password Settings Container,CN=System,<BASE_DN>`.

---

### Step 15 — ACL — what to look for

Rights that matter: `GenericAll`, `WriteDacl`, `WriteOwner`, `ExtendedRight`,
`GenericWrite`, `ForceChangePassword`, `AddMember`.

Exclude: `SYSTEM`, `Domain Admins`, `Enterprise Admins`, `BUILTIN\Administrators`,
`CREATOR OWNER` — these are expected.

Windows: `[DirectoryEntry].ObjectSecurity.Access` on the domain root and high-value objects.
Linux: `nTSecurityDescriptor` attribute via ldapsearch, or daclenum.py.

---

### Step 17 — Delegation LDAP filters

- Unconstrained (non-DC): objectCategory=computer +
  `userAccountControl:1.2.840.113556.1.4.803:=524288` +
  NOT `userAccountControl:1.2.840.113556.1.4.803:=8192`
- Constrained: `(msDS-AllowedToDelegateTo=*)`
- RBCD: `(msDS-AllowedToActOnBehalfOfOtherIdentity=*)`

---

### Step 19 — AD CS template attributes

Query `CN=Certificate Templates,CN=Public Key Services,CN=Services,CN=Configuration,<BASE_DN>`.
Filter: `(objectClass=pKICertificateTemplate)`.
Attributes: `cn`, `msPKI-Certificate-Name-Flag`, `msPKI-Enrollment-Flag`,
`pKIExtendedKeyUsage`, `msPKI-RA-Signature`.

ESC1 indicator: `msPKI-Certificate-Name-Flag` has bit 1 set (ENROLLEE_SUPPLIES_SUBJECT).
ESC8: check `http://<CA_HOST>/certsrv/` is reachable.

certipy or Certify for full structured output.

---

### Step 22 — AV/EDR detection (native only)

Windows: `Get-WmiObject -Namespace root\SecurityCenter2 -Class AntiVirusProduct`,
`Get-MpComputerStatus`, `sc query` filtered for known EDR service names.
`Get-Process` for process names.

Linux: `ps aux`, `ls /opt/`, `systemctl list-units`.

---

## Error handling pattern

Every step the AI generates follows:

**Windows:**
```powershell
try {
    # primary attempt
} catch {
    Write-Output "ERROR: <step> $($_.Exception.Message)"
    # fallback attempt or just continue
}
```

**Linux:**
```bash
<primary_command> 2>/dev/null || \
<fallback_command> 2>/dev/null || \
echo "ERROR: <step> both methods failed"
```

Write all output to `temp/bas/<step_name>.txt` even on partial failure.
