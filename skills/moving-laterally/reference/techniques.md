# Lateral Movement — Techniques reference

Full catalogue for `moving-laterally`.

## Contents
- Technique table L1-L17
- Per-technique cards: L1, L2, L3, L4
- OPSEC preference order
- Common credential-method matrix

## Technique table

| # | Technique | Priority | MITRE | Primary tools | OPSEC |
|---|---|---|---|---|---|
| L1  | Pass-the-Hash | Critical | T1550.002 | impacket, crackmapexec, mimikatz, evil-winrm, pth-toolkit | moderate |
| L2  | Pass-the-Ticket | Critical | T1550.003 | rubeus, mimikatz, impacket -k | stealth |
| L3  | WMI remote execution | Critical | T1047 | impacket-wmiexec, sharpwmi, crackmapexec -x | moderate |
| L4  | WinRM / PowerShell Remoting | Critical | T1021.006 | evil-winrm, crackmapexec, enter-pssession | moderate |
| L5  | SMBexec | Important | T1021.002 | impacket-smbexec, crackmapexec | loud |
| L6  | PsExec | Important | T1021.002 | impacket-psexec, sysinternals psexec | loud (last resort) |
| L7  | DCOM lateral execution | Important | T1021.003 | impacket-dcomexec, sharpcom | moderate |
| L8  | RDP session hijack | Important | T1563.002 | tscon | moderate, GUI focus |
| L9  | Overpass-the-Hash | Important | T1550.002 | rubeus, mimikatz | stealth (Kerberos) |
| L10 | SSH pivoting (Linux AD) | Important | T1021.004 | ssh, chisel | stealth |
| L11 | Tunnelling / SOCKS | Important | T1090.001 | chisel, ligolo-ng, revsocks, gost | stealth |
| L12 | Remote token impersonation | Important | T1134.001 | incognito, powersploit | moderate |
| L13 | Unconstrained delegation TGT capture | Important | T1558 | rubeus monitor, spoolsample, petitpotam, impacket | moderate |
| L14 | Shadow Credentials | Important | T1556 | whisker, pywhisker, certipy | stealth |
| L15 | Resource-Based Constrained Delegation (RBCD) | Important | T1134.005 | rubeus, impacket-addcomputer, powerview | stealth |
| L16 | MSSQL linked-server execution | Optional | T1210 | powerupsql, sqlcmd | moderate |
| L17 | WMI event subscription (lateral) | Optional | T1546.003 | sharpwmi, powerlurk, impacket | stealth — fileless |

## OPSEC preference order

When multiple methods accept the same credential, prefer:

1. **L2 PTT** (pure Kerberos — blends best)
2. **L3 WMI** (no service-creation event)
3. **L4 WinRM** (clean PSRP logs that we can clear after)
4. **L7 DCOM**
5. **L1 PTH** via wmiexec (NTLM 4624 LogonType 3)
6. **L5 SMBexec**
7. **L6 PsExec** (loud — service event 7045, expect EDR alert)

## L1 — Pass-the-Hash (Critical)

- Preconditions: NT hash in `credentials[]`; target accepts NTLM (check D8
  SMB signing / NTLM-disabled status).
- Success: remote command returns expected output; new shell registered.
- Fallback: if `LocalAccountTokenFilterPolicy` blocks (admin local hash but
  UAC remote token filter), switch to Overpass-the-Hash → PTT.

## L2 — Pass-the-Ticket (Critical)

- Preconditions: TGT or service TGS in vault; correct SPN for target.
- Success: `klist` shows injected ticket; auth without password prompt.
- OPSEC: stealth.
- Fallback: `KRB_AP_ERR_MODIFIED` → key in ticket doesn't match target's
  machine account; request fresh TGS via S4U or re-roast.

## L3 — WMI remote execution (Critical, preferred lateral exec)

- Why preferred: no service-creation events like PsExec (4697/7045); blends
  with normal admin activity.
- Pivot: shell → drop a beacon → `accessing-credentials` on the new host.

## L4 — WinRM (Critical)

- Preconditions: 5985/5986 open; controlled user in `Remote Management Users`
  or local admin.
- OPSEC: PSRP logs — clear via `Clear-EventLog -LogName
  Microsoft-Windows-PowerShell/Operational` after the job.
