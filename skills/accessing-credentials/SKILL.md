---
name: accessing-credentials
description: Harvests credentials (passwords, NTLM hashes, Kerberos tickets, AD CS-issued certs, browser/DPAPI secrets, cloud tokens) sufficient to reach DA-equivalent via DCSync, Kerberoast, AS-REP, LSASS dumping, or NTDS extraction. Use when the agent has acquired SYSTEM/local admin on a host, when DCSync rights are visible in ad_graph, when discovering-environment surfaced Kerberoastable / AS-REP / vulnerable AD CS templates, when password spraying is in scope, or when establishing-persistence needs the krbtgt hash for golden ticket forgery. Covers Kerberoasting, AS-REP roasting, LSASS dumping (comsvcs → nanodump → handlekatz → ppldump → mimikatz OPSEC ladder), DCSync, NTDS extraction, password spraying, Overpass / PTH / PTT collection, SAM / DPAPI / browser theft, AiTM phishing, GPP / LAPS / delegation TGT capture, and cloud token theft.
stage: credaccess
agent: CredAccessAgent
mitre_tactics: ["TA0006"]
default_opsec: moderate
ambient: false
tool_allowlist:
  - rubeus
  - impacket-getuserspns
  - impacket-getnpusers
  - impacket-secretsdump
  - mimikatz
  - sharpkatz
  - comsvcs
  - procdump
  - nanodump
  - handlekatz
  - ppldump
  - edrsandblast
  - hashcat
  - john
  - crackmapexec
  - kerbrute
  - sprayingtoolkit
  - sharpdpapi
  - sharpchrome
  - hackbrowserdata
  - lazagne
  - evilginx
  - lapstoolkit
  - dsinternals
  - vssadmin
  - ntdsutil
  - aadinternals
  - tokentactics
  - roadtoken
  - get-gpppassword
budget:
  max_tool_calls: 25
  max_wallclock_min: 25
---

# Accessing credentials

Harvest credentials sufficient to compromise at least one higher-privileged
account than the current beachhead user, and ideally reach DA-equivalent via
DCSync or NTDS. Every harvested secret is committed to `credentials[]` with
full provenance (`source`, `usable_for`, `validated_against`).

## Quick start

1. **Kerberoast + AS-REP roast** first — cheap, stealthy, requires only any
   domain user (C1, C2).
2. If SYSTEM available, run **LSASS dump** via OPSEC ladder (C3):
   `comsvcs → nanodump → handlekatz → ppldump → mimikatz`. Stop on first
   success; do NOT escalate to a louder tool unless the previous one was
   blocked.
3. If `ad_graph` shows DCSync rights for any controlled principal, run
   **DCSync** via `impacket-secretsdump` (C4, destructive-gate ACK required).
4. Extract NTDS.dit (C5) only if DCSync is unavailable.
5. Templated commands: [reference/tool-commands.md](reference/tool-commands.md).
   Full technique catalogue: [reference/techniques.md](reference/techniques.md).

## Preconditions

- Local admin / SYSTEM on the source host for any LSASS / SAM / DPAPI
  technique. Use `escalating-privileges` first if `access_level < local_admin`.
- For DCSync: controlled principal with `DS-Replication-Get-Changes` and
  `DS-Replication-Get-Changes-All` (verify edge in `ad_graph`).
- For password spray: `password_policy` from D15 in memory; respect
  `lockout_threshold` strictly.
- AMSI bypass + ETW patch loaded if Mimikatz / Rubeus run inline (ambient
  `evading-defenses`).

## Tool acquisition

Most credential-access tools are NOT pre-installed on the target. The agent
must autonomously determine what tools are needed, discover how to obtain
them, and handle the full lifecycle — all based on intelligence gathered
from the target environment.

**Strategy:**
1. **Enumerate first**: Before acquiring ANY tool, check what is already
   available on the foothold — installed runtimes (Python, .NET, Ruby),
   native transfer utilities, package managers, network access to external
   sources.
2. **Native first**: Prefer OS-built-in methods. Many credential-access
   techniques can be done with native commands and DLLs already present
   on the system — use these before downloading anything.
3. **Search when needed**: If a 3rd-party tool is required and you don't
   know the exact source, use web search to find the latest working
   download URL, alternative tool, or in-memory approach.
4. **Inline pattern**: Handle check → acquire → rename → use → cleanup
   in a SINGLE ability. Rename downloaded binaries to innocuous names.
5. **Adapt**: If a tool or download method is blocked by security controls,
   try alternative acquisition methods, in-memory execution, or a
   completely different tool that achieves the same goal.

The more environment details the agent collects (OS version, AV product,
available runtimes, network policies), the better it can choose the right
tool and acquisition path.

## Critical techniques

| # | Technique | MITRE | Tools |
|---|---|---|---|
| C1 | Kerberoasting | T1558.003 | `rubeus`, `impacket-getuserspns` |
| C2 | AS-REP roasting | T1558.004 | `rubeus`, `impacket-getnpusers`, `kerbrute` |
| C3 | LSASS dumping | T1003.001 | `comsvcs` → `nanodump` → `handlekatz` → `ppldump` → `mimikatz` |
| C4 | DCSync | T1003.006 | `impacket-secretsdump`, `mimikatz lsadump::dcsync` |
| C5 | NTDS.dit extraction | T1003.003 | `vssadmin` + `ntdsutil` + `impacket-secretsdump` |

Important (C6-C13) and Optional (C14-C18) techniques and per-technique cards
in [reference/techniques.md](reference/techniques.md).

## LSASS OPSEC ladder (C3)

Always try in this order; stop on first success.

| Step | Tool | Why |
|---|---|---|
| 1 | `comsvcs` | Signed LOLBin (`rundll32 comsvcs.dll MiniDump`). Quietest. |
| 2 | `nanodump` | Handle duplication; avoids direct `OpenProcess` on lsass. |
| 3 | `handlekatz` | Cloned handle; bypasses many EDR hooks. |
| 4 | `ppldump` | Required if `RunAsPPL == true` (LSA Protection). |
| 5 | `mimikatz` `sekurlsa::logonpasswords` | Last resort; expect EDR alert. |

## Pivot conditions

- `credentials.any(secret_type == 'nt_hash' and user_in_priv_group)` → `moving-laterally` (PTH).
- `credentials.any(username endswith 'krbtgt')` → `establishing-persistence` (golden ticket) + `achieving-impact`.
- `findings.any(technique == 'DCSync' and status == 'success')` → `achieving-impact`.
- AS-REP / Kerberoast hash cracked → `moving-laterally`.
- LSASS dump blocked twice by EDR → `evading-defenses` ambient (EDRSandBlast / BYOVD), retry once, otherwise signal `partial` and pivot to `moving-laterally` via PTT.
- New cleartext password validated → optionally rerun `discovering-environment` with the new identity to re-collect ACLs.

## Self-critique

- "Kerberoast returned 0 hashes despite D9 listing SPNs → RC4 likely disabled
  for those accounts. Re-request with AES and route to hashcat mode 19700/
  19600 instead of 13100."
- "LSASS dump produced a 0-byte file → AV truncated; switch tool. Do not
  re-run the same tool."
- "DCSync `Logon failure` despite BloodHound showing the edge → the right may
  apply to a different principal (group vs user, inherited). Verify the exact
  edge before retrying."
- "Hashcat exhausted on rockyou + best64 → escalate to OneRuleToRuleThemAll,
  then mark `crack_infeasible` and use Overpass with the hash directly."
- "Stop spraying immediately if `lockout_observation_window` is misconfigured.
  Verify D15 password policy is in memory before starting."

## Evidence to capture

- All hashes / tickets → vault (`vault://C-####`), never plain artifact files.
- DCSync output → vault-encrypted; only count + `krbtgt-present` flag in the
  report.
- LSASS dumps → vault; delete from host after parser completes.
- `pypykatz` JSON parse → finding `output_summary`.
- `klist` snapshots before/after PTT for the report timeline.
