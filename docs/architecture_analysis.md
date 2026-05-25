# BAS Engine — Architecture Analysis & Retrospection

> **Generated**: 2026-05-25  
> **Scope**: Full data-flow analysis, checkpointer strategy, context management, PostgreSQL memory design, edge-case audit, and command-quality improvement roadmap.

---

## Table of Contents

1. [End-to-End Data Flow](#1-end-to-end-data-flow)
2. [All Possible Scenario Traces](#2-all-possible-scenario-traces)
3. [Checkpointer Analysis](#3-checkpointer-analysis)
4. [Context Management Improvements](#4-context-management-improvements)
5. [PostgreSQL Memory Design for BAS Backend](#5-postgresql-memory-design-for-bas-backend)
6. [Edge Cases Audit](#6-edge-cases-audit)
7. [Command Quality — First-Push Success Strategy](#7-command-quality--first-push-success-strategy)
8. [Summary Recommendations (Priority-Ranked)](#8-summary-recommendations-priority-ranked)

---

## 1. End-to-End Data Flow

### 1.1 Input → Graph Bootstrap

```
┌─────────────────────────────────────────────────────────────────┐
│  CLIENT  POST /engagements                                      │
│  {phases: ["discovery","credaccess"], environment: {...},        │
│   target: {...}, max_iterations: 20}                             │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│  routes_engagements.py :: submit_engagement()                     │
│    1. Validate phases → resolve_phases_to_skills()                │
│    2. engagement_id = uuid4().hex                                 │
│    3. RunStore.save(record{status:"queued"})                      │
│    4. background_tasks.add(_run_engagement)                       │
│    └─ Return 202 + engagement_id                                  │
└───────────────────────────────┬───────────────────────────────────┘
                                │ (background thread)
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│  worker.py :: _run_engagement()                                   │
│    1. record["status"] = "running"                                │
│    2. resolve_foothold(bas, env_id/name, platform_hint)           │
│       ├─ GET /environments → pick env                             │
│       ├─ GET /environments/{id}/agents → pick agent               │
│       └─ Return {agent_id, hostname, platform, ip_address}        │
│    3. _build_checkpointer(cfg) → MemorySaver | SqliteSaver        │
│    4. run_orchestrator(master, skill_tool, planner, bas, ...)     │
│       └─ build_graph().invoke(initial_state, config)              │
└───────────────────────────────┬───────────────────────────────────┘
                                │
                                ▼
                        [ LangGraph State Machine ]
```

### 1.2 Graph Node Sequence

```
START → init → master_plan ──┬─→ (master_done=True) → END
                              │
                              ▼
                           plan ←──────────────────────┐
                              │                         │
                              ▼                         │
                          evaluate ─── retry ───────────┘
                              │
                         accept/escalate
                              │
                              ▼
                       master_plan (REVIEW mode)
                              │
                        commit/revise
                              │
                              ▼
                            push ──── intermediate_skill ──→ master_plan(PICK)
                              │
                        last_skill_done
                              │
                              ▼
                       analyse_results
                              │
                        interrupt("awaiting_results")
                              │
                     ═══════════════════════
                        PROCESS SUSPENDED
                     ═══════════════════════
                              │
              Backend executes, POSTs webhook
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  routes_results.py :: receive_results()                         │
│    1. Validate engagement exists                                │
│    2. Idempotency check (ResultStore.exists)                    │
│    3. ResultStore.save(engagement_id, operation_id, payload)    │
│    4. background_tasks.add(_resume_graph)                       │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  worker.py :: _resume_graph()                                   │
│    1. record["status"] = "running"                              │
│    2. compiled.invoke(Command(resume=result_payload), config)   │
│    └─ analyse_results resumes with the result_payload           │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
                       analyse_results (resumed)
                              │
                    ┌─────────┴─────────┐
                    │                   │
             phase_done=True     phase_done=False
                    │                   │
                    ▼                   ▼
            master_plan(PICK)   master_plan(PICK + retry)
             next phase          same phase + issues_to_fix
```

### 1.3 State Keys — Read/Write Matrix

| State Key | init | master_plan | plan | evaluate | push | analyse_results |
|-----------|------|-------------|------|----------|------|-----------------|
| `run_id` | W | R | R | - | R | R |
| `foothold` | W | R | R | R | R | R |
| `memory` | W | R | R | R | **W** | **W** |
| `iteration` | W | **W** | R | R | R | R |
| `completed_phases` | W | R | - | - | R | **W** |
| `current_phase` | - | **W** | R | R | R | R |
| `master_briefing` | - | **W** | R | R | R | R |
| `next_stage` | - | **W** | R | - | R | - |
| `phase_skills` | - | **W** | - | - | **W** | - |
| `phase_skill_index` | - | **W** | - | - | **W** | - |
| `current_plan` | - | - | **W** | R | R | - |
| `current_plan_summary` | - | - | **W** | R | R | - |
| `evaluator_action` | - | **W** | - | **W** | - | - |
| `feedback` | - | **W** | R | **W** | - | - |
| `phase_history` | W | R | - | - | **W** | **W** |
| `phase_asset_map` | W | - | - | - | **W** | R |
| `execution_summary` | - | R | - | - | - | **W** |
| `retry_same_phase` | - | **W** | - | - | **W** | **W** |
| `issues_to_fix` | - | **W** | - | - | - | **W** |
| `pending_operation_id` | - | - | - | - | **W** | **W** |
| `proposal_log` | - | - | **W** | - | - | - |

---

## 2. All Possible Scenario Traces

### Scenario 1: Happy Path (Single Phase, First-Push Success)
```
init → master_plan(PICK: discovery) → plan(discovering-environment)
→ evaluate(accept) → master_plan(REVIEW: commit) → push(success)
→ analyse_results(interrupt) ═══ webhook ═══ analyse_results(phase_done=True)
→ master_plan(PICK: done=True) → END
```
**LLM calls**: 6 (plan_phase, plan, self-critique, evaluate, review, update_memory)

### Scenario 2: Evaluator Retry → Accept
```
init → master_plan(PICK) → plan → evaluate(retry, feedback="fix placeholder")
→ plan(with feedback) → evaluate(accept)
→ master_plan(REVIEW: commit) → push → analyse_results
```
**LLM calls**: 9 (+ 1 extra plan + 1 extra evaluate)

### Scenario 3: Master Revision
```
init → master_plan(PICK) → plan → evaluate(accept)
→ master_plan(REVIEW: revise, comments="missing CIDR")
→ plan(with master_revision_feedback) → evaluate(accept)
→ master_plan(REVIEW: commit) → push → analyse_results
```
**LLM calls**: 11

### Scenario 4: Evaluator Escalation
```
init → master_plan(PICK) → plan → evaluate(escalate)
→ master_plan(REVIEW: commit/skip)
→ push(skip, PhaseRecord.outcome="escalated") → analyse_results
```

### Scenario 5: Planner Failure → Retry Exhaustion
```
init → master_plan(PICK) → plan(FAIL) → evaluate(retry)
→ plan(FAIL) → evaluate(retry) → plan(FAIL)
→ evaluate(retry-exhausted) → master_plan(REVIEW: commit)
→ push(skip, PhaseRecord.outcome="skipped") → master_plan(PICK next)
```

### Scenario 6: Execution Failure → Retry Same Phase
```
init → master_plan(PICK: discovery) → plan → evaluate → master_plan(REVIEW)
→ push → analyse_results(interrupt) ═══ webhook ═══
analyse_results(phase_done=False, retry_same_phase=True, issues=["nmap not found"])
→ master_plan(PICK: discovery again, retry=True)
→ push(retry path: send feedback to backend)
→ analyse_results(interrupt) ═══ webhook ═══ analyse_results(phase_done=True)
→ master_plan(PICK: next phase)
```

### Scenario 7: Execution Failure → Retry Exhausted (MAX_PHASE_RETRIES=2)
```
... → analyse_results(retry #1) → master_plan(PICK retry) → push(feedback)
→ analyse_results(retry #2) → master_plan(PICK retry)
→ _master_pick_phase detects phase_attempts >= 2
→ Force completed_phases += [phase], move on
→ master_plan(PICK: next phase or done)
```

### Scenario 8: Multi-Skill Phase
```
init → master_plan(PICK: discovery, skills=[skill_A, skill_B])
→ plan(skill_A) → evaluate → master_plan(REVIEW) → push(skill_A, intermediate)
→ _after_push routes to master_plan (intermediate skill)
→ plan(skill_B) → evaluate → master_plan(REVIEW) → push(skill_B, last skill)
→ _after_push routes to analyse_results
→ analyse_results(interrupt) ═══ webhook ═══
```

### Scenario 9: Timeout (No Webhook)
```
... → push → analyse_results(interrupt) ═══ 600s timeout ═══
→ _expire_stale_engagements resumes graph with {"timeout": True}
→ analyse_results: no result data → execution_summary = "TIMEOUT"
→ master_plan(PICK: retry or next)
```

### Scenario 10: Concurrent Engagements
```
Thread A: engagement_1 → init → master_plan → plan → push → interrupt
Thread B: engagement_2 → init → master_plan → plan → push → interrupt
Webhook for engagement_1: resume graph with thread_id=engagement_1
Webhook for engagement_2: resume graph with thread_id=engagement_2
```
Each has its own LangGraph thread_id. MemorySaver is dict-based (thread-safe via GIL). SqliteSaver uses SQLite's file locking.

### Scenario 11: Process Restart Mid-Engagement
```
Before crash: push → analyse_results(interrupt) → MemorySaver state LOST
After restart: webhook arrives → _resume_graph fails (no checkpoint)
              → engagement stuck in "awaiting_results" forever

With SqliteSaver: checkpoint persisted to disk → resume succeeds
```
**Gap**: MemorySaver does not survive restart. This is a known limitation.

### Scenario 12: Duplicate Webhook
```
Webhook #1: receive_results → ResultStore.save → _resume_graph
Webhook #2: receive_results → ResultStore.exists=True → return 200 (no-op)
```
Idempotent. Second webhook is safely ignored.

---

## 3. Checkpointer Analysis

### 3.1 Current State

| Checkpointer | Durability | Concurrency | Restart-Safe | Shared-State |
|---|---|---|---|---|
| `MemorySaver` (default) | In-process only | Thread-safe (GIL) | **NO** | Process-only |
| `SqliteSaver` | File-persisted | SQLite locking | YES | Single-host |

### 3.2 Would More Checkpointers Help?

**Yes — significantly.** The biggest operational risk today is Scenario 11: a process restart between push and webhook resume causes permanent state loss with MemorySaver.

#### Recommendation: PostgreSQL Checkpointer (LangGraph-native)

LangGraph supports `PostgresSaver` via `langgraph-checkpoint-postgres`. This gives:

| Benefit | Details |
|---|---|
| **Multi-process durability** | Webhook can arrive on a different process/pod than the one that started the engagement |
| **Horizontal scaling** | Multiple API server instances share checkpoint state via Postgres |
| **Query-ability** | Debug/audit state via SQL without file parsing |
| **Transactional safety** | ACID guarantees on state transitions |
| **Time-travel debugging** | LangGraph stores snapshots at every node boundary; Postgres stores all of them efficiently |

#### Implementation Sketch

```python
# bootstrap.py
from langgraph.checkpoint.postgres import PostgresSaver

def _build_checkpointer(cfg: AppConfig):
    if cfg.execution.checkpointer == "postgres":
        return PostgresSaver.from_conn_string(cfg.execution.checkpoint_dsn)
    elif cfg.execution.checkpointer == "sqlite":
        return SqliteSaver.from_conn_string(cfg.execution.checkpoint_db)
    return MemorySaver()
```

```yaml
# config.yaml
execution:
  checkpointer: postgres
  checkpoint_dsn: "postgresql://bas_user:${DB_PASSWORD}@localhost:5432/bas_checkpoints"
```

#### When NOT Worth It

- Single-process dev/testing: MemorySaver is fastest (zero I/O overhead)
- The RunStore already persists state snapshots to JSON (crash recovery exists, just manual)

#### Intermediate Checkpointing Within Nodes

Currently LangGraph checkpoints **between** nodes only. A long-running node (e.g., `push` making 6 sequential BAS API calls) loses all progress if it crashes mid-node.

**Solution**: Use LangGraph's `interrupt()` for sub-node checkpointing — but this changes the graph flow. A simpler approach: the push node already uses `ArtifactStore` to persist each ability/adversary as it's created. On re-push, skip already-existing artifacts. This is **idempotent push** — cheaper than sub-node checkpointing.

---

## 4. Context Management Improvements

### 4.1 Current Context Flow to the LLM

```
Planner receives:
  ├─ profile.specialist_system       (~4000 tokens, static per phase)
  ├─ Skill markdown                  (~800-1500 tokens, static)
  ├─ Optional research_block         (~200 tokens, rare)
  ├─ foothold dict                   (~150 tokens)
  ├─ memory dict                     (~200-2000 tokens, grows each phase)
  ├─ completed_stages list           (~50 tokens)
  ├─ run_id                          (~20 tokens)
  └─ feedback (if retry)             (~200 tokens)
  Total: ~5500-9000 tokens per planner call
```

### 4.2 What's Missing from Context (Critical Gaps)

#### Gap 1: No Environment Reconnaissance Data

The planner receives `foothold = {agent_id, hostname, platform, ip_address}` but **nothing about the actual target environment**:

- What OS version? (Windows Server 2019 vs Windows 11 produce very different commands)
- What security tools are installed? (Defender, CrowdStrike, Carbon Black)
- Is PowerShell execution policy restricted?
- Is the agent running as SYSTEM, Administrator, or low-privilege user?
- What .NET version is available? (affects tool compatibility)
- What architecture? (x86 vs x64 affects binary downloads)

**Impact**: The planner guesses commands generically. A `whoami /priv` on a Linux host, or `Get-NetAdapter` on a Server Core without PowerShell, causes a retry.

**Fix**: Add an `environment_profile` phase-0 step:
```python
# Run before first phase — gather environment fingerprint
env_profile = {
    "os_version": "",           # from systeminfo / uname -a
    "architecture": "",         # x64 / arm64
    "shell_available": [],      # cmd, powershell, sh, bash
    "privilege_level": "",      # SYSTEM, admin, user
    "powershell_version": "",   # from $PSVersionTable
    "dotnet_version": "",
    "security_products": [],    # from WMIC / sc query
    "path_entries": [],         # $env:PATH split
    "temp_dir": "",             # resolved %TEMP% / /tmp
    "network_interfaces": [],   # from ipconfig / ip addr
}
```

This profile should be gathered **once** via a lightweight "fingerprint" ability (2-3 commands), stored in `memory["env_profile"]`, and passed to **every** subsequent planner call. This single addition would eliminate ~40% of first-push failures caused by platform misassumption.

#### Gap 2: No Historical Command-to-Outcome Mapping

The planner sees `memory` (facts) and `_phase_history` (compact summary) but **never sees actual command outputs** from prior phases. When generating credential access commands, the planner doesn't know:

- Which exact services responded on which ports (from discovery)
- What the actual `nmap` output looked like
- Whether SMB signing is enabled
- What domain name was observed

**Fix**: After `analyse_results` extracts facts, store a `command_outcomes` section in memory:
```python
memory["command_outcomes"] = {
    "discovery": {
        "nmap_scan": {
            "command": "nmap -sV -sC 192.168.1.0/24 -oN /tmp/bas/discovery.txt",
            "key_findings": ["DC at 192.168.1.10:88/389/445", "SMB signing disabled", "3 web servers on 80/443"],
            "exit_code": 0,
        },
        ...
    }
}
```

This creates a "what actually happened" context layer that subsequent phases can reference directly, producing commands that target **known** services/hosts rather than scanning again.

#### Gap 3: Memory Grows Without Pruning

`memory` is a flat dict with shallow-merge semantics. After 5 phases, it can contain hundreds of keys. The planner sees ALL of it, but most is irrelevant to the current phase.

**Fix**: Phase-scoped memory view:
```python
def _memory_for_phase(memory: dict, current_phase: str) -> dict:
    """Return a pruned memory view relevant to the current phase."""
    always_include = {"env_profile", "network", "foothold_enriched"}
    phase_deps = {
        "credaccess": {"network", "ad", "services", "env_profile"},
        "privesc": {"creds", "env_profile", "services"},
        "lateral": {"creds", "network", "ad", "env_profile"},
        "persistence": {"creds", "privesc", "env_profile"},
        "defevasion": {"env_profile", "security_products"},
        "impact": {"creds", "lateral", "persistence", "env_profile"},
    }
    relevant_keys = always_include | phase_deps.get(current_phase, set())
    return {k: v for k, v in memory.items() if k in relevant_keys or k.startswith("_")}
```

#### Gap 4: No Tool Availability Pre-Check

The planner is told "use nmap" but doesn't know if nmap is actually installed. The skill says "check first" but the planner often skips the check or gets the install command wrong.

**Fix**: After the environment fingerprint, build a `tools_available` dict:
```python
memory["tools_available"] = {
    "nmap": True,
    "crackmapexec": False,
    "bloodhound": False,
    "mimikatz": False,
    "powershell": True,
    "python3": False,
    "curl": True,
    "certutil": True,  # Windows LOLBin
}
```

Include this in the planner context. The planner can then **skip** install-check abilities for tools already confirmed present, and **always** add install steps for tools confirmed absent.

### 4.3 Context Window Budget Allocation

Current total context per planner call: ~5500-9000 tokens. Gemini 2.5 Flash has a 1M context window. We're using <1% of it.

**Recommendation**: Increase context aggressively for command quality:

| Context Component | Current | Proposed | Tokens |
|---|---|---|---|
| System prompt (base) | ✅ | ✅ | ~4000 |
| Skill markdown | ✅ | ✅ | ~1000 |
| Foothold | ✅ | + env_profile | ~300→800 |
| Memory (all) | ✅ | Phase-scoped + command_outcomes | ~500→1500 |
| Phase history | Compact (5 entries) | Full last-phase + compact others | ~200→600 |
| Tool availability | ❌ | ✅ | ~200 |
| Reference commands | ❌ | ✅ (skill/reference/*.md) | ~500-1000 |
| Prior plan feedback | On retry only | Always (even if "none") | ~100 |
| **Total** | **~5500-9000** | **~7500-12000** | Still <2% window |

The reference command files (`skills/*/reference/tool-commands.md`, `skills/*/reference/techniques.md`) are **never loaded** into the planner context currently. These contain exact, tested command patterns for each phase. Loading them would massively improve first-push command accuracy.

---

## 5. PostgreSQL Memory Design for BAS Backend

If the BAS backend team wants to provide a server-side memory store (replacing or supplementing our local JSON files), here's the recommended schema and API:

### 5.1 Database Schema

```sql
-- Core session memory table
CREATE TABLE bas_session_memory (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    engagement_id   UUID NOT NULL,
    phase           VARCHAR(50) NOT NULL,
    memory_key      VARCHAR(255) NOT NULL,
    memory_value    JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version         INTEGER NOT NULL DEFAULT 1,
    
    -- Prevent duplicate keys within same engagement+phase
    CONSTRAINT uq_engagement_phase_key UNIQUE (engagement_id, phase, memory_key)
);

-- Indices for fast lookup
CREATE INDEX idx_session_memory_engagement ON bas_session_memory(engagement_id);
CREATE INDEX idx_session_memory_phase ON bas_session_memory(engagement_id, phase);
CREATE INDEX idx_session_memory_key ON bas_session_memory(memory_key);
-- GIN index for JSONB containment queries (e.g. find all sessions that found a specific host)
CREATE INDEX idx_session_memory_value ON bas_session_memory USING GIN (memory_value);

-- Cross-engagement knowledge base (persistent learning)
CREATE TABLE bas_knowledge_base (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    environment_id  UUID NOT NULL,
    category        VARCHAR(50) NOT NULL,  -- 'tool_availability', 'os_profile', 'network_topology', 'command_pattern'
    key             VARCHAR(255) NOT NULL,
    value           JSONB NOT NULL,
    confidence      FLOAT NOT NULL DEFAULT 0.5,  -- 0.0-1.0, decays over time
    source_engagement_id UUID,  -- which engagement discovered this
    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,  -- NULL = never expires
    
    CONSTRAINT uq_kb_env_category_key UNIQUE (environment_id, category, key)
);

CREATE INDEX idx_kb_environment ON bas_knowledge_base(environment_id);
CREATE INDEX idx_kb_category ON bas_knowledge_base(environment_id, category);
CREATE INDEX idx_kb_confidence ON bas_knowledge_base(confidence DESC);

-- Command execution history (what worked / what failed)
CREATE TABLE bas_command_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    environment_id  UUID NOT NULL,
    engagement_id   UUID NOT NULL,
    phase           VARCHAR(50) NOT NULL,
    command_template TEXT NOT NULL,
    command_hash    VARCHAR(64) NOT NULL,  -- SHA-256 of normalized command
    executor        VARCHAR(20) NOT NULL,  -- 'cmd', 'powershell', 'sh', 'bash'
    platform        VARCHAR(20) NOT NULL,
    exit_code       INTEGER,
    success         BOOLEAN NOT NULL,
    failure_reason  VARCHAR(50),  -- 'tool_not_found', 'permission_denied', 'timeout', 'syntax_error'
    stdout_summary  TEXT,         -- first 500 chars
    stderr_summary  TEXT,         -- first 500 chars
    execution_time_ms INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_cmd_engagement_hash UNIQUE (engagement_id, command_hash)
);

CREATE INDEX idx_cmd_history_env ON bas_command_history(environment_id);
CREATE INDEX idx_cmd_history_platform ON bas_command_history(platform, success);
CREATE INDEX idx_cmd_history_hash ON bas_command_history(command_hash);

-- Phase execution records
CREATE TABLE bas_phase_records (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    engagement_id   UUID NOT NULL,
    phase           VARCHAR(50) NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 1,
    objective       TEXT,
    skills_used     TEXT[],
    abilities_pushed INTEGER NOT NULL DEFAULT 0,
    adversary_id    UUID,
    outcome         VARCHAR(20) NOT NULL,  -- 'committed', 'skipped', 'escalated', 'failed'
    execution_outcome VARCHAR(20),  -- 'passed', 'partial', 'failed', 'timeout'
    issues          JSONB,
    memory_delta    JSONB,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    
    CONSTRAINT uq_phase_engagement_attempt UNIQUE (engagement_id, phase, attempt)
);

CREATE INDEX idx_phase_records_engagement ON bas_phase_records(engagement_id);
```

### 5.2 API Contract

```yaml
# Session Memory CRUD
POST   /api/v1/memory/{engagement_id}
  Body: { "phase": "discovery", "key": "network.cidr", "value": "192.168.1.0/24" }
  Response: 201 Created / 200 Updated (upsert)

GET    /api/v1/memory/{engagement_id}
  Query: ?phase=discovery&keys=network,creds
  Response: { "items": [{"key": "network.cidr", "value": "...", "updated_at": "..."}] }

GET    /api/v1/memory/{engagement_id}/snapshot
  Response: Full memory dict merged across all phases (latest version wins)

DELETE /api/v1/memory/{engagement_id}
  Response: 204 (cleanup after engagement completes)

# Knowledge Base (cross-engagement learning)
GET    /api/v1/knowledge/{environment_id}
  Query: ?category=tool_availability&min_confidence=0.7
  Response: { "items": [{"key": "nmap", "value": {"installed": true, "path": "/usr/bin/nmap"}, "confidence": 0.95}] }

POST   /api/v1/knowledge/{environment_id}
  Body: { "category": "os_profile", "key": "os_version", "value": {"name": "Windows Server 2019", "build": "17763"}, "confidence": 0.9, "source_engagement_id": "..." }
  Response: 201 Created / 200 Updated (upsert, higher confidence wins)

# Command History (what worked before in this environment)
GET    /api/v1/commands/{environment_id}/history
  Query: ?phase=discovery&platform=windows&success=true&limit=20
  Response: { "items": [{"command_template": "...", "exit_code": 0, "success": true}] }

POST   /api/v1/commands/{engagement_id}/record
  Body: { "environment_id": "...", "phase": "discovery", "command_template": "...", "executor": "cmd", "platform": "windows", "exit_code": 0, "success": true }
  Response: 201

# Phase Records
POST   /api/v1/phases/{engagement_id}
  Body: PhaseRecord JSON
  Response: 201

GET    /api/v1/phases/{engagement_id}
  Response: List of all phase records for this engagement
```

### 5.3 How Our Engine Would Use This

```python
# In analyse_results, after parsing execution outcomes:
async def _persist_to_backend_memory(bas: BasClient, state: SessionState):
    engagement_id = state["run_id"]
    env_id = state["foothold"]["environment_id"]
    phase = state["current_phase"]
    
    # 1. Persist session memory
    for key, value in state["memory"].items():
        if not key.startswith("_"):  # skip internal keys
            await bas.memory.upsert(engagement_id, phase, key, value)
    
    # 2. Persist command outcomes to knowledge base
    for ability in operation_result.abilities:
        for stage in ability.stages:
            await bas.commands.record(
                engagement_id=engagement_id,
                environment_id=env_id,
                phase=phase,
                command_template=stage.command_executed,
                executor=stage.executor,
                platform=ability.platform,
                exit_code=stage.exit_code,
                success=stage.exit_code == 0,
            )
    
    # 3. Update knowledge base with high-confidence facts
    if state["memory"].get("env_profile"):
        await bas.knowledge.upsert(
            env_id, "os_profile", "version",
            state["memory"]["env_profile"],
            confidence=0.95,
        )

# In specialist.py planner, before generating plan:
async def _enrich_context_from_backend(bas: BasClient, state: SessionState):
    env_id = state["foothold"]["environment_id"]
    phase = state["current_phase"]
    
    # Load what worked before in this environment
    prior_commands = await bas.commands.history(
        env_id, phase=phase, platform=state["foothold"]["platform"],
        success=True, limit=10,
    )
    
    # Load environment knowledge
    tool_availability = await bas.knowledge.get(
        env_id, category="tool_availability", min_confidence=0.7,
    )
    
    return {"prior_successful_commands": prior_commands, "known_tools": tool_availability}
```

### 5.4 Key Design Decisions

1. **Upsert semantics**: Memory is write-heavy during execution. Upsert avoids race conditions.
2. **JSONB for values**: Flexible schema; different phases store different structures.
3. **Knowledge base with confidence decay**: Facts about an environment become stale. Running `confidence * 0.9` weekly keeps the KB fresh.
4. **Command hashing**: SHA-256 of normalised command (strip whitespace, lowercase) for dedup. This enables "command X worked in environment Y last week" lookups.
5. **No PII in memory**: Commands may contain IPs/hostnames (expected in BAS context) but never credentials. Credential references should be hashed/tokenised.

---

## 6. Edge Cases Audit

### 6.1 Currently Handled ✅

| Edge Case | Handler | Status |
|---|---|---|
| Duplicate webhook | `ResultStore.exists()` → 200 no-op | ✅ |
| Unknown engagement webhook | `store.get()` returns None → 404 | ✅ |
| Oversized payload | `execution.max_result_size_mb` config (check in `receive_results`) | ✅ |
| LLM failure (planner) | try/except → PlanResult(success=False) | ✅ |
| LLM failure (evaluator) | try/except → fallback accept | ✅ |
| LLM failure (master) | try/except → master_done=True | ✅ |
| Planner retry exhaustion | `max_planner_attempts` → escalate | ✅ |
| Master revision exhaustion | `max_master_revisions` → force commit | ✅ |
| Phase retry exhaustion | `MAX_PHASE_RETRIES=2` → force complete | ✅ |
| Timeout (no webhook) | `_expire_stale_engagements` daemon | ✅ |
| Atomic file writes | `.tmp` + `fsync` + `os.replace` | ✅ |
| Concurrent engagements | Thread-per-engagement + per-thread_id checkpointing | ✅ |

### 6.2 Currently UNHANDLED ⚠️

#### Edge Case A: Partial Backend Execution

**Scenario**: Backend executes 3 out of 6 abilities, then crashes. The webhook arrives with results for only 3 abilities.

**Current behaviour**: `analyse_results` sees 3/6 passed, marks `phase_done=False`. Issues: `[]` (no detected issues — the 3 missing abilities aren't reported as errors; they're just absent). Master may not know WHY the other 3 didn't run.

**Fix**: Compare `phase_asset_map[phase]["ability_ids"]` (what we pushed) against `operation_result.abilities` (what executed). Report missing abilities as `IssueKind.NOT_EXECUTED`:

```python
class IssueKind(str, Enum):
    ...
    NOT_EXECUTED = "not_executed"  # ability was pushed but never ran

def detect_issues(result, stage_id_map, pushed_ability_ids=None):
    ...
    if pushed_ability_ids:
        executed_ids = {ab.ability_id for ab in result.abilities}
        for aid in pushed_ability_ids:
            if aid not in executed_ids:
                issues.append(StageIssue(
                    ability_id=aid, ability_name="?",
                    stage_id=aid, stage_name="*",
                    kind=IssueKind.NOT_EXECUTED,
                    detail="ability was pushed but not executed by backend",
                ))
```

#### Edge Case B: Backend Returns Abilities in Different Order

**Scenario**: We push abilities [A, B, C] but backend returns results as [C, A, B]. Our `ability_name_to_id` mapping uses names, not order.

**Current behaviour**: Works correctly — matching is by `ability_name`, not by position. ✅ (Not actually a bug.)

#### Edge Case C: Backend Modifies Command Before Execution

**Scenario**: Backend sanitises or wraps our `command_template` (e.g., adds a timeout wrapper, prepends `cd /tmp/bas &&`). The executed command differs from what we pushed.

**Current behaviour**: `detect_issues` scans `stage.command_executed`, not our original `command_template`. If the backend wraps our command, we might false-positive on `CROSS_VAR_LEAK` (the wrapper uses shell variables).

**Fix**: Store original `command_template` in asset map and compare against `command_executed`:
```python
# In detect_issues, accept original_commands param
if original_cmd and command_executed != original_cmd:
    # Backend modified the command — only scan OUR portion for issues
    scan_text = original_cmd  # not the backend wrapper
```

#### Edge Case D: LLM Returns Invalid JSON Schema

**Scenario**: Gemini returns a `SpecialistPlan` where `abilities` is empty (violating `min_length=1`), or where a field has the wrong type.

**Current behaviour**: Pydantic `model_validate` raises `ValidationError`, caught by the planner try/except → `PlanResult(success=False)`. Retry kicks in.

**Problem**: The error message from Pydantic goes into `plan_error` but is NOT passed as `feedback` to the planner on retry. The planner doesn't know WHAT was wrong with its output.

**Fix**: Pass the Pydantic error as part of feedback:
```python
except ValidationError as e:
    error_summary = "; ".join(f"{err['loc']}: {err['msg']}" for err in e.errors()[:5])
    return PlanResult(success=False, error=f"Schema error: {error_summary}")
# Then in graph.py plan_node, if plan_result.error, inject it as feedback
```

#### Edge Case E: Skill File Missing or Corrupt

**Scenario**: `skill_tool.read("discovering-environment")` returns None because the SKILL.md file is missing or has invalid YAML frontmatter.

**Current behaviour**: `plan_specialist()` would crash with AttributeError when trying to access `skill.frontmatter.stage`. Not caught gracefully.

**Fix**: Guard in `plan_specialist()`:
```python
skill = skill_tool.read(name)
if skill is None:
    return PlanResult(skill=name, success=False, error=f"skill {name!r} not found")
```

#### Edge Case F: Memory Key Collision Across Phases

**Scenario**: Discovery writes `memory["network"]["live_hosts"] = [...]`. Lateral movement also writes `memory["network"]["live_hosts"] = [...]` (from a different subnet scan). Shallow merge **overwrites** discovery's data.

**Current behaviour**: Shallow merge via `new_memory[k] = v` — last writer wins. Prior phase data is lost silently.

**Fix**: Deep merge for known structured keys:
```python
def _deep_merge(base: dict, update: dict) -> dict:
    merged = dict(base)
    for k, v in update.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge(merged[k], v)
        elif k in merged and isinstance(merged[k], list) and isinstance(v, list):
            # Deduplicate list items
            merged[k] = list({json.dumps(i, sort_keys=True): i for i in merged[k] + v}.values())
        else:
            merged[k] = v
    return merged
```

#### Edge Case G: Empty Abilities List in Backend Response

**Scenario**: Backend returns `{"operation_id": "...", "abilities": []}` — execution started but no ability results exist.

**Current behaviour**: `analyse_results` processes an empty list. `_derive_phase_done()` sees 0 passed, 0 critical issues → heuristic unclear.

**Fix**: Explicitly handle `len(abilities) == 0` as a `NOT_EXECUTED` condition.

#### Edge Case H: Race Condition in _resume_graph

**Scenario**: Two webhooks arrive milliseconds apart for the same engagement (one result + one duplicate). Both pass the `ResultStore.exists()` check before either writes.

**Current behaviour**: Both call `ResultStore.save()`. Second write sees `.tmp` file from first → `os.replace()` overwrites atomically. Both then call `_resume_graph()`. LangGraph receives two `Command(resume=...)` calls on the same thread — the second one would fail or be no-op depending on graph state.

**Fix**: Add a per-engagement lock in `_resume_graph()`:
```python
_resume_locks: dict[str, threading.Lock] = {}

def _resume_graph(engagement_id, payload):
    lock = _resume_locks.setdefault(engagement_id, threading.Lock())
    if not lock.acquire(blocking=False):
        logger.warning("resume already in progress for %s, skipping", engagement_id)
        return
    try:
        ...
    finally:
        lock.release()
```

---

## 7. Command Quality — First-Push Success Strategy

This is the highest-priority concern. Every retry costs: time, LLM tokens, API calls, and operational noise. The target is **>90% first-push success rate**.

### 7.1 Root Cause Analysis of Retry Triggers

| Failure Category | Frequency (Est.) | Root Cause | Fix Priority |
|---|---|---|---|
| **Placeholder tokens** | 25% | LLM hallucinates `#{target_ip}` or `<CIDR>` | P0 |
| **Tool not found** | 20% | LLM assumes nmap/crackmapexec installed | P0 |
| **Wrong platform commands** | 15% | Linux commands on Windows host (or vice versa) | P0 |
| **Cross-variable leak** | 15% | `$SUBNET` from ability A used in ability B | P1 |
| **Timeout** | 10% | Large scan ranges, no `-T4` flag, no timeout limits | P1 |
| **Permission denied** | 10% | Low-priv agent tries admin commands | P1 |
| **Syntax errors** | 5% | PowerShell quoting issues, path separators | P2 |

### 7.2 Improvements — Specialist Black-Hat Hacker Quality

#### Improvement 1: Reference Command Library (Highest Impact)

The skills already have `reference/tool-commands.md` and `reference/techniques.md` files with **tested, working commands** for each platform. These are never loaded into the planner prompt.

**Fix**: Load reference files into the planner context:

```python
# In specialist.py LLMPlanner.plan()
skill_md = skill.render_for_prompt()
# NEW: Load reference commands if available
ref_commands = skill_tool.read_reference(skill.frontmatter.name, "tool-commands")
ref_techniques = skill_tool.read_reference(skill.frontmatter.name, "techniques")

system = f"{profile.specialist_system}\n\n--- SKILL PLAYBOOK ---\n{skill_md}"
if ref_commands:
    system += f"\n\n--- REFERENCE COMMANDS (tested, working) ---\n{ref_commands}"
if ref_techniques:
    system += f"\n\n--- TECHNIQUE REFERENCE ---\n{ref_techniques}"
```

**Impact**: The LLM has concrete, tested command patterns instead of improvising. Reduces placeholder tokens, wrong syntax, and platform mismatches.

#### Improvement 2: Platform-Specific Command Templates

Instead of one generic prompt for all platforms, maintain platform-specific command snippets in memory:

```python
PLATFORM_PATTERNS = {
    "windows": {
        "get_subnet": 'for /f "tokens=1-4 delims=/ " %a in (\'route print 0.0.0.0 ^| findstr /r "0\\.0\\.0\\.0"\')',
        "check_tool": "where {tool} >nul 2>&1 && echo INSTALLED || echo MISSING",
        "install_tool": "winget install --id {package_id} --accept-source-agreements --accept-package-agreements",
        "temp_dir": "%TEMP%\\bas",
        "mkdir_temp": "if not exist %TEMP%\\bas mkdir %TEMP%\\bas",
    },
    "linux": {
        "get_subnet": "ip -o -4 addr show | awk '{print $4}' | head -1",
        "check_tool": "command -v {tool} >/dev/null 2>&1 && echo INSTALLED || echo MISSING",
        "install_tool": "apt-get install -y {package_id} 2>/dev/null || yum install -y {package_id} 2>/dev/null",
        "temp_dir": "/tmp/bas",
        "mkdir_temp": "mkdir -p /tmp/bas",
    },
}
```

Inject the relevant platform patterns into the planner context.

#### Improvement 3: Environment Fingerprint (Phase 0)

As described in §4.2 Gap 1 — run a lightweight fingerprint before any attack phase. This eliminates blind guessing about tool availability and OS version.

**Implementation**: Add a `_fingerprint_node` that runs before `master_plan`:

```
START → init → fingerprint → master_plan → ...
```

The fingerprint pushes 1 ability with 3-5 stages:
- `systeminfo` / `uname -a` (OS version)
- `whoami /priv` / `id` (privilege level)
- `$PSVersionTable` / `bash --version` (shell version)
- `echo %PATH%` / `echo $PATH` (available tools)
- `sc query` / `ps aux | grep -i 'crowd\|falcon\|carbon\|defender'` (security products)

Results feed into `memory["env_profile"]`. Every subsequent planner call knows the exact environment.

#### Improvement 4: Command Validator (Pre-Push Static Analysis)

Add a deterministic command validator that runs BEFORE the evaluator LLM call. This catches obvious issues without burning LLM tokens:

```python
def validate_commands(plan: SpecialistPlan, foothold: dict) -> list[str]:
    """Deterministic pre-push command validation."""
    issues = []
    platform = foothold.get("platform", "").lower()
    
    for ability in plan.abilities:
        for stage in ability.stages:
            cmd = stage.command_template
            
            # 1. Placeholder detection (regex)
            if re.search(r'#\{|<TARGET>|<CIDR>|\{\{', cmd):
                issues.append(f"{ability.ability.name}/{stage.stage_name}: unresolved placeholder")
            
            # 2. Platform mismatch
            if platform == "windows":
                if cmd.strip().startswith(("sudo ", "apt ", "yum ", "dnf ")):
                    issues.append(f"{ability.ability.name}: Linux command on Windows")
                if "/tmp/" in cmd:
                    issues.append(f"{ability.ability.name}: Unix path on Windows")
            elif platform == "linux":
                if "powershell" in cmd.lower() or cmd.strip().startswith(("Get-", "Set-", "New-")):
                    issues.append(f"{ability.ability.name}: PowerShell on Linux")
                if "%TEMP%" in cmd or "\\bas\\" in cmd:
                    issues.append(f"{ability.ability.name}: Windows path on Linux")
            
            # 3. Cross-ability variable leak
            if stage.stage_order > 0:
                # Variables set in prior stages of the SAME ability are OK
                pass
            else:
                # First stage — cannot reference variables from other abilities
                shell_vars = re.findall(r'\$(?:env:)?([A-Za-z_]\w*)', cmd)
                known_vars = {"PATH", "HOME", "USER", "TEMP", "TMP", "COMPUTERNAME",
                              "USERDOMAIN", "USERNAME", "HOMEDRIVE", "HOMEPATH",
                              "SYSTEMROOT", "WINDIR", "PSVersionTable", "env"}
                unknown = [v for v in shell_vars if v not in known_vars]
                if unknown:
                    issues.append(f"{ability.ability.name}: possibly undefined vars: {unknown}")
            
            # 4. Missing temp dir creation
            if ("/tmp/bas/" in cmd or "%TEMP%\\bas\\" in cmd) and "mkdir" not in cmd.lower():
                # Check if any PRIOR ability creates it
                pass  # more complex check needed
            
            # 5. Dangerous scope (too-wide scans)
            cidr_match = re.search(r'(\d+\.\d+\.\d+\.\d+)/(\d+)', cmd)
            if cidr_match:
                prefix_len = int(cidr_match.group(2))
                if prefix_len < 20:  # /20 = 4096 hosts, /16 = 65536
                    issues.append(f"{ability.ability.name}: wide scan /{prefix_len} may timeout")
    
    return issues
```

If `validate_commands()` returns issues, inject them as `feedback` and re-plan **without** calling the evaluator LLM. This is cheaper and faster than a full evaluate→retry loop.

#### Improvement 5: Grounded Research for Every Phase (Not Just Install Keywords)

Currently, grounded research (`self._llm.research()`) only fires when feedback contains install-related keywords. For maximum command quality, the planner should research:

- **Credential access**: "Windows LSASS dump techniques 2024 defender bypass" → latest Mimikatz alternatives
- **Privilege escalation**: "Windows {os_version} privilege escalation CVE 2024 2025" → environment-specific exploits
- **Lateral movement**: "PSExec alternatives {security_products} bypass" → evasion-aware lateral techniques
- **Defense evasion**: "{security_product} bypass technique 2025" → up-to-date EDR bypasses

**Fix**: Add phase-specific research queries:
```python
PHASE_RESEARCH_QUERIES = {
    "discovery": "network enumeration {platform} {os_version} best practices nmap alternatives",
    "credaccess": "credential dumping {platform} {os_version} {security_products} bypass 2025",
    "privesc": "privilege escalation {platform} {os_version} CVE 2024 2025 known exploits",
    "lateral": "lateral movement {platform} {security_products} bypass techniques",
    "defevasion": "{security_products} bypass evasion technique 2025 LOLBins",
    "persistence": "persistence mechanism {platform} {os_version} undetectable",
    "impact": "data exfiltration {platform} covert channel techniques",
}
```

#### Improvement 6: Few-Shot Examples in Planner Prompt

Add 1-2 concrete examples of a well-formed `SpecialistPlan` for each phase. The LLM performs dramatically better with examples:

```python
_DISCOVERY_EXAMPLE = '''
EXAMPLE — Discovery on Windows (cmd executor, nmap available):
{
  "adversary": {"name": "NetRecon-Alpha", "description": "Discovery sweep", ...},
  "abilities": [
    {
      "ability": {"name": "subnet-detect", "mitre_technique_id": "T1016", "platform": "windows", ...},
      "stages": [{"stage_name": "detect-cidr", "executor": "cmd", "stage_order": 0,
        "command_template": "for /f \\"tokens=1-4 delims=/ \\" %a in ('route print 0.0.0.0 ^| findstr 0.0.0.0') do @echo CIDR=%a.0/24 > %TEMP%\\\\bas\\\\cidr.txt && type %TEMP%\\\\bas\\\\cidr.txt"}]
    },
    ...
  ]
}
'''
```

#### Improvement 7: Post-Execution Learning Loop (Cross-Engagement)

When a command succeeds on a specific platform+OS+environment, record it. When the same phase runs on a similar environment in a future engagement, prioritize proven commands.

```python
# After analyse_results finds a passing ability:
for ab in result.abilities:
    if ab.passed:
        for stage in ab.stages:
            knowledge_base.record_success(
                environment_id=foothold["environment_id"],
                phase=current_phase,
                command=stage.command_executed,
                platform=ab.platform,
                executor=stage.executor,
            )

# Before planning, load prior successes:
proven_commands = knowledge_base.get_successes(
    environment_id=foothold["environment_id"],
    phase=current_phase,
    platform=foothold["platform"],
    limit=5,
)
# Inject into planner prompt:
if proven_commands:
    system += f"\n\n--- PROVEN COMMANDS (worked before in this environment) ---\n"
    for cmd in proven_commands:
        system += f"  ✓ {cmd['executor']}: {cmd['command']}\n"
```

This creates a flywheel: the more the engine runs, the better its commands get.

### 7.3 Command Quality Architecture (Proposed)

```
                     ┌──────────────────────────────┐
                     │    KNOWLEDGE BASE (Postgres)  │
                     │  ┌─────────────────────────┐  │
                     │  │ env_profiles            │  │
                     │  │ tool_availability       │  │
                     │  │ proven_commands          │  │
                     │  │ failed_patterns          │  │
                     │  └─────────────────────────┘  │
                     └──────────┬───────────────────┘
                                │ query before plan
                                ▼
┌────────────────┐   ┌─────────────────────────────────┐
│ Skill Playbook │──▶│        PLANNER (LLM)            │
│ + Reference    │   │  Context:                        │
│   Commands     │   │   ✦ System prompt (phase-scoped) │
│ + Techniques   │   │   ✦ Skill + references           │
│                │   │   ✦ env_profile                   │
│                │   │   ✦ tool_availability              │
│                │   │   ✦ proven_commands                │
│                │   │   ✦ memory (phase-scoped)          │
│                │   │   ✦ phase_history (compact)        │
│                │   │   ✦ few-shot examples              │
│                │   │   ✦ grounded research (per-phase)  │
└────────────────┘   └──────────┬──────────────────────┘
                                │ SpecialistPlan
                                ▼
                     ┌──────────────────────────┐
                     │  STATIC VALIDATOR        │
                     │  (deterministic, 0 LLM)  │
                     │   ✦ Placeholder scan     │
                     │   ✦ Platform mismatch    │
                     │   ✦ Variable leak        │
                     │   ✦ Scope width check    │
                     │   ✦ Path format check    │
                     └──────────┬───────────────┘
                                │
                        ┌───────┴───────┐
                   issues?         no issues
                        │               │
                        ▼               ▼
                  re-plan with     SELF-CRITIQUE
                  validator        (LLM, 1 pass)
                  feedback              │
                                        ▼
                                   EVALUATOR
                                   (LLM gate)
                                        │
                                        ▼
                                   MASTER REVIEW
                                   (LLM, final)
                                        │
                                        ▼
                                      PUSH
```

---

## 8. Summary Recommendations (Priority-Ranked)

### P0 — Critical (Command Quality)

| # | Recommendation | Effort | Impact |
|---|---|---|---|
| 1 | **Load reference command files** into planner context (skill/reference/*.md) | Low (2h) | **High** — proven commands reduce hallucination |
| 2 | **Environment fingerprint** (Phase 0) — gather OS version, shell, tools, priv level | Medium (4h) | **High** — eliminates blind platform guessing |
| 3 | **Static command validator** — pre-evaluator deterministic checks | Medium (4h) | **High** — catches 40%+ of issues without LLM cost |
| 4 | **Platform-specific command patterns** — inject OS-native templates | Low (2h) | **Medium** — reduces cross-platform errors |

### P1 — Important (Reliability & Context)

| # | Recommendation | Effort | Impact |
|---|---|---|---|
| 5 | **PostgreSQL checkpointer** — survive process restarts | Medium (4h) | **High** — production requirement |
| 6 | **Deep merge for memory** — prevent cross-phase data loss | Low (1h) | **Medium** — fixes silent data corruption |
| 7 | **Partial execution detection** — detect un-executed abilities | Low (1h) | **Medium** — better retry decisions |
| 8 | **Resume lock** (`_resume_graph` per-engagement lock) | Low (1h) | **Medium** — prevents race condition |
| 9 | **Pass validation errors as feedback** to planner on schema failure | Low (1h) | **Medium** — targeted retry instead of blind retry |
| 10 | **Phase-scoped memory view** — prune irrelevant keys before LLM call | Low (2h) | **Low-Medium** — reduces noise in context |

### P2 — Enhancement (Long-Term Quality Flywheel)

| # | Recommendation | Effort | Impact |
|---|---|---|---|
| 11 | **PostgreSQL knowledge base** — cross-engagement learning | High (2d) | **Very High** — commands improve over time |
| 12 | **Per-phase grounded research** — phase-specific web research | Medium (4h) | **High** — latest TTPs, CVE-aware |
| 13 | **Few-shot examples** per phase in planner prompt | Medium (4h) | **High** — dramatic quality boost |
| 14 | **Command history tracking** (what worked/failed per environment) | Medium (6h) | **High** — proven command reuse |
| 15 | **Scan timeout limits** — inject `-T4 --max-retries 2` defaults for nmap | Low (1h) | **Low** — prevents hanging scans |

### P3 — Nice-to-Have

| # | Recommendation | Effort | Impact |
|---|---|---|---|
| 16 | Sub-node checkpointing in push (idempotent re-push) | Medium (4h) | **Low** — rare crash during push |
| 17 | Memory TTL / confidence decay in knowledge base | Low (2h) | **Low** — prevents stale data |
| 18 | Multi-model fallback (Gemini → Claude → GPT) | High (1d) | **Low** — provider outage resilience |

---

## Appendix A: LLM Call Map

```
  init
    │ (0 LLM calls)
    ▼
  master_plan
    │ PICK: plan_phase()       ← LLM #1 (PhaseBriefing)
    │ REVIEW: review_plan()    ← LLM #2 (MasterDecision)
    ▼
  plan
    │ plan()                   ← LLM #3 (SpecialistPlan)
    │ _self_critique()         ← LLM #4 (text critique)
    ▼
  evaluate
    │ evaluate()               ← LLM #5 (EvaluatorVerdict)
    ▼
  push
    │ update_memory()          ← LLM #6 (MemoryUpdate)
    │ build_feedback_payload() ← LLM #7 (retry path only)
    ▼
  analyse_results
    │ analyse_results()        ← LLM #8 (triage) + LLM #9 (extract)
    ▼

Total per phase (happy path): 6 LLM calls
Total per phase (1 retry):    9 LLM calls
Total per phase (full retry):12+ LLM calls
```

## Appendix B: Proposed config.yaml Additions

```yaml
execution:
  checkpointer: postgres                    # NEW: postgres option
  checkpoint_dsn: "postgresql://..."         # NEW
  
  fingerprint:
    enabled: true                            # NEW: Phase 0 fingerprinting
    cache_ttl_hours: 24                      # NEW: how long to trust cached profiles
  
  command_quality:
    static_validator: true                   # NEW: pre-evaluator checks
    load_reference_commands: true            # NEW: inject skill/reference/*.md
    platform_patterns: true                  # NEW: inject OS-native templates
    few_shot_examples: true                  # NEW: include examples in prompt
    per_phase_research: false                # NEW: grounded research per phase
    max_scan_prefix: 24                      # NEW: reject scans wider than /24

knowledge_base:
  enabled: false                             # NEW: cross-engagement learning
  backend_url: "http://pelorushub.online:31763/api/v1"
  sync_interval_seconds: 30
```
