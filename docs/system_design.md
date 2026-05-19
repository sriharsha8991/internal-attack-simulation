# Internal Autonomous Red-Team — System Design

> Status: design draft (v0.1) — derived from `AD_Pentesting_Full.xlsx` (AD Attack Path · Tools Quick Reference · Legend) and the two architecture sketches in this repo.

This document is the from-scratch design for an **autonomous internal red-team agent** that is dropped onto a host *after* initial access (assumed-breach model) and chains AD attack stages end-to-end until Domain Admin / crown-jewel access is demonstrated and a full attack-path report is produced.

---

## 1. Goals, Non-Goals, Threat-Model Assumptions

### 1.1 Goals
1. **Assumed-breach autonomy.** Given (a) low-privilege domain credentials, (b) a beachhead host, (c) engagement scope, drive the kill-chain to DA / objectives with minimal human input.
2. **Real-tool execution** (not just LLM hallucination of output). The agent shells out to actual tools (BloodHound, Rubeus, Impacket, CrackMapExec, …) inside a controlled execution channel.
3. **Stage-specialised agents** — one specialist per ATT&CK-aligned stage (Discovery, PrivEsc, CredAccess, Lateral, Persistence, DefEvasion, Impact). Each owns its own *skill file* and tool subset.
4. **A superior Orchestrator agent** that owns the global plan, reads each specialist's findings, and routes the next stage based on priority + pivot conditions.
5. **Persistent session memory** — every fact (credential, host, ACL edge, ticket, finding) survives between agent invocations; nothing is re-discovered.
6. **Auditable attack-path report** at the end: MITRE ATT&CK mapped, with full timeline, evidence, and remediation hints.

### 1.2 Non-Goals (v1)
- Initial-access / phishing automation. We start *after* foothold.
- 0-day discovery or exploit dev. We chain *known* misconfig / TTP paths.
- Fully unsupervised production deployment. v1 has a human-in-the-loop gate for destructive actions (DCSync, persistence install, exfil-sim).
- Non-AD environments (cloud-only, OT, macOS-only). Hybrid Azure AD is in scope but secondary.

### 1.3 Threat-model assumptions
- Lab / authorised-engagement only. Out-of-scope hosts are enforced by the Orchestrator's scope filter (hard-coded allow-list, dropped on every tool dispatch).
- The C2 channel between Orchestrator and beachhead exists already (Cobalt Strike / Sliver / Mythic / Havoc / native WinRM). We design *on top of* it.
- LLM has no direct network access to the target. It reasons; the execution layer reaches the target.

---

## 2. Prior-Art Research (what we are borrowing from)

| System | What we take | What we drop |
|---|---|---|
| **MITRE Caldera** | ATT&CK-aligned planner, ability/operation/adversary model, fact-source pattern (≈ our session memory) | Rule-based planner is too rigid — replace with LLM reasoning |
| **Metasploit Pro automation / AutoRoute** | Pivot-graph idea, post-exploitation modules | Heavy footprint, weak stealth |
| **BloodHound + GoodHound** | Shortest-path graph as the *source of truth* for "what to attack next" | UI-only — we consume it programmatically |
| **PentestGPT, HackingBuddyGPT, CAI, Auto-Pentest-GPT (2023-25 academic)** | LLM-as-planner + tool-runner loop; ReAct-style reason→act→observe | Single-agent, no stage isolation, no persistent typed memory |
| **AutoGen / LangGraph / CrewAI** | Multi-agent orchestration primitives, deterministic state graph | Generic — we constrain it to a fixed 7-stage DAG |
| **Cobalt Strike Aggressor / BOFs** | In-memory tool execution model, OPSEC-aware command set | Closed-source; we use it as an execution backend, not as the brain |
| **Atomic Red Team / VECTR** | Tests-as-data idea → our skill files are tests-as-data | They are detection-test oriented, not chain-oriented |

**Key insight from prior art:** single-agent LLM red-teamers (PentestGPT-style) collapse under context length and lose track of credentials/hosts across long runs. The fix that consistently works in the literature and in Caldera is **externalised typed memory + a planner that only reads summaries**. Our design enforces that.

---

## 3. High-Level Architecture

```
                      ┌─────────────────────────────────────────────┐
   INPUT  ───────────►│              ORCHESTRATOR AGENT             │◄────────┐
   (creds, host,      │  plan • route • evaluate • escalate stage   │         │
    scope, goals)     └──────────────┬──────────────────────────────┘         │
                                     │ dispatch (stage, context)              │
       ┌──────────┬──────────┬───────┴───────┬──────────┬──────────┬──────────┤
       ▼          ▼          ▼               ▼          ▼          ▼          ▼
   Discovery  PrivEsc   CredAccess    LateralMove  Persistence DefEvasion  Impact
    Agent      Agent      Agent          Agent        Agent       Agent     Agent
       │          │          │               │          │          │          │
       │  each agent uses ONE skill file + a whitelisted tool subset          │
       ▼          ▼          ▼               ▼          ▼          ▼          ▼
   ┌─────────────────────────────────────────────────────────────────────────┐
   │                      EXECUTION LAYER (Tool Runner)                      │
   │   C2 backend (Sliver/CS/Mythic/WinRM)  +  sandbox/dry-run mode          │
   │   command-policy filter  •  output parsers  •  artifact store           │
   └─────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
                       ┌─────────────────────────────┐
                       │  SESSION MEMORY (typed)     │
                       │  session_memory.json + DB   │
                       │  credentials • hosts •      │
                       │  findings • attack_path[]   │
                       └──────────────┬──────────────┘
                                      │
                                      ▼
                       ┌─────────────────────────────┐
                       │   ATTACK-PATH REPORTER      │
                       │   MITRE map • timeline •    │
                       │   evidence • remediation    │
                       └─────────────────────────────┘
```

This is exactly the topology of your two attached diagrams, formalised. The key invariant: **agents never call each other; everything goes through the Orchestrator and the typed session memory.**

---

## 4. Stage Catalogue (from `AD_Pentesting_Full.xlsx`)

These are the seven specialist agents. Counts are *technique* rows from the workbook; tool counts are the union of tools listed per stage.

| # | Stage | Agent name | Goal | Tech count | Critical-priority techniques |
|---|---|---|---|---|---|
| 0 | Discovery / AD Enumeration | `DiscoveryAgent` | Map AD, find shortest path to DA | 19 | BloodHound/SharpHound full collection; ACL abuse discovery; AD CS vuln discovery (ESC1–13) |
| 1 | Privilege Escalation (Local) | `PrivEscAgent` | Reach SYSTEM / local admin on beachhead | 13 | Local host enum (Seatbelt/WinPEAS); AD CS abuse; Token impersonation / Potato |
| 2 | Credential Access | `CredAccessAgent` | Harvest domain credentials | 17 | Kerberoasting; AS-REP roasting; LSASS dump; DCSync; NTDS.dit extraction |
| 3 | Lateral Movement | `LateralAgent` | Move toward DCs / Tier-0 | 16 | PTH; PTT; WMI exec; WinRM/PSRemoting |
| 4 | Persistence | `PersistenceAgent` | Survive reboots / password resets | 15 | Golden Ticket; AdminSDHolder ACL; DCSync rights grant |
| 5 | Defense Evasion | `DefEvasionAgent` | Stay undetected | 16 | AMSI bypass; Process injection; LOLBAS; C2 obfuscation |
| 6 | Impact / Objectives | `ImpactAgent` | Prove DA / crown-jewel access | 12 | DCSync full extraction; DA compromise; Crown-jewel access; MITRE mapping |

Defense Evasion is cross-cutting — see §7.

---

## 5. Orchestrator — Decision Logic

The Orchestrator is a small, deterministic state machine wrapping an LLM "router" prompt. It never executes tools itself.

### 5.1 State machine

```
                    ┌────────────────────────────┐
                    │ S0: INIT                   │  load scope + creds
                    └─────────────┬──────────────┘
                                  ▼
                    ┌────────────────────────────┐
   ┌───────────────►│ S1: PLAN_NEXT_STAGE        │  LLM router picks next stage
   │                └─────────────┬──────────────┘
   │                              ▼
   │                ┌────────────────────────────┐
   │                │ S2: DISPATCH_AGENT          │  build handoff payload
   │                └─────────────┬──────────────┘
   │                              ▼
   │                ┌────────────────────────────┐
   │                │ S3: AGENT_RUN (specialist)  │  ReAct loop, writes findings
   │                └─────────────┬──────────────┘
   │                              ▼
   │                ┌────────────────────────────┐
   │                │ S4: EVALUATE                │  status, new edges, blocked?
   │                └────┬──────────┬─────────────┘
   │                     │          │
   │     success/partial │          │ blocked
   └─────────────────────┘          ▼
                       ┌────────────────────────────┐
                       │ S5: BACKTRACK / DEF-EVASION │
                       └─────────────┬──────────────┘
                                     ▼
                                  back to S1 ─────┐
                                                  │
                       objective met? ─► S6: REPORT
```

### 5.2 Stage-selection policy (the "router" prompt)

Inputs the LLM router sees (and *only* these — keeps tokens bounded):
- Current `stage_status` dict.
- Top-N **unresolved findings** ordered by priority (Critical > Important > Optional).
- Current **BloodHound shortest-path** to DA (cached JSON edge list).
- Last agent's `recommended_next` + `rationale`.

Hard rules that bypass the LLM:
1. If `local_integrity != HIGH` and current host has no AD CS / Kerberoast win yet → force **PrivEsc** before CredAccess "noisy" actions (LSASS dump, NTDS).
2. If AMSI / EDR signal observed in last 3 tool runs → force **DefEvasion** stage.
3. If DA hash or krbtgt obtained → force **Impact** (objectives + report), do *not* keep pivoting.
4. If stage retried 3× and still `blocked` → escalate to human (out-of-band notification, pause run).

The LLM only chooses among the remaining legal stages. This is a deliberate **constrained-LLM** pattern — the model picks *which* legal move, never invents one.

### 5.3 Handoff payload (Orchestrator → Agent)

```jsonc
{
  "role": "DiscoveryAgent",
  "stage": "discovery",
  "scope": { "domains": ["corp.local"], "subnets_allow": ["10.10.0.0/16"], "hosts_deny": [...] },
  "session_memory_snapshot": { /* full typed memory, see §6 */ },
  "skill_file": "<full content of skills/discovery.md>",
  "tool_allowlist": ["sharphound","bloodhound-python","powerview","ldapdomaindump","adidnsdump","kerbrute","crackmapexec","certipy","group3r","lapstoolkit"],
  "budget": { "max_tool_calls": 25, "max_wallclock_min": 20 },
  "instruction": "Run a ReAct loop until status=success|blocked|partial. Output strict JSON per agent_result schema."
}
```

### 5.4 Agent result (Agent → Orchestrator)

```jsonc
{
  "status": "success | partial | blocked",
  "stage": "discovery",
  "findings": [ /* new typed findings, see §6.2 */ ],
  "credentials_new": [...],
  "hosts_new": [...],
  "attack_path_step": { /* one or more edges appended */ },
  "evidence_refs": ["artifacts/2026-05-18T10-12_sharphound.zip"],
  "recommended_next": "credaccess",
  "rationale": "Kerberoastable accounts found (3 SPNs, weak crypto). LSASS dump risky on this host (Defender + cloud delivered protection). Prefer offline Kerberoast first.",
  "opsec_signals": { "edr_alert_observed": false, "amsi_blocks": 0 }
}
```

The Orchestrator merges these into session memory atomically (single writer).

---

## 6. Session Memory — Typed Schema

Single source of truth. Stored as `session_memory.json` (working copy in RAM, journaled to SQLite + append-only JSONL for replay). **Only the Orchestrator writes.** Agents propose; Orchestrator commits.

### 6.1 Top-level

```jsonc
{
  "context": {
    "engagement_id": "ENG-2026-018",
    "started_at": "2026-05-18T09:00:00Z",
    "scope": { "domains": [...], "subnets_allow": [...], "hosts_deny": [...], "destructive_actions": "ask" },
    "initial_access": { "host": "WS-FIN-07", "user": "corp\\jdoe", "integrity": "MEDIUM", "auth": "password" },
    "objectives": ["domain_admin", "access:CRM-DB", "demonstrate_exfil"]
  },
  "stage_status": {
    "discovery":   "in_progress",
    "privesc":     "not_started",
    "credaccess":  "not_started",
    "lateral":     "not_started",
    "persistence": "not_started",
    "defevasion":  "ambient",
    "impact":      "not_started"
  },
  "findings":      [ /* §6.2 */ ],
  "credentials":   [ /* §6.3 */ ],
  "hosts":         [ /* §6.4 */ ],
  "ad_graph":      { "nodes": [...], "edges": [...], "shortest_path_to_DA": [...] },
  "attack_path":   [ /* §6.5 */ ],
  "artifacts":     [ /* file refs, screenshots, hash dumps */ ],
  "opsec_state":   { "edr_product": "CrowdStrike", "amsi_bypassed": false, "burned_tools": ["mimikatz.exe"] }
}
```

### 6.2 Finding

```jsonc
{
  "id": "F-0042",
  "stage": "discovery",
  "technique": "Kerberoasting target enumeration",
  "mitre": "T1558.003",
  "tool": "Rubeus",
  "output_summary": "3 SPN accounts: svc_sql, svc_backup, svc_iis (RC4-HMAC)",
  "raw_output_ref": "artifacts/F-0042.txt",
  "timestamp": "...",
  "status": "actionable",
  "priority": "critical",
  "pivot": { "next_stage_hint": "credaccess", "technique_hint": "kerberoast_offline_crack" }
}
```

### 6.3 Credential

```jsonc
{
  "id": "C-0007",
  "username": "corp\\svc_sql",
  "secret_type": "nt_hash | aes256_key | tgt | tgs | password | dpapi_masterkey | cert_pfx",
  "value_ref": "vault://C-0007",          // secrets never in plain memory dumps
  "source": "kerberoast",                  // how acquired
  "usable_for": ["pth", "ptt", "overpass"],
  "validated_against": ["WS-FIN-07","SQL-01"],
  "burned": false
}
```

### 6.4 Host

```jsonc
{
  "hostname": "DC01",
  "ip": "10.10.0.10",
  "roles": ["DC","PDC","DNS"],
  "os": "Windows Server 2022",
  "access_level": "none | user | local_admin | system",
  "edr": "MDE",
  "tier": 0,
  "interesting": ["unconstrained_delegation:false", "ldap_signing:required"]
}
```

### 6.5 Attack-path step (built incrementally — becomes the final report)

```jsonc
{
  "step": 7,
  "from": { "host": "WS-FIN-07", "principal": "corp\\jdoe" },
  "to":   { "host": "SQL-01",   "principal": "corp\\svc_sql" },
  "stage": "lateral",
  "technique": "Pass-the-Hash via WMI",
  "mitre": ["T1550.002","T1047"],
  "tool_cmd_ref": "skills/lateral.md#pth-wmi",
  "evidence_refs": ["artifacts/step7_wmiexec.log"],
  "timestamp": "...",
  "detected": false
}
```

### 6.6 Why typed (and not "stuff it in the LLM context")
- **Token budget.** A real engagement produces megabytes of tool output. The LLM only ever sees `output_summary` + the top-K findings.
- **Replay & audit.** The JSONL journal lets us re-run the Orchestrator's decisions deterministically for debugging.
- **Secrets hygiene.** Hashes / tickets live in a local vault keyed by `value_ref`; the LLM never sees the actual hash unless an agent explicitly requests it for a tool call.

---

## 7. Skill Files — One Per Stage (Anthropic Agent Skills format)

Each skill is an **Anthropic Agent Skill**: a directory containing a `SKILL.md`
([overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview),
[best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices))
plus a one-level-deep `reference/` subdirectory that holds the bulk of
technique tables and templated tool commands. The specialist agent loads only
`SKILL.md` on dispatch (Level 2 of progressive disclosure); the reference
files are pulled in on demand when a specific technique is selected (Level 3).

### 7.1 Anatomy

```
skills/
├── discovering-environment/
│   ├── SKILL.md                   (≤500 lines, navigational overview)
│   └── reference/
│       ├── techniques.md          (full Critical → Important → Optional catalogue)
│       └── tool-commands.md       (templated YAML command blocks per tool)
├── escalating-privileges/
│   ├── SKILL.md
│   └── reference/
│       ├── techniques.md
│       └── tool-commands.md
├── accessing-credentials/         (same shape)
├── moving-laterally/              (same shape)
├── establishing-persistence/      (same shape)
├── evading-defenses/              (same shape — ambient: true)
└── achieving-impact/              (same shape)
```

Skill names are gerunds, lowercase-hyphen, ≤ 64 chars, no `anthropic` or
`claude` token — per the Agent Skills spec.

### 7.2 SKILL.md frontmatter (template in `docs/skill_template.md`)

Required fields (Anthropic spec):

- `name` — matches the directory name (e.g. `discovering-environment`).
- `description` — third person, ≤ 1024 chars, **must state both WHAT the skill
  does AND WHEN to invoke it**. The Orchestrator's router uses this same
  string when deciding which skill to dispatch.

Custom orchestrator fields (additional YAML keys; ignored by Claude's native
runtime but read by our `BaseAgent` + Orchestrator):

- `stage` (one of `discovery | privesc | credaccess | lateral | persistence | defevasion | impact`)
- `agent` (specialist class name)
- `mitre_tactics` (list of MITRE ATT&CK tactic ids)
- `default_opsec` (`stealth | moderate | loud`)
- `ambient` (`true` only for `evading-defenses`, loaded as a policy layer by every other stage)
- `tool_allowlist` (per §5.3)
- `budget` (`max_tool_calls`, `max_wallclock_min`, `destructive_default`)

### 7.3 SKILL.md body

Always concise (target ≤ 300 lines). Sections in this order:

1. **Quick start** — 3-5 numbered steps the agent must execute.
2. **Preconditions** — typed-memory predicates that must be true.
3. **Stage goal / commit criteria** — when to signal `success`.
4. **Critical techniques table** — summary only; full table lives in
   `reference/techniques.md`.
5. **Pivot conditions** — rule-priority list filling `recommended_next`.
6. **Self-critique** — failure modes seen on real engagements ("if X, do not
   retry Y; switch to Z").
7. **Evidence to capture** — what gets committed to memory / vault.

### 7.4 reference/ files (Level 3 of progressive disclosure)

- `reference/techniques.md` — full Critical → Important → Optional table plus
  per-technique cards (preconditions, success indicators, OPSEC, fallbacks).
- `reference/tool-commands.md` — YAML blocks per tool with templated
  `{{placeholders}}` filled at dispatch from typed session memory, plus
  `parser` name, `opsec` tag, optional `cleanup_cmd` and `destructive_gate`.
- Reference files include a table-of-contents block at the top (the spec
  requires this for files > 100 lines).

### 7.5 Authoring rules

- **Tool names only** — never embedded credentials, never engagement-specific
  values. The agent fills placeholders at runtime.
- **Every command has an `opsec` tag**: `loud | moderate | stealth | build-time`.
  Orchestrator + ambient `evading-defenses` drop `loud` commands when an EDR
  signal is hot.
- **Every technique lists at least one fallback** so the agent can adapt.
- **Persistence + log-clearing commands include `cleanup_cmd`** — the exact
  reverse — written into the engagement rollback manifest.
- **`evading-defenses` is ambient** (`ambient: true`): loaded by every other
  agent as a policy layer (AMSI bypass + ETW patch + OPSEC scoring run as
  preflight). When invoked as a stage on its own, it runs the recovery
  workflow in its SKILL.md Quick start.
- **No Windows paths in skill files** — forward slashes only, per the Agent
  Skills spec.

### 7.6 Tool-to-stage mapping (from `Tools Quick Reference` sheet)

The mapping is in the spreadsheet; at build time we generate
`config/tool_stage_map.yaml` from it so the allow-lists in §5.3 stay in sync
with the workbook and with each `SKILL.md`'s `tool_allowlist`.

---

## 8. Specialist-Agent Internals (per the second diagram)

Each stage agent is the **same code**, parameterised by skill file + tool allow-list. Internal loop:

```
1. RECEIVE CONTEXT  (handoff payload from Orchestrator)
2. LOAD SKILL FILE  (techniques + tool_commands + pivot_conditions)
3. SELECT TECHNIQUE (Critical first, filtered by preconditions from memory)
4. EXECUTE LOOP (ReAct):
     a. Generate tool command (template from skill, fill from memory)
     b. Policy filter (scope, opsec, destructive-gate)  ── may ask human
     c. Dispatch via Execution Layer  → stdout/stderr/files
     d. Parse output  (per-tool parser, NOT regex-in-LLM)
     e. Evaluate findings (LLM: did this work? what did we learn?)
     f. Decide: next tool in this technique, next technique, or signal done
     g. Append structured finding to local scratch memory
5. STAGE FINDINGS  (summarise scratch → typed findings list)
6. WRITE TO SESSION (propose to Orchestrator, never write directly)
7. SIGNAL ORCHESTRATOR (status + recommended_next + rationale)
```

### 8.1 ReAct loop hardening
- **Tool-call budget** per dispatch (e.g. 25 tool calls per stage) to prevent runaway loops.
- **Deterministic parsers** per tool (BloodHound JSON, Rubeus output, secretsdump format, CME `--json`). The LLM only sees the parsed struct, never raw stdout for high-volume tools.
- **Self-critique step** every N=5 calls: agent must answer "am I making progress against the stage goal? if no, switch technique or signal blocked."

### 8.2 Why not let one big agent do everything?
- Context bloat (all 80 techniques × all 120 tools in one prompt → unusable).
- Tool-namespace confusion (model conflates Rubeus flags between Kerberoast and S4U).
- Auditability — per-stage logs match how human red teams already report.
- We can swap models per stage later (a small/fast model for Discovery, a stronger one for Lateral planning).

---

## 9. Execution Layer (the only thing that actually touches the network)

```
┌─────────────────────────────────────────────────────────────┐
│ ToolRunner                                                  │
│  ├─ adapters/                                               │
│  │   ├─ sliver_adapter.py     (preferred, OSS)              │
│  │   ├─ cobaltstrike_adapter  (Aggressor + BOFs, optional)  │
│  │   ├─ mythic_adapter        (REST + RabbitMQ)             │
│  │   ├─ winrm_adapter         (PSRP / Evil-WinRM, lab mode) │
│  │   └─ local_lab_adapter     (run on engineer's box)       │
│  ├─ policy/                                                 │
│  │   ├─ scope_filter          (every cmd validated)         │
│  │   ├─ opsec_filter          (loud cmds blocked if hot)    │
│  │   └─ destructive_gate      (DCSync/persist → human ack)  │
│  ├─ parsers/                                                │
│  │   ├─ sharphound.py         (zip → BH JSON → graph edges) │
│  │   ├─ rubeus.py / impacket  (TGS / hash formats)          │
│  │   ├─ secretsdump.py        (NTDS users + machine accts)  │
│  │   ├─ cme.py                (--json output)               │
│  │   └─ certipy.py            (ESC1-13 findings)            │
│  └─ artifact_store/           (per-step zips, hashes, logs) │
└─────────────────────────────────────────────────────────────┘
```

**Three execution modes**, all driven by the same agent code:

| Mode | Purpose | Default |
|---|---|---|
| `dry-run` | LLM emits the exact command; runner records it but does **not** execute. Used in CI / unit tests. | yes for first run |
| `lab` | Executes against a contained lab range (e.g. GOAD, Vulnerable AD). | engagement default |
| `engagement` | Executes via C2 against a scoped production target. Requires signed authorisation file + human ack on destructive actions. | off by default |

---

## 10. Defense-Evasion as Cross-Cutting Concern

Defense Evasion is in the spreadsheet as a stage (16 techniques) but in practice it's a **policy layer**, not a sequential step. Implementation:

- A small `OpsecMonitor` watches the last N tool results for `edr_alert_observed`, `amsi_blocks`, `signature_hit` signals returned by parsers.
- When state goes `hot`: Orchestrator forces an Evasion *interlude* — load `defevasion.md`, pick AMSI bypass / process migration / sleep-jitter increase / payload re-pack, verify, then resume the previous stage.
- The `tool_allowlist` is shrunk dynamically (`mimikatz.exe` removed, `nanodump` substituted; `psexec` removed, `wmiexec` or `dcomexec` substituted).
- Every command tagged `loud` is rewritten or skipped while hot.

This mirrors how a human operator behaves and is the single biggest source of "the agent burned the engagement" failures in published autonomous-pentest systems — worth getting right early.

---

## 11. Reporting — Attack-Path Output

At objectives-met (or budget-exhausted) the **Reporter** consumes `attack_path[]`, `findings[]`, `evidence_refs[]`, and produces:

1. **Executive summary** (1 page) — initial access → DA → crown-jewel, business impact.
2. **Full kill chain** as a directed graph (Graphviz/Mermaid) — every step, host, principal, technique, MITRE id, timestamp, detected?
3. **MITRE ATT&CK Navigator JSON** — drop-in for the client's blue team (matches Impact-stage requirement in the workbook).
4. **Per-step evidence pack** — sanitised tool outputs, screenshots, hashes-with-timestamps (krbtgt for proof-of-DA, per workbook).
5. **Remediation table** — for each technique used, the corresponding mitigation (mapped from BloodHound + ATT&CK mitigation IDs).
6. **Detection-gap analysis** — which steps had `detected: false` → highest-value tuning recommendations for blue team.

---

## 12. Proposed Tech Stack

| Concern | Choice (v1) | Why |
|---|---|---|
| Orchestration framework | **LangGraph** (deterministic state graph) or AutoGen GroupChat with a constrained router | State graph fits §5.1 cleanly, replayable |
| LLM | Per-agent configurable; default GPT-class for Lateral/Impact, smaller for Discovery | Cost vs. reasoning depth |
| Memory store | SQLite + append-only JSONL journal + a local secrets vault (`age` / `sops`) | Simple, replayable, no cloud |
| AD graph | Neo4j (BloodHound CE schema) | We already speak its query language |
| C2 backend | Sliver (OSS) primary; Cobalt Strike adapter optional | OSS, scriptable gRPC API |
| Tool parsers | Plain Python modules per tool, unit-tested against captured fixtures | Keeps LLM off the raw bytes |
| Sandboxing | All tool execution inside a network-namespaced container that can only reach the C2 server | Defence-in-depth |
| Reporter | Jinja2 → Markdown → Pandoc (PDF), plus Graphviz for kill-chain, plus ATT&CK Navigator JSON exporter | Standard, scriptable |
| Tests | pytest + recorded fixtures from a GOAD lab; per-stage golden tests | Regression-proof skill files |

---

## 13. Proposed Repository Layout

```
internal-attack-simulation/
├── README.md
├── docs/
│   ├── AD_Pentesting_Full.xlsx            (source of truth — stages/tools)
│   ├── sample_architecture.md
│   ├── system_design.md                    (this document)
│   └── skill_template.md                   (template for stage skill files)
├── config/
│   ├── tool_stage_map.yaml                 (generated from xlsx)
│   ├── opsec_profiles.yaml
│   └── scope.example.yaml
├── skills/                                  (Anthropic Agent Skills — one dir per stage)
│   ├── discovering-environment/
│   │   ├── SKILL.md
│   │   └── reference/
│   │       ├── techniques.md
│   │       └── tool-commands.md
│   ├── escalating-privileges/
│   │   ├── SKILL.md
│   │   └── reference/{techniques.md,tool-commands.md}
│   ├── accessing-credentials/
│   │   ├── SKILL.md
│   │   └── reference/{techniques.md,tool-commands.md}
│   ├── moving-laterally/
│   │   ├── SKILL.md
│   │   └── reference/{techniques.md,tool-commands.md}
│   ├── establishing-persistence/
│   │   ├── SKILL.md
│   │   └── reference/{techniques.md,tool-commands.md}
│   ├── evading-defenses/                  (ambient: true)
│   │   ├── SKILL.md
│   │   └── reference/{techniques.md,tool-commands.md}
│   └── achieving-impact/
│       ├── SKILL.md
│       └── reference/{techniques.md,tool-commands.md}
├── src/
│   ├── orchestrator/
│   │   ├── graph.py                        (LangGraph state machine, §5.1)
│   │   ├── router.py                       (constrained LLM router, §5.2)
│   │   └── memory.py                       (typed session memory, §6)
│   ├── agents/
│   │   ├── base_agent.py                   (the shared ReAct loop, §8)
│   │   ├── discovery_agent.py
│   │   ├── privesc_agent.py
│   │   ├── credaccess_agent.py
│   │   ├── lateral_agent.py
│   │   ├── persistence_agent.py
│   │   ├── defevasion_agent.py             (also exposed as policy layer)
│   │   └── impact_agent.py
│   ├── execution/
│   │   ├── runner.py
│   │   ├── adapters/                       (sliver, cs, mythic, winrm, local)
│   │   ├── policy/                         (scope, opsec, destructive_gate)
│   │   └── parsers/                        (sharphound, rubeus, cme, …)
│   ├── reporting/
│   │   ├── attack_path.py
│   │   ├── mitre_export.py
│   │   └── templates/
│   └── tools/
│       └── xlsx_to_yaml.py                 (regenerate tool_stage_map.yaml)
├── tests/
│   ├── fixtures/                           (captured tool outputs)
│   ├── unit/
│   └── e2e_goad/                           (lab end-to-end runs)
└── scripts/
    └── bootstrap_lab.ps1                   (spin up GOAD-style range)
```

---

## 14. Build Plan / Milestones

**M1 — Skeleton + Discovery only (proof of value).**
Orchestrator state machine, typed memory, `DiscoveryAgent` only, local-lab adapter, parsers for SharpHound + CME + ldapdomaindump. Output: BloodHound graph + Kerberoast targets in `findings`.

**M2 — CredAccess + Lateral, single-machine lab.**
Add `CredAccessAgent` (Kerberoast offline, AS-REP, LSASS via Nanodump), `LateralAgent` (PTH-WMI, PTT, WinRM). Reach a second host. First `attack_path[]` with ≥3 steps.

**M3 — PrivEsc + Impact, end-to-end through GOAD lab.**
`PrivEscAgent` (AD CS ESC1, Potato), `ImpactAgent` (DCSync, MITRE export). First fully-autonomous run from foothold → DA hash → report on a known-vulnerable lab.

**M4 — DefEvasion policy layer + Persistence.**
OpsecMonitor, dynamic allow-list rewriting, Golden Ticket + Shadow Credentials. Add a Defender-on lab to validate.

**M5 — Hybrid Azure AD + Reporter polish.**
ROADtools / AADInternals integration for hybrid paths. Final report templates, ATT&CK Navigator export, detection-gap analysis.

**M6 — Hardening.**
Per-stage golden tests, replay tooling, destructive-action human-ack UX, scope-violation chaos tests.

---

## 15. Safety, Authorisation, Ethics

Non-negotiable controls baked into the system itself:

1. **Signed authorisation file** required at startup (`scope.yaml` + detached signature). The Orchestrator refuses to leave `dry-run` without it.
2. **Hard scope filter** in the Execution Layer. *Every* dispatched command is re-validated against `subnets_allow` / `hosts_deny` immediately before send; out-of-scope = drop + alert.
3. **Destructive-action gate.** DCSync, NTDS extraction, persistence install, exfil-sim, ransomware-sim require explicit human ACK *per occurrence*, not blanket consent.
4. **Append-only audit log** (JSONL, hash-chained) of every Orchestrator decision, every tool dispatch, every parsed result. Survives crash; signable for client handover.
5. **Secrets at rest.** All harvested credentials encrypted with engagement-scoped key; auto-wiped on `engagement_end`.
6. **No public LLM training feedback.** API calls run with telemetry / training opt-out flags set; on-prem model for highest-sensitivity engagements.

---

## 16. Open Questions for You

These are the choices that affect M1 scope — answering them unblocks the first code:

1. **LLM hosting** — on-prem (Ollama / vLLM) or API? Affects parser/tool placement and what we can feed the model.
2. **C2 of record** — Sliver only for v1, or do we need a Cobalt Strike adapter from day 1?
3. **Lab range** — GOAD, Vulnerable-AD, or a custom range? (affects test fixtures)
4. **Reporting format** — Markdown + PDF only, or also direct PlexTrac/VECTR push?
5. **Destructive-gate UX** — CLI prompt, Slack approval, or signed-token from a controller web UI?

Once these are answered I'll generate `skill_template.md`, the `discovery.md` first-pass skill file, and the M1 skeleton.
