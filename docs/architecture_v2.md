# Internal Attack Simulation — Architecture v2

> Result-Driven Phase Orchestration with Closed-Loop Execution Feedback

---

## 1. Problem Statement

The v1 orchestrator is fire-and-forget: it plans abilities, pushes them to the backend, then speculatively updates memory based on what the commands *would* produce if they succeeded. The master agent never learns whether nmap actually installed, whether the host sweep found anything, or whether a credential dump was blocked by Defender.

This means:
- Memory contains fiction ("network.live_hosts = pending") when commands actually failed
- The master picks the next phase assuming prior phases succeeded
- Placeholder tokens (`#{network.cidr}`) and tool-not-found errors go undetected until a human reviews the results
- There is no mechanism for the backend to report execution outcomes back to the orchestrator

v2 closes this loop.

---

## 2. Architecture Overview

### 2.1 System Context

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        ORCHESTRATOR (this system)                          │
│                                                                            │
│   FastAPI Server (port 8765)                                               │
│   ┌──────────────────────────────────────────────────────────────────────┐  │
│   │  POST /engagements           → create engagement, start graph       │  │
│   │  POST /engagements/{id}/results  → receive execution results (NEW)  │  │
│   │  GET  /engagements/{id}      → query status + state                 │  │
│   │  GET  /engagements/{id}/log  → audit log                           │  │
│   │  GET  /engagements/{id}/artifacts → abilities + adversaries pushed  │  │
│   └──────────────────────────────────────────────────────────────────────┘  │
│                                                                            │
│   LangGraph State Machine (per engagement, background thread)              │
│   ┌──────────────────────────────────────────────────────────────────────┐  │
│   │  init → master_plan → plan → evaluate → push → analyse_results ──┐ │  │
│   │    ↑                                                              │ │  │
│   │    └──────────────────────────────────────────────────────────────┘ │  │
│   └──────────────────────────────────────────────────────────────────────┘  │
│                                                                            │
│   Persistence Layer                                                        │
│   ┌──────────────────────────────────────────────────────────────────────┐  │
│   │  runs/{engagement_id}.json              ← engagement record         │  │
│   │  runs/{engagement_id}/abilities/        ← pushed ability specs      │  │
│   │  runs/{engagement_id}/adversaries/      ← pushed adversary specs    │  │
│   │  runs/{engagement_id}/results/          ← execution results (NEW)   │  │
│   └──────────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │
                     ┌──────────────┴──────────────┐
                     │                             │
                     ▼                             ▼
          ┌─────────────────────┐       ┌─────────────────────┐
          │   BAS Backend       │       │   LLM Provider      │
          │   (Pelorus Hub)     │       │   (Gemini / etc.)   │
          │                     │       │                     │
          │  POST /abilities    │       │  Structured output  │
          │  POST /adversaries  │       │  Grounded research  │
          │  POST /agents       │       │  Free-text analysis │
          │  GET  /environments │       └─────────────────────┘
          │                     │
          │  Auto-executes on   │
          │  created_by: "ai"   │
          │                     │
          │  POSTs results back │
          │  when done ─────────┼──► POST /engagements/{id}/results
          └─────────────────────┘
```

### 2.2 End-to-End Flow (Single Phase)

```
                    ORCHESTRATOR                          BACKEND
                    ──────────                            ───────
1. Master picks phase
   (discovery, privesc, etc.)
                         │
2. Planner emits                                    
   SpecialistPlan                                   
   (adversary + abilities)                          
                         │
3. Evaluator approves
   (or retry/escalate)
                         │
4. Master commits
                         │
5. Push node creates
   abilities + adversary ──────POST /abilities──────►  Backend receives
   via BAS API            ──────POST /adversaries───►  abilities + adversary
                         │                             
   Saves pending.*       │                          6. Backend auto-executes
   memory keys           │                             (created_by: "ai")
                         │                                    │
   Sets pending_op_id    │                          7. Abilities run on
                         │                             target agent
                         │                             (cmd/psh/sh)
                         │                                    │
6. Graph hits interrupt()│                                    │
   State checkpointed    │                                    │
   Thread released ──────┘                                    │
                                                              │
                    ◄─────────────────────────────  8. Backend POSTs
                                                       full result JSON
                                                       to webhook
                         │
9. Webhook resumes graph
   (load checkpoint,
    invoke with thread_id)
                         │
10. analyse_results node
    runs on fresh thread
                         │
11. Master analyses
    (two-step LLM call)
                         │
12. Memory updated with
    CONFIRMED facts only
                         │
13. Phase marked complete
    (or retry/adjust)
                         │
14. Master picks next
    phase (loop to 1)
    Graph checkpoints
    again at next push
```

---

## 3. Graph Topology

### 3.1 Node Inventory

| Node | Purpose | LLM Calls | State Mutations |
|------|---------|-----------|-----------------|
| `init` | Seed defaults, reset counters | 0 | run_id, foothold, memory, iteration, all tracking fields |
| `master_plan` | PICK mode: select next phase. REVIEW mode: commit or revise plan | 1-2 | current_phase, master_briefing, phase_skills, evaluator_action |
| `plan` | Emit SpecialistPlan for current skill | 1 (+1 self-critique) | current_plan, current_plan_summary, planner_attempts |
| `evaluate` | Grade the plan, accept/retry/escalate | 1 | evaluator_action, feedback, last_evaluator_verdict, phase_done |
| `push` | Create abilities + adversary on backend | 0 (HTTP only) | stage_results, completed_stages, pending memory |
| `analyse_results` | **NEW.** Receive backend results (resumed from checkpoint), analyse with master | 2 | memory (confirmed facts), phase_history, completed_phases |

### 3.2 Edge Map

```
START ──► init ──► master_plan
                      │
                      ├── [done=true] ──────────────────────────────────► END
                      │
                      ├── [evaluator_action='commit'] ──────────────────► push
                      │
                      └── [otherwise] ──► plan
                                           │
                                           ├── [eval finished] ──► master_plan (REVIEW)
                                           │
                                           └── [new plan] ──► evaluate
                                                               │
                                                               ├── [retry, attempts < max] ──► plan
                                                               │
                                                               └── [accept/escalate/exhausted] ──► master_plan (REVIEW)

push ──► analyse_results ──► master_plan (PICK: next phase)
```

### 3.3 Multi-Skill Phase Handling

A phase may map to multiple skills (e.g., discovery = [discovering-environment]). For multi-skill phases:

1. Push completes for skill A → accumulate in `phase_skills_buffer`
2. If more skills remain → loop back to plan (next skill in phase)
3. After last skill → `analyse_results` waits for backend, then consolidates ALL skill results into ONE `PhaseRecord`

---

## 4. Result Ingestion

### 4.1 Webhook Endpoint

```
POST /engagements/{engagement_id}/results
Content-Type: application/json
Body: <full operation result JSON from backend>
```

**Behaviour:**
- Validate `engagement_id` exists in RunStore and status is "running"
- Parse the result body into `OperationResult` (pydantic validation)
- Save raw JSON atomically to `runs/{engagement_id}/results/{operation_id}.json`
- **Idempotency**: if `results/{operation_id}.json` already exists → return 200 OK, skip re-processing
- Resume the graph from checkpoint: `graph.invoke(Command(resume=result_data), config={"thread_id": engagement_id})`
- Return 202 Accepted with `{"status": "accepted", "operation_id": "..."}`

**Security:**
- Reject payloads larger than `execution.max_result_size_mb` (default 10 MB)
- Validate operation_id format (UUID)
- Engagement must be in "running" status

### 4.2 Result Data Model

The backend result JSON contains:

```
OperationResult
├── operation_id: str
├── operation_name: str
├── operation_status: "completed" | "failed" | "partial"
├── completed_at: datetime
├── adversary: {adversary_id, name}
├── progress: {total_abilities, completed_abilities, progress_percent}
└── abilities: list[AbilityResult]
    ├── ability_id: str
    ├── name: str
    ├── mitre_technique_id: str
    ├── platform: str
    └── stages: list[StageExecution]
        ├── stage_name: str
        ├── executor: str
        ├── command_executed: str
        ├── execution_status: "passed" | "failed"
        ├── stdout: str
        ├── stderr: str
        ├── exit_code: int
        └── timestamp: datetime
```

### 4.3 Structural Summary (Code-Generated)

A deterministic function produces a compact text summary from `OperationResult`. This summary contains ONLY structural facts — no stdout extraction (the LLM does that).

```
PHASE: discovery | OP: 141be4c2 | STATUS: completed | 2/6 passed

[PASS] gather-initial-host-recon (T1033, cmd)
  1. get-user-context → exit=0 (has output)
  2. get-domain-membership → exit=0 (has output)
  3. find-logged-on-users → exit=1 (has output)
  4. find-security-software → exit=0 (has output)
[FAIL] install-nmap (T1518, cmd)
  1. run-winget-install → exit=-1 ⚠ timeout
[FAIL] scan-for-live-hosts (T1018, cmd)
  1. run-nmap-ping-sweep → exit=1 ⚠ tool not found ⚠ placeholder: #{output_dir}, #{network.cidr}

ISSUES: placeholder tokens (2), tool missing (3), timeout (1)
```

### 4.4 Issue Detection (Code-Based)

The `detect_issues()` function scans the result for known problems:

| Issue | Detection | Severity |
|-------|-----------|----------|
| Placeholder tokens | Regex for `#{...}`, `<TARGET>`, `{{...}}` in `command_executed` | Critical — command was never valid |
| Tool not found | `'not recognized'` or `'command not found'` in stderr | High — dependency not handled |
| Timeout | `exit_code == -1` or `'timed out'` in stderr | High — command hung |
| Cross-ability variable leak | `$variable` in command that was set in a prior ability | Medium — empty at runtime |

These are flagged in the structural summary and surfaced to the master agent.

---

## 5. Master Agent Result Analysis

### 5.1 The Two-Step LLM Call

Gemini's constraint: structured output (`response_schema`) and tool use are mutually exclusive in a single call. The master needs to both *explore* result data (tool-like behaviour) and *emit structured output* (MemoryUpdate). Solution: two sequential calls.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Step A: Triage (unstructured, with tool descriptions)                   │
│                                                                          │
│ System: "You are the master campaign director. The plan was executed.    │
│          Below is the structural summary. You have access to these       │
│          inspection functions: read_stage_output(ability_name),          │
│          grep_results(pattern). Tell me which abilities you need to      │
│          inspect deeper and why."                                        │
│                                                                          │
│ User: <structural summary>                                               │
│                                                                          │
│ Output (free text):                                                      │
│   "I need to inspect gather-initial-host-recon (passed, likely has       │
│    user/domain info) and discover-local-network-range (passed, has IP    │
│    config). The failed abilities (nmap install, scans) have no useful    │
│    output — nmap was never available."                                   │
│                                                                          │
│ → Code parses ability names, calls read_stage_output() for each         │
│ → Fallback: if parsing fails, pass ALL passed-ability stdout            │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ Step B: Extract + Emit (structured output, no tools)                    │
│                                                                          │
│ System: "Analyse the execution results. Emit a MemoryUpdate with        │
│          CONFIRMED facts only. Failed commands → issues key."            │
│                                                                          │
│ User: <structural summary> + <selected stdout from Step A>              │
│                                                                          │
│ Output (structured MemoryUpdate):                                        │
│   facts: {                                                               │
│     "host": {"hostname": "DESKTOP-OSMOT24", "user": "nidhi",            │
│              "integrity": "medium", "domain": "WORKGROUP"},              │
│     "network": {"ip": "192.168.60.119", "cidr": "192.168.60.0/24",     │
│                 "gateway": "192.168.60.1"},                              │
│     "security": {"av": ["WinDefend", "SecurityHealthService"]},         │
│     "issues": {"nmap_unavailable": true,                                 │
│                "install_timeout": "winget install timed out",            │
│                "placeholder_tokens": ["#{output_dir}", "#{network.cidr}"],│
│                "phase_incomplete": "host sweep + service scan not done"} │
│   }                                                                      │
│   narrative: "Foothold confirmed on DESKTOP-OSMOT24 as nidhi (medium    │
│     integrity, WORKGROUP, not domain-joined). IP 192.168.60.119/24.     │
│     Defender active. nmap install via winget timed out — all network     │
│     scanning abilities failed. Discovery phase is INCOMPLETE: we have   │
│     local host recon but no live-host or service map."                   │
└──────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Memory Update Policy

The master controls when memory gets updated:

| Scenario | Memory Action |
|----------|--------------|
| Command passed (exit=0) with stdout | Extract facts → write to `memory.{category}.{key}` |
| Command failed (exit≠0) | Record under `memory.issues.{phase}` — never as a confirmed fact |
| Timeout (exit=-1) | Record under `memory.issues` with "timeout" flag |
| Placeholder token detected | Record under `memory.issues` with "invalid_command" flag |
| Prior speculative `pending.*` key confirmed | Promote to real key (remove `pending.` prefix) |
| Prior speculative `pending.*` key contradicted | Delete pending key, record failure in issues |

### 5.3 Phase Completion Decision

After analysing results, the master decides the phase status:

| Outcome | Master Decision |
|---------|----------------|
| All abilities passed, objectives met | `phase_done=true`, move to next phase |
| Partial success, critical objectives still missing | `phase_done=false`, but master may re-plan the same phase with adjusted approach (e.g., fall back to PowerShell-native discovery if nmap failed) |
| All abilities failed | `phase_done=false`, master re-plans or escalates |
| Issues detected (placeholders, leaks) | Flag in memory.issues for planner to address on retry |

---

## 6. Master Agent Tools

Plain Python functions (not LLM function-calling tools) called by the `analyse_results` graph node. The node passes their output to the master LLM as context.

### 6.1 Tool Inventory

| Function | Input | Output | Purpose |
|----------|-------|--------|---------|
| `read_structural_summary(results_dir, operation_id)` | Path + op ID | Compact pass/fail text | Quick overview for Step A triage |
| `read_stage_output(results_dir, operation_id, ability_name, stage_name?)` | Path + op ID + ability name (optional stage) | Raw stdout + stderr for that ability/stage | Selective deep inspection — master asks for what it needs |
| `grep_results(results_dir, operation_id, pattern)` | Path + op ID + regex | Matching lines with ability/stage context | Find specific data across all outputs (e.g., "192.168", "Administrator") |
| `list_operations(results_dir)` | Path | List of operation IDs | Multi-phase inspection (see results from prior phases) |

### 6.2 Why Not LLM Function-Calling Tools?

Gemini cannot combine `google_search` grounding or custom function declarations with `response_schema` (structured output) in the same API call. Since the master must emit a structured `MemoryUpdate`, the tool-use step is separated into Step A (unstructured) and the extraction into Step B (structured). The Python functions are called by orchestrator code between the two steps.

Future: if the LLM provider constraint is lifted (or if we switch to a provider that supports both), these functions can be registered as native LangGraph tools for true autonomous tool use.

---

## 7. State Model

### 7.1 SessionState (complete)

```
SessionState (TypedDict, total=False)
│
├── Run Identity
│   ├── run_id: str                         # engagement UUID
│   └── foothold: dict                      # {hostname, platform, ip_address, ...}
│
├── Routing
│   └── next_stage: str                     # skill name or DONE_SENTINEL
│
├── Progress
│   ├── completed_stages: list[str]         # skill names pushed
│   └── stage_results: list[StageResult]    # per-skill push outcomes
│
├── Memory
│   └── memory: dict                        # confirmed findings (ONLY from analyse_results)
│
├── Bookkeeping
│   ├── iteration: int
│   ├── max_iterations: int
│   └── log: list[str]
│
├── Master Router
│   ├── available_phases: list[str]
│   ├── completed_phases: list[str]         # set by analyse_results (not push)
│   ├── current_phase: str
│   ├── master_briefing: dict | None
│   ├── master_revisions_used: int
│   ├── max_master_revisions: int
│   ├── master_revision_feedback: str
│   └── master_done: bool
│
├── Planner Inner Loop
│   ├── planner_attempts: int
│   ├── max_planner_attempts: int
│   ├── planner_tool_calls: int
│   └── max_planner_tool_calls: int
│
├── Proposal Audit Log
│   └── proposal_log: list[dict]
│
├── Phase History
│   └── phase_history: list[dict]           # PhaseRecord per committed phase
│
├── Multi-Skill Per Phase
│   ├── phase_skills: list[str]
│   ├── phase_skill_index: int
│   └── phase_skills_buffer: list[dict]
│
├── Plan/Evaluate/Push Pipeline
│   ├── current_plan: dict | None
│   ├── current_plan_summary: list[dict]
│   ├── current_plan_error: str | None
│   ├── current_provider_id: str | None
│   ├── last_evaluator_verdict: dict
│   └── phase_done: bool
│
├── Evaluator/Retry
│   ├── feedback: str
│   └── evaluator_action: str
│
└── Execution Feedback (NEW)
    ├── pending_operation_id: str | None     # set by push, cleared by analyse
    ├── results_dir: str | None             # path to results directory
    └── execution_summary: str | None       # structural summary from latest phase
```

### 7.2 PhaseRecord (enhanced with execution outcomes)

```
PhaseRecord
├── phase: str
├── objective: str
├── skills_used: list[str]
├── abilities_pushed: int
├── adversary_id: str | None
├── ability_names: list[str]
├── techniques_used: list[str]
├── key_commands: list[str]
├── outcome: str                            # "committed" | "skipped" | "escalated"
├── master_revisions: int
├── planner_attempts: int
├── memory_delta_keys: list[str]
│
├── execution_outcome (NEW)
│   ├── operation_id: str | None
│   ├── abilities_passed: int
│   ├── abilities_failed: int
│   ├── issues_detected: list[str]          # placeholder tokens, timeouts, missing tools
│   └── confirmed_facts: list[str]          # keys written to memory from actual results
```

---

## 8. Speculative vs Confirmed Memory

### 8.1 The Two-Layer Memory Model

```
PUSH (after plan committed)                    ANALYSE (after backend executes)
─────────────────────────                      ────────────────────────────────
memory.pending.network.cidr = "awaiting"  →    memory.network.cidr = "192.168.60.0/24"
memory.pending.network.live_hosts = "..."  →   (DELETED — nmap failed, no hosts found)
                                               memory.issues.discovery.nmap_unavailable = true
```

**Rules:**
1. Push writes speculative facts under `pending.*` prefix
2. `analyse_results` promotes confirmed facts (remove prefix) or rejects them (delete + record in issues)
3. Master's `plan_phase()` sees both confirmed AND pending facts, but treats pending as uncertain
4. If webhook never arrives (timeout), pending facts persist — master treats them as unverified

### 8.2 Memory Lifecycle Per Phase

```
Phase starts
    │
    ├── Master picks phase → briefing references confirmed memory
    │
    ├── Planner creates abilities → plan references confirmed + pending memory
    │
    ├── Push completes → pending.* keys written (speculative)
    │
    ├── Backend executes → results arrive via webhook
    │
    ├── analyse_results → master inspects actual stdout/stderr
    │   ├── Confirmed → promote pending.* to real keys
    │   ├── Failed → delete pending.*, record in issues.*
    │   └── Partial → promote what succeeded, flag what didn't
    │
    └── Phase complete → memory contains only confirmed facts + issue log
```

---

## 9. Wait Mechanism — LangGraph Checkpoint + Interrupt

The graph uses LangGraph's native checkpoint and interrupt pattern. No threads are blocked waiting for backend results.

### 9.1 How It Works

```
Graph Thread (phase N)                  API Layer
──────────────────────                  ─────────
push node completes
    │
analyse_results node starts
    │
interrupt() called ──────────────►  Graph state saved to checkpointer
    │                               Thread exits (freed)
    │                               Engagement status: "awaiting_results"
    │
    │                               ... time passes (backend executing) ...
    │
    │                               POST /engagements/{id}/results
    │                                 save result JSON to disk
    │                                 resume graph:
    │                                   graph.invoke(Command(resume=result_data),
    │                                     config={"thread_id": engagement_id})
    │                               ◄──────────────────────────────────────
    │
analyse_results resumes
    │
reads result from interrupt value
parses + analyses
updates memory
    │
continues to master_plan
(graph checkpoints at next interrupt)
```

### 9.2 Checkpointer Setup

The compiled graph receives a checkpointer at build time:

```
build_graph(..., checkpointer) → CompiledStateGraph
    g.compile(checkpointer=checkpointer)
```

Checkpointer options:
- **Development / testing**: `MemorySaver` — in-memory, fast, lost on process restart
- **Production**: `SqliteSaver` or `PostgresSaver` — durable, survives restarts

Each engagement uses `thread_id = engagement_id` as the checkpoint key. This means:
- Multiple engagements run independently (separate checkpoint threads)
- The same engagement can be resumed after process restart (with durable checkpointer)
- Full state is preserved: memory, phase_history, pending facts, plan audit log

### 9.3 The Interrupt Pattern in analyse_results

The `analyse_results` node calls `interrupt()` which:
1. Saves the full graph state to the checkpointer
2. Raises a special exception that halts graph execution
3. Returns control to the caller (`run_orchestrator`)
4. The background thread exits — no resources consumed while waiting

When the webhook arrives:
1. Webhook endpoint saves result JSON to disk
2. Calls `graph.invoke(Command(resume=result_payload), config={"thread_id": engagement_id})`
3. LangGraph loads the checkpoint, restores full state
4. `analyse_results` node resumes from where `interrupt()` was called
5. The interrupt value (result_payload) is available to the node
6. Graph continues execution on the webhook's request thread (or a new background thread)

### 9.4 Engagement Status During Wait

A new status is introduced:

| Status | Meaning |
|--------|---------|
| `queued` | Engagement created, graph not started |
| `running` | Graph executing (planning, evaluating, pushing) |
| `awaiting_results` | **NEW.** Graph checkpointed, waiting for backend results |
| `completed` | All phases done |
| `failed` | Unrecoverable error |

The webhook endpoint only accepts results for engagements in `running` or `awaiting_results` status.

### 9.5 Timeout Handling

Since no thread is blocked, timeout is handled differently:

- A **scheduled task** (or periodic check) scans for engagements in `awaiting_results` status
  longer than `execution.result_wait_timeout` seconds
- On timeout:
  - Resume the graph with a synthetic timeout result: `Command(resume={"timeout": True})`
  - `analyse_results` node checks for timeout flag
  - Keeps `pending.*` memory keys as-is (unverified)
  - Marks phase outcome as "timeout" in PhaseRecord
  - Proceeds to master_plan — master sees the timeout and decides how to handle it

### 9.6 Resume Safety

- **Idempotent resume**: if the graph has already moved past `analyse_results` (e.g., duplicate webhook), the resume is a no-op
- **Stale results**: if a result arrives after timeout-resume already fired, it is saved to disk (for audit) but does not re-trigger the graph
- **Process restart**: with a durable checkpointer (Sqlite/Postgres), engagements in `awaiting_results` survive restarts and resume when the webhook arrives

---

## 10. Persistence Layout

```
runs/
└── {engagement_id}/
    ├── engagement.json                     # engagement record (status, request, state)
    ├── abilities/
    │   ├── {ability_id_1}.json             # pushed ability spec
    │   └── {ability_id_2}.json
    ├── adversaries/
    │   └── {adversary_id}.json             # pushed adversary spec
    └── results/                            # NEW
        ├── {operation_id_1}.json           # raw backend result (phase 1)
        └── {operation_id_2}.json           # raw backend result (phase 2)
```

Result files are the source of truth. Structural summaries are generated on-the-fly from these files (not persisted separately).

---

## 11. API Contract

### 11.1 Existing Endpoints (unchanged)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/engagements` | Create engagement, start graph |
| GET | `/engagements` | List engagements |
| GET | `/engagements/{id}` | Engagement detail + state |
| GET | `/engagements/{id}/log` | Full audit log |
| GET | `/engagements/{id}/artifacts` | Pushed abilities + adversaries |
| DELETE | `/engagements/{id}` | Remove engagement |
| GET | `/healthz` | Health check |
| GET | `/phases` | Phase catalogue |
| GET | `/skills` | Skill catalogue |
| GET | `/environments` | BAS environments |

### 11.2 New Endpoint

```
POST /engagements/{engagement_id}/results
```

**Request:**
```json
{
  "operation_id": "141be4c2-5f02-4a63-a673-fcab8ba45579",
  "operation_name": "Auto Test - Local-Observer-v1",
  "operation_status": "completed",
  "completed_at": "2026-05-20T16:51:07.049293",
  "adversary": {"adversary_id": "...", "name": "Local-Observer-v1"},
  "progress": {"total_abilities": 6, "completed_abilities": 6, "progress_percent": 100},
  "abilities": [
    {
      "ability_id": "...",
      "name": "gather-initial-host-recon",
      "mitre_technique_id": "T1033",
      "platform": "windows",
      "stages": [
        {
          "stage_name": "get-user-context",
          "executor": "cmd",
          "command_executed": "whoami /all",
          "execution_status": "passed",
          "stdout": "...",
          "stderr": "",
          "exit_code": 0,
          "timestamp": "2026-05-20T16:45:46.328847"
        }
      ]
    }
  ]
}
```

**Response (202):**
```json
{"status": "accepted", "operation_id": "141be4c2-5f02-4a63-a673-fcab8ba45579"}
```

**Error Responses:**
- 404: engagement_id not found
- 409: engagement not in "running" status
- 413: payload exceeds max_result_size_mb
- 422: invalid result JSON (pydantic validation)
- 200: result already received (idempotent, no re-processing)

---

## 12. Configuration

### 12.1 New Config Block

```yaml
# config/config.yaml
execution:
  result_wait_timeout: 600      # seconds before timeout-resume fires for awaiting engagements
  max_result_size_mb: 10        # reject webhook payloads larger than this
  checkpointer: memory          # "memory" (dev) or "sqlite" (production)
  checkpoint_db: runs/checkpoints.db  # path for sqlite checkpointer
```

### 12.2 Full Config Structure

```yaml
bas:
  base_url: ${BAS_BASE_URL:-http://pelorushub.online:31763}
  sleep_ms: 250
  timeout: 30
  dry_run: ${BAS_DRY_RUN:-false}
  environment:
    id: ${BAS_ENVIRONMENT_ID}
    name: ${BAS_ENVIRONMENT_NAME}

llm:
  provider: gemini
  model: gemini-3.5-flash
  api_key_env: GEMINI_API_KEY
  temperature: 0.2
  grounding:
    max_grounded_calls_per_run: 40
    default_depth: light

execution:                          # NEW
  result_wait_timeout: 600
  max_result_size_mb: 10

run:
  output_dir: runs
  log_level: ${LOG_LEVEL:-INFO}
```

---

## 13. Implementation Phasing

### 13.1 Dependency Graph

```
Step 1: results.py ─────────┐
                             ├──► Step 4: webhook + persistence
Step 2: master_tools.py ─────┤
                             ├──► Step 5: master.analyse_results()
Step 3: state.py changes ────┤
                             ├──► Step 6: graph wiring
Step 7: config changes ──────┘
```

Steps 1, 2, 3, 7 have no dependencies on each other — they can be built in parallel.

### 13.2 Step Details

| Step | Files | What | Depends On |
|------|-------|------|------------|
| 1 | `src/bas/results.py` (create) | Pydantic models for backend result shape. `parse_operation_result()`, `detect_issues()`, `build_structural_summary()`. Deterministic code only — no LLM. | — |
| 2 | `src/bas/tools/master_tools.py` (create) | `read_structural_summary()`, `read_stage_output()`, `grep_results()`, `list_operations()`. Plain Python, returns strings. | — |
| 3 | `src/bas/orchestrator/state.py` (modify) | Add `pending_operation_id`, `results_dir`, `execution_summary` to SessionState | — |
| 4 | `src/bas/api.py`, `src/bas/persistence.py` (modify) | Webhook endpoint, result storage methods, graph resume via checkpoint, idempotency guard | Step 1 |
| 5 | `src/bas/agents/master.py` (modify) | `analyse_results()` method, `_MASTER_ANALYSE_PROMPT`, two-step LLM call, StaticMasterRouter stub | Step 2 |
| 6 | `src/bas/orchestrator/graph.py` (modify) | `analyse_results` node with `interrupt()`, push→analyse→master_plan wiring, checkpointer in `build_graph()`, pending.* memory in push, phase completion moved to analyse | Steps 3, 4, 5 |
| 7 | `config/config.yaml`, `src/bas/config.py` (modify) | `ExecutionConfig` model, result_wait_timeout, max_result_size_mb, checkpointer type + path | — |

### 13.3 Testing Strategy

| Test | Type | Validates |
|------|------|-----------|
| Parse attached result JSON | Unit | `OperationResult` model, structural summary output, issue detection |
| `read_stage_output("gather-initial-host-recon")` | Unit | Correct stdout returned for specific ability |
| `grep_results("192.168")` | Unit | Finds IP in ipconfig stdout, returns with context |
| POST result to webhook | Integration | JSON saved, idempotent on re-POST, Event signalled |
| `master.analyse_results()` | Integration | Memory has confirmed facts only, issues logged |
| Full flow: push → webhook → analyse → next phase | E2E | Master recognises nmap failed, adjusts next plan |
| Timeout: no webhook in 600s | Edge | Graph continues, pending facts preserved, warning logged |
| Duplicate POST (same operation_id) | Edge | 200 OK returned, no re-processing |

---

## 14. Design Decisions & Rationale

| Decision | Rationale | Alternative Considered |
|----------|-----------|----------------------|
| Raw JSON is source of truth; summaries generated on-the-fly | Avoid redundant storage and format-sync burden | Persisted .txt log (rejected: redundant) |
| LLM extracts semantic findings from stdout | More robust than regex parsing for diverse command outputs | Code-based extraction (rejected: brittle, needs per-command parsers) |
| Code handles structural facts only (exit codes, pass/fail, placeholders) | These are deterministic and don't need LLM reasoning | All-LLM parsing (rejected: wastes tokens on obvious things) |
| Two-step LLM call for analysis | Works around Gemini structured-output + tool constraint | Single call with tools (rejected: provider constraint) |
| Speculative memory under `pending.*` keys | Fallback if webhook never arrives; master can still reason | No speculative memory (rejected: graph hangs with empty memory on timeout) |
| LangGraph checkpoint + interrupt | No threads blocked; survives process restarts with durable checkpointer; native LangGraph pattern | threading.Event (rejected: wastes a thread per engagement during backend execution) |
| Idempotent webhook by operation_id | Prevents double-processing on backend retry | No guard (rejected: duplicate memory entries possible) |
| Phase completion moved from push to analyse | Phase is only "done" when actual outcomes confirm it | Keep in push (rejected: marks phase done before knowing if commands worked) |
| Master decides memory updates (not code) | Master LLM is better at judging what's significant in stdout | Hardcoded extraction rules (rejected: can't generalise across phases) |
