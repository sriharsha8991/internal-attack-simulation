---
name: enumerating-active-directory
description: Enumerates Active Directory after discovery confirms a domain controller or domain-joined foothold. Use immediately after discovering-environment sets recon.ad_present or network.has_domain_controller, before privilege escalation or credential access. Produces BloodHound collection, DC/domain inventory, password policy, Kerberoast and AS-REP candidates, AD CS exposure, ACL abuse paths, delegation, trusts, GPO, DNS, sessions, shares, and LAPS/gMSA findings using built-in LDAP first with tool fallbacks only when needed.
stage: ad-enumeration
agent: ActiveDirectoryEnumerationAgent
mitre_tactics: ["TA0007", "TA0006", "TA0008"]
default_opsec: moderate
ambient: false
tool_allowlist:
  - sharphound
  - bloodhound-python
  - ldapsearch
  - netexec
  - powerview
  - certipy
  - certify
  - daclenum
  - rubeus
  - impacket-getuserspns
  - impacket-getnpusers
  - impacket-finddelegation
  - rpcclient
  - adidnsdump
  - nslookup
  - group3r
  - lapstoolkit
budget:
  max_tool_calls: 20
  max_wallclock_min: 20
---

# Enumerating Active Directory

Build a focused AD attack-surface map after `discovering-environment` confirms
a domain controller or domain-joined host. This phase must run before
`escalating-privileges`, `accessing-credentials`, or `moving-laterally` uses AD
assumptions.

## Quick start

1. Start from confirmed discovery memory: `network.dc_ip`, `domain.base_dn`,
   `host.domain_name`, `network.services`, and any available domain principal.
2. Run the collector appropriate to the foothold: SharpHound.exe on Windows,
   bloodhound-python on Linux. Do not swap them.
3. Enumerate DCs, users, groups, trusts, password policy, DNS, GPOs,
   delegation, Kerberoastable and AS-REP roastable accounts.
4. Check AD CS templates and ACL abuse paths before recommending privilege
   escalation or credential access.
5. Use built-in LDAP first: `[ADSI]`, `[ADSISearcher]`, or explicit
  `New-Object System.DirectoryServices.DirectorySearcher` on Windows;
  `ldapsearch` on Linux. If retry feedback reports `type_not_found` for
  bare `[DirectorySearcher]`, do not repeat that syntax. Third-party tools
  are fallbacks or task-specific.

## Preconditions

- `network.has_domain_controller=true` or `recon.ad_present=true`, OR
  `host.domain_joined=true` from discovery.
- A concrete DC target exists in memory, or the plan dynamically discovers one
  from DNS/LDAP without placeholders.
- Discovery has already run Step 0 and Phase A network service detection.

## Required outputs

- `ad.bloodhound_zip`
- `domain.dcs`
- `ad.users`
- `ad.kerberoastable_accounts`
- `ad.asrep_roastable_accounts`
- `domain.password_policy`
- `ad.adcs_vulns`
- `ad.acl_abuse_paths`
- `ad.da_sessions`
- `domain.trusts`
- `ad.unconstrained_delegation`
- `domain.dns_records`
- `ad.gpos`
- `ad.laps_readable_hosts`

## Evidence to capture

- `bloodhound/<timestamp>_BloodHound.zip`
- `users.txt`
- `password_policy.txt`
- `kerberoastable.txt`
- `delegation.txt`
- `trusts.txt`
- `acl_abuse.csv`
- `adcs.txt`
- `gpos.txt`
- `laps.txt`

## Pivot conditions

- `ad.adcs_vulns` non-empty -> `escalating-privileges` with AD CS path.
- `ad.kerberoastable_accounts` or `ad.asrep_roastable_accounts` non-empty ->
  `accessing-credentials`.
- `ad.acl_abuse_paths` non-empty -> `accessing-credentials` for DCSync grant
  or credential material needed to exploit the path.
- `ad.da_sessions` on a reachable host -> `moving-laterally` or
  `accessing-credentials` depending on current access.
- No AD path found -> `escalating-privileges` for local host escalation.

## Self-critique

- Reject plans that run password spraying before `domain.password_policy` is
  confirmed.
- Reject plans that skip BloodHound collection and then claim ACL/delegation
  coverage.
- Reject unresolved placeholders such as `<DC_IP>` or `#{domain.base_dn}`.
- Prefer partial, typed evidence over aborting the whole phase on one LDAP
  failure; every LDAP step must have fallback and error output.
- Treat stdout/stderr `ERROR`, `failed`, `Unable to find type`, unsupported
  parameter, or access-denied markers as real failures even if the backend
  reports exit code 0. Valid empty results must be written as `NONE:` or `OK:`
  rather than `failed`.
- On retry, read structured `retry_feedback` and repair the exact failed
  command. For `type_not_found` on `[DirectorySearcher]`, pivot to
  `[ADSISearcher]`, explicit `New-Object ...DirectorySearcher`, Linux/sidecar
  `ldapsearch`, or `netexec`; do not retry the same failing form.