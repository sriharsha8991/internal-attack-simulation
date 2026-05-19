# Discovery — Techniques reference

Full catalogue for the `discovering-environment` skill. SKILL.md lists only the Critical
techniques; this file holds the complete table plus per-technique cards used
when the agent needs detail.

## Contents
- Technique table (D1–D20, Critical → Important → Optional)
- Per-technique cards for Critical techniques (D1, D2, D3) and the most-used
  Important ones (D9, D16)
- Quick selection guidance

## Technique table

| # | Technique | Priority | MITRE | Primary tools | Pivot hint |
|---|---|---|---|---|---|
| D1  | BloodHound full collection | Critical | T1087.002 | sharphound, bloodhound-python, azurehound | drives every other stage |
| D2  | ACL / Permission Abuse Discovery (GenericAll, WriteDACL, ForceChangePassword, DCSync) | Critical | T1069.002 | bloodhound, powerview, adexplorer, adaclscanner | accessing-credentials (DCSync) / moving-laterally |
| D3  | AD CS Vulnerability Discovery (ESC1–ESC13) | Critical | T1649 | certipy, certify, pspkiaudit, locksmith | escalating-privileges / accessing-credentials |
| D4  | SMB & Network Share Enumeration | Important | T1135 | snaffler, sauroneye, sharpshares, powerview, crackmapexec | accessing-credentials (creds in files) |
| D5  | Logged-on User / Admin Session Hunting | Important | T1033 | powerview, bloodhound, crackmapexec, netsess | moving-laterally (target host with DA session) |
| D6  | Domain User / Group Enumeration | Important | T1087.002 | powerview, adexplorer, ldapdomaindump, windapsearch | input to spray/roast |
| D7  | Forest & Trust Relationship Mapping | Important | T1482 | powerview, bloodhound, adexplorer, impacket | cross-domain pivot |
| D8  | Internal Network Scanning | Important | T1046 | nmap, masscan, crackmapexec, invoke-portscan, pingcastle | finds DCs/admin hosts |
| D9  | Kerberoastable / AS-REP Roastable Discovery | Important | T1558.003 / T1558.004 | bloodhound, powerview, rubeus, impacket-getuserspns, kerbrute | accessing-credentials (offline crack) |
| D10 | GPO Enumeration | Important | T1615 | powerview, bloodhound, group3r, gpoddity, sharpgpo | escalating-privileges / moving-laterally |
| D11 | Domain Controller Discovery (FSMO roles) | Important | T1018 | nslookup, nltest, powerview, crackmapexec, adexplorer | Tier-0 target list |
| D12 | Process / Service Discovery | Important | T1057 / T1007 | seatbelt, tasklist, powerview, wmi | identifies AV/EDR, DA-spawned processes |
| D13 | DNS Enumeration | Important | T1590.002 | adidnsdump, dnsx, powerview, nslookup, dig | hidden hosts |
| D14 | Azure AD / Hybrid Enumeration | Important | T1538 | roadtools, aadinternals, azurehound, stormspotter | hybrid impact path |
| D15 | Password Policy Discovery | Important | T1201 | powerview, crackmapexec, ldap, net | gates password spray |
| D16 | Delegation Discovery (Unconstrained / Constrained / RBCD) | Important | T1558 | bloodhound, powerview, adexplorer, impacket-finddelegation | moving-laterally (TGT capture / S4U) |
| D17 | MSSQL Enumeration | Optional | T1046 | powerupsql, crackmapexec, sqlcmd | moving-laterally via SQL links |
| D18 | Exchange / Mail Enumeration | Optional | T1087 | mailsniper, ruler, crackmapexec | accessing-credentials (mailbox secrets) |
| D19 | Printer / Spooler Discovery | Optional | T1187 | spoolsample, petitpotam, crackmapexec, impacket | moving-laterally (coercion → unconstrained TGT) |
| D20 | LAPS / gMSA Reader Discovery | Optional | T1555 | lapstoolkit, powerview, crackmapexec, adexplorer | accessing-credentials (LAPS pw read) |

## Selection guidance

- Always run D1 first; D2 follows automatically once BloodHound data is
  ingested.
- D3 runs in parallel with D1 — different network paths, no contention.
- D9 is cheap and stealthy; run it any time a new domain user is acquired.
- D14 only if `context.scope.azure_tenant` is set.
- Optional techniques (D17-D20) are run only when an earlier finding points to
  them (e.g. MSSQL linked-server reachable, Spooler enabled, LAPS readers
  found in D2).

## D1 — BloodHound full collection (Critical)

- MITRE: T1087.002
- Tools: `sharphound` (Windows), `bloodhound-python` (Linux)
- Preconditions: any domain user in `credentials[]`; reachable DC.
- Success indicators: ≥ 1 SharpHound zip parsed; graph ingested with ≥ 10
  user nodes; `shortest_path_to_DA` length ≥ 1.
- OPSEC: moderate. `--Stealth` reduces volume; `--CollectionMethod DCOnly` is
  quieter but less complete.
- Fallback: `bloodhound-python -c All` from a Linux pivot if SharpHound is
  AV-blocked.
- Pivot hint: drives downstream stage selection.

## D2 — ACL / permission abuse discovery (Critical)

- MITRE: T1069.002
- Tools: BloodHound (post-ingest queries), PowerView `Get-DomainObjectAcl`.
- Preconditions: D1 already ingested.
- Success indicators: ≥ 1 outbound abuse edge (GenericAll / GenericWrite /
  WriteDacl / WriteOwner / ForceChangePassword / AddMember /
  AllExtendedRights / DCSync) from any controlled principal.
- OPSEC: moderate (read-only LDAP; same noise envelope as D1).
- Pivot hint: `WriteDACL on DomainObject` → `accessing-credentials`
  (DCSync grant); `GenericAll on group` → `moving-laterally`.

## D3 — AD CS vulnerability discovery, ESC1-ESC13 (Critical)

- MITRE: T1649
- Tools: `certipy` (Linux, preferred), `certify` (Windows), `pspkiaudit`,
  `locksmith`.
- Preconditions: any domain user; CA host reachable on 443/445.
- Success indicators: parser returns ≥ 1 row in `vulnerable_templates` with an
  ESC1-ESC13 tag.
- OPSEC: stealth (read-only LDAP + DCOM query of the CA).
- Fallback: if `certipy` blocked, query `pKIEnrollmentService` and
  `pKICertificateTemplate` directly via `ldapdomaindump` and pattern-match
  offline.
- Pivot hint: ESC1/4/6/8 → `escalating-privileges`; ESC9/10/13 →
  `accessing-credentials`.

## D9 — Kerberoastable / AS-REP roastable discovery (Important, cheap)

- MITRE: T1558.003 / T1558.004
- Tools: `rubeus`, `impacket-getuserspns`, `kerbrute`.
- Preconditions: one valid domain user (Kerberoast); none required for AS-REP
  user enumeration.
- Success indicators: ≥ 1 SPN account with `encType=RC4-HMAC`, or ≥ 1
  `DONT_REQ_PREAUTH` user.
- OPSEC: stealth (one TGS-REQ is indistinguishable from normal Kerberos).
- Pivot hint: always → `accessing-credentials` (offline crack).

## D16 — Delegation discovery (Important)

- MITRE: T1558
- Tools: BloodHound, PowerView, `impacket-finddelegation`.
- Success indicators: hosts annotated with `unconstrained`, `constrained`,
  `rbcd_writable`.
- Pivot hint: unconstrained non-DC host → `accessing-credentials` (coerce +
  TGT capture chain); RBCD writable → `moving-laterally` (S4U2Proxy).
