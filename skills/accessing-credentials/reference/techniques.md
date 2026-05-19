# Credential Access — Techniques reference

Full catalogue for `accessing-credentials`.

## Contents
- Technique table C1-C18
- Per-technique cards: C1, C2, C3, C4, C5
- Cracking modes quick-reference (hashcat)

## Technique table

| # | Technique | Priority | MITRE | Primary tools | Notes |
|---|---|---|---|---|---|
| C1  | Kerberoasting | Critical | T1558.003 | rubeus, impacket-getuserspns, powerview, mimikatz | No admin needed; first action with any domain user |
| C2  | AS-REP roasting | Critical | T1558.004 | rubeus, impacket-getnpusers, kerbrute | No creds needed for enum phase |
| C3  | LSASS dumping | Critical | T1003.001 | comsvcs, nanodump, handlekatz, mimikatz, procdump, ppldump, edrsandblast | Requires local admin / SYSTEM |
| C4  | DCSync | Critical | T1003.006 | mimikatz, impacket-secretsdump, sharpkatz | Requires DCSync rights; pulls krbtgt |
| C5  | NTDS.dit extraction | Critical | T1003.003 | vssadmin, ntdsutil, impacket-secretsdump, dsinternals | Source-of-truth dump |
| C6  | Password spraying | Important | T1110.003 | crackmapexec, kerbrute, sprayingtoolkit, ruler, mailsniper | Respect lockout policy (D15) |
| C7  | Overpass-the-Hash / Pass-the-Key | Important | T1550.002 | rubeus, mimikatz, impacket | NTLM hash → TGT (blends with Kerberos) |
| C8  | Pass-the-Hash (collection sense) | Important | T1550.002 | mimikatz, impacket, crackmapexec | Also a `moving-laterally` use technique |
| C9  | Pass-the-Ticket (collection sense) | Important | T1550.003 | rubeus, mimikatz, impacket | TGT/TGS injection |
| C10 | SAM extraction | Important | T1003.002 | mimikatz, impacket-secretsdump, reg, crackmapexec | Local-admin shared-password lateral |
| C11 | DPAPI secret extraction | Important | T1555.003 | mimikatz, sharpdpapi, dpapick, sharpchrome | Browser / RDP / Credential Manager |
| C12 | Browser credential theft | Important | T1555.003 | lazagne, sharpchrome, hackbrowserdata, browserghost | Often yields cloud SSO cookies |
| C13 | AiTM credential phishing | Important | T1557 | evilginx, modlishka, muraena, evilnovnc | When spray is blocked |
| C14 | Clipboard / keylogging | Optional | T1056.001 | cobaltstrike, metasploit, get-keystrokes | Passive — long dwell |
| C15 | Cloud token theft | Optional | T1528 | tokentactics, roadtoken, aadinternals, lazagne | Hybrid environments |
| C16 | GPP password extraction | Optional | T1552.006 | get-gpppassword, crackmapexec | Old but still found in SYSVOL |
| C17 | LAPS password extraction | Optional | T1555 | lapstoolkit, crackmapexec, powerview | Requires LAPS reader rights |
| C18 | Kerberos delegation TGT capture | Optional | T1558 | rubeus, powerview, impacket | Force DC auth → capture TGT |

## hashcat mode quick-reference

| Hash | Mode |
|---|---|
| Kerberos 5 TGS-REP RC4 (Kerberoast) | 13100 |
| Kerberos 5 TGS-REP AES128 | 19600 |
| Kerberos 5 TGS-REP AES256 | 19700 |
| Kerberos 5 AS-REP RC4 | 18200 |
| NTLM | 1000 |
| NetNTLMv2 | 5600 |

## C1 — Kerberoasting (Critical)

- Tools: `rubeus kerberoast`, `impacket-getuserspns -request`.
- Preconditions: ≥1 SPN target (from D9) or run `Get-DomainUser -SPN` first.
- Success: ≥1 `$krb5tgs$23$...` hash; cracked offline → cleartext credential.
- OPSEC: stealth on collection (one TGS-REQ each); cracking is offline.
- Fallback: if RC4 disabled domain-wide, use AES (`/aes`), hashcat 19700/19600
  (slower but feasible).
- Pivot: cracked → `moving-laterally` (PTH/PTT to host where the SPN account
  is admin).

## C2 — AS-REP roasting (Critical)

- Success: ≥1 `$krb5asrep$...` hash; cracked → cleartext credential.
- OPSEC: stealth.

## C3 — LSASS dumping (Critical, EDR-loud)

- OPSEC ladder (see SKILL.md). Stop on first success.
- Preconditions: SYSTEM or local admin with `SeDebugPrivilege`. If RunAsPPL,
  use `ppldump` or `edrsandblast`.
- Success: dump parsed by `pypykatz` returns ≥1 NTLM hash / Kerberos ticket
  not already in `credentials[]`.
- Pivot: new hashes → `moving-laterally` (PTH); TGTs → `moving-laterally`
  (PTT); DA hash → `achieving-impact`.

## C4 — DCSync (Critical, destructive-gate)

- Tools: `impacket-secretsdump -just-dc-ntlm` (Linux, quieter),
  `mimikatz lsadump::dcsync` (Windows).
- Preconditions: controlled principal has DCSync edge in `ad_graph`.
- Success: krbtgt hash + ≥1 other privileged user hash; AES256 key included.
- OPSEC: moderate — looks like legitimate DC replication, but EventID 4662
  with the replication GUID is logged on DC.
- Destructive gate: YES (per occurrence human ACK).
- Pivot: always → `achieving-impact` (golden ticket prep) and
  `establishing-persistence` (krbtgt for golden ticket).

## C5 — NTDS.dit extraction (Critical, only when DCSync unavailable)

- Tools: DC-side `vssadmin create shadow` + copy `ntds.dit` + `SYSTEM` hive
  → offline `secretsdump.py -ntds -system local`.
- Preconditions: SYSTEM on a DC (prefer DCSync — quieter).
- OPSEC: loud (VSS creation is logged).
- Destructive gate: YES.
