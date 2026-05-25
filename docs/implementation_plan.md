# Implementation Plan — Closed-Loop Execution Engine

> Complete phase-by-phase engineering plan.  
> Following every phase in order produces a fully autonomous attack-simulation engine.

---

## Revision Log

**v2 (2026-05-24) — 9 issues fixed before implementation:**

| # | Severity | Issue | Fix |
|---|----------|-------|-----|
| 1 | **Critical** | `push → analyse_results` unconditional edge breaks multi-skill phases | Added `_after_push()` conditional edge: intermediate skills skip analysis |
| 2 | **Critical** | `_run_engagement()` marks interrupted graphs as "completed" | Catch `GraphInterrupt`, set `awaiting_results` instead |
| 3 | **Critical** | `BasClient` closed after first thread exits; graph resume uses stale client | Made `BasClient` process-global in `_state` |
| 4 | **Critical** | Webhook runs `graph.invoke()` synchronously — backend caller may timeout | Moved graph resume to `BackgroundTasks` |
| 5 | **Critical** | Phase 1 sets `awaiting_results` on RECEIVING results (backwards) | Removed status change from receiver; graph nodes own transitions |
| 6 | **Significant** | `is_retry = asset_map is not None` triggers on completed phases too | Added `phase not in completed_phases AND retry_same_phase` guards |
| 7 | **Significant** | Phase 7 reads `retry_same_phase` but Phase 11 adds it (dependency cycle) | Moved `retry_same_phase` + `issues_to_fix` to Phase 4 state extensions |
| 8 | **Significant** | Operation create/start in `push_specialist()` violates separation of concerns | Moved to `push_node` (graph.py) — specialist stays pure BAS-push |
| 9 | **Significant** | `RunStatus` type alias missing `awaiting_results` | Added to Phase 3 (persistence.py) |

Each fix is marked inline with `> **Design note (Issue #N fix):**` at the relevant section.

---

## Ground Truth Before We Start

### What already exists (do not recreate)

| File | What it does |
|------|--------------|
| `src/bas/api.py` | FastAPI server. Engagements CRUD. No results receiver yet. |
| `src/bas/orchestrator/graph.py` | LangGraph graph: init → master_plan → plan → evaluate → push → master_plan. No analyse_results node. No checkpointer. |
| `src/bas/orchestrator/state.py` | `SessionState` TypedDict + `PhaseRecord` + `StageResult`. No execution feedback fields yet. |
| `src/bas/agents/master.py` | `LLMMasterRouter`: `plan_phase()`, `review_plan()`, `update_memory()`. No `analyse_results()` method. |
| `src/bas/agents/specialist.py` | `plan_specialist()` + `push_specialist()`. Push always creates new IDs (POST-only). |
| `src/bas/agents/evaluator.py` | `LLMEvaluator` emits `EvaluatorVerdict`. No backend result awareness. |
| `src/bas/client/abilities.py` | `AbilitiesApi.create()`, `create_stage()`. No update methods. |
| `src/bas/client/adversaries.py` | `AdversariesApi.create()`, `link_ability()`. No update/feedback methods. |
| `src/bas/client/facade.py` | `BasClient` flat façade. No feedback path. |
| `src/bas/persistence.py` | `RunStore` (engagement JSON files) + `ArtifactStore` (abilities/adversaries subdirs). No results subdir. |
| `src/bas/config.py` | `AppConfig`: bas, llm, run sections. No execution section. |

### What the BAS backend OpenAPI gives us

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `POST /abilities` | Create | First push for any ability |
| `POST /abilities/{id}/stages` | Create | First push for any stage |
| `POST /adversaries` | Create | First push for any adversary |
| `POST /adversaries/{adv}/abilities/{ab}` | Link | Attach ability to adversary |
| **`POST /ai/operation-feedback`** | **Feedback** | **Send AI-generated command corrections to existing stage IDs** |
| `POST /operations` | Create | Create an operation tied to adversary + env + agents |
| `POST /operations/{id}/start` | Control | Start the operation |
| `GET /operations/{id}` | Read | Poll operation status |
| `GET /agent/result` (internal) | Read | Agent submits results to backend (not our concern) |

### The feedback contract (`POST /ai/operation-feedback`)

```json
{
  "source": "operation-analyzer",
  "loop_status": "continue",
  "changes": [
    {
      "operation_id": "<uuid>",
      "ability_id": "<uuid>",
      "stage_id": "<uuid>",
      "suggested_command_template": "updated command here",
      "reason": "nmap not available — use PowerShell fallback",
      "confidence": 0.92,
      "apply_immediately": true
    }
  ]
}
```

- `ability_id` and `stage_id` must be IDs we already POSTed.  
- `apply_immediately: true` tells the backend to rewrite the stage before the next run.  
- No new abilities or adversaries are created — the same operation/adversary/ability tree is reused.

### Key design invariant

> **First time a phase runs: POST to create all IDs.  
> Same phase retry: POST /ai/operation-feedback to patch only the failed stages.**  
> Never create new ability or adversary IDs for a retry within the same phase.

---

## System Flow (what we are building)

```
API caller
  │
  ▼
POST /engagements
  │
  ▼
init → master_plan (PICK) → plan → evaluate → master_plan (REVIEW)
                                                     │
                                               commit → push
                                                     │
                                         [first time for phase]
                                         POST /abilities × N
                                         POST /adversaries
                                         POST /adversaries/{adv}/abilities/{ab} × N
                                         POST /operations
                                         POST /operations/{id}/start
                                         Store: phase_asset_map[phase] = {adversary_id, ability_ids, stage_ids, operation_id}
                                                     │
                                         interrupt() → checkpoint saved → thread freed
                                         engagement status = awaiting_results
                                                     │
                              ◄──── Backend auto-executes ────────────────────────────
                              ◄──── Backend POSTs results to POST /engagements/{id}/results
                                                     │
                                         Webhook saves raw JSON to disk
                                         graph.invoke(Command(resume=result), config={thread_id})
                                                     │
                                         analyse_results node resumes
                                         Step A: structural summary + issue triage (LLM)
                                         Step B: structured MemoryUpdate (LLM)
                                         Promote pending.* → confirmed memory
                                                     │
                              ┌─────────────────────────────────────────┐
                              │ Master decides: phase done OR retry     │
                              └─────────────────────────────────────────┘
                                                     │
                              [retry same phase]     │     [phase done]
                                      │              │           │
                              master_plan (PICK)     │     master_plan (PICK next)
                              plan → evaluate →      │
                              master_plan (REVIEW) → │
                              push:                  │
                                POST /ai/operation-feedback
                                (only for failed stages,
                                 same ability_id + stage_id)
                              interrupt() → checkpoint
                                      │
                                  [loop back to await_results]
```

---

## Phase 1 — Result Receiver

**Single responsibility:** Accept execution results POSTed by the backend and store them safely.

### Files touched
- `src/bas/api.py` — add one endpoint
- `src/bas/persistence.py` — add `ResultStore` and `save_result()`

### What to build

**`ResultStore` in `persistence.py`**

```
ResultStore
├── __init__(runs_dir: str | Path)
├── save(engagement_id, operation_id, raw_payload: dict) -> Path
│     atomic write to runs/{engagement_id}/results/{operation_id}.json
│     idempotent: if file already exists, return path without overwriting
├── get(engagement_id, operation_id) -> dict | None
├── list_ids(engagement_id) -> list[str]   # all operation_ids received
└── exists(engagement_id, operation_id) -> bool
```

**New endpoint in `api.py`**

```
POST /engagements/{engagement_id}/results
Headers: Content-Type: application/json
Body: raw operation result JSON from backend
```

Behaviour (in order):
1. Look up engagement by `engagement_id` — 404 if not found.
2. Verify engagement status is `awaiting_results` — 409 if not (prevents stale/premature deliveries).
3. Extract `operation_id` from the body — 422 if missing or not a UUID.
4. Check payload size — 413 if body exceeds `execution.max_result_size_mb` (default 10 MB).
5. If `result_store.exists(engagement_id, operation_id)` → return 200 `{"status": "already_received"}` (idempotent, do nothing else).
6. Validate body is valid JSON — 422 if malformed.
7. `result_store.save(engagement_id, operation_id, body)`.
8. **Do NOT change engagement status here.** Status transitions are owned by graph nodes (interrupt sets `awaiting_results`, resume sets `running`). The webhook only saves data.
9. Return 202 `{"status": "accepted", "operation_id": operation_id}`.

**No graph resume yet** — that is Phase 3. Phase 1 only stores.

> **Design note (Issue #5 fix):** `awaiting_results` means "graph is paused, waiting for backend." It is set by the graph node at `interrupt()` time, NOT by the webhook. When the webhook fires, it saves the file and triggers graph resume (Phase 3) — the graph node then sets status back to `running`.

### Acceptance criteria
- POST same `operation_id` twice → second returns 200, file written once.
- POST with body > 10 MB → 413.
- POST with missing `operation_id` → 422.
- POST to unknown engagement → 404.
- POST to engagement not in `awaiting_results` → 409.
- Engagement status is NOT changed by the receiver (graph owns transitions).

---

## Phase 2 — Result Parser and Structural Summarizer

**Single responsibility:** Parse raw backend result JSON into typed models and produce a compact structural summary that the LLM can consume efficiently.

### Files touched
- `src/bas/results.py` — **create**

### What to build

**Pydantic models:**

```python
class StageExecution(BaseModel):
    stage_name: str
    executor: str
    command_executed: str
    execution_status: Literal["passed", "failed"]
    stdout: str
    stderr: str
    exit_code: int
    timestamp: datetime

class AbilityResult(BaseModel):
    ability_id: str
    name: str
    mitre_technique_id: str | None
    platform: str
    stages: list[StageExecution]

    @property
    def passed(self) -> bool: ...    # all stages exit_code == 0
    @property
    def failed(self) -> bool: ...    # any stage exit_code != 0

class OperationResult(BaseModel):
    operation_id: str
    operation_name: str
    operation_status: Literal["completed", "failed", "partial"]
    completed_at: datetime
    adversary: dict
    progress: dict
    abilities: list[AbilityResult]
```

**Issue types (code-based, no LLM):**

```python
class IssueKind(str, Enum):
    PLACEHOLDER_TOKEN = "placeholder_token"   # #{...}, <TARGET>, {{...}} in command_executed
    TOOL_NOT_FOUND    = "tool_not_found"       # 'not recognized' or 'command not found' in stderr
    TIMEOUT           = "timeout"              # exit_code == -1 or 'timed out' in stderr
    CROSS_VAR_LEAK    = "cross_var_leak"       # $variable in command likely empty at runtime

class StageIssue(BaseModel):
    ability_id: str
    ability_name: str
    stage_id: str          # from ability's original stage_id map (passed via context)
    stage_name: str
    kind: IssueKind
    detail: str
```

**Functions:**

```python
def parse_operation_result(raw: dict) -> OperationResult:
    """Validate and parse raw backend JSON into typed model."""

def detect_issues(result: OperationResult, stage_id_map: dict[str, str]) -> list[StageIssue]:
    """
    Scan all stages for known issue patterns.
    stage_id_map: {ability_id -> {stage_name -> stage_id}} — needed so
    each StageIssue carries the real stage_id for feedback construction.
    """

def build_structural_summary(result: OperationResult, issues: list[StageIssue]) -> str:
    """
    Return compact text, e.g.:
    PHASE: discovery | OP: 141be4c2 | STATUS: completed | 2/6 passed

    [PASS] gather-initial-host-recon (T1033, cmd)
      1. get-user-context → exit=0
      2. find-logged-on-users → exit=1
    [FAIL] install-nmap (T1518, cmd)
      1. run-winget-install → exit=-1 ⚠ timeout
    [FAIL] scan-for-live-hosts (T1018, cmd)
      1. run-nmap-ping-sweep → exit=1 ⚠ tool_not_found ⚠ placeholder: #{network.cidr}

    ISSUES: placeholder_token(2), tool_not_found(3), timeout(1)
    """
```

### What the master gets vs what it does NOT get

The master never receives full stdout in the structural summary. It requests specific stdout via `read_stage_output()` in Phase 5. This is the context budget control.

### Acceptance criteria
- `parse_operation_result()` validates the sample result JSON attached in session history.
- `detect_issues()` catches all four issue kinds from synthetic test cases.
- `build_structural_summary()` output is deterministic (same input → same text).

---

## Phase 3 — Checkpoint and Resume Wiring

**Single responsibility:** Make the graph pause after push and resume from the webhook without blocking any thread.

### Files touched
- `src/bas/orchestrator/graph.py` — add checkpointer, add `analyse_results` stub node
- `src/bas/config.py` — add `ExecutionConfig` section
- `config/config.yaml` — add execution block
- `src/bas/api.py` — wire graph resume in the results receiver endpoint

### What to build

**`ExecutionConfig` in `config.py`:**

```python
class ExecutionConfig(StrictModel):
    result_wait_timeout: int = 600       # seconds before timeout-resume fires
    max_result_size_mb: int = 10
    checkpointer: Literal["memory", "sqlite"] = "memory"
    checkpoint_db: str = "runs/checkpoints.db"

class AppConfig(StrictModel):
    bas: BasConfig = ...
    llm: LlmConfig = ...
    run: RunConfig = ...
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)  # NEW
```

**`build_graph()` in `graph.py` — add checkpointer parameter:**

```python
def build_graph(
    *,
    master, skill_tool, planner, bas, artifacts, evaluator,
    checkpointer=None,     # NEW
):
    ...
    return g.compile(checkpointer=checkpointer)
```

**Stub `analyse_results` node:**

```python
def _analyse_results_node(state: SessionState) -> dict[str, Any]:
    """Placeholder — Phase 4 fills this in.
    For now: just resumes from interrupt and passes through to master_plan."""
    result_data = interrupt("awaiting_results")   # graph pauses here
    # Phase 4 will process result_data; for now just log it
    log = list(state.get("log") or [])
    log.append(f"[analyse_results] received result for op={result_data.get('operation_id')}")
    return {"log": log}
```

**Edge change in `build_graph()`:**

```
Before: push → master_plan (unconditional)
After:  push → [conditional]
          ├── intermediate skill (more skills in phase) → master_plan
          └── last skill in phase (phase complete) → analyse_results → master_plan
```

> **Design note (Issue #1 fix):** The unconditional `push → analyse_results` edge
> would break multi-skill phases. Push already handles multi-skill routing internally
> (returning `next_stage` for the next skill). Only the LAST skill in a phase should
> trigger `analyse_results`. Add `_after_push(state)` conditional edge:
> ```python
> def _after_push(state: SessionState) -> str:
>     # If push set next_stage to a new skill (multi-skill loop), skip analysis
>     phase_skills = state.get("phase_skills") or []
>     skill_idx = state.get("phase_skill_index", 0)
>     if skill_idx + 1 < len(phase_skills):
>         return "master_plan"   # intermediate skill → plan the next one
>     return "analyse_results"   # last skill → wait for backend results
> ```
> Wire it:
> ```python
> g.add_conditional_edges(
>     "push",
>     _after_push,
>     {"analyse_results": "analyse_results", "master_plan": "master_plan"},
> )
> g.add_edge("analyse_results", "master_plan")
> ```

**Graph resume in `api.py` results receiver (extend Phase 1 endpoint):**

After saving the result file, kick off a **background task** (not synchronous):
```python
background_tasks.add_task(
    _resume_graph, engagement_id, raw_payload
)
```

```python
def _resume_graph(engagement_id: str, result_payload: dict) -> None:
    """Resume the paused graph in background. Must not block the webhook response."""
    store = _state["store"]
    record = store.get(engagement_id)
    if not record:
        return
    record["status"] = "running"
    record.pop("awaiting_since", None)
    store.save(record)
    try:
        _state["compiled_graph"].invoke(
            Command(resume=result_payload),
            config={"configurable": {"thread_id": engagement_id}},
        )
    except GraphInterrupt:
        # Graph paused again (next phase push) — status set by graph node
        pass
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["finished_at"] = now_iso()
        store.save(record)
```

> **Design note (Issue #4 fix):** Running `graph.invoke()` synchronously inside the
> webhook handler would block the HTTP response for the duration of LLM calls in
> `analyse_results`. The BAS backend caller might timeout. Using `BackgroundTasks`
> returns 202 immediately while the graph resumes on a worker thread.

The compiled graph must be a module-level singleton or stored in `_state` bootstrap dict — the same instance built with the checkpointer.

**`run_orchestrator()` — pass checkpointer:**

Build checkpointer from config:
```python
if cfg.execution.checkpointer == "sqlite":
    from langgraph.checkpoint.sqlite import SqliteSaver
    checkpointer = SqliteSaver.from_conn_string(cfg.execution.checkpoint_db)
else:
    from langgraph.checkpoint.memory import MemorySaver
    checkpointer = MemorySaver()
```

Pass `thread_id=engagement_id` in the config dict when calling `app.stream()`.

**Update `RunStatus` type alias in `persistence.py`:**

```python
RunStatus = Literal["queued", "running", "awaiting_results", "completed", "failed"]
```

> **Design note (Issue #9 fix):** The `RunStatus` type alias must include
> `awaiting_results` from Phase 3 onward. Otherwise `record["status"] = "awaiting_results"`
> would silently violate the type contract.

### Acceptance criteria
- Push completes → engagement status transitions to `awaiting_results`.
- POST to `/results` endpoint → engagement resumes (in background task) → status returns to `running` → transitions to `completed` or next phase.
- No thread is sleeping/blocked between push and webhook arrival.

### Critical fixes required in existing code (Phase 3)

**Fix `_run_engagement()` in `api.py` — handle `GraphInterrupt` (Issue #2):**

Currently `_run_engagement()` calls `run_orchestrator()` and its `finally` block
sets `record["status"] = "completed"`. But when `interrupt()` fires, `run_orchestrator()`
returns normally (the stream ends) — the engagement is NOT complete, just paused.

```python
def _run_engagement(engagement_id: str) -> None:
    ...
    try:
        from langgraph.errors import GraphInterrupt
        try:
            state = run_orchestrator(...)
        except GraphInterrupt:
            # Graph paused at interrupt() — engagement is waiting for results
            record["status"] = "awaiting_results"
            record["awaiting_since"] = now_iso()
            store.save(record)
            return   # thread exits, graph resumes via webhook
        
        record["state"] = _serialise_state(state)
        record["status"] = "completed"
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = ...
    finally:
        if record["status"] not in ("awaiting_results",):
            record["finished_at"] = now_iso()
        store.save(record)
```

> Without this fix, every engagement that reaches `interrupt()` is immediately
> marked `completed` with `finished_at` set — making it impossible to resume.

**Make `BasClient` process-global (Issue #3):**

Currently `_run_engagement()` creates a `BasClient` per engagement and calls
`bas.close()` in its `finally` block. When the graph resumes via webhook, the
`bas` reference inside the compiled graph is stale (HTTP client closed).

Fix: Move `BasClient` to `_state` (lazy singleton). The graph captures a
reference to the long-lived client. Never close it until process shutdown.

```python
_state: dict[str, Any] = {
    "cfg": None, "skills": None, "store": None, "artifacts": None,
    "bas": None,   # NEW — process-global BasClient
}

def _bootstrap():
    ...
    if _state["bas"] is None:
        _state["bas"] = BasClient.from_config(cfg.bas)
    ...
```

Remove per-engagement `bas = BasClient.from_config(...)` and `bas.close()`
from `_run_engagement()`. Use `_state["bas"]` instead.

---

## Phase 4 — analyse_results Node (Memory Promotion)

**Single responsibility:** Turn raw execution outcomes into confirmed memory updates and drive the phase-done vs retry decision.

### Files touched
- `src/bas/agents/master.py` — add `analyse_results()` method + `_MASTER_ANALYSE_PROMPT`
- `src/bas/tools/master_tools.py` — **create** (plain Python, no LLM)
- `src/bas/orchestrator/state.py` — add execution feedback fields
- `src/bas/orchestrator/graph.py` — replace stub analyse node with full implementation

### What to build

**New `SessionState` fields (state.py):**

```python
# Execution Feedback (added in Phase 4)
pending_operation_id: str | None      # set by push, cleared by analyse
results_dir: str | None               # path to runs/{engagement_id}/results/
execution_summary: str | None         # structural summary from latest phase
phase_asset_map: dict[str, Any]       # {phase -> {adversary_id, ability_ids, stage_id_map, operation_id}}
                                      # stage_id_map: {ability_name -> {stage_name -> stage_id}}
retry_same_phase: bool                # set by master PICK when retrying a failed phase
issues_to_fix: list[str]              # specific issue descriptions for the planner on retry
```

> **Design note (Issue #7 fix):** `retry_same_phase` and `issues_to_fix` are added
> here in Phase 4 (not Phase 11) because Phase 7 (feedback loop) reads them to
> decide POST vs feedback path. The original plan had a dependency cycle.

`phase_asset_map` is the key that enables Phase 7 (feedback). Push writes into it; analyse_results reads from it.

**`master_tools.py`** — plain Python, no LLM:

```python
def read_structural_summary(results_dir: str, operation_id: str) -> str:
    """Load raw result JSON and return structural summary text."""

def read_stage_output(results_dir: str, operation_id: str, ability_name: str, stage_name: str | None = None) -> str:
    """Return stdout + stderr for a specific ability/stage. Returns all stages if stage_name is None."""

def grep_results(results_dir: str, operation_id: str, pattern: str) -> str:
    """Regex search across all stdout/stderr. Returns matching lines with ability/stage context."""

def list_operations(results_dir: str) -> list[str]:
    """Return all operation_ids available in this engagement's results dir."""
```

**`master.analyse_results()` — two-step LLM call:**

```
Step A (unstructured, grounding=skip):
  System: _MASTER_ANALYSE_TRIAGE_PROMPT
  User: structural_summary
  Output: free text naming which abilities need deeper inspection.
  Code after Step A: parse ability names from free text,
                     call read_stage_output() for each named ability.
  Fallback: if parsing yields nothing, call read_stage_output() for ALL passed abilities.

Step B (structured output, grounding=skip):
  System: _MASTER_ANALYSE_EXTRACT_PROMPT
  User: structural_summary + selected stdout from Step A
  Output: MemoryUpdate (facts + narrative)
```

**`_analyse_results_node` (full, replaces stub from Phase 3):**

```python
def _analyse_results_node(state, master, skill_tool, results_store):
    result_data = interrupt("awaiting_results")   # resumes here with webhook payload

    # 1. Parse + issue detection
    op_result = parse_operation_result(result_data)
    phase = state.get("current_phase") or ""
    asset_map = (state.get("phase_asset_map") or {}).get(phase, {})
    stage_id_map = asset_map.get("stage_id_map", {})
    issues = detect_issues(op_result, stage_id_map)
    summary = build_structural_summary(op_result, issues)

    # 2. LLM analysis
    mem_update = master.analyse_results(
        results_dir=state.get("results_dir"),
        operation_id=op_result.operation_id,
        structural_summary=summary,
        current_memory=state.get("memory", {}),
    )

    # 3. Promote pending.* → confirmed facts
    new_memory = dict(state.get("memory") or {})
    pending = {k: v for k, v in new_memory.items() if k.startswith("pending.")}
    for pk in pending:
        del new_memory[pk]
    for k, v in (mem_update.facts or {}).items():
        new_memory[k] = v
    if mem_update.narrative:
        narratives = list(new_memory.get("narratives") or [])
        narratives.append({"phase": phase, "ts": _now(), "text": mem_update.narrative})
        new_memory["narratives"] = narratives

    # 4. Phase done decision — embed in MemoryUpdate or derive from issues
    phase_done = _derive_phase_done(op_result, issues, mem_update)

    # 5. Update phase_history entry with execution_outcome
    history = list(state.get("phase_history") or [])
    if history:
        last = dict(history[-1])
        last["execution_outcome"] = {
            "operation_id": op_result.operation_id,
            "abilities_passed": sum(1 for a in op_result.abilities if a.passed),
            "abilities_failed": sum(1 for a in op_result.abilities if a.failed),
            "issues_detected": [i.kind.value for i in issues],
        }
        history[-1] = last

    completed_phases = list(state.get("completed_phases") or [])
    if phase_done and phase and phase not in completed_phases:
        completed_phases.append(phase)

    return {
        "memory": new_memory,
        "phase_history": history,
        "completed_phases": completed_phases if phase_done else state.get("completed_phases"),
        "execution_summary": summary,
        "pending_operation_id": None,
        "log": list(state.get("log") or []) + [
            f"[analyse_results] op={op_result.operation_id} "
            f"passed={sum(1 for a in op_result.abilities if a.passed)}/"
            f"{len(op_result.abilities)} phase_done={phase_done}"
        ],
    }
```

**Memory promotion rules:**

| Scenario | Action |
|----------|--------|
| exit_code == 0, stdout has data | Extract facts → `memory.{category}.{key}` |
| exit_code != 0 | Record under `memory.issues.{phase}` only |
| exit_code == -1 (timeout) | `memory.issues.{phase}.timeout` |
| Placeholder token in command_executed | `memory.issues.{phase}.placeholder` |
| Prior `pending.X` confirmed by output | Delete `pending.X`, write `memory.X` |
| Prior `pending.X` contradicted | Delete `pending.X`, write `memory.issues.X` |

### Acceptance criteria
- A phase with 2 passed / 4 failed abilities → only 2 abilities' facts in memory.
- Placeholder token in failed stage → `memory.issues` entry, not `memory` fact.
- `phase_done=True` only when critical objectives are met (confirmed by output).
- `phase_history[-1].execution_outcome` is populated after analyse_results runs.

---

## Phase 5 — Push Node: Record Phase Asset Map

**Single responsibility:** After creating new IDs on first push for a phase, store them in `phase_asset_map` so subsequent phases know what to update vs create.

### Files touched
- `src/bas/orchestrator/graph.py` — `_make_push_node()`, add asset map tracking
- `src/bas/agents/specialist.py` — extend `PushResult` with `stage_id_map`

### What to build

**Extend `PushResult`:**

```python
class PushResult(BaseModel):
    ...
    stage_id_map: dict[str, dict[str, str]] = Field(default_factory=dict)
    # {ability_name -> {stage_name -> stage_id}}
```

In `push_specialist()`, after creating each stage, populate `stage_id_map`:
```python
stage_id_map.setdefault(gen.ability.name, {})[stage.stage_name] = str(st_resp.stage_id)
```

**In `push_node` — write `phase_asset_map` for the current phase:**

```python
# After all skills in phase complete (when phase_done consolidation fires):
phase_asset_map = dict(state.get("phase_asset_map") or {})
phase_asset_map[phase] = {
    "adversary_id": push.adversary_id,
    "ability_ids": push.ability_ids,
    "stage_id_map": push.stage_id_map,   # {ability_name -> {stage_name -> stage_id}}
    "ability_name_to_id": {
        ab.get("name"): push.ability_ids[i]
        for i, ab in enumerate(push.plan_summary)
    },
    "operation_id": None,   # set after operation is created (Phase 6)
}
```

**Write `pending.*` memory keys at push time:**

```python
# After memory update in push_node:
for gen in plan.abilities:
    new_memory[f"pending.{phase}.{gen.ability.name}"] = "awaiting"
new_memory["pending.operation_id"] = adversary_id   # to track which op we expect
```

**Set `pending_operation_id` and `results_dir`:**

```python
return {
    ...
    "pending_operation_id": adversary_id,   # or real operation_id once Phase 6 adds it
    "results_dir": str(artifacts.root / (state.get("run_id") or "") / "results"),
    "phase_asset_map": phase_asset_map,
    ...
}
```

### Acceptance criteria
- After push, `state["phase_asset_map"]["discovery"]` contains adversary_id, all ability_ids, and `stage_id_map`.
- `pending.*` keys exist in memory after push.
- `results_dir` points to correct path on disk.

---

## Phase 6 — Operation Lifecycle

**Single responsibility:** Create and start an operation on the backend after push; store operation_id in `phase_asset_map`.

### Files touched
- `src/bas/client/facade.py` — add `create_operation()` and `start_operation()`
- `src/bas/client/operations.py` — **create** (new resource client)
- `src/bas/orchestrator/graph.py` — call operation lifecycle in push_node after ability/adversary creation

### What to build

**`OperationsApi` in `client/operations.py`:**

```python
class OperationsApi:
    def create(self, operation: OperationCreate) -> OperationResponse
    def start(self, operation_id: str | UUID) -> None
    def get(self, operation_id: str | UUID) -> OperationResponse
    def stop(self, operation_id: str | UUID) -> None
```

Backend's `OperationCreate` requires:
- `name: str`
- `adversary_id: uuid`
- `environment_id: uuid` (from foothold)
- `windows_agent_id / linux_agent_id / mac_agent_id` (from foothold, by platform)

**Wire into `BasClient` facade:**

```python
# In facade.py __init__:
self.operations = OperationsApi(self._transport, dry_run=dry_run)
```

**In `push_specialist()` — after successful link step:**

Do NOT create operations in `push_specialist()`. Operations are an orchestration
concern, not a BAS-push concern. `push_specialist()` only creates abilities,
adversaries, and links.

> **Design note (Issue #8 fix):** Operation lifecycle (create + start) belongs in
> `push_node` (graph.py), not `push_specialist()` (specialist.py). The specialist
> has no natural access to foothold context (environment_id, agent_id). Moving it
> to push_node keeps specialist.py as a pure abilities/adversary push helper.

**In `push_node` (graph.py) — after `push_specialist()` returns successfully:**

```python
# Create + start operation (only for last skill in multi-skill phase)
if not is_retry:
    op_resp = bas.operations.create(OperationCreate(
        name=f"AI - {plan.adversary.name}",
        adversary_id=push.adversary_id,
        environment_id=foothold.get("environment_id"),
        windows_agent_id=foothold.get("agent_id") if foothold.get("platform") == "windows" else None,
        linux_agent_id=foothold.get("agent_id") if foothold.get("platform") == "linux" else None,
        mac_agent_id=foothold.get("agent_id") if foothold.get("platform") == "mac" else None,
    ))
    operation_id = str(op_resp.operation_id)
    bas.operations.start(operation_id)
else:
    operation_id = asset_map["operation_id"]  # reuse existing
```

**In `push_node` — store `operation_id` into `phase_asset_map`:**

```python
if push.operation_id:
    phase_asset_map[phase]["operation_id"] = push.operation_id
```

**In `analyse_results` node — match incoming result to operation_id:**

```python
expected_op_id = (state.get("phase_asset_map") or {}).get(phase, {}).get("operation_id")
received_op_id = result_data.get("operation_id")
if expected_op_id and received_op_id != expected_op_id:
    log.append(f"[analyse_results] WARNING: expected op={expected_op_id} got op={received_op_id}")
```

### Acceptance criteria
- After push, an operation is created and started on the backend.
- `phase_asset_map[phase]["operation_id"]` is populated.
- Webhook result is matched to the correct engagement via `operation_id`.

---

## Phase 7 — AI Feedback Loop (POST /ai/operation-feedback)

**Single responsibility:** When master decides to retry a phase, send command corrections to the backend using existing stage IDs instead of creating new abilities.

### Files touched
- `src/bas/client/feedback.py` — **create**
- `src/bas/client/facade.py` — add `send_ai_feedback()`
- `src/bas/agents/master.py` — add `build_feedback_payload()` method
- `src/bas/orchestrator/graph.py` — `_make_push_node()` retry path

### What to build

**`FeedbackApi` in `client/feedback.py`:**

```python
class AIStageChange(BaseModel):
    operation_id: str
    ability_id: str
    stage_id: str
    suggested_command_template: str
    reason: str
    confidence: float = 0.9
    apply_immediately: bool = True

class FeedbackApi:
    def send(
        self,
        operation_id: str,
        changes: list[AIStageChange],
        *,
        loop_status: str = "continue",
    ) -> None:
        """POST /ai/operation-feedback"""
```

**`master.build_feedback_payload()` — called in push_node for retry:**

```python
def build_feedback_payload(
    self,
    *,
    issues: list[StageIssue],
    current_plan: SpecialistPlan,
    asset_map: dict,               # {ability_name -> {stage_name -> stage_id}, operation_id}
) -> list[AIStageChange]:
    """
    For each issue, generate a corrected command_template.
    Only includes stages that changed (diff by stage_name + ability_name).
    Returns list of AIStageChange targeting the ORIGINAL stage IDs.
    """
```

**Push_node retry path (replaces create-everything for retry):**

```python
def _push_node(state):
    phase = state.get("current_phase")
    asset_map = (state.get("phase_asset_map") or {}).get(phase)
    completed_phases = state.get("completed_phases") or []
    is_retry = (
        asset_map is not None
        and phase not in completed_phases
        and state.get("retry_same_phase", False)
    )
```

> **Design note (Issue #6 fix):** The original `is_retry = asset_map is not None`
> would incorrectly trigger the feedback path for completed phases that the master
> re-picks. The fix requires all three conditions: asset_map exists, phase not yet
> completed, AND master explicitly requested retry via `retry_same_phase`.

    if is_retry:
        # Build feedback from current plan vs prior issues
        issues = _extract_prior_issues(state)
        changes = master.build_feedback_payload(
            issues=issues,
            current_plan=plan,
            asset_map=asset_map,
        )
        if changes:
            bas.feedback.send(
                operation_id=asset_map["operation_id"],
                changes=changes,
            )
            # Restart the same operation
            bas.operations.start(asset_map["operation_id"])
        else:
            logger.info("[push] retry: no changes to send; re-running same operation")
            bas.operations.start(asset_map["operation_id"])
        # Do NOT create new abilities/adversaries
    else:
        # First time — existing create path (POST everything)
        push = push_specialist(...)
        # update phase_asset_map
```

### Decision tree for retry vs new phase

```
analyse_results completes
         │
         ├── phase_done=True → master_plan (PICK next phase)
         │
         └── phase_done=False
                  │
                  ├── issues are fixable (placeholder, tool_not_found, timeout)
                  │   → master_plan (PICK same phase with retry context)
                  │   → plan → evaluate → push (RETRY path: feedback only)
                  │
                  └── issues are not fixable (wrong platform, wrong technique)
                      → master_plan (PICK different phase or escalate)
```

### Acceptance criteria
- On retry: no new ability or adversary IDs are created.
- `POST /ai/operation-feedback` is called with changes targeting original `stage_id` values.
- Re-run of the same operation starts after feedback is sent.
- `phase_asset_map` is not overwritten on retry — only `operation_id` updated if a new operation is forced.

---

## Phase 8 — Master Analyse Prompts and Context Budget

**Single responsibility:** Tune the LLM prompts for `analyse_results()` to give the master the minimum necessary context for a correct retry vs proceed decision.

### Files touched
- `src/bas/agents/master.py` — prompts `_MASTER_ANALYSE_TRIAGE_PROMPT` + `_MASTER_ANALYSE_EXTRACT_PROMPT`

### What to build

**`_MASTER_ANALYSE_TRIAGE_PROMPT`** (Step A):

```
ROLE
  You are the master campaign director reviewing execution results.

TASK
  You are given a structural summary of an operation.
  Name which abilities (by exact name) you need to inspect deeper and WHY.
  Focus on: passed abilities (for confirmed facts), and any failed ability
  whose stderr might reveal how to fix the issue.
  
RULES
  * Only name abilities you actually need. Do NOT say "all abilities".
  * Format your response as:
    INSPECT: <ability-name> — <one-sentence reason>
    INSPECT: <ability-name> — <one-sentence reason>
  * If the summary is enough to make a confident memory update, say: SUFFICIENT
```

**`_MASTER_ANALYSE_EXTRACT_PROMPT`** (Step B):

```
ROLE
  Master campaign director. You have reviewed execution output.

TASK
  Emit a MemoryUpdate JSON.

RULES
  * `facts`: only keys where the ACTUAL stdout confirms the value.
    Failed commands → never as facts.
  * Pending keys that are confirmed: include WITHOUT the "pending." prefix.
  * Pending keys that failed: do NOT include — they will be deleted by code.
  * `issues`: a sub-dict under facts for problems found.
  * `narrative`: one paragraph in operator voice for the next planner.
  * Do NOT invent values. If unsure, omit the key.
  * Include a `phase_done` bool in facts if you are certain the phase
    objective is met or unachievable. Omit if uncertain — code will infer.
```

**Context budget enforcement:**

```python
MAX_STDOUT_CHARS_PER_ABILITY = 4000
MAX_ABILITIES_IN_STEP_B = 10
```

In the triage step: if Step A names more than `MAX_ABILITIES_IN_STEP_B`, keep only the top N by priority (passed abilities first, then failed with fixable issues).

### Acceptance criteria
- Token usage for Step A < 1000 tokens for a 6-ability operation.
- Step B always receives < 30,000 characters of combined stdout.
- Memory update never contains facts from failed stages.

---

## Phase 9 — Config, Boot, and Process-Global Graph Instance

**Single responsibility:** Wire the compiled graph singleton, checkpointer lifecycle, and timeout scanner into the server boot path.

### Files touched
- `src/bas/config.py` — finalize `ExecutionConfig`
- `config/config.yaml` — add execution block
- `src/bas/api.py` — store compiled graph in `_state`, add timeout scanner background task

### What to build

**`_state` bootstrap dict** — extend:

```python
_state: dict[str, Any] = {
    "cfg": None,
    "skills": None,
    "store": None,
    "artifacts": None,
    "results_store": None,    # NEW — ResultStore
    "compiled_graph": None,   # NEW — compiled LangGraph with checkpointer
    "checkpointer": None,     # NEW — MemorySaver or SqliteSaver
}
```

**`_bootstrap()` — extend:**

```python
from langgraph.checkpoint.memory import MemorySaver
# (or SqliteSaver if config says so)
checkpointer = MemorySaver()
compiled = build_graph(..., checkpointer=checkpointer)
_state["compiled_graph"] = compiled
_state["checkpointer"] = checkpointer
_state["results_store"] = ResultStore(runs_dir)
```

**`run_orchestrator()` — use the singleton if provided:**

```python
def run_orchestrator(..., compiled_graph=None) -> SessionState:
    app = compiled_graph or build_graph(...)
    ...
    for chunk in app.stream(seed, config={"configurable": {"thread_id": run_id}}, stream_mode="updates"):
```

**Timeout scanner — background task:**

Add to `_bootstrap()`:
```python
_start_timeout_scanner(cfg.execution.result_wait_timeout)
```

```python
def _start_timeout_scanner(timeout_seconds: int) -> None:
    """Periodically resume engagements stuck in awaiting_results past timeout."""
    def _scan():
        import time
        while True:
            time.sleep(60)    # scan every minute
            _expire_stale_engagements(timeout_seconds)
    t = threading.Thread(target=_scan, daemon=True, name="timeout-scanner")
    t.start()

def _expire_stale_engagements(timeout_seconds: int) -> None:
    _, _, store, _, compiled_graph = _bootstrap_all()
    now = datetime.now(timezone.utc)
    for record in store.list_all():
        if record["status"] != "awaiting_results":
            continue
        waiting_since = record.get("awaiting_since")
        if not waiting_since:
            continue
        elapsed = (now - datetime.fromisoformat(waiting_since)).total_seconds()
        if elapsed > timeout_seconds:
            engagement_id = record["run_id"]
            logger.warning("[timeout-scanner] expiring engagement %s after %ds", engagement_id, elapsed)
            compiled_graph.invoke(
                Command(resume={"timeout": True, "engagement_id": engagement_id}),
                config={"configurable": {"thread_id": engagement_id}},
            )
```

**`analyse_results` node — handle timeout case:**

```python
result_data = interrupt("awaiting_results")
if result_data.get("timeout"):
    log.append("[analyse_results] timeout — keeping pending.* as unverified")
    return {"log": log}   # proceed to master_plan without memory update
```

### Acceptance criteria
- Server boots with checkpointer initialized once.
- Compiled graph is reused across requests (not rebuilt per engagement).
- Engagements stuck in `awaiting_results` for > 600s are automatically resumed with timeout payload.

---

## Phase 10 — Engagement Status Transitions

**Single responsibility:** Make engagement status reflect reality throughout the full loop.

### Files touched
- `src/bas/api.py` — update status transitions
- `src/bas/persistence.py` — add `awaiting_since` field to records

### Status machine

```
queued
  → running         when graph starts
  → awaiting_results  when interrupt() fires (push + analyse starts)
  → running         when webhook resume fires
  → completed       when master emits done
  → failed          on unrecoverable exception
```

**Transitions to add:**
- On `interrupt()` in `analyse_results`: `record["status"] = "awaiting_results"`, `record["awaiting_since"] = now_iso()`
- On `Command(resume=...)` in webhook: `record["status"] = "running"`, remove `awaiting_since`
- On timeout-resume: `record["status"] = "running"` briefly, then normal flow

**`EngagementSummary` and `EngagementDetail` — add new status:**

```python
status: Literal["queued", "running", "awaiting_results", "completed", "failed"]
awaiting_since: datetime | None = None
```

### Acceptance criteria
- `GET /engagements/{id}` shows `awaiting_results` while backend is executing.
- `awaiting_since` timestamp is present when in that status.
- Status transitions are atomic with RunStore saves.

---

## Phase 11 — Master Plan Integration: Retry Context

**Single responsibility:** Give the master rich retry context in its PICK mode prompt so it can make an informed retry vs proceed decision.

### Files touched
- `src/bas/agents/master.py` — extend `plan_phase()` to accept execution context
- `src/bas/orchestrator/graph.py` — pass `execution_summary` + issues to master in PICK mode

### What to build

**Extend `plan_phase()` signature:**

```python
def plan_phase(
    self,
    *,
    foothold: dict,
    memory: dict,
    available_phases: list[str],
    completed_phases: list[str],
    phase_history: list[dict],
    attempt: int,
    execution_summary: str | None = None,   # NEW — structural summary from last run
) -> PhaseBriefing:
```

**Extend `PhaseBriefing` to carry retry hint:**

```python
class PhaseBriefing(BaseModel):
    ...
    retry_same_phase: bool = False
    retry_reason: str = ""
    issues_to_fix: list[str] = Field(default_factory=list)
```

> These fields are also added to `SessionState` in Phase 4. The master writes them
> into the briefing; `master_plan_node` copies them into state so `push_node` can
> read `state["retry_same_phase"]` to choose POST vs feedback path.

**`_MASTER_PLAN_PROMPT` — add execution context section:**

```
If `execution_summary` is provided, you are re-planning a phase based on
actual execution results. Analyse the summary:
  * If critical objectives were NOT met but the cause is fixable (tool missing,
    placeholder token, timeout), set retry_same_phase=true and list the
    specific issues in issues_to_fix.
  * If objectives WERE met or the phase is unachievable, set done=true or
    pick a different phase.
  * Never retry a phase more than 2 times (check phase_history for retry count).
```

### Acceptance criteria
- After a failed discovery run, master PICK returns `retry_same_phase=True` with issues list.
- After a successful (confirmed) discovery run, master PICK picks next phase.
- Master never retries the same phase more than 2 times (enforced by iteration cap + prompt).

---

## Phase 12 — End-to-End Testing and Hardening

**Single responsibility:** Validate the complete loop works under all edge cases before enabling full autonomous operation.

### Test matrix

| # | Test | Type | Validates |
|---|------|------|-----------|
| 1 | Parse sample result JSON | Unit | `OperationResult`, structural summary, issue detection |
| 2 | Duplicate webhook POST | Integration | Idempotent, single file, no double-resume |
| 3 | Webhook for unknown engagement | Integration | 404 returned cleanly |
| 4 | Oversized payload | Integration | 413, not saved, not resumed |
| 5 | Push → interrupt → resume | Integration | Engagement goes awaiting → running → completed |
| 6 | Full loop: discovery → analyse → master picks next phase | E2E | Memory confirmed, phase_history has execution_outcome |
| 7 | Retry: discovery fails (nmap) → analyse → master retries → feedback sent → same stage IDs | E2E | POST /ai/operation-feedback called with original stage_id |
| 8 | Timeout: no webhook in 600s | Edge | Graph resumes with timeout=True, pending.* preserved, phase proceeds |
| 9 | Process restart with SqliteSaver | Edge | Engagement in awaiting_results resumes after restart |
| 10 | Two concurrent engagements | Load | Each uses separate thread_id, no state crossover |

### Guard rails to add before enabling in production

1. `requires_approval: false` must be set for all AI-created abilities/adversaries.
2. `created_by: "ai"` must be present on all abilities/adversaries (already enforced in `plan_specialist`).
3. `POST /ai/operation-feedback` must only target `stage_id` values that exist in the current `phase_asset_map` — never fabricated.
4. Maximum retries per phase: 2 (enforced by iteration counter + `_MASTER_PLAN_PROMPT` rule).
5. `max_result_size_mb` enforcement in receiver (Phase 1).
6. `operation_id` in incoming webhook must match the expected `operation_id` from `phase_asset_map` (Phase 6 validates this).

---

## File Creation and Modification Index

| File | Action | Phase |
|------|--------|-------|
| `src/bas/api.py` | Modify | 1, 3, 9, 10 |
| `src/bas/persistence.py` | Modify | 1, 10 |
| `src/bas/results.py` | Create | 2 |
| `src/bas/config.py` | Modify | 3, 9 |
| `config/config.yaml` | Modify | 3, 9 |
| `src/bas/orchestrator/graph.py` | Modify | 3, 4, 5, 7, 11 |
| `src/bas/orchestrator/state.py` | Modify | 4, 5 |
| `src/bas/agents/master.py` | Modify | 4, 8, 11 |
| `src/bas/tools/master_tools.py` | Create | 4 |
| `src/bas/agents/specialist.py` | Modify | 5 |
| `src/bas/client/operations.py` | Create | 6 |
| `src/bas/client/facade.py` | Modify | 6, 7 |
| `src/bas/client/feedback.py` | Create | 7 |

**No other files need to change.** Evaluator, router, llm, foothold, phases, skills — untouched.

---

## Dependency Order (which phase can start after which)

```
Phase 1  (standalone)  ───────────────────────────────┐
Phase 2  (standalone)  ───────────────────────────────┤
Phase 3  (depends on 1 for receiver stub)                  │
Phase 4  (depends on 2 for parsers, 3 for interrupt)       │
Phase 5  (depends on 4 for state fields)                   │
Phase 6  (depends on 5 for phase_asset_map)                │
Phase 7  (depends on 4 for retry_same_phase + issues,     ─┤
          depends on 6 for operation_id)                    │
Phase 8  (depends on 4 for analyse node)                   │
Phase 9  (depends on 3 for checkpointer pattern)           │
Phase 10 (depends on 3 for status transitions)             │
Phase 11 (depends on 4, 7)                                 │
Phase 12 (depends on all)                                 ─┘
```

Phases 1 and 2 are fully independent and can be developed in parallel.

> **Change from original plan:** Phase 7 no longer has a hidden dependency on
> Phase 11 — `retry_same_phase` and `issues_to_fix` fields were moved to Phase 4.

---

## What "done" looks like

The engine is complete when:

1. `POST /engagements` starts the loop.
2. Each phase: plan → evaluate → master commit → push (POST all new IDs) → create + start operation.
3. Backend executes → POSTs result to `/engagements/{id}/results` automatically.
4. Receiver validates + stores + resumes graph.
5. `analyse_results` extracts confirmed facts, detects issues, updates memory.
6. Master receives structural summary + issues; decides retry or next phase.
7. Retry: `POST /ai/operation-feedback` with same stage IDs, re-run same operation.
8. New phase: start loop from step 2 with next phase.
9. All phases complete → master sets `done=True` → engagement status `completed`.
10. Full audit trail in `runs/{engagement_id}/` for every phase.
