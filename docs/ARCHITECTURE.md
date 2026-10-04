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
Version 3 adds `ingest_keys`, `users`, `sessions` and `audit`. Existing history
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

## Boot and crash events

Each snapshot carries a boot identifier. When it changes, watchpost records a
boot event. The classifier, adapted from hostwatch, then decides whether the
previous session ended cleanly (a shutdown marker was pushed) or not (a crash
or power loss), and records a crash event in the second case. Events appear in
the existing event log and on the host view.

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
