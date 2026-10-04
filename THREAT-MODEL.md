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
| Dashboard basic auth | advisory | Off unless configured. Constant-time comparison. By owner decision it opens only the read-only API and `/metrics`; it is never accepted for admin, user, ingest-key or future action routes, which need a session login with CSRF (tested against every admin route). Basic auth over plain HTTP is readable on the wire; put a TLS reverse proxy in front or bind to a management VLAN. |
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
| Ingest keys bound to one host | built, used by `POST /api/ingest` | A key is valid only for the host name it was created for, shown once, stored as a SHA-256 digest of 256 random bits, and revocable. Verification compares in constant time, treats an unknown key like a wrong one, and records last use only on success. The endpoint answers 401 for a bad or revoked key and 403 for a good key presented for another host, and stores nothing in either case. The `wpi_` marker is the ingest scope, and no other surface accepts these keys. A stolen key can impersonate only its own host. Keys are created and revoked from the admin screen (`/admin`, admin session and CSRF) or the command line. The new key appears once, in the create response, which is sent with `Cache-Control: no-store`; the key list never contains a secret. |
| Ingest input validated | built | The endpoint reads no body until the key is valid, caps the body at 1 MiB, and validates against the strict wire models, which reject unknown fields and schema versions and cap list sizes, string lengths and event detail. A failed request stores nothing. A boot kind the server does not recognise is classified unknown, never clean. Replay by `batch_id` is idempotent. Residual risk: a holder of a valid key can still report false readings or false boot events for its own host. |
| Ingest rate limit and denial logging | built | A fixed-window limit per peer address (`server.ingest_rate_per_minute`, default 120) returns 429. Denied requests are audited at most once per peer per minute with a count, and at most 4096 peers are tracked, so a scanner cannot grow the database or memory without bound. The peer is the socket address, so behind a reverse proxy every client shares one bucket; put the limit in the proxy as well. The audit row holds the key's public prefix, never the key. |
| Unconfirmed pushed hosts never alert | planned | A new host stays pending until an admin confirms it, so an unknown sender cannot create alerts. |
| Sessions | built | Passwords are Argon2id hashes (argon2-cffi). A login makes a 256-bit random session identifier, stored only as a SHA-256 digest, sent in an HttpOnly, Secure, SameSite=Strict cookie. Sessions end at an idle limit (`session_idle_s`, 30 minutes) and an absolute limit (`session_absolute_s`, 12 hours), and a session is revoked once seen expired, on logout, or when its user is disabled. An unknown, locked or disabled account costs one dummy hash check and gets the same 401 body as a wrong password. An account locks after `login_max_failures` failures for `login_lock_s`, and a correct password does not unlock it early. Logins are also limited per peer (`login_rate_per_minute`), and failures are audited at most once per peer per minute. Residual risks: the lock lets someone who knows a username keep that account locked out; the peer is the socket address, so behind a proxy every client shares one bucket; `session_cookie_secure` can be turned off for plain HTTP, which exposes the cookie on the wire. |
| CSRF protection | built | The token is an HMAC of the session identifier, so it exists only for the holder of the session and a leaked database does not reveal it. Every state-changing route that needs a session (`/api/logout`, `/api/admin/*`) requires it in `X-CSRF-Token`, compared in constant time, in addition to SameSite=Strict. A token from another session is refused. Residual risk: `POST /api/login` has no token because there is no session yet; SameSite=Strict and the JSON-only body limit login CSRF, and the worst case is signing a victim in as the attacker. |
| Admin role | built, minimal surface | `users.is_admin` is read from the database on every request, never from the cookie. The admin routes (`/api/admin/users`, user disable and role, and `/api/admin/keys` with revoke) all use the same dependencies; confirmation and future action routes will too. A non-admin gets 403, no session gets 401, and the tests drive every admin POST without a token. The last active admin cannot be disabled or demoted: the check and the change are one SQL statement, so two requests cannot both remove it. Disabling a user ends their sessions on the next request. An admin can still grant admin to another account, so the admin role is as sensitive as the database. The first admin is created from the command line with `--create-admin`, which reads the password from `WATCHPOST_ADMIN_PASSWORD` or a prompt and never from an argument. |
| Pushed hosts as confirmed monitors | built | A host is a monitor only when it is listed in the YAML, so a valid ingest key alone cannot add a monitor or page anyone. Its status goes through the same confirmation counts as every other monitor, so one bad batch does not page. Silence is treated as failure: no batch within `stale_after` fails the check, so a host that stops pushing, or a key holder who stops reporting, shows as down. Residual risk: a key holder can report healthy readings for its own host and keep it green, and can report false Critical readings to page. The key limits that to one host. Only the YAML sets component thresholds, never the agent. |
| Audit log | partly built | Rejected ingest (`ingest_denied`), `login_ok`, `login_failed`, `logout`, `user_created`, `key_created`, `key_revoked`, `user_disabled`, `user_enabled`, `user_promoted` and `user_demoted` are written now, plus a `*_failed` or `*_error` row when an action stops partway (`login_error`, `user_create_failed`, `user_create_error`, `key_create_failed`, `key_revoke_failed`, `user_change_failed`, `user_change_error`, `ingest_failed`). Every row goes through `audit.record`, which sanitizes the path and redacts detail fields named like secrets; passwords, tokens, keys and attempted passwords are never written, and a failed login records the account name only when the account exists. `GET /api/audit` is admin only and needs a session, so basic auth cannot read it. Confirmations are planned, because no route exists for them yet. Changes made on the admin screen are recorded with the signed-in admin as actor. Changes made with the command line are recorded with the actor `cli`, which proves only that someone had shell access. The redaction is name based, so a caller that put a secret under an innocent field name would still leak it; the tests check each writer. Append-only from the application's side. Anyone who can edit the database file can still alter it, so protect `./data`. |
| Versioned store | built, partly used | Migrations are additive, guarded, and transactional per step. A database written by a newer version is refused rather than opened. The audit table has its own retention, `server.audit_retention_days`, separate from poll retention. Host data, ingest keys, batch ids and rejected-ingest audit rows are written now; users, sessions and login audit rows are written by the login routes. Expired sessions are pruned with poll retention. |
| Per-host hardware views | built | `GET /api/hosts`, `GET /api/hosts/{host}` and the `/host` page are read-only. The two API routes need a login session, and basic auth does not open them, because they expose hardware inventory, disk serials and boot history. The `/host` page itself is a static file with no data, like `/login`, and sends a visitor without a session to the login page. Agent text (labels, reasons, event titles) is written with `textContent` only, so hostile text renders as text under the existing CSP. A stale, failed or missing source is labelled as such and never shown as healthy or as zero, so a key holder cannot hide a silent host by sending nothing; a key holder can still send false readings for its own host. Residual risk: a host that is not listed in the YAML still appears here, marked as not monitored, so a valid key reveals that host name to every logged-in user. |
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
