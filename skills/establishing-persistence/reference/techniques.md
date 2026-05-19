# Persistence — Techniques reference

Full catalogue for `establishing-persistence`.

## Contents
- Technique table Pe1-Pe15
- Per-technique cards: Pe1 (Golden Ticket), Pe2 (AdminSDHolder), Pe3 (DCSync grant)

## Technique table

| # | Technique | Priority | MITRE | Primary tools | Notes |
|---|---|---|---|---|---|
| Pe1 | Golden Ticket | Critical | T1558.001 | mimikatz, rubeus, impacket-ticketer | Forge TGT with krbtgt hash; ~10y validity default |
| Pe2 | AdminSDHolder ACL modification | Critical | T1098 | powerview, adexplorer, damp, mimikatz | SDProp propagates the ACL every ~60 min |
| Pe3 | DCSync rights grant to controlled account | Critical | T1098.002 | powerview, mimikatz, AD module | Pseudo-DA without group membership |
| Pe4 | Silver Ticket | Important | T1558.002 | mimikatz, rubeus, impacket-ticketer | Forged TGS — no DC contact at use |
| Pe5 | Skeleton Key | Important | T1556.001 | mimikatz misc::skeleton | Patches LSASS on DC; lost on reboot; AV-detected on disk |
| Pe6 | WMI event subscription | Important | T1546.003 | powerlurk, sharpwmi, empire | Fileless; in WMI repository |
| Pe7 | Scheduled task | Important | T1053.005 | schtasks, sharptask, powershell | Reliable; EDR-noisy |
| Pe8 | Registry Run / Startup folder | Important | T1547.001 | reg.exe, powershell | HKCU = no admin; HKLM = admin |
| Pe9 | Service (new / hijack) | Important | T1543.003 | sc.exe, powershell | Event 7045 monitored |
| Pe10 | Account manipulation (backdoor account / niche group add) | Important | T1098 | net user, AD module, mimikatz | Use niche privileged groups |
| Pe11 | SSH authorized_keys (Linux AD) | Important | T1098.004 | ssh-keygen, bash | Survives pw changes |
| Pe12 | DSRM account abuse | Important | T1003 / T1078 | mimikatz, reg, powershell | DC local-admin persistence; rarely rotated |
| Pe13 | DLL sideloading persistence | Optional | T1574.002 | custom dll | Very stealthy — trusted parent |
| Pe14 | Bootkit / rootkit | Optional | T1542.003 | custom | Out of scope for most engagements |
| Pe15 | Azure AD persistence (hybrid) | Optional | T1098.001 | aadinternals, roadtools, tokentactics | Survives on-prem cleanup |

## Pe1 — Golden Ticket (Critical, keystone persistence)

- Preconditions: krbtgt NTLM (or AES) hash in vault from DCSync; domain SID;
  target user (default `Administrator`, or a benign-looking name).
- Success indicators: TGT injected, `klist` shows it, `\\dc\C$` access works
  without further auth, validity dates correct.
- OPSEC: stealth at use, moderate at create (mimikatz LSASS load detected) —
  prefer `rubeus golden` or `impacket-ticketer` offline.
- Destructive gate: YES.
- Pivot: → `achieving-impact` + `evading-defenses` (log clear).

## Pe2 — AdminSDHolder ACL (Critical, long-term stealthy)

- Action: Add ACL on `CN=AdminSDHolder,CN=System,<DN>` granting the controlled
  principal `GenericAll`. SDProp re-applies the ACL to all protected groups
  (DA, EA, BA, Schema Admins, etc.) every ~60 min.
- Survives membership cleanup unless AdminSDHolder ACL itself is reverted.
- Destructive gate: YES.

## Pe3 — DCSync rights grant (Critical)

- Action: Grant `DS-Replication-Get-Changes` + `DS-Replication-Get-Changes-All`
  to a controlled low-priv account.
- Destructive gate: YES.
