---
name: achieving-impact
description: Demonstrates and documents the engagement objectives — Domain Admin / Tier-0 compromise, crown-jewel access, MITRE ATT&CK coverage, and where in scope, ransomware / BEC / data-exfil simulations on isolated assets. Every action is evidence-producing, never destructive without per-action human ACK, and always reversible or simulation-only. Use when the krbtgt hash is in vault from DCSync, when a controlled principal is a confirmed DA on a DC, when crown-jewel access has been reached via the lateral chain, when the engagement requires final reporting artefacts (ATT&CK Navigator layer, kill-chain SVG, detection-gap analysis), or when scope authorises isolated-host simulations. Covers DCSync proof, DA shell capture, crown-jewel listing (metadata only), ATT&CK mapping, ransomware/BEC simulations, dummy exfil, hybrid cloud impact, and purple-team replay.
stage: impact
agent: ImpactAgent
mitre_tactics: ["TA0040"]
default_opsec: moderate
ambient: false
tool_allowlist:
  - mimikatz
  - impacket-secretsdump
  - sharpkatz
  - crackmapexec
  - rubeus
  - cobaltstrike
  - sliver
  - mailsniper
  - ruler
  - atomic-red-team
  - caldera
  - attack-navigator
  - vectr
  - plextrac
  - aadinternals
  - roadtools
  - tokentactics
  - powerzure
  - dnscat2
budget:
  max_tool_calls: 15
  max_wallclock_min: 20
  destructive_default: ask
---

# Achieving impact

Demonstrate and document the agreed objectives. Every action here is
**evidence-producing**; nothing destructive runs without an explicit
per-action ACK; every action is reversible or simulation-only. Outputs feed
the Reporter (system_design §11): MITRE ATT&CK Navigator JSON, kill-chain
SVG, detection-gap analysis, remediation table.

## Quick start

1. If `vault.has('krbtgt_nt')`, run **I1 full-domain DCSync proof** (the
   `-just-dc` proof copy goes into the vault; only a redacted summary into
   the report).
2. Capture **I2 DA shell on a DC** — `whoami /groups` showing `Domain
   Admins`, hostname being a DC, `klist` showing the TGT.
3. Walk `context.objectives` and capture **I3 crown-jewel listings** —
   metadata only, never file contents.
4. Run **I4 MITRE ATT&CK mapping** export of `attack_path[]`.
5. If scope authorises, run isolated-host simulations (ransomware, BEC, exfil
   on dummy data). Strict scope check + ACK token per action.
6. Templated commands: [reference/tool-commands.md](reference/tool-commands.md).
   Full technique catalogue: [reference/techniques.md](reference/techniques.md).

## Preconditions

- At least one of:
  - `vault.has('krbtgt_nt')` from DCSync (Critical objective met), OR
  - `credentials.any(user in domain_admins or equivalent)` with validated
    `local_admin` on a DC, OR
  - Documented crown-jewel access via lateral chain.
- For ransomware / BEC / exfil sims: `context.scope.simulations` flag set AND
  per-action ACK in `context.acks[]`.

## Tool acquisition

Impact phase mostly reuses tools already acquired in prior phases. The
agent should check what is already available before attempting to acquire
anything new.

**Strategy:**
1. **Check prior phases**: Tools downloaded or installed during credential
   access, privilege escalation, or lateral movement may still be
   available. Verify before re-acquiring.
2. **Native for data operations**: OS-native commands for file listing,
   archiving, hashing, and copying are sufficient for evidence capture.
   No 3rd-party tools needed for data staging.
3. **Evidence over extraction**: Impact is about PROVING capability, not
   causing damage. Prefer metadata capture (file listings, hash
   verification, directory enumeration) over copying actual sensitive data.
4. **Search if needed**: If a specific capability is required that isn't
   already available, use web search to find the right tool and source.

## Critical techniques

| # | Technique | MITRE | Tools |
|---|---|---|---|
| I1 | DCSync full-domain hash extraction | T1003.006 | `impacket-secretsdump -just-dc`, `mimikatz` |
| I2 | Domain Admin account compromise (path proof) | varies | bloodhound, rubeus, mimikatz, impacket, crackmapexec |
| I3 | Crown-jewel access | T1530 / T1213 | manual, cobaltstrike, crackmapexec, impacket |
| I4 | MITRE ATT&CK mapping of all TTPs used | reporting | attack-navigator, atomic-red-team, vectr, plextrac |

Important (I5-I9) and Optional (I10-I12) techniques and per-technique cards
in [reference/techniques.md](reference/techniques.md).

## Exhibit schema (handed to the Reporter)

| Exhibit | Contents |
|---|---|
| DA-Proof-1 | `secretsdump -just-dc` redacted summary + krbtgt RID-500 hash hex + timestamp + DC hostname |
| DA-Proof-2 | DA `whoami /all` + `hostname` on DC + `klist` showing TGT |
| Crown-Jewel-N | per crown jewel: host, principal, share/database/URL, listing of objects (names only), file SHA256s |
| MITRE-Map | `report/attack_navigator.json` + heatmap SVG |
| Kill-Chain | `report/attack_path.svg` (Graphviz from `attack_path[]`) |
| Cleanup-Manifest | Aggregated cleanup commands from `establishing-persistence` + `evading-defenses` |
| Detection-Gap | per attack-path step: `detected: true|false`, source = blue-team SIEM review or C2/EDR signal log |
| Remediation | per technique used: MITRE mitigation ids + BloodHound remediation hints |

When all exhibits exist → `agent_result.status = 'success'` and
`recommended_next = 'report'`. This ends the engagement.

## Pivot conditions

- `vault.has('krbtgt_nt')` and `report.exhibits` missing `DA-Proof-1` → run
  I1 then continue.
- DA shell + crown-jewel listings captured → build report.
- Hybrid scope set + on-prem DA proven → I10 (cloud lateral) → return to
  report.
- All Critical objectives complete → invoke Reporter; signal
  `engagement_complete`.
- Destructive simulation requested without ACK → `blocked`,
  `reason='needs_human_ack'`.

## Self-critique

- "Mimikatz DCSync fails after `privilege::debug` → `whoami /priv` may not
  include `SeDebugPrivilege`; escalate via `escalating-privileges` first."
- "Crown-jewel host accessible but agent is about to download an actual file
  → STOP. Metadata only. The engagement contract requires no real exfil."
- "Ransomware sim on a non-isolated host → STOP. Require
  `target.host.tag == 'isolated_test_host'` before any T1486."
- "ATT&CK Navigator layer missing techniques from `attack_path[]` → exporter
  expects every step to have a `mitre` id; re-walk findings and fill any
  missing ids."
- "Hybrid claim 'on-prem DA implies Cloud GA' is only true with default
  AzureADSyncAccount + PTA + ADFS configurations. Verify each prerequisite
  before asserting cloud GA."

## Evidence to capture

See the Exhibit schema above. All raw hashes / dumps go to the vault; only
redacted summaries and counts appear in the report.
