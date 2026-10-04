# Threat model

watchpost holds credentials that can read most of the lab (SNMP v3 users,
a WinRM account, an MQTT account) and exposes an inventory of what is
monitored. Those are the assets. Each control below is labelled
**enforced** (the code or container prevents it) or **advisory** (it depends
on how you deploy).

## Trust boundaries

1. **Config and secrets to process.** YAML is mounted read-only; secrets come
   from environment variables or files under `/run/secrets`.
2. **Process to monitored devices.** Outbound SNMP, WinRM, MQTT, HTTP, DNS,
   ICMP, and TCP. Devices are treated as untrusted data sources.
3. **Dashboard to browser.** Inbound HTTP on 8080.
4. **Process to alert targets.** Outbound to ntfy, webhook, SMTP, MQTT.

## Controls

| Control | Status | Notes |
|---|---|---|
| No write endpoints for monitors, alerts, or credentials | enforced | Monitors, alerts, and credentials can only change by editing YAML and restarting. A dashboard compromise can read inventory, not mute alerts. The earlier blanket rule is superseded: the login, admin, and ingest surfaces below do write, and are listed separately. |
| Unresolved secret reference fails startup | enforced | Prevents silently running with an empty community or password. |
| Unknown config keys rejected | enforced | Typos such as `verfy_tls: false` fail loudly instead of being ignored. |
| Credential type must match monitor type | enforced | A WinRM secret can not be sent as an SNMP community by mistake. |
| TLS validation on by default (HTTP, TLS cert, WinRM, MQTT TLS) | enforced | Turning it off is an explicit per-monitor setting. |
| Device-supplied text rendered as text | enforced | `app.js` uses `textContent` only; CSP forbids inline script and third-party origins. Covers hostile SNMP strings, MQTT payloads, HTTP error text. |
| PowerShell injection from config values | enforced | Values are embedded as single-quoted literals with quotes doubled; `disk` is pattern-validated. Tested in `test_windows.py`. |
| Secrets redacted from SNMP error text | enforced | Tested with a wrong community. |
| Container: non-root UID 10001, all capabilities dropped, read-only root, no-new-privileges | enforced by compose | Only if you run it with the provided compose file. |
| Unprivileged ICMP instead of CAP_NET_RAW | enforced by compose | Via the `net.ipv4.ping_group_range` sysctl, namespaced to the container. |
| Dashboard basic auth | advisory | Off unless configured. Constant-time comparison. Basic auth over plain HTTP is readable on the wire; put a TLS reverse proxy in front or bind to a management VLAN. |
| Dependency suppression can hide a real outage | advisory | A wrong `depends_on` (a server marked behind a switch it does not use) suppresses that server's alerts whenever the switch is down. Suppression is always recorded in the event log with the blocking monitor's name, and cycles and unknown parents are rejected at load. Review dependencies like firewall rules. |
| Network exposure of 8080 | advisory | Compose publishes on all interfaces by default; bind to one address if needed. |

## Ingest, logins, and control

These rows describe the hostwatch ingest and login work in
`docs/ARCHITECTURE.md`. **planned** means the design is decided and the
control is not yet verified or, for the control channel, not built at all.
The owner reversed the earlier read-only-by-design decision, so these
surfaces write.

| Control | Status | Notes |
|---|---|---|
| Ingest keys bound to one host | planned | A key is valid only for the host name it was created for, shown once, stored hashed, revocable. A stolen key can impersonate only its own host. |
| Ingest input validated | schema built, endpoint planned | The wire models in `watchpost/ingest/schema.py` reject unknown fields and schema versions and cap list sizes, string lengths and event detail. The body size cap and the rule that nothing is stored from a failed request belong to the endpoint, which is not built yet. |
| Unconfirmed pushed hosts never alert | planned | A new host stays pending until an admin confirms it, so an unknown sender cannot create alerts. |
| Sessions | planned | Random identifiers stored hashed, HttpOnly, Secure, SameSite=Strict cookie, idle and absolute expiry, Argon2id passwords, login rate limiting. |
| CSRF protection | planned | A per-session token is required on every state-changing request, in addition to SameSite. |
| Admin role | planned | Only admins manage users and keys and confirm hosts. Viewers are read-only. The role is checked on the server for each request. |
| Audit log | planned | Append-only from the application's side; records logins, failures, key and user changes, confirmations, and rejected ingest. Secrets are never written. Anyone who can edit the database file can still alter it, so protect `./data`. |
| Versioned store | built, tables not yet used | Migrations are additive, guarded, and transactional per step. A database written by a newer version is refused rather than opened. The audit table has its own retention, `server.audit_retention_days`, separate from poll retention. The tables for keys, users, sessions and audit exist but nothing writes them until the later slices land. |
| Future control channel | planned, not built | Agent-side allowlist, actions signed by watchpost, admin only, per-action confirmation, typed host name for reboot, all audited. A compromised watchpost could request any allowlisted action on every host, which is why the allowlist lives on the agent and the signing key is separate from ingest keys. No action that changes a host is built in the current phase. |

## Discovery

Discovery sends credentials to addresses it has not seen before, which is a
different risk from polling known hosts. Controls:

| Control | Status | Notes |
|---|---|---|
| Only named credentials are tried, in the order listed | enforced | `discovery.credentials` must reference existing entries; nothing else is sent. |
| WinRM credentials only after the listener's certificate validates for the host's DNS name | enforced | A rogue or compromised host inside the range could otherwise run a WinRM listener to collect an NTLM exchange for offline cracking or relay. With validation, it would also need a certificate your CA issued for that name. `winrm_require_valid_tls: false` turns this off; do not, unless the range is fully trusted. |
| Rejected WinRM credential retired after `max_auth_failures` | enforced | Attempts with a credential are serialised until it succeeds once, so parallel scanning can not overshoot the limit (tested with 12 concurrent hosts). Keeps a wrong password from walking a domain account into lockout. |
| Target size capped (`max_hosts`) and computed before expansion | enforced | |
| Discovery never edits the running config | enforced | Output is a proposal file for review. |
| SNMP v2c communities are sent in cleartext to every address in range | advisory | Inherent to v2c. Prefer v3 credentials for discovery, list v2c last, or scope v2c discovery to the device subnet that needs it. |
| SNMP v3 probes to unknown hosts | advisory | A listener can capture the authenticated request and attempt an offline guess of the auth passphrase. Use long random passphrases. |
| SSH host keys verified for every monitor | enforced | No monitor-level option disables it. Agent use and agent forwarding are off, so a compromised host can not use keys from the watchpost machine. |
| Discovery trust-on-first-use offers key credentials only | enforced | A public-key signature is bound to the session and gives an impostor nothing reusable; a password would. Password credentials are only used against hosts whose key is already trusted. First-seen keys are listed for review, not silently added. |
| Changed or unparseable known_hosts entry stops SSH for that host | enforced | asyncssh skips malformed lines silently; discovery checks the file text so a corrupted entry is not mistaken for no entry. |
| Rejected SSH credential retired after `max_auth_failures` | enforced | Same serialisation as WinRM. Protects against account lockout and fail2ban bans of the watchpost host. |
| Shell injection through config values | enforced | Mount, unit, container, and docker command are pattern-validated at load and shlex-quoted when sent. Tested with metacharacters in each. The `command` field of `linux` monitors is run as written: it is code you chose to run, like a cron entry. |
| Docker checks read no container config | enforced | Only State and RestartCount are requested, so environment variables (often secrets) never cross the wire. |
| LDAPS only, chain and hostname validated | enforced | ldap3 `Tls(validate=CERT_REQUIRED)` with its post-handshake hostname check. Simple bind inside TLS; no plain LDAP option. |
| Docker access is root-equivalent on the host | advisory | Anyone who can run `docker` can start a privileged container. Instead of adding the account to the `docker` group, grant only what watchpost runs and set `docker_command: "sudo -n docker"`: `watchpost ALL=(root) NOPASSWD: /usr/bin/docker ps *, /usr/bin/docker inspect *`. Sudo argument wildcards match across spaces, so this still allows any `ps` or `inspect` arguments; both are read-only, but `inspect` without `--format` can show container environment variables. |
| Directory account scope | advisory | Use a dedicated account with no rights beyond default authenticated read. It can read most of the directory by default; that is AD's model, not something watchpost can narrow. |
| TrueNAS, Proxmox, vSphere credentials only over validated TLS during discovery | enforced | `api_require_valid_tls` (default on). An appliance's self-signed certificate is captured with its fingerprint for you to verify and pin; nothing is sent until then. Monitors verify by default too. |
| vSphere unauthenticated fingerprint | enforced | Discovery identifies ESXi/vCenter from the public `/sdk/vimServiceVersions.xml` before any login, so vSphere passwords are never tried against non-vSphere hosts. |
| vSphere sessions are closed every poll | enforced | Login and logout in a `finally`; leaked sessions would exhaust ESXi's session limit. |
| Plain-HTTP tokens (Home Assistant, Technitium) | advisory | Both default to HTTP. A monitor with `https: false` sends its bearer token in cleartext on every poll, readable by anything on the path. Discovery refuses unless `api_require_valid_tls: false`. Prefer TLS on both; otherwise keep the path on a trusted segment and scope each token to a read-only user. |
| Technitium token kept out of URLs | enforced | Sent as a Bearer header by default; the legacy `?token=` form (which ends up in access logs) is opt-in per monitor. |
| API keys reach only hosts you vouched for, not only the intended product | advisory | With validated TLS, a credential is offered to a host whose identity your CA or a pinned certificate vouches for. That proves who the host is, not what it runs: a UniFi key could be offered to your TrueNAS box during discovery. vSphere is fingerprinted unauthenticated first; TrueNAS keys are sent only after a WebSocket upgrade at /api/current succeeds. Scope discovery targets if this matters. |
| Least-privilege platform accounts | advisory | TrueNAS: a dedicated user with READONLY_ADMIN. Proxmox: a user with PVEAuditor at `/` and a token (tokens can be revoked without touching the user). vSphere: a local or SSO user with the built-in Read-only role. Home Assistant: a dedicated non-admin user's token. UniFi: a key created by a view-only admin. Technitium: a user with only Dashboard: View. None of the checks write anything. |
| Scanning can trip IDS/IPS and host firewalls | advisory | Run it against ranges you own, and expect alerts if you monitor for scans. |

## Accepted risks

**SNMP secrets in process arguments.** The net-snmp tools take the community
or v3 passphrases as argv, readable from `/proc/<pid>/cmdline` for the life of
each poll by anything in the same PID namespace. The container runs only
watchpost, and host root can already read the container's environment and
memory, so this does not add an attacker class. Reconsider if you run other
processes in the container.

**Secrets in memory and environment.** Resolved secrets live in the process
for its lifetime. Use `${file:...}` references to keep them out of
`docker inspect` output, which shows environment variables.

**Kerberos keytab is a long-lived bearer secret.** A keytab lets its holder
authenticate as the account indefinitely, until the account's password is
rotated (which invalidates every keytab derived from it, unlike a leaked
password that can be changed while other holders keep working from other
credentials). It never touches `${ENV}`/`docker inspect`: `keytab_path`
points at a file under `./secrets`, read directly by `kinit`. The acquired
ticket is cached in the container's `/tmp` tmpfs, so it does not survive a
restart and is never written to the read-only root filesystem or to `./data`.
Treat the keytab file itself exactly like a private key: readable only by
the account that runs watchpost, and rotated (regenerate with `ktpass`, no
disable/re-enable of the account needed) on the same schedule you'd rotate a
WinRM password.

**SQLite history is plaintext.** It holds hostnames, check messages, and
values, not credentials. Protect `./data` like the config.

**Alert payloads leave the lab.** A public ntfy topic or webhook receives
monitor names and messages. Use a self-hosted or authenticated target if that
matters.

## Least privilege for monitoring accounts (advisory)

- SNMP: a v3 user with a read-only view scoped to the MIB subtrees you poll.
- WinRM: a dedicated domain account, not an administrator, granted remote
  management access and CIM read rights on the target classes, and denied
  interactive logon. Certificate mapping removes the password entirely; so
  does Kerberos (`transport: kerberos`), at the cost of holding a keytab
  instead (see "Kerberos keytab is a long-lived bearer secret" above). Not a
  gMSA: gMSA passwords are retrievable only by domain-joined Windows
  computer accounts, which a container is not.
- MQTT: an account with ACLs limited to subscribing to the topics you check
  and publishing under the alert `topic_prefix`.
