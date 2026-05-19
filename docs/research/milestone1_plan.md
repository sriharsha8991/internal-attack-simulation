# Milestone 1 — Plan

Scope: **generate Abilities + Adversary for Discovery phase, push to backend, link them, and stop**. The backend auto-executes anything with `created_by: "ai"`. No execution loop, no result polling, no feedback handling in M1 — those are M2+.

Last updated 2026-05-19.

## 1. What changed since the research dossier

User-confirmed constraints that simplify M1:

- **Stack**: `google-genai` SDK for the first LLM provider + **LangGraph** for agent orchestration, but **all model calls go through a `LLMProvider` interface** so we can swap to Anthropic (`anthropic` SDK), OpenAI (`openai` SDK), or Azure later by changing one config line. Provider-specific features (Gemini's `google_search` grounding, Anthropic's `web_search_20250305` tool, OpenAI's `web_search_preview` tool) are exposed through a unified `provider.research(...)` call — see §4.
- **Web search**: when enabled, use whatever native grounding/search tool the configured provider offers. The agent decides *whether* to ground per technique (skip/light/deep) — grounding is conditional, not mandatory.
- Backend base URL: `http://pelorushub.online:31763` (Swagger UI at `/docs`). Login is straightforward via `/auth/login`.
- `created_by: "ai"` → backend starts the Operation on its own. Our job: **create + link, then exit**.
- No enums anywhere. Free-form strings only.
- Execution logs / feedback / mid-flight ability mutation = M2.
- **Environment selection**: all environments are labs; pick any active one (highest `created_at` / `updated_at`).
- **Agent selection**: pick highest `last_seen` per platform from `/environments/{env}/agents`.
- **Payloads deferred**. M1 covers Discovery only; ~33 of the 34 TA0007 techniques use native OS tools or `apt-get`/`choco install` to fetch their binary. No custom payload upload in M1. We will add `POST /payloads` (name-based dedup) when we hit the first technique that needs a bundled binary (SharpHound on a host without internet egress — likely M2). Plan still notes the integration point in §6 step 5 (currently no-op).
- **Every command Ability must include preflight stages (install, detect distro, sudo check, fallback path) up-front** — the LLM acts like a human pentester who retrospects edge cases before pressing Enter.
- **Clean pushes**: no backend throttle today, but the pipeline must insert a small sleep (default 250 ms) between bulk POSTs (abilities, stages, links) to keep the audit trail readable and avoid future-rate-limit pain.

## 2. M1 components to build

```
src/
├── bas_client.py            ← typed HTTP client (only the endpoints M1 needs)
├── models.py                ← pydantic mirrors of OpenAPI schemas
├── llm/
│   ├── base.py              ← LLMProvider Protocol (chat, generate_structured, research)
│   ├── gemini.py            ← google-genai implementation (uses google_search grounding)
│   ├── anthropic.py         ← stub for now; uses web_search_20250305 tool later
│   ├── openai.py            ← stub for now; uses web_search_preview tool later
│   └── factory.py           ← reads config/llm.yaml, returns the configured provider
├── tools/
│   └── skill_loader.py      ← read SKILL.md + reference/*.md from disk
├── agents/
│   ├── base_agent.py        ← skill loading, optional grounding, rationale capture
│   └── discovery_agent.py   ← LangGraph node that turns techniques into Abilities + Stages
└── orchestrator/
    ├── graph.py             ← LangGraph state machine for M1 (linear: load → generate → push)
    └── push_pipeline.py     ← the M1 happy path (§6); invoked as the graph's terminal node
```

Plus:

```
config/
├── bas.yaml                 ← base_url, env_name, sleep_ms
└── llm.yaml                 ← provider: gemini|anthropic|openai, model, temperature, grounding caps
```

`config/llm.yaml` example:

```yaml
provider: gemini                   # gemini | anthropic | openai
model: gemini-2.5-pro              # provider-specific
classifier_model: gemini-2.5-flash # cheap model for skip/light/deep decision
temperature: 0.2
grounding:
  max_grounded_calls_per_run: 40
  default_depth: light             # used when classifier returns ambiguous
api_key_env: GEMINI_API_KEY        # where to read the key from at runtime
```

Switching to Anthropic later = change two lines (`provider: anthropic`, `model: claude-sonnet-4`) and set `ANTHROPIC_API_KEY`. No code change.

## 3. BAS API surface used in M1

Only these endpoints. Everything else is M2+.
Base URL: `http://pelorushub.online:31763`. Swagger: `/docs`.

| Verb | Endpoint | Why |
|---|---|---|
| POST | `/auth/login` | Get bearer token (one shot per run) |
| GET | `/environments` | Resolve target environment; pick most recent if multiple match |
| GET | `/environments/{env}/agents` | Pick agent per platform by highest `last_seen` |
| GET | `/payloads` | Lookup existing payloads by `name` (dedupe) |
| POST | `/payloads` | Upload missing payload binaries (multipart) |
| POST | `/abilities` | Create one Ability per (technique × platform) |
| POST | `/abilities/{id}/stages` | Create ordered stages (preflight → install → run → harvest) |
| POST | `/adversaries` | Create the Discovery adversary |
| POST | `/adversaries/{adv}/abilities/{ab}` | Link each Ability to the Adversary |

Auth: bearer token from `/auth/login`. No retry/refresh in M1.
Sleep `bas.sleep_ms` (default 250 ms) between successive POSTs in any bulk loop.

## 4. Internet search tool (shared across all specialist agents)

### 4.1 Purpose

The agent uses web search **only during Ability generation**, never on the victim. The agent reads:

- The technique row (e.g. D22, T1018, ping sweep, platform=linux)
- The current environment context (network ranges, observed defenses, platform mix)
- Any prior findings already pushed to typed memory

…and produces:

- An ordered list of stages (preflight → install → execute → harvest)
- A **rationale** explaining each command choice in operator voice ("why this flag, why this fallback, what would an EDR see")

### 4.2 Provider plan

All LLM calls go through a thin **`LLMProvider`** Protocol in `src/llm/base.py`. M1 ships a Gemini implementation; Anthropic and OpenAI are stub files we'll fill in when needed.

```python
class LLMProvider(Protocol):
    def chat(self, messages, *, system=None, model=None) -> str: ...
    def generate_structured(self, prompt, schema, *, system=None) -> dict: ...
    def classify_grounding_depth(self, technique, context) -> Literal["skip","light","deep"]: ...
    def research(self, topic, context, depth) -> ResearchBrief: ...  # uses provider-native search
```

Provider mapping:

| Provider | Generation model | Grounding tool |
|---|---|---|
| Gemini (M1) | `gemini-2.5-pro` | `tools=[{"google_search": {}}]` |
| Anthropic (future) | `claude-sonnet-4` (or current) | `web_search_20250305` tool |
| OpenAI (future) | `gpt-4.1` / `o-series` | `web_search_preview` tool |

- **When to enable grounding** — conditional, picked by `classify_grounding_depth()` which uses the cheapest model the provider offers (no tools attached):
  1. **`skip`** — technique well-known and reference already has a concrete templated command (e.g. `whoami`, `id`, `ipconfig /all`). Generate stages straight from the reference.
  2. **`light`** — a single grounded query for current-best-practice flags / edge cases.
  3. **`deep`** — up to 3 grounded queries; only when the technique is novel, the platform has known quirks, or defenses are detected.
- **Output capture**: when grounding fires, persist provider-native metadata (Gemini's `grounding_metadata`, Anthropic's `web_search_tool_result` blocks, OpenAI's `web_search_call` events) alongside the generated Ability JSON in `runs/<ts>/abilities/<id>.json` so cited URLs are auditable.
- **No third-party search dependency** — no Tavily, Serper, Brave, page fetcher, or local cache layer in M1. Whatever provider is configured handles search + summarisation + citation natively.

### 4.3 Cost + safety controls

- Hard cap: `llm.yaml::grounding.max_grounded_calls_per_run` (default 40). Pipeline fails closed if exceeded.
- The classification call itself counts toward the budget log but uses the cheap model.
- Generated commands are never executed locally during planning — grounding is read-only.
- We **reject any Ability whose rationale cites zero URLs when grounding was enabled** — forces the model to actually use the search results it asked for.

## 5. The "act like a human" command pattern

Every technique we push expands into a defensive **5-step stage sequence** instead of one raw command. Concretely, for D22 ping sweep on Linux:

| order | stage_name | purpose | example template (linux/sh) |
|---|---|---|---|
| 1 | `detect_distro` | Branch on package manager. Writes `/tmp/.bas/distro` | `for m in apt-get yum dnf apk pacman; do command -v $m >/dev/null && echo $m && break; done > /tmp/.bas/distro` |
| 2 | `ensure_internet_or_use_offline` | Probe egress; if blocked, fall back to bundled portable payload | `getent hosts download.nmap.org >/dev/null && echo online \|\| echo offline > /tmp/.bas/net` |
| 3 | `install_nmap_idempotent` | Install only if missing; honour distro from step 1; non-interactive | `command -v nmap >/dev/null \|\| { case "$(cat /tmp/.bas/distro)" in apt-get) apt-get update -y && DEBIAN_FRONTEND=noninteractive apt-get install -y nmap;; yum\|dnf) "$(cat /tmp/.bas/distro)" -y install nmap;; apk) apk add --no-cache nmap;; pacman) pacman -Sy --noconfirm nmap;; esac; }` |
| 4 | `execute_with_fallback` | Primary command + automatic fallback if ICMP blocked | `nmap -sn -PE -PP -PM -PR -oX /tmp/.bas/d22.xml {{ env.network_ranges[0] }} \|\| nmap -sn -PS22,80,135,139,443,445,3389 -PA80,443,3389 -oX /tmp/.bas/d22.xml {{ env.network_ranges[0] }}` |
| 5 | `print_results_compact` | Emit small parseable summary the backend can capture | `awk -F\\" '/host endtime/ && /up/{print $4}' /tmp/.bas/d22.xml \|\| true` |

Edge cases the agent must always retrospect before emitting a stage list:

- Tool not present → install path (with offline fallback)
- Multiple package managers (apt/yum/dnf/apk/pacman/zypper for linux; choco/winget/scoop for windows; brew/port for mac)
- No internet egress → use bundled payload `payload_id`
- No sudo / not root → either `sudo -n` probe + skip-with-explanation, or use unprivileged equivalent (e.g. `nmap -sT` instead of `-sS`)
- Tool exits non-zero but produced partial output → keep output (`|| true` guard on terminal stages, never on detect stages)
- Output paths must be platform-portable (`/tmp/.bas/` on \*nix, `%TEMP%\bas\` on windows, scoped to a per-operation subdir)
- Very large outputs → tail or summarise inline so backend logs stay readable
- Time skew between victim and DC (Kerberos) → record `date` early
- Hostnames vs FQDNs vs IPs → always log all three when discovered
- Locale / language differences breaking `findstr` / `awk` parsing → prefer XML / JSON output modes when the tool supports them (`-oX`, `--json`, `ConvertTo-Json`)
- Windows execution policy / AMSI / WDAC → wrap PowerShell in `-ExecutionPolicy Bypass -NoProfile` and use `-EncodedCommand` only if AMSI is hot (later phase decides)

## 6. M1 happy-path pipeline (what `push_pipeline.py` does)

Wraps the LangGraph state machine. Linear graph in M1 (no branching nodes yet):
`login → resolve_env → resolve_agents → (sync_payloads: no-op in M1) → generate_abilities → push_abilities → create_adversary → link_abilities → emit_report`.

```
1.  load config/bas.yaml + POST /auth/login   → bearer token
2.  GET /environments → pick most-recent active (all envs are labs) → env_id
3.  GET /environments/{env}/agents → group by platform, pick highest last_seen per platform
4.  load skills/discovering-environment/SKILL.md + reference/*
5.  payloads: no-op in M1.  M1 techniques are all native-tool or package-manager-installed.
       (Future: GET /payloads, dedupe by name, POST /payloads for missing binaries.)
6.  for each technique row in reference/techniques.md:
       for each platform the technique supports (win/linux/mac):
           depth = provider.classify_grounding_depth(technique, env_context)   # skip|light|deep
           brief = provider.research(technique, env_context, depth) if depth != "skip" else None
           stages = provider.generate_structured(prompt=stage_gen_prompt,
                                                 schema=StageList,
                                                 ...)
                    # produces the 5-step defensive sequence (§5)
           rationale = provider.chat(prompt=rationale_prompt, ...)
                       # black-hat operator voice; if brief is not None, MUST cite >=1 URL
           ability = POST /abilities {
                       name, description=rationale,
                       mitre_tactic="TA0007", mitre_technique_id=<row.mitre>,
                       platform, impact_type="recon",
                       default_severity="info", requires_approval=false,
                       tags=[stage:discovery, subtrack:<row.subtrack>, grounding:<depth>, ...],
                       created_by="ai"
                     }
           for s in stages: POST /abilities/{ability.id}/stages s
           record ability.id
7.  adv = POST /adversaries {
            name="Discovery — Environment (v1) [run-{ts}]",
            description=<one-paragraph summary of phase + all techniques covered>,
            profile="discovery",
            execution_strategy="branching",     # free-form string per user
            requires_approval=false,
            is_tested=false,
            created_by="ai"
          }
8.  for each ability.id: POST /adversaries/{adv.id}/abilities/{ability.id}
9.  emit run report:
       runs/<timestamp>/
         adversary.json          (adv response + linked ability ids)
         abilities/<id>.json     (ability + stages + rationale + research brief urls)
         payloads.json           (logical name → payload_id table)
         summary.md              (human-readable, what was pushed)
10. exit 0.  Backend auto-executes because created_by=ai.
```

That's it. No polling, no feedback, no cleanup. M2 adds the polling + feedback loop. M3 adds the Rollback Adversary.

## 7. Skill file changes implied by this plan

- Rename `skills/discovering-environment/` → `skills/discovering-environment/` (awaiting OK).
- Expand `reference/techniques.md` from the current 20 AD-centric rows to the 34-row TA0007 catalogue (per dossier §3).
- `reference/tool-commands.md` becomes the **template** the LLM consults to produce the 5-step defensive stage list — not a literal source of stages. The LLM is free to rewrite based on the Gemini research brief. Treat it as a "hint" file, not the source of truth.
- Add a top-level `skills/_shared/research-tool.md` reference doc that describes the Gemini grounding tool, the `skip/light/deep` classification policy, and the black-hat rationale voice so every specialist agent loads it.

## 8. Risk + control checklist for M1

- [ ] BAS bearer token and `GEMINI_API_KEY` only in env vars, never logged.
- [ ] Pipeline enforces `gemini.max_grounded_calls_per_run` (default 40); fail closed when exceeded.
- [ ] Generated `command_template`s validated against the engagement scope (no IPs / hostnames outside `env.network_ranges`) before push.
- [ ] Generated commands run through a static linter that flags: `rm -rf /`, `shutdown`, `format`, `cipher /w`, `vssadmin delete shadows`, `wevtutil cl`, etc. — anything destructive gets `requires_approval: true` regardless of `created_by`.
- [ ] When grounding depth was `light` or `deep`, the resulting Ability rationale MUST cite ≥1 URL from `grounding_metadata` — reject and regenerate otherwise.
- [ ] Dry-run mode (`--dry-run`): pipeline emits all the JSON it would POST but does not actually call the backend. Default ON for the first run.
- [ ] Sleep `bas.sleep_ms` between bulk POSTs to keep the audit trail readable.
- [ ] Every run is reproducible via `runs/<ts>/` artefacts — enough to diff between runs and explain what the LLM proposed.

## 9. Resolved + remaining questions

Resolved this round:

- Stack → `google-genai` for M1 LLM, wrapped behind `LLMProvider` Protocol so Anthropic / OpenAI / Azure can drop in later without code changes.
- Web search → provider-native grounding (Gemini's `google_search` in M1), conditional (skip/light/deep). No Tavily/Serper.
- Backend URL → `http://pelorushub.online:31763` (Swagger at `/docs`).
- Auth → `/auth/login` email+password is fine.
- Environment selection → most recent active (all envs are labs, so any active is fair game).
- Skill rename → done. `discovering-ad` is now `discovering-environment`.
- Payloads → deferred. M1 uses only native / package-manager-installed tools. We will add the `POST /payloads` flow when the first bundled-binary technique lands (likely M2).
- Throttle → none yet; we still sleep 250 ms between bulk POSTs.

Still open (non-blocking):

1. **`POST /abilities` collision behaviour** — will check empirically on first run; if backend creates duplicates by name, add a `list-then-create` dedupe step.
2. **Exact env-id selection rule** — "most recent active" is fine for M1, but if you ever want to pin a specific environment, we'll add a `--env-name` CLI flag.

## 10. Acceptance criteria for M1

- Running `python -m bas push discovering-environment` (env auto-picked as most recent active) results in:
  - 1 Adversary visible in `GET /adversaries` with `name` starting `Discovery — Environment`
  - N Abilities (one per (technique × supported platform)) linked to it
  - Each Ability has the 5-step defensive stage sequence (detect_distro → ensure_internet_or_offline → install_idempotent → execute_with_fallback → harvest)
  - Each Ability `description` contains the rationale; when grounding fired, it cites ≥1 URL
  - A `runs/<timestamp>/summary.md` is committable to the repo for review
  - Backend auto-starts the Operation (because `created_by=ai`); we do not poll it in M1
- Re-running the same command is safe (idempotent payloads when we add them; abilities behaviour TBD).
- Dry-run mode produces the same JSON without calling the backend.
- Total grounded calls per run stays within `grounding.max_grounded_calls_per_run`.
- Swapping `config/llm.yaml::provider` from `gemini` to `anthropic` (once the stub is filled) requires no edits in `agents/`, `orchestrator/`, or `bas_client.py`.
