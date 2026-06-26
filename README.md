# internal-attack-simulation

Autonomous internal-attack orchestrator. Generates Abilities + Adversaries with grounded LLMs and pushes them to the BAS Platform for execution.

## Stack

- Python **3.11** (managed by `uv`)
- `pydantic` v2 — typed models mirroring the BAS OpenAPI
- `httpx` — BAS Platform client
- `google-genai` + `langgraph` — agent orchestration with Gemini grounding
- Provider-agnostic LLM layer (Anthropic / OpenAI swappable via config)

## Getting started

### 1. Install `uv`

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart your shell, or add `~/.local/bin` (Linux/macOS) or `%USERPROFILE%\.local\bin` (Windows) to `PATH` for the current session.

### 2. Bootstrap the project

```powershell
uv sync
```

This will:
- Install CPython 3.11 if missing.
- Create `.venv/` in the repo root.
- Resolve all dependencies and install them.
- Write `uv.lock` (commit this).

### 3. Configure

All settings live in [config/config.yaml](config/config.yaml). Runtime tunables come from env vars — copy [.env.example](.env.example) to `.env` and adjust as needed:

```
GEMINI_API_KEY=...
# BAS_ENVIRONMENT_NAME=my-lab   # optional pin
```

The YAML reads env vars with `${VAR}` and `${VAR:-default}` interpolation. To use a different file, set `BAS_CONFIG=/path/to/your.yaml`.

> The BAS Platform does not require authentication in M1 — there is no login step.

### 4. Run things

You never need to activate the venv. Just prefix commands with `uv run`:

```powershell
uv run python -m bas --help            # CLI (wired in Step 7)
uv run pytest                          # tests
uv add <package>                       # add a dep
uv lock --upgrade                      # bump locks
```

## Repository layout

```
internal-attack-simulation/
├── pyproject.toml          # project + deps (PEP 621)
├── uv.lock                 # deterministic lockfile (commit)
├── .python-version         # pinned Python 3.11
├── config/
│   └── config.yaml         # central configuration (one file controls everything)
├── docs/                   # system design + research dossiers
├── skills/                 # Anthropic-format skill dirs
│   ├── discovering-environment/
│   ├── enumerating-active-directory/
│   ├── escalating-privileges/
│   ├── accessing-credentials/
│   ├── moving-laterally/
│   ├── establishing-persistence/
│   ├── evading-defenses/
│   └── achieving-impact/
├── src/bas/                # implementation
│   ├── models.py           # pydantic mirrors of BAS OpenAPI
│   ├── bas_client.py       # (Step 2) httpx client
│   ├── llm/                # (Step 3) provider abstraction
│   ├── tools/              # (Step 4) skill loader
│   ├── agents/             # (Step 5) LangGraph agents
│   └── orchestrator/       # (Step 6) push pipeline
└── runs/                   # per-run artefacts (gitignored)
```