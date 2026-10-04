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
after a successful check. The code is `watchpost/ingest/keys.py`. Until the
admin screen exists, `--ingest-key-create`, `--ingest-key-list` and
`--ingest-key-revoke` manage keys from the command line.

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

A confirmed pushed host becomes a monitor of type `pushed` that takes its
state from freshness and readings rather than from a poll. Missing pushes for
longer than the configured interval produce FAIL through the same state
machine, so confirmation counts, dependencies, groups, alerts, and
`/metrics` all work unchanged. Unconfirmed hosts never alert. The grouped
summary is adapted from hostwatch's `integrations/summary.py`.

## Host views

Each pushed host has a page showing current hardware readings (temperatures,
fans, disks, memory, load), recent history, and its boot and crash events.
Views are read-only. Device-supplied text is rendered as text only, as in
the rest of the dashboard.

## Logins, sessions, and CSRF

Users sign in with a password hashed by Argon2id (argon2-cffi). A successful
login creates a server-side session whose identifier is random, stored only
as a hash, sent in a cookie marked HttpOnly, Secure, and SameSite=Strict, and
expiring after idle and absolute limits. Every state-changing request needs
a CSRF token tied to the session, in addition to SameSite. Failed logins are
rate limited per account and per source address and are recorded.

Two roles exist. A **viewer** can read dashboards and host views. An
**admin** can also manage users and ingest keys and confirm pushed hosts.
The existing optional basic auth keeps working for deployments that do not
create users.

## Audit log

Every security-relevant event is appended to an audit table: logins and
failures, logouts, user and key creation and revocation, role changes, host
confirmation, and rejected ingest attempts. Each row records time, actor,
source address, action, target, and outcome. The application never updates
or deletes audit rows, and secrets are never written to it. Admins can read
it from the admin screen.

## Admin screen

A single admin-only page lists users and ingest keys, creates and revokes
them, and shows the audit log. It is the first write surface in watchpost's
web UI, which is why it sits behind login, role, and CSRF checks.

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
