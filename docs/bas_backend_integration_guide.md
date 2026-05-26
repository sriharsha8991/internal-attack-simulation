# BAS Backend Integration Guide

**Orchestrator Base URL:** `http://<orchestrator_host>:8765`

---

## Flow Summary

```
1. GET  /healthz                              → verify orchestrator is up
2. GET  /environments                         → list available BAS environments
3. POST /engagements                          → create & start an engagement (returns engagement_id)
4. GET  /engagements/{engagement_id}          → poll status
5. POST /results                              → YOU send execution results here (webhook)
6. GET  /engagements/{engagement_id}          → poll until status = "completed"
7. GET  /engagements/{engagement_id}/artifacts→ get the abilities/adversaries that were pushed
```

---

## 1. Health Check

Verify the orchestrator is running.

```
GET /healthz
```

**Response `200`**
```json
{
  "status": "ok",
  "base_url": "http://pelorushub.online:31763",
  "dry_run_default": false,
  "engagements_dir": "runs",
  "artifacts_dir": "artifacts"
}
```

---

## 2. List Environments

Get available BAS environments to pick one for the engagement.

```
GET /environments
```

**Response `200`**
```json
[
  {
    "id": "a1b2c3d4-...",
    "name": "Production Lab",
    "agents": 5
  }
]
```

---

## 3. Create Engagement

Starts a new attack simulation engagement. Returns immediately; execution runs in background.

```
POST /engagements
Content-Type: application/json
```

**Request Body**
```json
{
  "phases": ["discovery", "privesc", "credaccess"],
  "environment": {
    "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "name": null
  },
  "target": {
    "platform": "windows"
  },
  "max_iterations": 20,
  "dry_run": false
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `phases` | `string[]` | No | `discovery`, `privesc`, `credaccess`, `lateral`, `persistence`, `defevasion`, `impact`. Omit to let AI decide. |
| `environment.id` | `UUID` | No | BAS environment UUID. Omit to use most recent. |
| `environment.name` | `string` | No | Exact environment name (alternative to id). |
| `target.platform` | `string` | No | `"windows"`, `"linux"`, or `"mac"`. |
| `max_iterations` | `int` | No | Default `20`, range 1–50. |
| `dry_run` | `bool` | No | `true` = plan only, don't push to BAS. |

**Response `202`**
```json
{
  "engagement_id": "08c047ac0cba49b3ae85257406a5cc28",
  "status_url": "/engagements/08c047ac0cba49b3ae85257406a5cc28",
  "status": "queued"
}
```

**Errors**
| Status | When |
|--------|------|
| `400` | Unknown phase name in `phases` array |

---

## 4. Get Engagement Status

Poll this to track progress. The engagement will reach `awaiting_results` when it has pushed abilities and is waiting for your execution results.

```
GET /engagements/{engagement_id}
```

**Response `200`**
```json
{
  "engagement_id": "08c047ac0cba49b3ae85257406a5cc28",
  "status": "awaiting_results",
  "started_at": "2026-05-26T10:00:00Z",
  "finished_at": null,
  "awaiting_since": "2026-05-26T10:02:30Z",
  "iterations": 3,
  "completed_stages": ["discovery"],
  "error": null,
  "phases": ["discovery", "credaccess"],
  "environment": { "id": "a1b2c3d4-...", "name": null },
  "target": { "platform": "windows" },
  "foothold": {
    "host": "10.10.20.15",
    "platform": "windows",
    "username": "CORP\\agent01",
    "access_level": "user"
  },
  "skill_order": ["discovering-environment"],
  "dry_run": false,
  "state": null,
  "log_tail": [
    "[10:00:01] init: resolved foothold 10.10.20.15",
    "[10:01:15] master: picked phase discovery",
    "[10:02:30] push: created adversary + 4 abilities"
  ]
}
```

**Status values:**

| Status | Meaning |
|--------|---------|
| `queued` | Created, not yet started |
| `running` | LLM is planning / evaluating |
| `awaiting_results` | Abilities pushed — **waiting for YOUR execution results** |
| `completed` | All phases done |
| `failed` | Error or timeout |

**Errors**
| Status | When |
|--------|------|
| `404` | Engagement not found |

---

## 5. Send Execution Results (Webhook)

**This is the most important endpoint for the backend team.** After the BAS backend executes the abilities, POST the results here.

```
POST /results
Content-Type: application/json
```

**Request Body**
```json
{
  "engagement_id": "08c047ac0cba49b3ae85257406a5cc28",
  "operation_id": "f9e8d7c6-b5a4-3210-fedc-ba0987654321",
  "operation_name": "discovery-recon-phase1",
  "operation_status": "completed",
  "completed_at": "2026-05-26T10:30:00Z",
  "adversary": {
    "adversary_id": "aaaa-bbbb-cccc-dddd",
    "name": "BAS-discovery-recon"
  },
  "progress": {
    "total_abilities": 4,
    "completed_abilities": 4,
    "progress_percent": 100
  },
  "abilities": [
    {
      "ability_id": "11111111-aaaa-bbbb-cccc-dddddddddddd",
      "name": "Network interface enumeration",
      "mitre_technique_id": "T1016",
      "platform": "windows",
      "stages": [
        {
          "stage_name": "Enumerate NICs and IPs",
          "executor": "psh",
          "command_executed": "Get-NetIPAddress | Format-Table -AutoSize",
          "execution_status": "passed",
          "stdout": "InterfaceAlias  IPAddress    PrefixLength\nEthernet0       10.10.20.15  24\n",
          "stderr": "",
          "exit_code": 0,
          "timestamp": "2026-05-26T10:28:01Z"
        }
      ]
    },
    {
      "ability_id": "22222222-aaaa-bbbb-cccc-dddddddddddd",
      "name": "Port scan fallback",
      "mitre_technique_id": "T1046",
      "platform": "windows",
      "stages": [
        {
          "stage_name": "Check nmap",
          "executor": "cmd",
          "command_executed": "where nmap",
          "execution_status": "failed",
          "stdout": "",
          "stderr": "INFO: Could not find files for the given pattern(s).",
          "exit_code": 1,
          "timestamp": "2026-05-26T10:29:00Z"
        },
        {
          "stage_name": "Run PowerShell scan",
          "executor": "psh",
          "command_executed": "1..1024 | % { ... }",
          "execution_status": "passed",
          "stdout": "53 open\n88 open\n445 open\n",
          "stderr": "",
          "exit_code": 0,
          "timestamp": "2026-05-26T10:29:45Z"
        }
      ]
    }
  ]
}
```

**Field Reference**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `engagement_id` | `string` | **Yes** | The engagement ID returned by `POST /engagements`. |
| `operation_id` | `UUID string` | **Yes** | Unique ID for this execution run. Must be valid UUID. |
| `operation_name` | `string` | No | Human-readable label |
| `operation_status` | `string` | No | `"completed"`, `"failed"`, or `"partial"` |
| `completed_at` | `ISO 8601` | No | When execution finished |
| `adversary` | `object` | No | `{ "adversary_id": str, "name": str }` |
| `progress` | `object` | No | `{ "total_abilities": int, "completed_abilities": int, "progress_percent": float }` |
| `abilities` | `array` | **Yes** | List of ability execution results (see below) |

**Each ability:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `ability_id` | `string` | **Yes** | The ability UUID you received when it was pushed |
| `name` | `string` | No | Ability name |
| `mitre_technique_id` | `string` | No | e.g. `"T1016"` |
| `platform` | `string` | No | `"windows"`, `"linux"`, `"mac"` |
| `stages` | `array` | **Yes** | List of stage execution results |

**Each stage:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `stage_name` | `string` | **Yes** | Stage identifier |
| `executor` | `string` | No | `"cmd"`, `"psh"`, `"sh"`, `"bash"` |
| `command_executed` | `string` | **Yes** | The actual command that was run |
| `execution_status` | `string` | **Yes** | `"passed"` or `"failed"` |
| `stdout` | `string` | **Yes** | Full standard output captured |
| `stderr` | `string` | **Yes** | Full standard error captured |
| `exit_code` | `int` | **Yes** | `0` = success, non-zero = failure |
| `timestamp` | `ISO 8601` | No | When this stage executed |

**Response `202`** — Result accepted, graph resumes in background.
```json
{
  "status": "accepted",
  "operation_id": "f9e8d7c6-b5a4-3210-fedc-ba0987654321"
}
```

**Response `200`** — Duplicate result (already received, safe to ignore).
```json
{
  "status": "already_received",
  "operation_id": "f9e8d7c6-b5a4-3210-fedc-ba0987654321"
}
```

**Errors**

| Status | When |
|--------|------|
| `404` | Engagement not found |
| `409` | Engagement not in `awaiting_results` or `running` status |
| `422` | Invalid JSON or missing/invalid fields |

---

## 6. Get Engagement Log

Get the execution log for debugging.

```
GET /engagements/{engagement_id}/log
```

**Response `200`**
```json
[
  "[10:00:01] init: resolved foothold 10.10.20.15",
  "[10:01:15] master: picked phase discovery",
  "[10:02:30] push: created adversary BAS-discovery-recon + 4 abilities",
  "[10:05:00] analyse_results: phase discovery completed successfully"
]
```

---

## 7. Get Pushed Artifacts

Retrieve the exact abilities and adversaries that were pushed to the BAS backend for a given engagement.

```
GET /engagements/{engagement_id}/artifacts
```

**Response `200`**
```json
{
  "abilities": [
    {
      "ability_id": "11111111-...",
      "name": "Network interface enumeration",
      "platform": "windows",
      "executor": "psh",
      "command": "Get-NetIPAddress | Format-Table -AutoSize",
      "mitre_technique_id": "T1016"
    }
  ],
  "adversaries": [
    {
      "adversary_id": "aaaa-...",
      "name": "BAS-discovery-recon",
      "abilities": ["11111111-...", "22222222-..."]
    }
  ]
}
```

---

## 8. List All Engagements

```
GET /engagements?limit=50
```

**Response `200`**
```json
[
  {
    "engagement_id": "08c047ac0cba49b3ae85257406a5cc28",
    "status": "completed",
    "started_at": "2026-05-26T10:00:00Z",
    "finished_at": "2026-05-26T10:35:00Z",
    "awaiting_since": null,
    "iterations": 8,
    "completed_stages": ["discovery", "credaccess", "lateral"],
    "error": null
  }
]
```

---

## 9. Delete Engagement

Removes an engagement and its data.

```
DELETE /engagements/{engagement_id}
```

**Response:** `204 No Content`

**Errors**
| Status | When |
|--------|------|
| `404` | Engagement not found |

---

## 10. List Available Phases

```
GET /phases
```

**Response `200`**
```json
[
  { "name": "discovery", "skills": ["discovering-environment"] },
  { "name": "privesc",   "skills": ["escalating-privileges"] },
  { "name": "credaccess","skills": ["accessing-credentials"] },
  { "name": "lateral",   "skills": ["moving-laterally"] },
  { "name": "persistence","skills": ["establishing-persistence"] },
  { "name": "defevasion", "skills": ["evading-defenses"] },
  { "name": "impact",    "skills": ["achieving-impact"] }
]
```

---

## 11. List Available Skills

```
GET /skills
```

**Response `200`**
```json
[
  {
    "name": "discovering-environment",
    "description": "Enumerates hosts, services, AD structure...",
    "stage": "discovery",
    "mitre_tactics": ["TA0007"],
    "tool_count": 12
  }
]
```

---

## Typical Integration Sequence

```
Backend                              Orchestrator
  │                                      │
  │  POST /engagements                   │
  │  { phases, environment, target }     │
  │ ──────────────────────────────────►  │
  │                                      │
  │  ◄── 202 { engagement_id, status }   │
  │                                      │
  │       (orchestrator plans abilities   │
  │        and pushes them to BAS ↓)     │
  │                                      │
  │  ◄── POST /abilities                 │
  │      { name, ...,                    │
  │        engagement_id: "08c0..." }    │
  │  ◄── POST /abilities/{id}/stages     │
  │  ◄── POST /adversaries               │
  │      { name, ...,                    │
  │        engagement_id: "08c0..." }    │
  │  ◄── POST /adversaries/{id}/         │
  │       abilities/{ab_id}  (link)      │
  │                                      │
  │  GET /engagements/{id}               │
  │ ──────────────────────────────────►  │
  │  ◄── 200 { status: "awaiting_results" }
  │                                      │
  │  (BAS executes the abilities on      │
  │   the target agent)                  │
  │                                      │
  │  POST /results                        │
  │  { engagement_id, operation_id,       │
  │    abilities[] }                      │
  │ ──────────────────────────────────►  │
  │                                      │
  │  ◄── 202 { status: "accepted" }      │
  │                                      │
  │  (orchestrator analyses results,     │
  │   plans next phase, pushes again)    │
  │                                      │
  │  ... repeat results webhook ...      │
  │                                      │
  │  GET /engagements/{id}               │
  │ ──────────────────────────────────►  │
  │  ◄── 200 { status: "completed" }     │
  │                                      │
  │  GET /engagements/{id}/artifacts     │
  │ ──────────────────────────────────►  │
  │  ◄── 200 { abilities, adversaries }  │
```

---

## Key Rules for the Backend

1. **`engagement_id` is the correlation key** — every ability and adversary the orchestrator pushes to your API includes `engagement_id`. Store it. Use it when sending results back via `POST /results`.
2. **`operation_id` must be a valid UUID** — the orchestrator validates it.
3. **`stdout` and `stderr` are critical** — the AI analyses the raw output to decide next steps. Capture EVERYTHING.
4. **`exit_code` matters** — `0` = passed, anything else = failed. The AI uses this to detect blocked commands.
5. **Webhook is idempotent** — safe to retry on network failure (same `operation_id` → `200` no-op).
6. **Send results when status is `awaiting_results`** — sending when status is something else returns `409`.
7. **Timeout** — if no results arrive within the configured timeout (default 600s), the orchestrator marks the engagement as timed out and may retry the phase.

---

## What the Backend Receives (Ability / Adversary Push)

The orchestrator calls **your** API to push abilities and adversaries. Every payload includes `engagement_id` so you can correlate entities across concurrent engagements.

### POST /abilities (orchestrator → backend)

```json
{
  "name": "Network interface enumeration",
  "description": "Enumerate NICs and IPs on the target host",
  "mitre_tactic": "TA0007",
  "mitre_technique_id": "T1016",
  "platform": "windows",
  "default_severity": "low",
  "created_by": "ai",
  "engagement_id": "08c047ac0cba49b3ae85257406a5cc28"
}
```

### POST /abilities/{ability_id}/stages (orchestrator → backend)

```json
{
  "stage_name": "Enumerate NICs and IPs",
  "stage_order": 1,
  "executor": "psh",
  "command_template": "Get-NetIPAddress | Format-Table -AutoSize",
  "timeout_seconds": 120
}
```

### POST /adversaries (orchestrator → backend)

```json
{
  "name": "BAS-discovery-recon",
  "description": "Discovery phase adversary",
  "profile": "ai-generated",
  "created_by": "ai",
  "engagement_id": "08c047ac0cba49b3ae85257406a5cc28"
}
```

### POST /adversaries/{adversary_id}/abilities/{ability_id} (link)

No body — just links an ability to the adversary.

### Backend responsibility

| Step | Action |
|------|--------|
| 1 | Store `engagement_id` when you receive abilities and adversaries |
| 2 | Execute the abilities on the target agent |
| 3 | Include the **same `engagement_id`** in your `POST /results` webhook payload |
| 4 | Generate a new UUID for `operation_id` per execution run |
