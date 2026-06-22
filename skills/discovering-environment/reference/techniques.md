# Discovery — Techniques reference

Full catalogue for the `discovering-ad-environment` skill. `SKILL.md` references
technique IDs (D1–D20) for step-to-MITRE mapping; this file holds the complete
table, per-technique cards with commands, expected output, OPSEC, and pivot hints,
and quick selection guidance.

## Contents
- Technique table (D1–D20, Critical → Important → Optional)
- Per-technique cards (Critical first, then Important, then Optional)
- Selection guidance

---

## Technique table

| #   | Technique                                                                             | Priority  | MITRE               | Primary tools                                               | Pivot hint                                            |
|-----|---------------------------------------------------------------------------------------|-----------|---------------------|-------------------------------------------------------------|-------------------------------------------------------|
| D1  | BloodHound full collection                                                            | Critical  | T1087.002           | sharphound, bloodhound-python, azurehound                   | drives every other stage                              |
| D2  | ACL / Permission Abuse Discovery (GenericAll, WriteDACL, ForceChangePassword, DCSync) | Critical  | T1069.002           | [DirectoryEntry].ObjectSecurity (Win), ldapsearch (Lin), daclenum | accessing-credentials (DCSync) / moving-laterally     |
| D3  | AD CS Vulnerability Discovery (ESC1–ESC13)                                            | Critical  | T1649               | certipy, certify, pspkiaudit, locksmith                     | escalating-privileges / accessing-credentials         |
| D4  | SMB & Network Share Enumeration                                                       | Important | T1135               | snaffler, sauroneye, sharpshares, powerview, netexec        | accessing-credentials (creds in files)                |
| D5  | Logged-on User / Admin Session Hunting                                                | Important | T1033               | powerview, bloodhound, netexec, netsess                     | moving-laterally (target host with DA session)        |
| D6  | Domain User / Group Enumeration                                                       | Important | T1087.002           | [DirectorySearcher] (Win), ldapsearch (Lin)                 | input to spray/roast                                  |
| D7  | Forest & Trust Relationship Mapping                                                   | Important | T1482               | [Domain]::GetCurrentDomain() (Win), ldapsearch trustedDomain (Lin) | cross-domain pivot                              |
| D8  | Internal Network Scanning                                                             | Important | T1046               | nmap, masscan, netexec, invoke-portscan, pingcastle         | finds DCs/admin hosts                                 |
| D9  | Kerberoastable / AS-REP Roastable Discovery                                           | Critical  | T1558.003/T1558.004 | bloodhound, powerview, rubeus, impacket-getuserspns, kerbrute | accessing-credentials (offline crack)               |
| D10 | GPO Enumeration                                                                       | Important | T1615               | [DirectorySearcher] CN=Policies + findstr SYSVOL (Win), ldapsearch (Lin) | escalating-privileges / moving-laterally  |
| D11 | Domain Controller Discovery (FSMO roles)                                              | Important | T1018               | [DirectorySearcher] UAC filter (Win), ldapsearch (Lin), nltest | Tier-0 target list                                 |
| D12 | Process / Service Discovery                                                           | Important | T1057 / T1007       | seatbelt, winpeas, tasklist, powerview, wmi                 | identifies AV/EDR, DA-spawned processes               |
| D13 | DNS Enumeration                                                                       | Important | T1590.002           | adidnsdump, dnsx, powerview, nslookup, dig                  | hidden hosts / new segments                           |
| D14 | Azure AD / Hybrid Enumeration                                                         | Important | T1538               | roadtools, aadinternals, azurehound, stormspotter           | hybrid impact path                                    |
| D15 | Password Policy Discovery                                                             | Critical  | T1201               | [DirectoryEntry] lockoutThreshold attr (Win), ldapsearch (Lin) | gates password spray — abort if lockout threshold ≤ 3 |
| D16 | Delegation Discovery (Unconstrained / Constrained / RBCD)                             | Important | T1558               | [DirectorySearcher] UAC/msDS attrs (Win), ldapsearch (Lin)  | moving-laterally (TGT capture / S4U)                  |
| D17 | MSSQL Enumeration                                                                     | Optional  | T1046               | powerupsql, netexec, sqlcmd                                 | moving-laterally via SQL links                        |
| D18 | Exchange / Mail Enumeration                                                           | Optional  | T1087               | mailsniper, ruler, netexec                                  | accessing-credentials (mailbox secrets)               |
| D19 | Printer / Spooler Discovery                                                           | Optional  | T1187               | spoolsample, petitpotam, netexec, impacket                  | moving-laterally (coercion → unconstrained TGT)       |
| D20 | LAPS / gMSA Reader Discovery                                                          | Optional  | T1555               | [DirectorySearcher] ms-Mcs-AdmPwd (Win), ldapsearch (Lin)  | accessing-credentials (LAPS pw read)                  |

---

## Selection guidance

- Always run D1 first; D2 follows automatically once BloodHound data is ingested.
- D3 runs in parallel with D1 — different network paths, no contention.
- D9 and D15 are Critical — run D15 before any spray; run D9 any time a new domain
  user is acquired.
- D14 only if `context.scope.azure_tenant` is set or hybrid indicators found in DNS/Step 8.
- Optional techniques (D17–D20) run only when an earlier finding points to them:
  - D17: TCP/1433 or TCP/1434 observed in Step 5.
  - D18: OWA banner / TCP/25 / TCP/587 observed in Step 5.
  - D19: TCP/445 + TCP/135 on non-DC hosts in Step 5, or spooler confirmed in D16.
  - D20: LAPS readers found in D2 output.
- **Re-run D1, D5, D9 on every new machine reached via lateral movement.**

---

## D1 — BloodHound full collection (Critical)

- MITRE: T1087.002
- Tools: `SharpHound.exe` (Windows — PRIMARY), `bloodhound-python` (Linux fallback), `azurehound` (Azure/hybrid)
- **Tool name clarification:**
  - `SharpHound.exe` = the Windows collector binary that runs on the target host and produces the zip file.
  - `BloodHound` = the GUI/server application that ingests the zip SharpHound produces.
  - On a Windows domain-joined host (agent.exe running inside the target network): ALWAYS use `SharpHound.exe`.
  - `bloodhound-python` = Linux-only fallback for when SharpHound.exe cannot run.
  - Never try to run `bloodhound` or `bloodhound.py` directly on Windows — it does not exist as a Windows CLI tool.
- Preconditions: any domain user in `credentials[]`; reachable DC; on Windows: `SharpHound.exe` present in agent tool path.
- **Preconditions check:** assert `network.has_domain_controller=true`; assert `recon.ad_present=true`; assert `host.platform=windows` → use SharpHound.exe; assert `host.platform=linux` → use bloodhound-python.
- Success indicators: ≥ 1 SharpHound zip produced; zip contains `computers.json`, `users.json`, `groups.json`; graph ingested with ≥ 10 user nodes; `shortest_path_to_DA` length ≥ 1.
- OPSEC: moderate. `--Stealth` reduces volume; `--CollectionMethod DCOnly` is quieter but less complete.
- Fallback: `bloodhound-python -c All` from a Linux pivot if SharpHound.exe is AV-blocked on the Windows target.
- **On success — save:** `ad.bloodhound_zip` = path to zip file. Write zip to `temp/bas/bloodhound/<timestamp>_BloodHound.zip`.
- Expected output:
  - SharpHound.exe produces a zip file containing: `computers.json`, `users.json`, `groups.json`, `sessions.json`, `acls.json`, `gpos.json`
  - Shortest Paths to Domain Admins graph in BloodHound GUI
  - List of Kerberoastable accounts with SPNs
  - List of accounts with DCSync rights (DS-Replication-Get-Changes*)
  - Hosts with unconstrained / constrained delegation flags
  - ACL attack paths: GenericAll, WriteDACL, ForceChangePassword edges
  - Active DA sessions: which machine DA is logged on right now
- Cycle / next:
  - DA path found → `moving-laterally` (skip remaining steps)
  - Kerberoastable accounts found → D9 → `accessing-credentials`
  - GenericAll / WriteDACL found → D2 → `accessing-credentials`
  - After lateral move to new machine → RE-RUN from that machine (SharpHound.exe again)

---

## D2 — ACL / permission abuse discovery (Critical)

- MITRE: T1069.002
- Tools (Windows): BloodHound post-ingest queries (zero traffic) + `[DirectoryEntry].ObjectSecurity.Access` for live LDAP read — no PowerView.
- Tools (Linux): `ldapsearch` nTSecurityDescriptor + `daclenum.py`.
- Preconditions: D1 already ingested (BloodHound path) or domain credentials (LDAP path).
- Success indicators: ≥ 1 outbound abuse edge (GenericAll / GenericWrite / WriteDACL /
  WriteOwner / ForceChangePassword / AddMember / AllExtendedRights / DCSync)
  from any controlled principal.
- OPSEC: moderate (read-only LDAP; same noise envelope as D1).
- Expected output:
  - Objects with GenericAll → full control
  - Objects with WriteDACL → grant self any right
  - Objects with WriteOwner → change object owner
  - Objects with ForceChangePassword → reset without knowing old password
  - Objects with GenericWrite → modify attributes (set SPN for Kerberoast, set RBCD)
  - Format: IdentityReference, ObjectDN, ActiveDirectoryRights
- Cycle / next:
  - WriteDACL on domain object → grant self DCSync → `accessing-credentials`
  - GenericAll on group → add self → elevated group membership
  - GenericWrite on computer → RBCD attack → `moving-laterally`

---

## D3 — AD CS vulnerability discovery, ESC1–ESC13 (Critical)

- MITRE: T1649
- Tools: `certipy` (Linux, preferred), `certify` (Windows), `pspkiaudit`, `locksmith`
- Preconditions: any domain user; CA host reachable on 443/445.
- Success indicators: parser returns ≥ 1 row in `vulnerable_templates` with an ESC1–ESC13 tag.
- OPSEC: stealth (read-only LDAP + DCOM query of the CA).
- Fallback: if `certipy` is blocked, query `pKIEnrollmentService` and
  `pKICertificateTemplate` directly via `ldapdomaindump` and pattern-match offline.
- Expected output:
  - Certify `[!] Vulnerable Certificate Templates` list
  - Template name, CA name, enrollment rights (who can enroll)
  - ESC1: `msPKI-Certificate-Name-Flag: ENROLLEE_SUPPLIES_SUBJECT` + no manager approval
  - ESC4: low-priv user has WriteProperty/WriteDacl on template
  - ESC8: `Certificate Authority Web Enrollment is enabled`
  - Certipy: vulnerable templates saved to `.txt` and BloodHound JSON
- Cycle / next:
  - ESC1 / 4 / 6 / 8 → `escalating-privileges`
  - ESC9 / 10 / 13 → `accessing-credentials`

---

## D4 — SMB & network share enumeration (Important)

- MITRE: T1135
- Tools: snaffler, sauroneye, sharpshares, powerview, netexec
- Preconditions: any domain user; SMB reachable on target hosts.
- OPSEC: moderate. Snaffler generates SMB traffic to many hosts; use `-s` (stealth)
  to limit scanning.
- Expected output:
  - List of readable shares: `\\server\share_name`
  - Snaffler tiers: Black=credentials, Red=config with password, Amber=interesting
  - SYSVOL `Groups.xml` files containing `cpassword` values
  - Found files: `web.config`, `unattend.xml`, `*.ps1`, `*.bat` with hardcoded creds
- Cycle / next:
  - Credentials / hashes found in files → `accessing-credentials`
  - GPP `cpassword` found → `accessing-credentials` (GPP decrypt)

---

## D5 — Logged-on user / admin session hunting (Important)

- MITRE: T1033
- Tools: PowerView, BloodHound, netexec, netsess
- Preconditions: domain user with net-session query rights (default for domain members).
- OPSEC: moderate. Use `Invoke-UserHunter -Stealth` to limit queries to DCs and servers only.
- Expected output:
  - List: `username@domain → computername` where they are logged on
  - `Invoke-UserHunter` output with `IPAddress` and `SessionUser`
  - Target hosts where DA / privileged users are active right now
  - `User has access to machine` confirmation from `-CheckAccess`
- Success indicators: ≥ 1 host where a privileged session is confirmed and reachable.
- Cycle / next: DA session on reachable host → `moving-laterally` (token impersonation / PTT)

---

## D6 — Domain user & group enumeration (Important)

- MITRE: T1087.002
- Tools (Windows): `[DirectorySearcher]` with LDAP filters — no PowerView or AD module.
- Tools (Linux): `ldapsearch` with objectClass=user / objectClass=group filters.
- Preconditions: any domain user credential; DC reachable on port 389.
- Expected output: complete user list with SPNs and admincount; group membership
  including nested groups; privileged group members (Domain Admins, Enterprise Admins,
  Schema Admins, Backup Operators, Account Operators).
- Cycle / next: user list feeds D9 (Kerberoast / AS-REP roast) and password spray.

---

## D7 — Forest & trust relationship mapping (Important)

- MITRE: T1482
- Tools: powerview, bloodhound, adexplorer, impacket
- Preconditions: any domain user.
- OPSEC: stealth (read-only LDAP).
- Expected output: trust list with SourceName, TargetName, TrustType, TrustDirection,
  TrustAttributes. Flag bidirectional trusts with SID filtering disabled = full
  compromise chain across all trusted domains.
- Cycle / next: trust found → `moving-laterally` across trust boundary.

---

## D8 — Internal network scanning (Important)

- MITRE: T1046
- Tools: nmap, masscan, netexec, invoke-portscan, pingcastle
- Preconditions: network reachability to target segment.
- Note: covered by Phase A Steps 4–6 for the primary subnet. Re-invoke for newly
  discovered segments from D13 DNS output.
- Cycle / next: newly found DCs → repeat Phase B from Step 8 in new segment.

---

## D9 — Kerberoastable / AS-REP roastable discovery (Critical)

- MITRE: T1558.003 / T1558.004
- Tools: `rubeus`, `impacket-getuserspns`, `impacket-getnpusers`, `kerbrute`
- Preconditions: one valid domain user (Kerberoast); none required for AS-REP enumeration.
- Success indicators: ≥ 1 SPN account with `encType=RC4-HMAC`, or ≥ 1 `DONT_REQ_PREAUTH` user.
- OPSEC: stealth (one TGS-REQ is indistinguishable from normal Kerberos traffic).
- Expected output:
  - List: `samaccountname → ServicePrincipalName` mapping
  - Rubeus stats: number of accounts, encryption types (RC4=crackable, AES=harder)
  - Hashes in `$krb5tgs$23$` format (RC4) → Hashcat mode 13100
  - AS-REP hashes in `$krb5asrep$23$` format → Hashcat mode 18200
  - Target priority: `admincount=1` service accounts = DA equivalent if cracked
- Cycle / next: always → `accessing-credentials` (offline crack)

---

## D10 — GPO enumeration (Important)

- MITRE: T1615
- Tools: powerview, bloodhound, group3r, gpoddity, sharpgpo
- Preconditions: any domain user.
- Expected output: all GPO names, IDs, and status; local-group mappings that place
  non-admin accounts into privileged local groups; GPOs linked to OU containing DCs.
- Cycle / next:
  - GPO with writable permissions → `escalating-privileges`
  - GPO placing controlled account in local admins on server → `moving-laterally`

---

## D11 — Domain controller discovery / FSMO roles (Important)

- MITRE: T1018
- Tools: nslookup, nltest, powerview, netexec, adexplorer
- Preconditions: any domain user or unauthenticated (SRV DNS records).
- Expected output: all DC hostnames with IPv4, FSMO role holders (PDC Emulator,
  RID Master, Infrastructure Master, Schema Master, Domain Naming Master).
- Cycle / next: Tier-0 DC list feeds all subsequent Phase B steps.

---

## D12 — Process / service discovery (Important)

- MITRE: T1057 / T1007
- Tools: seatbelt, winpeas, tasklist, powerview, wmi
- Preconditions: local execution on target host (any privilege; SYSTEM for full output).
- Expected output:
  - Seatbelt: AV products running, EDR hooks, PowerShell logging status
  - WinPEAS: `[+]` = confirmed finding, `[?]` = potential finding
  - `whoami /all`: current SID, group memberships, token privileges
  - Token privileges of interest: SeImpersonatePrivilege (Potato attacks),
    SeBackupPrivilege (read NTDS.dit), SeDebugPrivilege (attach to LSASS)
  - Installed AV/EDR: defines which tools can run without modification
- Cycle / next:
  - EDR present → `defense-evasion` (AMSI + EDR bypass) before any noisy tools
  - SeImpersonatePrivilege → `escalating-privileges` (Potato attacks)

---

## D13 — DNS enumeration (Important)

- MITRE: T1590.002
- Tools: adidnsdump, dnsx, powerview, nslookup, dig
- Preconditions: any domain user; DNS server reachable.
- Expected output:
  - `adidnsdump` CSV: all DNS A/CNAME/MX records in AD zones
  - Hidden internal hostnames not visible via external recon
  - New subnets and IP ranges to scan (segmented network discovery)
  - DC hostnames: `dc01.domain.local`, `dc02.domain.local`
- Cycle / next: new segment found → pivot (tunneling) → restart Phase A in new segment.

---

## D14 — Azure AD / hybrid enumeration (Important)

- MITRE: T1538
- Tools: roadtools, aadinternals, azurehound, stormspotter
- Preconditions: hybrid join indicators or `*.onmicrosoft.com` UPN suffix observed.
- Expected output: tenant ID, Azure AD Connect server hostname, hybrid-joined hosts,
  MSOL sync account (on-prem DA → MSOL → Azure Global Admin in default hybrid config).
- Cycle / next: MSOL account found → `moving-laterally` (cloud lateral movement).

---

## D15 — Password policy discovery (Critical)

- MITRE: T1201
- Tools (Windows): `[DirectoryEntry]` read of `lockoutThreshold`, `lockoutObservationWindow`, `minPwdLength` directly from domain object — no PowerView or AD module.
- Tools (Linux): `ldapsearch -b "<BASE_DN>" -s base lockoutThreshold lockoutObservationWindow`.
- Preconditions: any domain user. **Always run before any password spray.**
- Success indicators: `LockoutThreshold` and `LockoutObservationWindow` values retrieved.
- **Gate rule: abort spray planning if lockout threshold ≤ 3.**
- Expected output:
  - `LockoutThreshold`: e.g. 5 → max 4 safe spray attempts
  - `LockoutObservationWindow`: e.g. 30 min → reset counter after this period
  - `MinPasswordLength`, `PasswordComplexity` settings
  - Fine-grained policies per group if any exist
  - Safe spray rate: 1 attempt per (ObservationWindow + 5 min buffer)
- Cycle / next: feeds `accessing-credentials` (password spraying) — defines safe rate.

---

## D16 — Delegation discovery (Important)

- MITRE: T1558
- Tools (Windows): `[DirectorySearcher]` with `userAccountControl:1.2.840.113556.1.4.803:=524288` (unconstrained) and `msDS-AllowedToDelegateTo=*` (constrained) — no PowerView.
- Tools (Linux): `ldapsearch` with same filters.
- Preconditions: any domain user.
- Expected output: hosts annotated with `unconstrained`, `constrained`, `rbcd_writable`.
- Cycle / next:
  - Unconstrained non-DC host → `accessing-credentials` (coerce + TGT capture)
  - RBCD writable → `moving-laterally` (S4U2Proxy)

---

## D17 — MSSQL enumeration (Optional)

- MITRE: T1046
- Tools: powerupsql, netexec, sqlcmd
- Preconditions: TCP/1433 or TCP/1434 observed in Step 5.
- Expected output: accessible MSSQL instances; linked-server chains; `xp_cmdshell`
  availability; `sysadmin` role holders.
- Cycle / next: linked server with `sysadmin` on remote instance → `moving-laterally`.

---

## D18 — Exchange / mail enumeration (Optional)

- MITRE: T1087
- Tools: mailsniper, ruler, netexec
- Preconditions: TCP/443 with OWA banner, TCP/25, or TCP/587 found in Step 5.
- Expected output: Exchange server hostnames; GAL contacts list; mailbox credentials
  or tokens found via Ruler / MailSniper.
- Cycle / next: credentials found → `accessing-credentials`.

---

## D19 — Printer / spooler discovery (Optional)

- MITRE: T1187
- Tools: spoolsample, petitpotam, netexec, impacket
- Preconditions: TCP/445 + TCP/135 on non-DC hosts; best paired with unconstrained
  delegation host found in D16.
- Expected output: hosts with Spooler service running; hosts vulnerable to PetitPotam
  (unauthenticated NTLM coercion).
- Cycle / next: spooler host + unconstrained delegation → `moving-laterally` (TGT capture).

---

## D20 — LAPS / gMSA reader discovery (Optional)

- MITRE: T1555
- Tools: lapstoolkit, powerview, netexec, adexplorer
- Preconditions: any domain user; LAPS deployed (ms-Mcs-AdmPwd attribute present).
- Expected output:
  - Computers where current user can read `ms-Mcs-AdmPwd` (plaintext local admin password)
  - LAPS password format: random 14-char string e.g. `Q!3xK9mP2nL7vA`
  - gMSA accounts where current user is in `PrincipalsAllowedToRetrieveManagedPassword`
- Cycle / next: readable LAPS / gMSA → `accessing-credentials`.
