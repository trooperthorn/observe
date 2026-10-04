# Architecture: hostwatch ingest, logins, and the future control phase

The owner reversed the earlier decision that watchpost is read-only by
design. watchpost is becoming the single monitoring UI and, later, the
control plane for the hosts it watches, replacing the hostwatch hub and web
view. This document describes the target design in phases. Sections marked
**built in this phase** are being implemented now; sections marked
**planned** describe a later phase, and nothing for them exists in code. No
action that changes a host is built in this phase.

## Phases

| Phase | Scope | State |
|---|---|---|
| 1 | Versioned store, ingest, host-bound keys, boot and crash events, pushed hosts as monitors, logins, audit log, host views, admin screen | built in this phase |
| 2 | Agent-side control service and signed actions | planned |

## Ingest

Hosts run the hostwatch agent, which pushes a JSON snapshot over HTTPS to
watchpost. The wire schema is the one hostwatch already defines in
`hostwatch/schema.py`; watchpost carries its own copy, adapted with an
attribution comment, and never imports hostwatch. Each push carries a host
identity, a boot identifier, an uptime, and hardware readings.

The ingest endpoint is a write path that does not use a login, so it is the
most constrained one. A request must carry an ingest key. The body size is
capped, the schema is validated strictly with unknown fields rejected, and a
request that fails validation is dropped and counted, never stored in part.

The models live in `watchpost/ingest/schema.py` (Batch, Sample, SourceStatus,
Event). Field names and types match hostwatch, so a well-formed agent needs no
change. watchpost tightens them: unknown fields and unknown `schema_version`
values are validation errors, and each batch is limited to 256 sources, 5000
samples and 500 events, with bounded string lengths, 32 labels per sample, and
event detail of at most 64 keys and 8192 bytes of JSON. Non-finite numbers are
rejected. `MAX_BODY_BYTES` (1 MiB) is defined there and enforced by the
endpoint.

The endpoint is `POST /api/ingest` in `watchpost/ingest/api.py`. It checks, in
this order: a per-peer rate limit (429), a valid unrevoked bearer key (401,
before the body is read), the body size cap (413), the key's bound host against
the host in the body (403), and the strict schema (422). Nothing is stored from
a request that fails a check. A valid batch is written in one transaction by
`Store.ingest_batch`: the host row, samples, source status and events. A
`batch_id` is recorded per host in `ingest_batches` (schema version 4), so an
agent that replays its outbox gets `duplicate: true` and nothing is stored a
second time. Events are kept once per host and `dedup_key`. A source reported
with `present: false` is stored as unavailable with the reason "not present on
this host", because the version 2 table has no separate present column.

Denied requests are written to the audit log as `ingest_denied`, through an
aggregator adapted from hostwatch's hub: at most one row per peer per minute,
carrying the number of denials it covers, with at most 4096 peers tracked. The
row holds the key's public prefix and never the key. The rate limit is a fixed
window per peer address, set by `server.ingest_rate_per_minute`. The peer is the
socket address; forwarded headers are not trusted. The route does not use the
dashboard basic auth, because agents authenticate with their own key.

## Storage

Pushed data lives in the existing SQLite database, behind a versioned schema.
A `schema_version` table records the applied version. At startup the store
applies each missing migration in order, one transaction per step, and rolls a
failed step back. Every step is additive and guarded with `IF NOT EXISTS`, so
rerunning one changes nothing. A database created before versioning existed
holds only `results` and `events`; it is treated as version 1 and keeps all its
rows. A database with a newer version than the code supports raises
`SchemaTooNewError` and is left untouched.

Version 2 adds `hosts`, `host_samples`, `host_sources` and `host_events`.
Version 3 adds `ingest_keys`, `users`, `sessions` and `audit`, and version 4 adds
`ingest_batches`. Existing history
tables are untouched. The layout is adapted from hostwatch's `store.py`.

Retention is applied by `Store.prune`. Poll results and host samples are
dropped after `server.retention_days`. Transitions and host events are kept for
at least a year. The audit log has its own setting,
`server.audit_retention_days` (default 365), so shortening poll retention never
shortens the audit trail. Expired sessions are deleted in the same pass.

## Host-bound keys

An ingest key is created by an admin for exactly one host name. The key is
shown once, and only a hash of it is stored. A push is accepted only when the
key's bound host name matches the host name in the body. A key stolen from
one host therefore cannot report as another, and revoking a key stops that
host without affecting any other. A host that pushes with a valid key but has
no confirmed monitor appears as pending until an admin confirms it.

The key format is `wpi_<prefix>_<secret>`. The prefix is public and is the
key's id in the `ingest_keys` table and in listings. The secret is 256 random
bits, and only its SHA-256 digest is stored. A fast digest is adequate for a
random secret, unlike a user password, which uses argon2. Lookup is by
prefix, the digest and host name are compared in constant time, and a
revoked key fails the same way a wrong one does. Last use is recorded only
after a successful check. The code is `watchpost/ingest/keys.py`. Keys are managed on the admin screen,
and also by `--ingest-key-create`, `--ingest-key-list` and
`--ingest-key-revoke` from the command line.

## Boot and crash events

The agent gathers the evidence (heartbeat, pstore, watchdog status, previous
boot journal) and sends one `boot.<kind>` event per detected reboot. The
classifier in `watchpost/ingest/boot.py`, adapted from hostwatch, reduces the
kind to clean (`clean_shutdown`), crash (`kernel_panic`, `watchdog_reset`,
`power_loss`, `unknown_unclean`, `unclean_shutdown`) or unknown (anything else,
including `agent_stopped` and kinds this version has not seen). It never
upgrades unknown to clean. The result is stored in the event detail as
`classification`, and the host row keeps the newest boot id and a
`clean_shutdown` flag (1 clean, 0 crash, null unknown). An older event never
overwrites a newer one. Showing events in the event log and on the host view
is a later slice.

## Status integration

A pushed host becomes a monitor of type `pushed_host` when it is listed in the
YAML. Listing it is the confirmation; the `hosts.confirmed` column is reserved
for the admin screen's confirm action in a later slice. The check
(`watchpost/checks/host.py`) takes its state from freshness and readings
rather than from a poll. It reads the newest sample per source, metric and
label set through `Store.latest_host`, grades each configured component Good,
Warning or Critical, and returns OK, WARN or FAIL for the worst one. Those go
through `MonitorState.observe` like any other result, so `failures_to_down`
confirmation applies before a host is DOWN or pages. No batch within
`stale_after` seconds (default three intervals), or none ever, is FAIL.
Because the result is an ordinary check result, `group`, `depends_on`,
`critical`, rollup, alerts and `/metrics` work unchanged, and `/metrics` adds
`watchpost_host_age_seconds` and `watchpost_host_component_state`. The grouped
summary adapted from hostwatch's `integrations/summary.py` is the host views section below.

## Host views

Each pushed host has a page at `/host?name=HOST`, linked from the dashboard
row of its `pushed_host` monitor. It is served by two routes, `GET /api/hosts`
(one summary row per host) and `GET /api/hosts/{host}` (the full document), both
built by `watchpost/hostview.py` from the newest sample per series in the store.
The sections are CPU, memory, power, temperatures, fans with the fan controller
state, RAID, ZFS pools, disks, UPS, alerts and events, plus the boot state and
the list of sources. Each section and each reading carries Good, Warning or
Critical. Built-in limits live in `hostview.py`; thresholds on the monitor in the
YAML override them, and the agent never sets any.

Missing data is shown, not hidden. Each section has a `state`: `ok`, `stale`
(no reading inside the stale window, or the host is silent), `unavailable` (a
source reported a failure, with its reason), `absent` (the agent says the host
has no such hardware) or `not_reported` (no source for it ever reported). Stale
and unavailable make the section at least Warning; absent and not_reported claim
nothing and stay Good. A reading with no value is a Warning and never zero. A
host with no batch inside its stale window is Critical, matching the monitor.
The stale window is the monitor's `stale_after`, or three default intervals for
a host that is not listed. The routes need a login session and ignore basic auth.
Views are read-only. Device-supplied text is rendered as text only, as in the
rest of the dashboard. The page has no actions section yet; phase 2 adds one.

## Logins, sessions, and CSRF

Users sign in with a password hashed by Argon2id (argon2-cffi). A successful
login creates a server-side session whose identifier is random, stored only
as a hash, sent in a cookie marked HttpOnly, Secure, and SameSite=Strict, and
expiring after idle and absolute limits. Every state-changing request needs
a CSRF token tied to the session, in addition to SameSite. Failed logins are
rate limited per account and per source address and are recorded.

Two roles exist. A **viewer** can read dashboards and host views. An
**admin** can also manage users and ingest keys and confirm pushed hosts.
The existing optional basic auth is kept, by owner decision, for the read-only
API and `/metrics` only. It can never reach admin, ingest-key, user or future
action routes, which require a session login with CSRF.

Implemented in `watchpost/auth.py` and the routes in `watchpost/web.py`. The
session identifier and CSRF token are never stored as plaintext: the table
holds a SHA-256 digest of the identifier, and the CSRF token is an HMAC of the
identifier, recomputed on each request. Four FastAPI dependencies enforce the
boundary: `session` (any user), `mutating` (session plus CSRF), `admin`, and
`admin_mutating`. None of them looks at the Authorization header, so basic
auth credentials get 401 there. Read routes accept a session or, when
configured, basic auth. Login is `POST /api/login` (JSON), logout is
`POST /api/logout`, and `GET /api/session` returns the current user and token.
The first admin is created from the command line (`--create-admin`). Failed
logins lock the account (`login_max_failures`, `login_lock_s`) and are limited
per peer, with an unknown account taking the same time and answer as a wrong
password. Settings live under `server:` in the config.

## Audit log

Every security-relevant event is appended to an audit table: logins and
failures, logouts, user and key creation and revocation, role changes, host
confirmation, and rejected ingest attempts. Each row records time, actor,
source address, action, target, and outcome. The application never updates
or deletes audit rows, and secrets are never written to it. Admins can read
it from the admin screen.

Built in `watchpost/audit.py`. Every writer calls `audit.record`, which
sanitizes the path (control characters become `?`, 256 characters at most,
adapted from hostwatch's `sanitize_audit_path`) and replaces the value of any
detail field whose name suggests a secret, such as password, token, csrf or
hash. Written now: `login_ok`, `login_failed` (aggregated per peer),
`logout`, `user_created`, `user_disabled`, `user_enabled`, `user_promoted`,
`user_demoted`, `key_created`, `key_revoked`, and `ingest_denied`.
An action that stops partway also leaves a row: `login_error` (right password,
no session), `user_create_failed` and `user_create_error`, `key_create_failed`,
`key_revoke_failed`, `user_change_failed` and `user_change_error`, and `ingest_failed` (a valid batch the store could not
write). Admin screen changes are recorded with the signed-in admin as actor, and
CLI key and user changes with the actor `cli`. Host confirmation has no route
yet, so it has no rows yet. `GET /api/audit` returns rows newest first and is admin only,
session only, so basic auth never reaches it. It takes `limit` (1 to 500),
`kind`, and `before` (a row id, to page backwards).

## Admin screen

A single admin-only page, `/admin` (`static/admin.html` and `admin.js`), lists
users and ingest keys, creates and revokes keys, creates users, disables or
enables them, grants or removes the admin role, and shows the latest audit
rows. It is the first write surface in watchpost's web UI, which is why every
route behind it sits behind the login, role, and CSRF dependencies.

The page is a static file with no data, so a visitor without a session is sent
to `/login` by the script. The routes are `GET` and `POST /api/admin/users`,
`POST /api/admin/users/{id}/disabled` and `/admin` (body `{"value": bool}`),
`GET` and `POST /api/admin/keys`, and `POST /api/admin/keys/{id}/revoke`. A
created key is returned once in the create response and is never listed. The
last active admin cannot be disabled or demoted, enforced in one SQL
statement. Rendering uses `textContent` only. Host confirmation is not on the
screen yet, and no control changes a host.

## Phase 2 (planned): control

Nothing in this section is built. It records the intended design so that
phase 1 leaves the right seams.

- **Agent-side allowlist.** A small control service on each host accepts only
  actions in a fixed local allowlist. An action absent from that list cannot
  be run, whatever watchpost sends. There is no shell, no arbitrary command,
  and no argument that is not validated against the action's declared type.
- **Signed by watchpost.** Each action request is signed with a watchpost
  signing key, includes the target host, the action, its arguments, a nonce,
  and an expiry, and is verified by the agent before anything runs. The agent
  rejects replays and expired requests. The signing key is separate from
  ingest keys and from session secrets.
- **Admin only.** Only the admin role can request an action.
- **Per-action confirmation.** Each request needs an explicit confirmation
  step in the UI. A confirmation covers one action on one host and is never
  remembered.
- **Typed host name for reboot.** Rebooting a host additionally requires the
  admin to type that host's name exactly.
- **Audited.** Requests, confirmations, refusals, and results are all written
  to the audit log.

### Open seams

This interface sketch is for the docs only. No code exists for it.

```text
ControlAction
    name                       fixed identifier from the agent's allowlist
    arguments                  typed, validated by the agent, not by watchpost alone
    requires_typed_host_name   true for reboot
    describe()                 text shown on the confirmation step
    sign(host, nonce, expiry)  produces the signed request
    result                     ok, refused, or failed, with a message for the audit log
```

Other seams left open: a `signing_key` slot in settings that is unused in
phase 1, an `actions` section on the host view that stays empty, and an
audit action namespace (`control.*`) reserved for phase 2.
