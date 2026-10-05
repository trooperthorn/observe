# Architecture: hostwatch ingest, logins, and the future control phase

The owner reversed the earlier decision that Observe is read-only by
design. Observe is becoming the single monitoring UI and, later, the
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
Observe. The wire schema is the one hostwatch already defines in
`hostwatch/schema.py`; Observe carries its own copy, adapted with an
attribution comment, and never imports hostwatch. Each push carries a host
identity, a boot identifier, an uptime, and hardware readings.

The ingest endpoint is a write path that does not use a login, so it is the
most constrained one. A request must carry an ingest key. The body size is
capped, the schema is validated, with unknown fields ignored as hostwatch ignores them, and a
request that fails validation is dropped and counted, never stored in part.

The models live in `observe/ingest/schema.py` (Batch, Sample, SourceStatus,
Event). Field names and types match hostwatch, so a well-formed agent needs no
change. Observe tightens them: unknown fields are ignored so a newer agent is not
dead-lettered, an unknown `schema_version` is a validation error, and each batch is limited to 256 sources, 5000
samples and 500 events, with bounded string lengths, 32 labels per sample, and
event detail of at most 64 keys and 8192 bytes of JSON. Non-finite numbers are
rejected. `MAX_BODY_BYTES` (1 MiB) is defined there and enforced by the
endpoint.

The endpoint is `POST /internal/v1/ingest`, the path unmodified hostwatch agents
use, with `POST /api/ingest` kept as an alias, in `observe/ingest/api.py`. It checks, in
this order: a per-peer rate limit (429), a valid unrevoked bearer key (401,
before the body is read), the body size cap (413), a JSON nesting limit of 32 checked before parsing (400), the key's bound host against
the host in the body (403), and the schema (422). hostwatch agents dead-letter
400 and 422 and keep retrying every other failure, so 422 is reserved for a
batch that is malformed or missing required fields, and the reason is logged. Nothing is stored from
a request that fails a check. A valid batch is written in one transaction by
`Store.ingest_batch`: the host row, samples, source status and events. A
`batch_id` is recorded per host in `ingest_batches` (schema version 4), so an
agent that replays its outbox gets `duplicate: true` and nothing is stored a
second time. A batch without `batch_id` is identified by a SHA-256 of its
content, so a resend is acknowledged the same way. Events are kept once per host and `dedup_key`. A source reported
with `present: false` is stored as unavailable with the reason "not present on
this host", because the version 2 table has no separate present column.
Time and order are guarded in `Store.ingest_batch`. Any sample, event or batch
`sent_at` more than `MAX_FUTURE_SKEW_S` (300 seconds) ahead of receive time is clamped to
receive time, so a bad agent clock can not mask later readings or freeze `boot_id` and
`clean_shutdown`. The host row's platform, agent version and heartbeat, and each source's
status, are only replaced by a batch whose `sent_at` is not older than the stored one, so a
replayed older batch leaves newer state alone.

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

Plugins keep their own version sequence. `plugin_schema` holds one row per plugin
with the highest migration applied. After the core steps, `Store` runs the
migrations of each listed plugin (`migrate_plugins`), one transaction per step,
rolling a failed step back. A plugin whose recorded version is newer than the
migrations in its code raises `PluginSchemaTooNewError` (a `SchemaTooNewError`),
and every plugin is checked before any is changed. A plugin that is not listed
is not touched: its tables and its `plugin_schema` row stay as they were, so
listing it again later picks up where it stopped. Observe never drops plugin
tables.

Version 2 adds `hosts`, `host_samples`, `host_sources` and `host_events`.
Version 3 adds `ingest_keys`, `users`, `sessions` and `audit`, and version 4 adds
`ingest_batches`, version 5 adds `plugin_schema`, and version 6 adds the
infrastructure tables (see "Infrastructure map core"). Version 9 adds the
`scope` column to `ingest_keys`; existing keys get `wpi`. Version 10 adds
`enrolments` (see "Host enrolment") and version 11 adds its `step_hash` and `reports` columns. Version 12 adds
the `allowlist_rev`, `allowlist_saved_at` and `reissued_at` columns of `enrolments` and the `host_tasks` table (see "Host
settings"). Version 13 adds `ui_layouts` (see "Dashboard layout"). A migration step may
be a function as well as a statement, so an `ALTER TABLE` can check first and
stay safe to run again. Existing history
tables are untouched. The layout is adapted from hostwatch's `store.py`.

Both retention settings must be at least 1 day; config validation rejects 0 and negative values.
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
after a successful check. The code is `observe/ingest/keys.py`. Keys are managed on the admin screen,
and also by `--ingest-key-create`, `--ingest-key-list` and
`--ingest-key-revoke` from the command line.

A key also has a scope. The marker is the scope (`wpi` for host ingest, `wpf`
for Pockethernet field reports) and the scope is stored in the `scope` column
too. `verify_key` and `key_host` take the scope the caller needs, and accept a
key only when its marker and its stored scope both equal it, so a key of one
scope cannot be used on another surface, even with its marker edited. For a
scope other than `wpi` the host column holds the device label. A plugin
registers a scope through its `key_scopes` hook, and the admin create route and
the command line issue only `wpi` or a scope of a listed plugin. The Pockethernet
plugin's helpers are in `plugins/pockethernet/observe_pockethernet/keys.py`,
and its report schema, `pockethernet.report` version 1, is in `schema.py` next to
it. `parse_report` checks size, nesting depth and JSON before validation, and the
model rejects unknown fields, SSH transcripts and script values, properties
outside the allowlist, over-long strings and lists, control characters and
non-finite numbers.

## Boot and crash events

The agent gathers the evidence (heartbeat, pstore, watchdog status, previous
boot journal) and sends one `boot.<kind>` event per detected reboot. The
classifier in `observe/ingest/boot.py`, adapted from hostwatch, reduces the
kind to clean (`clean_shutdown`), crash (`kernel_panic`, `watchdog_reset`,
`power_loss`, `unknown_unclean`, `unclean_shutdown`) or unknown (anything else,
including `agent_stopped` and kinds this version has not seen). It never
upgrades unknown to clean. The result is stored in the event detail as
`classification`, and the host row keeps the newest boot id and a
`clean_shutdown` flag (1 clean, 0 crash, null unknown). An older event never
overwrites a newer one.

Event severity is normalized when the batch is stored, ignoring case: `info`,
`warning` and `critical` are kept, `fatal`, `error`, `emerg`, `alert`, `panic`
and similar map to `critical`, and an unknown value maps to `warning`. The value
the agent sent is kept as `severity_raw` in the event detail whenever it
differs. A crash boot makes its `pushed_host` monitor WARN (or DOWN with
`crash_result: fail`) from the boot event time until `crash_hold_s` (default
3600 seconds) has passed, and the result goes through the ordinary
`failures_to_down` confirmation and alerting. A clean boot, an unknown boot or
a later clean boot does not alert. Showing events in the event log and on the host view
is a later slice.

## Status integration

A pushed host becomes a monitor of type `pushed_host` when it is listed in the
YAML. Listing it is the confirmation; the `hosts.confirmed` column is reserved
for the admin screen's confirm action in a later slice. The check
(`observe/checks/host.py`) takes its state from freshness and readings
rather than from a poll. It reads the newest sample per source, metric and
label set through `Store.latest_host`, grades each configured component Good,
Warning or Critical, and returns OK, WARN or FAIL for the worst one. Those go
through `MonitorState.observe` like any other result, so `failures_to_down`
confirmation applies before a host is DOWN or pages. No batch within
`stale_after` seconds (default three intervals), or none ever, is FAIL. A component whose newest sample is older than `stale_after` is graded stale and is also FAIL, so an outbox replay or a lagging agent clock can not read as healthy.
Because the result is an ordinary check result, `group`, `depends_on`,
`critical`, rollup, alerts and `/metrics` work unchanged, and `/metrics` adds
`observe_host_age_seconds` and `observe_host_component_state`. The grouped
summary adapted from hostwatch's `integrations/summary.py` is the host views section below.

## Host views

The host page at `/host` is a static page too: `host.js` and `host-control.js` are ES modules, the Control section is a card inside `<main>`, and both use the shared chip, dialog and toast modules. The dashboard at `/` is a static page whose module script (`app.js`) builds the KPI row, availability tiles and group cards in the browser from `/api/monitors`, `/api/events` and `/api/infra/findings`, using the shared chip and DOM modules.

Each pushed host has a page at `/host?name=HOST`, linked from the dashboard
row of its `pushed_host` monitor. It is served by two routes, `GET /api/hosts`
(one summary row per host) and `GET /api/hosts/{host:path}` (host names may contain slashes; the full document), both
built by `observe/hostview.py` from the newest sample per series in the store.
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

Implemented in `observe/auth.py` and the routes in `observe/web.py`. The
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

Built in `observe/audit.py`. Every writer calls `audit.record`, which
sanitizes the path (anything shaped like an ingest key or session token is replaced with `[redacted]`, control
characters become `?`, 256 characters at most,
adapted from hostwatch's `sanitize_audit_path`) and replaces the value of any
detail field whose name suggests a secret, such as password, token, csrf or
hash; string detail values get the same secret-shape redaction. Written now: `login_ok`, `login_failed` (aggregated per peer),
`logout`, `user_created`, `user_disabled`, `user_enabled`, `user_promoted`,
`user_demoted`, `key_created`, `key_revoked`, `ingest_denied`, and `port_property_custom` (a hand-entered custom port property).
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
enables them, grants or removes the admin role, and links to the audit page.
It is the first write surface in Observe's web UI, which is why every
route behind it sits behind the login, role, and CSRF dependencies.

The page is a static file with no data, so a visitor without a session is sent
to `/login` by the script. The routes are `GET` and `POST /api/admin/users`,
`POST /api/admin/users/{id}/disabled` and `/admin` (body `{"value": bool}`),
`GET` and `POST /api/admin/keys`, and `POST /api/admin/keys/{id}/revoke`. A
created key is returned once in the create response and is never listed. The
last active admin cannot be disabled or demoted, enforced in one SQL
statement. Rendering uses `textContent` only. Host confirmation is not on the
screen yet, and no control changes a host.

### Host enrolment

`observe/enrol.py` and the `enrolments` table (schema version 10) back the Add
host flow of `docs/GUI-DESIGN.md` section 3.10. `POST /api/hosts` (admin
session and CSRF) takes `name` (lower-case letters, digits and dashes, 1 to 63
characters), `platform` (`linux`, `truenas`, `windows` or `raspberry-pi`),
`agent`, `control` and an `allowlist` of `fans` (a header name, or an object
with `header` and `min_duty_limit` from 0 to 100, which thermalctl needs per
header for remote floors), `services` and `reboot`. Every entry is matched
against the same character set the control daemon accepts, so none can hold
shell syntax. Control is refused for Windows until thermal-control has a
Windows path (Q10), and an allowlist needs control. A name that is already
enrolled or already reporting is refused with 409.

The response carries the install command once. Its first line is a comment
that names the host and platform, for example `# Observe install for nas01
(TrueNAS). Run this on nas01 only.`, followed by a `curl ... | sudo sh` line
(a PowerShell `irm ... | iex` line for Windows) with the token in the path
(Q9). The token is `wpe_` plus 256 random bits, valid for 30 minutes and one
redemption (Q8). Only its SHA-256 digest is stored. Redeeming it, which is the
install script fetch, claims the row with one conditional `UPDATE`, so two
fetches cannot both win, and then mints the host-bound keys: a `wpi` key for
the agent and a `wpc` key for control. Until then no key exists. The function
is `enrol.redeem`, called by `GET /i/{token}`. That route needs no session: the token
is the credential. For `linux` and `raspberry-pi` it answers with the POSIX sh script
rendered by `observe/scripts.py`; `truenas` gets a POSIX sh script and `windows` a
PowerShell script (agent only, `text/plain`); a second fetch, an expired token or
garbage is 410. Control on TrueNAS or Windows, and control when no control plugin is
loaded, is 409, and a bad `pool` query is 400, all before the token is spent. A
TrueNAS command carries the pool as `?pool=NAME` (the create body's optional `pool`,
TrueNAS only, a ZFS-style name; default `Apps`). The fetch also
mints a step key (`wps_`), stored as a digest, which authenticates the script's progress
reports.

The script (see `docs/GUI-DESIGN.md`, "S11b notes") checks that it runs as root, that
the machine's short or fully qualified hostname equals the host name (printing both when
it does not), and that it is not the Observe host (a machine-id comparison and a
comparison of Observe's addresses, plus the address the console URL resolves to, against
the local ones). Only then does it change anything: the agent container, and for control
the `hostwatch-control` account, a venv install of `hostwatch[control]`, `control.toml`
and `control.env`, a sudoers file rendered by hostwatch's `render_sudoers` and checked
with `visudo -c`, and the systemd unit. Every value is validated server side and single
quoted. The TrueNAS and Windows scripts reuse the same guard text (the TrueNAS one is
cut from the Linux script, so they cannot drift) and the same step reports. TrueNAS
then checks that `/mnt/<pool>` exists, writes `/mnt/<pool>/hostwatch/agent.env` (0400)
and `compose.yaml` (no key in it, mirroring hostwatch's `deploy/truenas/compose.yaml`
with the journal group detected locally) and prints the manual step. Windows downloads
the hostwatch source archive to a temporary folder, runs its `deploy/windows/install.ps1`
with `-IngestKey` as a SecureString (after its `uninstall.ps1` when the service already
exists, which keeps the data folder), checks the service runs, and deletes the
folder. `POST /api/enrol/step` (Bearer step key, valid two hours after the fetch) stores
each step report, which `GET /api/hosts/{name}/enrolment` returns as `install`.

`GET /api/hosts/{name}/enrolment` (admin session) returns the state machine:
steps `script` (fetched), `data` (first batch, which is the `hosts` row),
`control` (the `wpc` key's first authenticated pull, from its last-use time)
and `ready`, each `done`, `waiting`, `skipped` or `expired`, with the time.
`state` is `waiting`, `script_fetched`, `first_data`, `control_pulled`, `ready`
or `expired`. A token that was never fetched reads as expired from 30 minutes
after creation. The route is registered before `/api/hosts/{host:path}`, which
would otherwise answer it.

`POST /api/hosts/{name}/enrolment/regenerate` (admin session and CSRF, body
`{"pool": ...}` for TrueNAS) is how the wizard recovers from an expired command.
`enrol.regenerate_enrolment` replaces the token digest, the creation time and the expiry of
an enrolment whose script has not been fetched, with one conditional `UPDATE`, and keeps
the stored platform, choices and allowlist. The old token stops working at once because
only the new digest is kept. It answers 404 for an unknown host or one whose script was
already fetched, and the response is `no-store` like the create response. The expiry
audit flag is reset, so a second expiry is audited again.

The wizard is `static/hosts-new.html` (route `GET /hosts/new`, a static page like the
other admin pages), `static/hosts-new.js` and the pure rules in `static/js/wizard-logic.js`,
with `static/css/wizard.css`. The step is kept in the URL hash (`#host`, `#agent`,
`#allowlist`, `#install`, `#live`). The install command and token are held in memory only:
they are never put in the URL, storage or a toast, so a reload on step 4 or 5 returns to
step 1. Progress is polled every 3 seconds while step 4 or 5 is showing and stops when the
host is ready or the command has expired.

Audit kinds: `enrol_created`, `enrol_create_failed`, `enrol_regenerated`, `enrol_regenerate_failed`, `enrol_fetched`,
`enrol_fetch_failed`, `enrol_expired` (one row per enrolment, written when
the expiry is first observed), `enrol_script_failed`, `enrol_step_refused` and
`enrol_install_problem` (a step reported failed or refused). The rows name the host, the actor and the
choices, never the token or a key. The audit redactor also recognises `wpc_`,
`wpe_`, `wps_` and `wpt_` shapes.

### Host settings

`observe/hosttasks.py`, `observe/taskscripts.py` and the `host_tasks` table (schema version 12)
back the host settings page (`GET /hosts/{name}/settings`, the static `host-settings.html`,
`host-settings.js`, `js/settings-logic.js` and `css/settings.css`, admin only like the wizard).

- **Reading.** `GET /api/hosts/{name}/settings` (admin session) returns the identity, the saved
  allowlist, `allowlist_status`, which of the update and cleanup commands the platform has, the
  number of active keys and the newest task with its state and step reports. It never returns a
  token or a key. A host that was not added through the console is readable (`enrolled: false`)
  so its keys can still be revoked or the host removed.
- **Saving.** `PUT /api/hosts/{name}/allowlist` (admin and CSRF) takes the allowlist and
  `confirmed: true`, validates it with the same rules as the create request
  (`enrol.parse_allowlist`), refuses an unchanged list (409) and stores it with a new
  `allowlist_rev`. When the install script was already fetched it also makes an update task and
  returns its command. Before that there is no host to update, so no command is made and the
  saved list goes into the install script. If only the command cannot be made, the list stays
  saved and the response carries `command_error`.
- **Tasks.** A task is a row in `host_tasks`: a host, a kind (`update` or `cleanup`), the platform,
  the allowlist snapshot, a digest of a single-use `wpt_` token (30 minutes, one fetch), a digest
  of the step key that the fetch mints and the script's reports. `POST /api/hosts/{name}/tasks`
  (admin and CSRF, `kind` and `confirmed: true`) makes one, which is how an expired update command
  is made again and how a cleanup command is made. `GET /t/{token}` serves the script. It follows
  `GET /i/{token}`: no session, rate limited per peer, a dry render with placeholder values before
  the token is spent, 410 for a used, expired or unknown token, `no-store`. A newer task of the
  same kind removes the older unfetched one, so only the newest command works. The scripts report
  through `POST /api/enrol/step`, which tries the install step keys first and then the task step
  keys. The task state is `waiting`, `fetched`, `done` (a `done` report), `failed` (a step failed
  or was refused) or `expired`.
- **Update script (Linux and Raspberry Pi).** Same three guards as the install script, then it
  checks that a control install is there, writes `control.toml.new` (with the local `machine_id`
  before the first table), renders the sudoers rules from it, checks them with `visudo -c`, and only
  then moves the file and the rules into place and restarts `hostwatch-control`. It carries no key
  and does not touch the keys.
- **Cleanup scripts (all four platforms).** The root guard, then a match guard in place of the
  hostname and Observe-host guards. The machine that holds a mistaken install is by definition not
  the machine with that name, so the cleanup runs where an install made for the named host is found
  (`HOSTWATCH_HOST_NAME=` in `agent.env`, or `host =` in `control.toml`, or the Windows
  `agent.env`, or `/mnt/*/hostwatch/agent.env` on TrueNAS) and refuses anywhere else before it
  changes anything. It removes the agent container and settings, the control unit, rules, files,
  folder and account, and keeps the data volumes and folders. It does not revoke keys.
- **Status.** `hosttasks.allowlist_status` derives `pending`, `written` and `applied`. The list is
  written when the install script was fetched after the save (and did not report a failure), or when
  an update script of the current revision reported `control_unit` ok (the service restart) with no
  failed or refused step in that task, because the daemon reads control.toml only when it starts.
  It is applied when the `wpc` key's last use is not older than that write. `none` is a host without control.
- **Reissue.** `POST /api/hosts/{name}/enrolment/reissue` (admin and CSRF, `confirmed: true`) is the
  regenerate that also works after the script was fetched. In one transaction it replaces the token,
  revokes every `wpi` and `wpc` key bound to the host, and clears the fetch, the step key, the
  reports and the key prefixes. It also removes unfetched tasks. The saved allowlist goes into the new
  script, and the progress counts data only from a batch after the reissue. A fetch that was already
  claimed when the reissue ran could still mint keys afterwards, which is a narrow race left
  open (see THREAT-MODEL.md).
- **Danger zone.** `POST /api/hosts/{name}/keys/revoke` and `POST /api/hosts/{name}/remove` need
  `confirm_host` equal to the host name. Revoke revokes the `wpi` and `wpc` keys of the host. Remove
  does that, then deletes the enrolment, its tasks and the host's stored rows (`hosts`,
  `host_samples`, `host_sources`, `host_events`, `ingest_batches`) in one transaction, and keeps the
  audit log and the control command history. A host listed in the Observe config is refused (409)
  because it would come back.
- **Audit kinds.** `host_allowlist_saved`, `host_allowlist_failed`, `host_task_created`,
  `host_task_failed`, `host_task_fetched`, `host_task_fetch_failed`, `host_task_script_failed`,
  `host_task_expired`, `enrol_reissued`, `enrol_reissue_failed`, `host_keys_revoked`,
  `host_keys_revoke_failed`, `host_removed` and `host_remove_failed`. They name the host, the
  actor, counts and a reason, never a token or a key.

`admin.js`, `audit.js`, `infra-admin.js` and `port.js` are ES modules that use the shared
`table.js`, `chips.js`, `dialog.js` and `toast.js` modules, plus `js/admin-ui.js` (card, button,
copy-to-clipboard and the "admin account needed" card) and `css/admin.css` (tokens only). The audit
log has its own static page, `/audit` (`audit.html`, `audit.js`), listed under Admin in the
navigation for admins. It uses the same `GET /api/audit` route, loads the newest 500 rows and
filters them in the browser by actor, kind, status group and time range. The page serves no data,
and a viewer who opens it sees an "admin account needed" notice while the API answers 403.

## Plugin host

`observe/plugins.py` loads plugins (design in `docs/FIELD-DATA.md`). A plugin
is a package that publishes a `Plugin` object under the entry point group
`observe.plugins`. Only names listed under `plugins:` in the config are
loaded; an installed plugin that is not listed is never imported. Startup stops
with a `PluginError` naming the plugin when a listed plugin is not installed,
its declared `core_versions` range (a PEP 440 specifier) does not contain this
release, two packages claim one name, its settings fail validation, or a hook
returns something malformed. `--validate` runs the same checks.

The hooks are `routers`, `key_scopes`, `config_model` (settings come from
`plugin_settings.<name>`), `migrations`, `pages`, `static_dir`, `nav_entries`,
`monitor_types` and `map_contribution`. `PluginBase` gives each an empty
default. Routers, the config section, migrations, pages, static files and the
navigation list are used now. Key scopes, monitor types and map contributions
are validated at load and applied by the later slices that build those features.
A settings section is validated by the plugin's own model under its own
`plugin_settings.<name>` key; the startup error names each bad field and the
reason but never the rejected value, which may be a secret.

Pages and static files appear only for listed plugins. A `PluginPage` path must
be `/plugins/<name>` or below it and not under `/plugins/<name>/static`, so a
plugin cannot replace a core page, and its file must exist. A page needs a
session like the other pages (or the admin role when `admin_only` is set); the
page holds no data and its script reads the plugin's API. `static_dir` is a
folder inside the plugin package, served read-only at `/plugins/<name>/static`
like the core's own `/static`. Both go through the core's security-headers
middleware, so they carry the same CSP, which forbids inline script. `tests/test_ui_static.py` enforces the rules of docs/GUI-DESIGN.md section 4.2 over every such file and asserts the header on every page route. Every page loads `/static/css/tokens.css`, `/static/css/base.css` and then `/static/app.css`. The tokens define light, system dark and manual dark colours as custom properties, and `js/theme.js` stores the Auto, Light or Dark choice in the browser's `localStorage`, never on the server. `tests/test_ui_tokens.py` checks the token blocks and the WCAG contrast.
Monitor type names must start with the plugin name and a dot, and key scope
markers are three to eight lower-case letters that may not be `wpi`.

The core mounts every plugin router under `/api/plugins/<name>/` and chooses
the dependencies itself (`create_app` in `observe/web.py`):

1. a per-peer rate limit (`server.plugin_rate_per_minute`, default 300). A route
   that takes a plugin key counts valid keys per key and per peer, and failed or
   missing keys per peer in a separate counter, so bad-key traffic cannot block a
   valid key;
2. a login session for `GET` and `HEAD`, and a session plus the CSRF token for
   every other method; a router may ask for `admin=True`, which also requires
   the admin role, and can never ask for less;
3. an audit middleware, so a plugin cannot skip it.

Only plain HTTP routes are accepted, so a mounted sub-application or websocket
cannot sit outside those dependencies. Basic auth never opens a plugin route.
Audit rows: `plugin_request` for every state-changing request from a session
(actor, method, path, status, plugin name), `plugin_denied` for 401, 403 and 429
answers (at most one per peer per minute, with a count), and `plugin_failed`
when a route raises. Reads that succeed are not audited, like the core read
routes. `GET /api/plugins` lists loaded plugins and the navigation entries the
caller may see. Each entry carries a `workspace` (`overview`, `hosts`, `network`, `reports` or
`admin`, default `network`) that says which group of the console navigation it sits under; any
other value stops startup.

A router may instead be key-authenticated: `PluginRouter(router, key_scope="wpf")`.
The scope must be one the plugin registered, and the router cannot also be
`admin`. The core then replaces the session check with a bearer key check for that
scope (`plugin_key` in `create_app`): rate limit first, then the key (401 with
`WWW-Authenticate: Bearer`), and the body is not read before the key passes. The
key prefix and bound device label are set on `request.state.plugin_key`. A
`public_prefix` of `/api/v1` or a path below it mounts the router there instead of
under `/api/plugins/<name>`, so a device can be configured with a stable path; the
audit middleware covers both. A key request that changes state is audited as
`plugin_request` with the key prefix as actor and the device in the detail, a
handler may add facts about the outcome through `request.state.audit_detail`, and
refusals are `plugin_denied`. Handlers reach the store and the wall clock through
`app.state.plugin_store` and `app.state.plugin_clock`. The optional `prune(store,
now)` hook lets a plugin apply its own retention; a scheduler hook calls it for
each listed plugin about once an hour. The key scope itself is built: see "Ingest
keys".

The Pockethernet pages load the core `components.css`, `admin.css`, the shared table and chip modules and their own `static/pockethernet.css`; the report list route also returns each report's cable verdict, read from the report's `cable_verdict` property row.

### Pockethernet upload

`plugins/pockethernet/observe_pockethernet/upload.py` serves
`POST /api/v1/field-reports` and `GET /api/v1/field-reports/ping` with the `wpf`
key scope. After the core's rate limit and key checks, an upload is read under a
256 KiB cap (413), `Content-Encoding` must be absent, `identity` or `gzip` (415),
and a gzip body is inflated by `zlib.decompressobj` with a maximum output of the cap
plus one byte, refused when it exceeds the cap or a ratio of 50 inflated bytes per
compressed byte (413, the ratio is checked above 16 KiB), and refused when truncated
or followed by more data (400). The schema then validates the report. The ping
route checks a key and returns the server time and the limits.

Reports are stored in the plugin's `field_reports` table (plugin schema versions 1 and 2),
keyed by `(source, report_id)` where the source is the key's device label, so one
phone's key cannot replace another phone's report. The row holds summary columns
and the exact inflated body. A higher revision replaces the row, an equal revision
is a duplicate and a lower one is ignored; the decision and write are one
transaction under the store lock. The answer is `200` with `result` of `accepted`,
`replaced`, `duplicate` or `ignored`.

Clock correction (`correct_clock`): a phone may send `X-Report-Sent-Ms`, its clock
at send time. When that differs from the server clock by more than 300 s, the whole
difference is added to the report time. Whatever the header says, a report time
more than 300 s in the future is set to now. Either case sets `clock_corrected`,
and both the corrected `taken_at_ms` and the phone's `reported_taken_at_ms` are
kept. A past time without the header is kept, because a queued report is old on
purpose.

Retention is `plugin_settings.pockethernet.evidence_retention_days` (default 365).
After that long without an update the body is set to null by the plugin's `prune`
hook and the summary row stays, so a late replay is still a duplicate.

### Pockethernet derivation

`derive.py` runs after an upload that is `accepted` or `replaced`. It picks the LLDP
(else CDP) neighbour that names a switch and a port, then calls `InfraService` to upsert
the switch, the port (role `access`) and the jack, to link jack and port with source
`field_report`, and to append each allowlisted property with the report id and
`recorded_by` set to `<key prefix>:<device>`. `upsert_link` closes the jack's earlier
link when the port changes, and `append_property` turns an unchanged value into a
`last_verified` bump. The time passed as `now` is the report's receive time, which the
store keeps as `updated_at`, so the live path and the rebuild write identical rows.
A derivation error is caught in the upload route and recorded in the audit detail, and
the upload still succeeds because the body is stored first.

`POST /api/plugins/pockethernet/rebuild` is an admin-only plugin router. `rebuild()`
checks for dropped bodies, then in one transaction reads the reports and deletes the
plugin's properties and `field_report` links and unpatches their jacks, and finally
replays each report through `derive_report` in `updated_at` order. Plugin schema version
2 adds `field_reports.key_prefix` for that replay.

### Pockethernet pages

`pages.py` builds one plugin router with three read-only routes, `GET /reports`, `/report` and
`/jack`, which the core mounts under `/api/plugins/pockethernet/` behind the session check and
the rate limit. They read `field_reports`, `port_properties` and `infra_jacks` through the
store's lock and return JSON, never markup. A report is joined to its ports through the
property rows that carry its report id, and a jack to its history through its `jack_label`
rows. The plugin's `pages()` hook registers three static files from `pages/` at
`/plugins/pockethernet`, `/report` and `/jack`, `nav_entries()` registers "Field reports", and
`static_dir()` serves `static/pockethernet.js` at `/plugins/pockethernet/static`. The script
is loaded as a module on each page and imports its helpers from the `infra-common.js` module (which builds on `js/dom.js` and `js/api.js`). It writes every string with `textContent`.
The shell module (`js/shell.js`) reads `GET /api/plugins` for the navigation links.

The console shell: every signed-in page has a `<header id="shell-header">` (holding the
`aria-live` `#summary` region the page scripts write into), a `<nav id="shell-nav">` mount and
`<script type="module" src="/static/js/shell.js">`. The module adds the "O" badge brand, a theme
toggle and the user name to the header, and draws the navigation from its `NAV` table plus the
plugin entries. The user's role comes from `GET /api/session`; admin entries are left out for a
viewer, which is tidiness only because the server enforces every admin route. The login page has
no shell.

Shared components: `css/components.css` plus `js/chips.js`, `js/table.js`, `js/dialog.js` and
`js/toast.js`. They build every node with `el()` and `svg()` from `js/dom.js`, so text always
goes in through `textContent`. Status is an icon and a word, never colour alone. The pure logic
sits in `js/table-core.js`, `js/chip-states.js` and `js/dialog-logic.js` so it can be tested
without a browser. No page loads them yet.

The Network map page (slice S10) offers Graph, Tiers and Table views side by side. `/api/infra/map` marks top-level switches with `anchor`, and `js/graph/infra.js` turns the payload into graph input (switches as nodes, endpoints as a count badge). The graph engine (slice S9) lives in `js/graph/`: `force.js` (a d3-free force layout, run once
and deterministic, limited to 300 nodes), `render.js` (canvas painter reading colours from the CSS
tokens, with a status ring and glyph on each node) and `view.js` (camera, input, resize and
repaint scheduling), with `css/graph.css`. The code is ported from relationship-maps (commit
0c4d268) via ha_Int_soc, both MIT and the same owner. No page loads it yet.

## Dashboard layout

`observe/layout.py` and the `ui_layouts` table (schema version 13) hold one row per user and view:
the tile order and the hidden tiles as JSON. `GET /api/ui/layout/{view}` needs a session and returns
only the caller's own row (`order`, `hidden`, `saved`; an empty, unsaved layout when none exists).
`PUT` needs a session and the CSRF token, and `DELETE` resets to the declared order. Only the view
`dashboard` exists; any other name is a 404. A tile id is `capacity`, `findings`, `events` or
`group:<name>`. The server checks the shape of every id, drops ids of the wrong shape and repeats,
keeps at most 200 ids per list and refuses a body over 32 KiB (413). It does not know which groups
still exist when a layout is read, so the client does that: `effectiveOrder` in
`js/tiles-logic.js` drops saved ids that are no longer shown and appends new ones in declared
order (groups by name, then the three fixed cards). `js/tiles.js` draws the Up, Down and Hide
controls and, because the CSP forbids inline styles, orders the cards by appending them to
`#sections` in order. A failed load keeps the declared order and a failed save keeps the screen
and shows a message. There is no drag and drop. Layout changes are not audited, because they hold
no secret and are the user's own preference.

## Infrastructure map core

Schema version 6 (and 7, below) adds `infra_switches`, `infra_ports`, `infra_jacks`, `infra_links`,
`infra_endpoints` and `port_properties`, as described in `docs/FIELD-DATA.md`. Nothing
existing changes. The `scope` column on `ingest_keys` belongs to the key scope slice and is
not part of this step.

`observe/portkey.py` holds the pure normalisers. `port_key` maps the spellings of one
interface to one key (`Gi1/0/5` and `GigabitEthernet1/0/5`, Juniper `ge-0/0/5.0`, UniFi
`Port 5`, Linux `eth0`) and keeps different ports apart (`Gi1/0/5` and `Gi1/0/50`, `Gi1/0/5`
and `Te1/0/5`, `eth0` and `eth0.100`, `ge-0/0/5` and `ge-0/0/5.1`); an unrecognised name is
only lower-cased and stripped of whitespace. `lldp_port_key` reads an LLDP port id by its
subtype: names and aliases are normalised like interface names, while MAC, network address,
circuit id and port component keep a prefix so they cannot collide with a name. `switch_id`
gives `mac:<12 hex digits>` from the chassis id, else `name:<lower-cased sysName>`.

`observe/infra.py` is the service plugins call (`InfraService`). It upserts switches,
ports, jacks, endpoints and links, and appends typed port properties. An upsert refreshes
`last_seen` and never blanks a stored value with an empty one. A link names two ends built
with `port_ref`, `jack_ref` or `endpoint_ref`, both of which must exist; the ends are stored
in sorted order, so an edge has one row whichever way and in whichever spelling it was
reported, and confirming it again reopens it. Port properties are append-only: the newest
row per name is the current value, a write identical to the newest row only moves its
`last_verified`, and a value that changes and then returns is a new row each time. Names must
be in the allowlist (`PROPERTY_TYPES`) or `custom.<name>`; values are type checked and
capped; the port must already exist. A custom write needs `recorded_by` and writes a
`port_property_custom` audit row that names the property but not its value. This slice has no
routes; the service is called in process.

`observe/infra_match.py` links the map to monitors. `Matcher.match_switch` tries the chassis
MAC (a `unifi_network` monitor whose `device` is that MAC), then the management addresses, then
the sysName against monitor hosts and UniFi device names; the first key with candidates decides,
the best monitor type wins (snmp, unifi_network, ping, tcp), and a tie between monitors of the
same type matches nothing. Interface monitors never stand for a whole switch. The result is
computed on each read and never written. `infra_switches.matched_monitor` holds only an
admin's link, which wins while its monitor is configured, enabled or not. `match_port` finds `snmp` interface
monitors on the switch's host whose `interface` has the same port key (or whose number is the
port's ifIndex) and the UniFi device monitor for the chassis MAC when the port has a UniFi
index. Nothing creates a monitor. Switches with no match form the unlinked queue.
`link_switch` is the admin action; it audits `infra_switch_linked` or
`infra_switch_link_failed` with the switch id and monitor slug.

`Matcher.findings` runs at request time. It takes the newest value of each property and a
`LiveReader`; the web layer's reader returns the last polled `detail` of the matched monitor
(`speed_mbps` from the SNMP interface check, and `vlan` and `poe_w` where a check reports them).
An unknown live value never produces a finding. The kinds are `speed_above_live`,
`vlan_mismatch`, `poe_no_power` (all warnings) and `repatched` (info, from a jack label that
moved to another port). `observe/infra_changes.py` adds the field change kinds `speed_drop`, `cable_fault`, `length_change`, `poe_drop`, `dhcp_fail` and `verdict_worse` (warnings) and `vlan_change` (info). `Matcher.findings` reads the newest two rows of each tracked property in one window query and passes them to the pure function `port_changes`, so a change needs two history rows and clears when the value is restored. Findings are not stored and never reach the alerter. Routes:
`GET /api/admin/infra/unlinked` (admin session), `POST /api/admin/infra/link` (admin session and
CSRF token) and `GET /api/infra/findings` (session).

`observe/infra_map.py` (`MapService`) builds the map and the effective dependency set.
Schema version 7 adds `infra_dependencies` (child slug, parent slug, accepted or rejected, who
and when); proposals are never stored. `link_state` ages a link from `last_seen` and the clock
(stale after `map.stale_days`, hidden after twice that, closed when `closed_at` is set), and
`InfraService.upsert_link` sets `closed_at` on a link that a newer report contradicts. `plan`
derives proposals from open links and the monitor matches, marks strong ones (LLDP, CDP or
SNMP LLDP, confirmed within `stale_days`), orders admin acceptances before automatic edges, and
refuses any edge that would close a cycle with the YAML plus the edges applied so far.
`refresh` hands the applied edges to `Config.set_applied_dependencies`; `Config.parents` then
returns the YAML parents plus those edges, so `Rollup` and the scheduler need no change. The
web layer refreshes on each map or dependency read and registers `refresh` as a scheduler
hook that runs once a minute. `decide` records an admin decision and audits it. Routes:
`GET /api/infra/map` and `GET /api/infra/dependencies` (session), and
`POST /api/admin/infra/depends/accept` and `/reject` (admin session and CSRF token).

Map pages. `observe/infra_port.py` (`PortPages`) builds `GET /api/infra/port?switch_id=&port=`
(session): the port's switch, role, the matched monitors with their state and last polled
speed, VLAN and PoE, the current properties, up to 50 history rows per property, and the
findings for that port. A port is `up` only when no matched monitor is worse and no
unacknowledged warning finding exists; the map applies the same rule through the same
acknowledgement rows, so an info finding or an acknowledged warning does not turn it to
Warning there either. Schema version 8 adds `infra_finding_acks`, keyed by
finding kind and port, which stores the message acknowledged; `acknowledge` refuses a finding
that does not exist now, and a finding whose message changed is shown as unacknowledged. The
route is `POST /api/admin/infra/findings/ack` (admin session and CSRF token, audited as
`infra_finding_acknowledged` and `infra_finding_ack_failed`). The pages `/map`, `/port` and
`/admin/infra` are static files like `/host`: they hold no data and their scripts send a
visitor without a session to `/login`; `infra-admin.js` also needs an admin session for all of
its data, and the port page shows state as status chips (`stateChip` in `infra-common.js`). They load as ES modules (`<script type="module">`, allowed by `default-src 'self'`), share `infra-common.js` and write every string with `textContent`. The map
places each switch by its uplink depth (core, distribution, access), draws edges in an SVG
overlay with stale links dashed, and repeats the links as a table so the picture is never the
only source. Colour is paired with a word for every state.

## Phase 2 (planned): control

Nothing in this section is built. It records the intended design so that
phase 1 leaves the right seams.

- **Agent-side allowlist.** A small control service on each host accepts only
  actions in a fixed local allowlist. An action absent from that list cannot
  be run, whatever Observe sends. There is no shell, no arbitrary command,
  and no argument that is not validated against the action's declared type.
- **Signed by Observe.** Each action request is signed with an Observe
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
    arguments                  typed, validated by the agent, not by Observe alone
    requires_typed_host_name   true for reboot
    describe()                 text shown on the confirmation step
    sign(host, nonce, expiry)  produces the signed request
    result                     ok, refused, or failed, with a message for the audit log
```

Other seams left open: a `signing_key` slot in settings that is unused in
phase 1, an `actions` section on the host view that stays empty, and an
audit action namespace (`control.*`) reserved for phase 2.
