# Skill Template (Anthropic Agent Skills format)

A "skill" in this project is a **directory** under `skills/` that complies with
the Anthropic Agent Skills spec
([overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview),
[best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices))
**plus** a few custom YAML fields that our Orchestrator reads. Claude's native
runtime ignores unknown fields, so the same files work for both.

```
skills/
└── <gerund-name>/
    ├── SKILL.md          (concise navigational doc, ≤500 lines, ideally ≤300)
    └── reference/
        ├── techniques.md (full technique table + per-technique cards)
        └── tool-commands.md (templated YAML command blocks per tool)
```

## Naming rules (from the Agent Skills spec)

- Skill directory name **must equal** the `name` frontmatter field.
- Gerund preferred (`discovering-environment`, `escalating-privileges`).
- Lowercase letters, numbers, and hyphens only.
- ≤ 64 characters.
- Must NOT contain the tokens `anthropic` or `claude`.
- No Windows paths anywhere in the skill files — forward slashes only.

## SKILL.md template

````markdown
---
name: <gerund-name>                       # required — matches directory
description: >                            # required — third person, ≤1024 chars
  <WHAT this skill does in one sentence>. Use when <WHEN to invoke it: list
  the scenarios>. Covers <comma-separated technique families>.
# --- Custom orchestrator fields (ignored by Claude runtime) ---
stage: <discovery|privesc|credaccess|lateral|persistence|defevasion|impact>
agent: <SpecialistAgentClassName>
mitre_tactics: ["TA00xx"]
default_opsec: <stealth|moderate|loud>
ambient: false                            # true only for evading-defenses
tool_allowlist:
  - tool_a
  - tool_b
budget:
  max_tool_calls: 20
  max_wallclock_min: 20
  destructive_default: ask                # optional; for persistence + impact
---

# <Stage display name>

<One-paragraph mission statement. What does success look like? What memory
fields are populated?>

## Quick start

1. <First action — usually the cheapest / most informative technique>.
2. <Second action>.
3. <Branching condition>.
4. Pointer to reference files:
   Templated commands: [reference/tool-commands.md](reference/tool-commands.md).
   Full technique catalogue: [reference/techniques.md](reference/techniques.md).

## Preconditions

- <Typed-memory predicates that must hold before this skill is dispatched>.

## Stage goal (commit criteria)

Signal `success` only when ALL of these hold:

- <Required memory mutations>.
- <Minimum findings count + priority>.

## Critical techniques

| # | Technique | MITRE | Tools |
|---|---|---|---|
| <id> | <name> | T1xxx | `tool_a`, `tool_b` |

Important and Optional techniques in [reference/techniques.md](reference/techniques.md).

## Pivot conditions

The agent fills `agent_result.recommended_next` from the first rule that
matches. Orchestrator may override.

- <Condition> → `<next-skill-name>`
- <Condition> → `<next-skill-name>`

## Self-critique (run every N tool calls)

- "<Concrete failure mode and the corrective action>."
- "<Anti-pattern to avoid>."

## Evidence to capture

- <What goes to artifacts/>.
- <What goes to vault://>.
- <What gets summarised into findings[]>.
````

## reference/techniques.md template

````markdown
# <Stage> — Techniques reference

Full catalogue for the `<gerund-name>` skill.

## Contents

- Technique table (Critical → Important → Optional)
- Per-technique cards for Critical techniques
- Selection guidance

## Technique table

| # | Technique | Priority | MITRE | Primary tools | Pivot hint |
|---|---|---|---|---|---|
| <id> | <name> | Critical | Txxx | `tool_a`, `tool_b` | <next-skill> |

## <id> — <Critical technique name>

- MITRE: Txxx
- Tools: `tool_a`, `tool_b`
- Preconditions: <typed-memory predicates>.
- Success indicators: <observable parser outputs>.
- OPSEC: stealth | moderate | loud.
- Fallback: <when blocked, do this>.
- Pivot hint: → `<next-skill-name>`.
````

## reference/tool-commands.md template

````markdown
# <Stage> — Tool commands reference

Templated commands the `<SpecialistAgent>` renders and dispatches via the
Execution Layer. Placeholders:

- `{{ memory.path.dots }}` — resolved against typed session memory.
- `{{ artifact.path }}` — allocated by the ToolRunner per step.

## Contents
- `<tool_a>` (techniques <ids>)
- `<tool_b>` (techniques <ids>)

## `<tool_a>`  (<technique ids>)

```yaml
tool: tool_a
opsec: stealth                # stealth | moderate | loud | build-time
preconditions:
  - "<memory predicate>"
commands:
  - id: <command_id>
    cmd: |
      tool_a --flag {{ memory.field }} \
        --out {{ artifact.path }}/out.json
    parser: parsers.<parser_name>
    on_success:
      - "<memory mutation 1>"
      - "<memory mutation 2>"
    on_failure:
      fallback: <other_command_id>
    cleanup_cmd: "<exact reverse command>"   # required if destructive
    destructive_gate: true                   # if true, requires human ACK
```
````

## Authoring rules (recap)

1. **Description** in SKILL.md must say BOTH what the skill does AND when to
   invoke it. The Orchestrator's router uses this same string.
2. **Tool names only** — no embedded credentials or engagement-specific
   values.
3. **OPSEC tag on every command**. `loud` commands are dropped when the
   ambient `evading-defenses` skill reports `edr_hot == true`.
4. **Every technique has a fallback** (alternate tool or technique).
5. **Persistence + log-clearing commands include `cleanup_cmd`** (the exact
   reverse).
6. **References are one level deep** from SKILL.md. Reference files do not
   themselves link to further reference files.
7. **Reference files > 100 lines must include a Contents block** at the top.
