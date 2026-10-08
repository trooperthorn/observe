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

Producers push OpenTelemetry data over HTTPS (OTLP/HTTP, docs/DATA-API-DESIGN.md section 6):
metrics to `POST /v1/metrics` and logs to `POST /v1/logs`, at the server root, so a stock
OpenTelemetry SDK needs only an endpoint and a header. hostwatch agents, ha_Int_soc and the
Pockethernet app all use these two routes, and OTLP is the only push format. The hostwatch batch
route (`POST /internal/v1/ingest` with its alias `POST /api/ingest`) and the Pockethernet report
upload (`POST /api/v1/field-reports` with its ping) were removed in slice r7-otlp-ingest, as the
owner decided in section 11 of the design (there is no migration path and no compatibility
window). `POST /v1/traces` answers 404 problem details, because traces are out of scope.

The code is in `observe/otlp`: `wire.py` (the protobuf decoder), `normalize.py` (from a decoded
request to a `Batch`) and `api.py` (the two routes). `observe/ingest/api.py` keeps what the
ingest routes share, the per-peer rate limit and the aggregated denial audit (`Guard`), and the
`GET /internal/v1/agent-config` route.

Bodies are `application/x-protobuf` or `application/json` (the OTLP JSON mapping), optionally with
`Content-Encoding: gzip`, and the answer uses the encoding of the request. `wire.py` is a decoder
of about 200 lines with no dependency for the messages Observe reads. It skips unknown fields by
wire type, returns the shape of the OTLP JSON mapping so that one normalizer reads both encodings,
and enforces its limits while it decodes: a nesting depth of 16, a work budget of 200,000 units per
request (a field costs one, a sub message costs four more, because building a message is what
dominates the time of a body of empty messages), varints of at most 10 bytes, every length-delimited field must fit in the bytes that
remain, strings must be UTF-8 and groups are refused. A malformed message is a 400. Decoding and
normalizing run on a worker thread, not on the event loop.

A request is checked in this order, cheapest and least informative first: the per-peer rate limit
(429 with `Retry-After: 60`); a valid unrevoked bearer key (401, before the body is read), where a
valid key of the control (`wpc`) or read (`wpr`) scope is a 403; the per-key rate limit (429); the
content type and content encoding (415); the body cap of 1 MiB on the wire (413); gzip inflation
capped at 4 MiB while inflating (413, and a malformed, truncated or multi-member stream is 400),
and a body that inflates to more than 100 times its compressed size (at least 64 KiB are always
allowed) is a 400 before any decoding work, so a tiny gzip can not buy a large decode;
for JSON, a nesting limit of 32 checked before parsing (400); the repeat check described below;
and then decoding (400) and normalizing. Nothing is stored from a request that fails any of these.
A body refused after it was inflated or decoded is charged four extra requests to the key's rate
limit, so repeated expensive refusals run out of the allowance sooner than cheap requests. The
worst body that passes the size checks (1 MB of empty messages) is stopped by the work budget in
about 0.05 s of decoder time, measured in `tests/test_ingest_integrity.py`. The last use of a key is
recorded at most once a minute.

Authentication and host binding. A `wpi` key is bound to one host. Every `resource` of a request
must carry `host.name` equal to that host (ignoring case and surrounding space), and the data is
stored under the bound host. A resource for another host, or without `host.name`, is rejected and
counted. The request is still a 200 with a partial success when something else in it was accepted,
so a producer that also sends container resources of the same host is not refused, but a request
in which nothing was accepted because of a host mismatch is a 403 and an `ingest_denied` audit row
with the bound host and the count, so a key bound to the wrong host fails loudly. A `wpf` field key
is accepted on both routes for field data only and never writes a host. On `/v1/metrics` every
resource becomes a `field_tester` resource named by the key's device label, and
`observe.field.device` is overwritten with the key's device label, so a client cannot spoof it.
A resource that carries `observe.switch` or `observe.port` is refused and counted, because a port
is owned by the collectors that name it: the Pockethernet plugin derives port properties from a
report through its own validated path, and a field key never writes port metrics. On `/v1/logs` each record goes to the
plugin that registered a handler for its `event.name` through the optional `log_handlers()` plugin
hook (a name must start with `observe.<plugin name>.` and can be taken once); a record that no
plugin handles is rejected.

What a host producer sends, and how it is stored. The instrumentation scope name is the source
(the collector), the metric name is the metric, the point attributes are the labels (as strings)
and the unit is the unit, so a point lands in the same series as the same reading did before and
as the readings Observe pulls itself. Gauge and sum points store the value they carry, a histogram
point is stored as `<name>.count` and `<name>.sum`, and a point with the no-recorded-value flag is
an unavailable sample, never zero. Exponential histograms and summaries are rejected. The gauges
`observe.source.available` and `observe.source.present`, with the point attribute `observe.source`
(and `observe.source.reason`), say whether a source works, and are stored as source status. A log
record is an event: `event.name` is its kind, the string body its title, `observe.source` its
source, `observe.dedup_key` its dedup key, `observe.boot_id` its boot id, `severityNumber` or
`severityText` its severity (reduced to info, warning or critical), and the other scalar
attributes its detail. The resource attributes `os.type`, `service.version` and
`observe.agent.sent_at` name the platform, the agent version and the send time.

Limits and partial success. A request holds at most 5,000 points and 500 log records, a metric name
must match `^[a-z][a-z0-9_.]{0,127}$`, an attribute key has at most 128 characters, a string value
at most 1,024, a point at most 32 attributes and a resource at most 64, and a point attribute that
is an array, a map or bytes, a non-finite value or an unusable time rejects that point. Each bad
point, record or resource is rejected on its own and counted. The answer is a 200 whose
`partial_success` has `rejected_data_points` (or `rejected_log_records`) and an `error_message`
that groups the reasons with counts; an empty request is a 200 that stores nothing, which is the
cheap probe an agent uses to check its key.

A value whose magnitude is above 1e300 is rejected per point ("a value is too large to store"),
because rollups add values up and two values near the float limit sum to infinity, which JSON
cannot carry. The pushed-batch schema refuses such a value the same way as a non-finite one. The
metrics query and the latest values also turn any aggregate that is still not finite into `null`,
and the JSON encoder of the v2 routes first turns every remaining non-finite float, such as a
monitor reading of "nan", into `null` and then runs with `allow_nan=False`, so no response holds
`Infinity` or `NaN`. An integer point (`asInt`) is bounded like a double, as an integer, so a
huge one is refused with "a value is too large to store". On PostgreSQL the ingest bound is the
guard, because a float8 sum raises an overflow error instead of giving infinity; rows stored
above the bound before it existed are not cleaned up. A valid point that is not stored is named in the partial success message:
"N points dropped: series limit reached (at most 2000 series per resource and 50000 in all)" for
the cardinality guard, and "N points ignored: older than the raw retention" for late points. Both
count in `rejected_data_points`. `Store.ingest_batch` fills an `IngestOutcome` with the two counts
for the route to word.

The models live in `observe/ingest/schema.py` (Batch, Sample, SourceStatus, Event). They are the
normalized form every producer is reduced to: the OTLP normalizer builds them, and so do the pull
checks that run inside Observe (Home Assistant host mode and SNMP). They limit each batch to 256
sources, 5000 samples and 500 events, with bounded string lengths, 32 labels per sample, and event
detail of at most 64 keys and 8192 bytes of JSON, and they reject non-finite numbers. A valid
request is written in one transaction by `Store.ingest_batch`: the host row, samples, source
status and events. A source reported with `present: false` is stored as unavailable with the
reason "not present on this host", because the version 2 table has no separate present column.

Idempotency has three layers, so a producer may retry any request that carries a key. A point is stored once per
series and millisecond (`INSERT ... ON CONFLICT DO NOTHING`), and the summary levels change only
for a point that was newly stored, so a resent point is a no-op; a point sent again with another
value replaces it and corrects its summaries. Events are kept once per host and `dedup_key`.
A repeated request is recognised before it is decoded by its `Idempotency-Key` header (hashed
together with the signal, so the metrics and the logs request of one batch may share a key),
remembered per host in `ingest_batches` together with a SHA-256 of the inflated body. A repeat with
the same body is answered with an empty 200 and stores nothing; a repeat with a different body is a
409 and an `ingest_denied` audit row, never a silent drop (the check also runs inside the write
unit, so two concurrent requests can not slip past it). A request without a key is not recognised
by its body: a body whose points carry no `timeUnixNano` repeats legitimately, every such point
takes the receive time, and points are deduplicated by series and timestamp alone. An
`Idempotency-Key` must be 1 to 128 printable ASCII characters (400 otherwise).

A point older than the raw retention boundary of its metric (`cuts_for(...).raw`, the time before
which raw rows are trimmed) is late: it is not stored and not folded into any level, and it is
counted in the partial success. Storing it would re-create a raw row and a 5 minute row past their
retention and count it a second time in the hourly and daily rows, which compaction can not detect
because the coarse count still covers the raw count. The same holds for the field key's metrics.

Time and order are guarded in `Store.ingest_batch`. Any sample, event or batch
`sent_at` more than `MAX_FUTURE_SKEW_S` (5 seconds) ahead of receive time is clamped to
receive time, so a bad agent clock can not mask later readings, hold the latest value of a series
(the latest row only moves forward in time) or freeze `boot_id` and `clean_shutdown`. A field
key's points are clamped the same way. The host row's platform, agent version and heartbeat, and each source's
status, are only replaced by a batch whose `sent_at` is not older than the stored one, so a
replayed older batch leaves newer state alone.

Denied requests are written to the audit log as `ingest_denied`, through an
aggregator adapted from hostwatch's hub: at most one row per peer per minute,
carrying the number of denials it covers, with at most 4096 peers tracked. The
row holds the key's public prefix and never the key. The rate limit is a fixed
window per ingest key, set by `server.ingest_rate_per_minute` (default 120, against about 8 requests a
minute from one host sending metrics and logs every 15 s). A valid key is never counted against its peer,
so 16 hosts behind one NAT or proxy are not throttled together. A missing or wrong key is counted per
peer in a second window of the same size, and that window answers 429 once it is used up. The peer is the socket address; forwarded headers are not
trusted. The routes do not use the dashboard basic auth, because producers authenticate with
their own key. A batch the store could not write is recorded as `ingest_failed` and is a server
error; a database with no free read connection in time is a 503 with `Retry-After: 5`.

Not built in this slice: the metric allow-list and the `legacy_key` mapping of section 3 (a series
is named by the scope, metric and attributes the producer sends), cumulative reset tracking for
sums (a sum is stored as the value it reports), writing points older than raw retention to the
summary levels only, the `observe.ingest.clock_skew` counter, and returning the stored response
for a repeat (the repeat gets an empty 200).

### OTLP export

Observe can also push to an external collector (docs/DATA-API-DESIGN.md section 6.6). It is off
until `export.otlp.endpoint` is set. `observe/otlp/export.py` runs as one task beside the
scheduler, started in `observe/__main__.py` and reachable as `store.exporter`. It reads committed
rows after a cursor kept in `export_cursor` (raw samples in insertion order, by the number each new point takes from the `ingest_seq` counter, `host_events`
and optionally `audit` by id), builds an OTLP request grouped by resource and scope, encodes it
with `observe/otlp/encode.py` (protobuf or JSON, always gzip) and posts it with one long-lived
`httpx.AsyncClient` that never follows redirects. The cursor moves only after the collector
answers, so a crash or an outage repeats at most one batch. A point that arrives late is stored
with a new number and is exported like any other. Network errors and every answer that is not
final (500, 502, 503, 504, 429, 401, 403 and so on) back off exponentially with full jitter and
honour `Retry-After`. A 400 or 413 is retried in halves, and only a single refused point is
dropped; 404, a redirect, 405, 410, 415, 422 and 501 are final, and a 404 or a redirect also
raises an alert in the status. Every drop, and a cursor that falls behind raw retention, is
reported as an `observe.export.gap` log record and an `export_gap` audit row. Counters are in `/metrics` and `GET /api/v2/admin/exporter`.

## Storage

Compaction also removes dead series: after the levels are trimmed, a series with no raw sample and
no summary row is deleted with its `latest` row, and a resource with no series is deleted, so
churned labels stop counting against the cardinality caps.

### Durable alerts and the bounded writer queue

Slice o8-alert-durability. Schema step 23 adds `alert_outbox` (one row per alert and target, with
its attempts, `next_at` and last error) and `alert_open` (the monitors whose problem alert went
out, with the state and time). `Alerter.notify` writes one outbox row per target and then calls
`flush`, which tries every due row once and deletes it on success; a failure keeps the row and
sets `next_at` to `retry_delay(attempts)` (10 s doubling to 300 s). `Alerter.run`, started by
`Scheduler.start`, flushes every 5 s, which also delivers what a previous run left behind. A row
for a target that is no longer configured, or older than 24 h, is dropped with a log line.
Delivery is at least once: a crash between the target's answer and the delete repeats that alert.
The Scheduler binds its store to the alerter. Without a store (a few tests) an alert is tried
twice and dropped. The one-shot check command builds a Scheduler on an in-memory store, so it
gets a single attempt and a failed alert is lost when the process exits.

`Scheduler._send` queues the alert with `Alerter.enqueue` first, then `_set_open` writes
`alert_open`, then delivery runs in the background; `restore` reads the mark, so a monitor with an
open alert comes back in that state with `alert_open` set. A restart does not alert again and the
recovery still does. The two writes are separate units, but the outbox row comes first, so a crash
between them repeats the problem alert after the restart instead of losing it. Stopping the
scheduler cancels delivery tasks; their alerts are already in the outbox and go out on the next
start. `outbox_due` limits each target to 100 rows, so one target's backlog never delays another's.

A host or field push refused with `StorageBusy` is answered 503 at once and writes no audit row,
so a refused push adds no unit to the full queue. Writes made by plugin collectors run with
`SERVER_WORK` set and are critical like the other pollers, so a full queue never drops a cycle.

`WriteGate` (`observe/storage/base.py`) counts the units submitted to the single writer from
submission until they finish. A submission past `WRITE_QUEUE_LIMIT` (256) raises `StorageBusy`
instead of queueing; both backends use it in `write` and `write_sync`, and ingest answers 503 with
`Retry-After: 5`, other v2 routes 503 with `Retry-After: 2`. Before this the executor queue was
unbounded. Tests: `tests/test_alert_durability.py` (10 000 refused submissions leave the pending
count at the limit).

### Storage interface

All database access goes through the `Storage` protocol in `observe/storage/base.py`
(slice r1, design O-1 and section 12 of `docs/DATA-API-DESIGN.md`). `Store` holds one
(`store.storage`) and its methods only build units of work for it. A write unit is a callable that
receives the transaction connection; `await storage.write(unit, touches=(...))` runs it on the
backend's single writer inside one `BEGIN IMMEDIATE ... COMMIT`, in submission order, and rolls
the whole unit back when it raises. A read unit (`await storage.read(unit)`) runs on a read-only
connection in one snapshot. The blocking forms `write_sync` and `read_sync` serve code that
already runs on a worker thread; called from inside a running write unit they join it. A
constraint failure surfaces as `IntegrityConflict`, and callers never import a driver
(`DB_ERRORS` names every error a unit can meet). The protocol also carries the change counters
(`change_seq`, `change_seqs`), the rollup and retention operations (`rollup`,
`apply_retention`) and plugin DDL (`apply_plugin_migrations`).

The SQLite backend (`observe/storage/sqlite.py`) has one writer thread
(`ThreadPoolExecutor(max_workers=1)`, so the default pool that `getaddrinfo` uses never waits for the
database) and a pool of three connections opened with a `mode=ro` URI and `query_only=ON`. A read
that finds the pool empty for 2 seconds raises `StorageBusy`, and a progress handler interrupts a
read that runs past 2 seconds (`StorageTimeout`), so a long read cannot hold back WAL
checkpoints. The writer uses WAL, `synchronous=NORMAL`, `foreign_keys=ON` (now enforced),
`busy_timeout=5000`, a 16 MB cache, in-memory temp files, a 64 MB mmap, `wal_autocheckpoint=1000`
and a 64 MB journal size limit; readers use an 8 MB cache each. An in-memory database (tests
only) runs its reads on the writer connection. Migration 17 adds the `change_seq` table
(`domain`, `seq`); a unit names the domains it changed in `touches` and the counter is bumped in the
same transaction and mirrored in memory after the commit. The domains are `metrics`, `hosts`,
`monitors`, `events`, `map`, `ports`, `unifi`, `ha`, `audit` and `admin`. `store._db`,
`store._lock`, `Store._run` and `Store._exec` no longer exist, and `tests/test_storage_ban.py`
fails the build when code under `observe/` or `plugins/` imports a driver, opens a connection
or reaches for them. The contract tests in `tests/test_storage.py` run against every backend;
the PostgreSQL cases run only when `OBSERVE_TEST_PG_DSN` is set and skip otherwise. The re-check
cases in `tests/test_recheck.py` also run on a PostgreSQL dialect fake (`tests/fakes/pg_fake.py`):
the SQLite engine behind the same rewrite the PostgreSQL connection applies, which records each
rewritten statement and refuses any SQLite-only spelling. `tests/test_pg_summary.py` checks the
summary-view and latest-table reads as PostgreSQL text and that the TimescaleDB step compiles. The
fake proves the SQL text and the results callers expect, not server types, planning or locking.

### PostgreSQL and TimescaleDB backend

`storage.backend: postgres` selects `observe/storage/postgres.py` (psycopg 3 and psycopg-pool,
imported only by that module). It has the same shape as the SQLite backend: one writer thread over
one connection, each unit in its own transaction in submission order, and a pool of three
read-only repeatable-read connections, so a response sees one snapshot. An empty pool for 2
seconds raises `StorageBusy`, and a server-side `statement_timeout` of 2 seconds raises
`StorageTimeout`. Units and schema steps are written once in portable SQL. The connection
rewrites them in `observe/storage/pg_dialect.py`: `?` becomes `%s`, `INSERT OR IGNORE` becomes
`ON CONFLICT DO NOTHING`, `INTEGER PRIMARY KEY [AUTOINCREMENT]` becomes an identity column,
`REAL` becomes `DOUBLE PRECISION` and `INTEGER` becomes `BIGINT`, the two-argument `MAX` and `MIN`
become `GREATEST` and `LEAST`, and `PRAGMA table_info` becomes a catalogue query. `INSERT OR
REPLACE` is refused. The core steps and the plugin steps run through the same rewrite, and an
advisory lock makes two starting processes migrate one at a time. The pooled readers add their
statement timeout to the `options` of the connection string instead of replacing them, so a
`search_path` in the string reaches every connection and the readers see the same schema as the
writer. In an upsert, a column the existing row keeps is named through the table (or an alias on
the `INSERT`, such as `INSERT INTO infra_switches AS sw`), because PostgreSQL rejects a bare name
there as ambiguous with the `excluded` row; both backends accept the alias form.

Series storage and the summary levels are shared. Migration 16 adds `resources`, `scopes`,
`series`, `samples` and `latest` (`observe/storage/series.py`), and migration 17 adds `rollup_5m`,
`rollup_1h`, `rollup_1d`, the compaction table `rollup_state` and four views with the same columns
on every backend: `metric_5m`, `metric_hourly`, `metric_daily` and `availability_history` (over
`events`). There is no `host_samples` table.

A resource is an entity that produces data; a pushed host is a resource of kind `host` whose
identity is its `host.name`. A scope is the producer of a point, which for a pushed host is the
source name (`hostwatch.collector.hwmon`, `hostwatch.collector.zfs`). A series is a resource, a scope, a metric and the
point attributes (a host's labels), stored once with a stable integer id; its key is a BLAKE2b
digest of the canonical form, attributes sorted by key, scalar values only. The design leaves the
scope out of the series identity, but until the OpenTelemetry normalizer gives every producer one
metric namespace two sources can send the same metric name for one host, so the scope is part of
the identity for now. At most 2,000 series per resource and 50,000 in all are kept; a point for a
series over a cap is dropped and counted in `Recorded.dropped`. `samples` holds `(series_id, ts,
value)` with `ts` in integer milliseconds and the primary key `(series_id, ts)`, so a replay of a
point cannot be stored twice, and `latest` holds the newest point of each series with the one
before it. Both ids are never reused (`AUTOINCREMENT`), so removing a host cannot leave summary
rows that a later series would adopt.

`series.record_points` runs inside the ingest write unit, and works per batch, not per point (slice
o10-perf-ingest). The series ids of the batch come from one `key_hash IN (...)` lookup (new series are
inserted together, in first-use order, so they take the ids a one by one insert gives), the stored
values of the batch from one `(series_id, ts) IN (...)` read, and then the new samples, the latest
rows and each of the three summary levels are written with one `executemany` each, in point order, so
each summary row is added to in the same order and the float sums are identical to the point by
point path. The latest rows are worked out in memory from the stored rows and written once per
changed series. A 45 point batch of existing series takes 12 statements where it took 274 (23 times
fewer; 373 and 23 for a batch that creates its 45 series); lookups are chunked at 400 parameters, so
ten times the points add only a few statements. A batch that sends a stored point again with a
different value, which is rare, takes the old point by point path (`_record_one_by_one`), because
correcting a summary reads rows that earlier points of the batch changed. `tests/test_ingest_batched.py`
keeps the old implementation as a reference and compares a hash of the stored samples, latest rows,
summary levels and series of both paths on SQLite, the PostgreSQL stand-in and PostgreSQL (and the
raw tables, ids and insertion numbers on SQLite). Throughput on SQLite is 1.1 to 1.5 times the old
path (about 78,000 against 51,000 to 69,000 points per second in one transaction; the gain is the
saved statements, which cost more on PostgreSQL where each is a round trip). A point is stored with `ON
CONFLICT DO NOTHING`, and only a point that was newly inserted updates `latest` (kept when the
point is not older than the current one, so arrival order does not matter) and, on SQLite and plain
PostgreSQL, the three summary levels: one upsert each into `rollup_5m`, `rollup_1h` and
`rollup_1d` holding the count, sum, minimum and maximum of the non-null values of the bucket, whose
key is the millisecond start of the bucket. The levels are therefore current in the same
transaction as the raw point and are never recomputed at read time, an out-of-order point lands in
its own bucket, and a replay changes nothing. A point resent with a different value replaces the
stored one; the 5 minute bucket is recomputed from raw samples, and a coarser bucket from the level
below when that level still holds all of it, otherwise its count and sum are adjusted by the
difference and its extremes widened; a bucket with no row yet, because every point in it was
null, gets its row when a null becomes a value, at every level. A value of null is stored (the gap stays visible) and counts
toward nothing. The views join the series, scope and resource names and show the bucket in whole
seconds with `avg_v` as sum over count; there is no view over raw samples.

Compaction (`observe/storage/compaction.py`, run by `apply_retention` from the scheduler every
hour) trims, level by level, raw samples (default 7 days, 1 to 30), 5 minute summaries (14 days),
hourly summaries (90 days, 90 to 180) and daily summaries (730 days), and the up and down history
(730 days), read from `app_settings` keys `retention.raw_days`, `retention.5m_days`,
`retention.hourly_days`, `retention.daily_days` and `retention.history_days`. When no raw setting
exists, `server.retention_days` is the raw level. Before a series loses rows, the compaction
verifies coverage: the points about to go must be counted in the levels above that still hold their
time range (the count of the coarser level at least the count of the finer one, each checked over
the range that level keeps). A series that fails is left alone, the first failure is kept in
`rollup_state` and shown on the admin page, and the pass goes on. A cut is aligned down to the
width of the level that covers it (5 minutes for raw, an hour for 5 minute rows, a day for hourly
and daily rows), so a bucket is never split. Deletes are chunked: a write unit removes at most
5,000 rows and looks at no more than 500 series, so ingest is never held back behind one delete.
`retention.compress_after_days` (1 to 30, default 1) is the TimescaleDB compression delay. Per-metric
overrides are one JSON value in `retention.overrides`: a metric name maps to its own `raw_days`,
`rollup_5m_days`, `hourly_days` or `daily_days`, within the same bounds, at most 100 metrics, and the
compaction gives that metric's series their own cuts, on SQLite and plain PostgreSQL.
`observe/retention.py` validates a change (whole numbers inside the bounds, unknown names refused)
and `Storage.save_retention_settings` writes the keys and one `retention_settings_changed` audit row
holding the old and new values in one transaction. `PUT /api/admin/retention` (admin
session and CSRF) changes them, and `GET /api/v2/admin/settings/retention` reads them; a refused value is 422 and audited as
`retention_settings_failed`. On TimescaleDB a change also registers the policies again at once.

The admin retention page `/admin/retention` (Admin menu) is a static page like the other admin pages
(slice r8-ui-client): `admin-retention.js` reads `GET /api/v2/admin/settings/retention` and saves the
form with `PUT /api/admin/retention`, taking the CSRF token from the session read. The last compaction
and rollup runs moved to the storage page `/admin/storage` (`admin-storage.js`, read only, refreshed by
the poller every 30 seconds), which shows the backend, whether TimescaleDB runs the rollups, the last
run of each level with its error, and the change counters from `GET /api/v2/admin/settings/storage`.
The former server-written retention and re-check pages (`observe/retention_page.py` and
`observe/recheck_page.py`) were deleted.

The re-check settings (`observe/recheck_settings.py`) are the global window, interval and good-reply
count and one optional override per monitor. They are `app_settings` keys `recheck.window`,
`recheck.interval`, `recheck.good` and `recheck.overrides` (one JSON value of monitor slug to values),
written through `Storage.write` on both backends together with one `recheck_settings_changed` audit row
holding the old and new values. `PUT /api/admin/recheck` needs an admin session and the CSRF token
(the document is `GET /api/v2/admin/settings/recheck`), and a refused value answers 422 and writes `recheck_settings_failed`. The scheduler
loads the values before the first poll and again after each save (`Scheduler.apply_recheck`), and
`Scheduler.recheck_value` resolves a value as: saved per-monitor override, then the monitor's own config
value, then the saved global value, then the config default. The page `/admin/recheck` is static and saved by
`admin-recheck.js`.

The polling tiers (`observe/tiers.py`) are availability, device metrics, storage health, SMART and
inventory, each with a default rate and a minimum and maximum in seconds (section 10.1 of the data
design). The saved global rates and the per-host overrides are the `app_settings` keys `tiers.global`
and `tiers.hosts` (one JSON value of host name to rates), written through `Storage.write` on both
backends together with one `tier_rates_changed` audit row holding the old and new values.
`PUT /api/admin/tiers` needs an admin session and the CSRF token (the document is
`GET /api/v2/admin/settings/tiers`; the page is `/admin/tiers`, `admin-tiers.js`), and a refused value answers
422 and writes `tier_rates_failed`. An override must name a host with an unrevoked `wpi` key. For one
host the effective rate is its override, then the saved global value, then the default.
`GET /internal/v1/agent-config` (in `observe/ingest/api.py`) returns `{"host", "intervals"}` for the
host the bearer `wpi` key is bound to. It uses the ingest rate limit and denial audit, takes the host
from the key and never from the request, refuses a key of another scope with 401, and answers with
`Cache-Control: no-store`. The agent reads it at start and after each interval, so a console change
applies without visiting the host. Wiring the agent scheduler and the staleness check to these rates
is a later slice.

The threshold rule engine (`observe/rules.py`) evaluates the rules of section 10.4 of the data design.
A rule is one of four kinds: `consecutive` (X polls in a row), `ratio` (X of the last Y polls),
`window` (min, max or average over a time window) and `missing` (no data for a gap, or fewer than X of
the last Y polls returned data), with a condition of above, below, equal, not equal or outside a range
against a warn and a crit value. `RuleEngine` keeps a `SeriesState` per series: the latest value next to
a ring of the last 100 samples (`RING_CAPACITY`), so an evaluation reads memory and never scans history,
and no rule may look back further than the ring. State clears only after N polls with the condition
false (hysteresis, default N equal to X); a poll that breaches on its own restarts that count, and a
move to a higher level is immediate. The clock is a function passed to the engine, and `tick` re-checks
the missing-data rules for time that passed with no sample. The rule set is validated by
`rules.validate` (fixed bounds, unique ids, at most 500 rules) and stored as the `app_settings` key
`rules.config` by `rules.save` inside one `Storage.write` unit with one `rules_changed` audit row holding
the old and new rules. `PUT /api/admin/rules` (admin session, CSRF) replaces the whole list and answers 422
with a `rules_failed` audit row for a refused rule. A submitted rule whose metric is an old `<collector>.<metric>` name (`validate(..., refuse_legacy=True)`) is refused with 422 naming the OpenTelemetry replacement and the unit change (`otelnames.legacy_rule_advice`). A rule already stored with an old name still loads, and `rules.describe` marks it with an `invalid` sentence in `GET /api/v2/admin/settings/rules` and in the `PUT` answer; the console shows an Invalid chip with that sentence, and the scheduler still logs it once at start. The page `/admin/rules` (`admin-rules.js`) edits the
list. The scheduler owns one engine and wires it in two places. For pushed data, `POST /v1/metrics` calls
`Scheduler.observe_pushed` after a batch is stored (not for a resend), which feeds every sample whose
series a rule applies to; the rule metric is the OpenTelemetry metric name (for example `hw.temperature`, which applies to
every sensor of the host; the engine key also carries the scope and the point attributes, so two
collectors never share a state), or `<source>.<metric>` for a source outside the
`hostwatch.collector.` scopes (`observe/otelnames.py`), and the rule host is the pushed host name. For pulled data, `poll_once` feeds `monitor.value` and `monitor.latency`
of each poll (a failed poll feeds no value, so a missing-data rule sees it), and the rule host is the
monitor slug. A series with no applicable rule gets no ring, so memory follows the rules and not the data.
A series seen for the first time after a restart has its ring filled from the newest stored samples
(`samples`, at most 100). `RuleEngine.worst(host)` gives the highest level held on a host; `poll_once`
raises the result of that host's poll to Warn (Warning) or Fail (Critical) when it is better, and the
state machine then applies `failures_to_down` and the other confirmation counts, so the group status,
the dashboard and the alerts follow the ordinary path. `PUT /api/admin/rules` applies the new set at
once (`Scheduler.apply_rules`); a rule that is removed forgets its states. Rules never stop a poll or
undo a stored batch: an error is logged and the poll or batch goes on. A window rule is judged only when
the full ring reaches back over its window, so a window longer than 100 samples of the poll rate never
fires (owner decision: raise `RING_CAPACITY` or bound the window by the poll rate).

Monitor loops survive storage errors. `Scheduler._loop` catches any error from a poll, its state write
or its start-up reads, logs it once per streak, retries at the next cycle and logs the recovery. After
a recovery from a missed reply the monitor cannot start another Degraded episode for
`defaults.degraded_cooldown` seconds (default 120, 0 turns it off; also a per-monitor field), so a host
that alternates between answering and not answering is judged by the ordinary failure counts and does not
create a Degraded and Up pair every cycle.
 The table reads
`rollup_state`: one row per trimmed level (`raw`, `5m`, `1h`, `1d`) with the time it was trimmed to,
when, the rows removed and the first coverage problem, and one `compaction` row that the
maintenance loop writes after each pass (ingest batch records removed, and the error text, cut to 200
characters, when the pass failed). The page names the storage backend and never the DSN or its
password.

With the TimescaleDB extension (`storage.timescaledb: auto` uses it when the database offers it,
`on` requires it, `off` never uses it) `samples` is a hypertable chunked by day on `ts`
(milliseconds, compressed by series), and `rollup_5m`, `rollup_1h` and `rollup_1d` are continuous
aggregates over it (the hourly one over the 5 minute one, the daily over the hourly) with the same
names and columns as the plain tables, so the views are the same text on both. The writer then
stores only the point and `latest` (`Storage.incremental_rollups` is false) and the aggregates
build the levels, which are current as of the last refresh (every 5 minutes, and at each
compaction). Refresh and compression policies, and the retention policies of the aggregates, are
removed and added again from the retention levels at start and at every retention run, so a changed
setting applies at the next compaction, or at once when it is saved through the admin endpoint. A
refresh window never reaches back past half of its source's retention, so a refresh cannot rebuild a
bucket whose source rows are gone. Raw samples have no retention policy: `PgStorage.drop_raw`
refreshes the aggregates, checks coverage over the range about to go, and only then drops whole
chunks older than the longest raw level any metric keeps, and a metric with a shorter override is
first trimmed by row. A chunk is a day, so raw rows can outlive the raw level by up to a day. The
aggregates cannot be deleted from by metric, so their metrics keep the longest level (a known
difference from SQLite), and removing a host deletes its raw samples and series but leaves its
buckets in the aggregates to retention; the views join the series table, so they no longer show
them. On plain PostgreSQL the shared incremental rollups and compaction run unchanged. The choice
is made when the database is created: a database created without TimescaleDB is refused under `on`,
and a TimescaleDB database is refused when the extension is off, because there is no migration
path; destroy and redeploy.

Application queries are written once and return the same Python types on both backends. Counts
and sums that feed arithmetic are cast to `BIGINT` and averages to `DOUBLE PRECISION` in the SQL,
so PostgreSQL never hands back a `Decimal`; a boolean is never summed (a `CASE` counts instead);
an hour bucket uses `FLOOR`, because PostgreSQL rounds a cast where SQLite truncates; every
derived table has an alias; and `rowid`, which the newest-row tie break uses, is rewritten to
`ctid` by the PostgreSQL dialect. The contract suite in `tests/test_storage.py` runs these
queries on both backends and asserts plain `int` and `float` results. The TimescaleDB setup runs
on a connection switched to autocommit (`pg_timescale.apply_setup`), refresh windows and policy
offsets are multiples of their bucket width, and the policies are preceded by registering the
`observe_now_s` integer_now function for the hypertable and the aggregates.

The connection string has no password. `storage.password_file` names a secret file that is read
when the database opens and passed to the driver on its own. Every error, log record from
`psycopg.pool` and repr that could carry the string or the password is scrubbed
(`observe/storage/postgres.py`, `Scrubber`), a string that carries a password is refused at
config load, and a configuration error never echoes the string. `docker-compose.yml` has an
optional `postgres` profile (TimescaleDB image, volume, `pg_isready` health check, no published
port) and `.github/workflows/tests.yml` runs the suite on SQLite and on TimescaleDB and plain
PostgreSQL service containers, with every action pinned to a commit.

Pushed data lives in the database behind a versioned schema (SQLite, or PostgreSQL).
A `schema_version` table records the applied version. At startup the storage layer
applies each missing migration in order, one transaction per step, and rolls a
failed step back. Every step is additive and guarded with `IF NOT EXISTS`, so
rerunning one changes nothing. A database created before versioning existed
held `results` and `events` (the `results` table is no longer created); it is treated as version 1 and keeps all its
rows. A database with a newer version than the code supports raises
`SchemaTooNewError` and is left untouched.

Plugins keep their own version sequence. `plugin_schema` holds one row per plugin
with the highest migration applied. After the core steps, the storage layer runs the
migrations of each listed plugin (`migrate_plugins` in `observe/storage/schema.py`), one transaction per step,
rolling a failed step back. A plugin whose recorded version is newer than the
migrations in its code raises `PluginSchemaTooNewError` (a `SchemaTooNewError`),
and every plugin is checked before any is changed. A plugin that is not listed
is not touched: its tables and its `plugin_schema` row stay as they were, so
listing it again later picks up where it stopped. Observe never drops plugin
tables.

A plugin may also declare collectors through `collectors()`: periodic async jobs
with an interval of at least 30 seconds and a timeout. The scheduler runs each in
its own task, once at startup and then on its interval, so one failing or slow
collector is logged once per streak and cannot stop the others or the scheduler.

Version 2 adds `hosts`, `host_sources` and `host_events`.
Version 3 adds `ingest_keys`, `users`, `sessions` and `audit`, and version 4 adds
`ingest_batches`, version 5 adds `plugin_schema`, and version 6 adds the
infrastructure tables (see "Infrastructure map core"). Version 9 adds the
`scope` column to `ingest_keys`; existing keys get `wpi`. Version 10 adds
`enrolments` (see "Host enrolment") and version 11 adds its `step_hash` and `reports` columns. Version 12 adds
the `allowlist_rev`, `allowlist_saved_at` and `reissued_at` columns of `enrolments` and the `host_tasks` table (see "Host
settings"). Version 13 adds `ui_layouts` (see "Dashboard layout"). Version 14 adds the `guard_step`, `guard_reason` and
`guard_at` columns of `enrolments` and the `app_settings` table (see "Host enrolment"). Version 15 adds `change_seq`, version 16 the series tables
(`resources`, `scopes`, `series`, `samples`, `latest`) and version 17 the summary levels and views. A migration step may
be a function as well as a statement, so an `ALTER TABLE` can check first and
stay safe to run again. Existing history
tables are untouched. The layout is adapted from hostwatch's `store.py`.

Both retention settings must be at least 1 day; config validation rejects 0 and negative values.
Retention is applied by `Store.prune`. Samples and the ingest batch records are
dropped after `server.retention_days`. Transitions and host events are kept for
at least a year. The audit log has its own setting,
`server.audit_retention_days` (default 365), so shortening poll retention never
shortens the audit trail. Expired sessions are deleted in the same pass.

`Store.history` and the `results` table are gone (slice o10-perf-ingest): nothing read the per-poll rows except that method, which no route called, and the one time copy of old poll rows into the series. `Store.record` writes a poll only as series points, one fewer statement and one fewer table to trim on every poll. A database that still has a `results` table keeps it untouched and unread. `Storage.apply_retention` now returns the number of ingest batch records removed.

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
boot journal) and sends one `boot.<kind>` event per detected reboot (as an OTLP log with `event.name`
`observe.host.boot`, the kind in `observe.event.kind` and the agent's severity in `observe.severity`,
which the normalizer reads back as the event kind and severity). A log `observe.source.change` (event.name) also updates the source status of `observe.source` with its `observe.source.reason`, so the reason shows on the host page. The
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

## State from summaries and the Degraded re-check

Each poll result is written as points in the series store (there is no separate per-poll row; the
check message is not stored, and the state transition message goes to `events`) under a resource of kind `monitor`, scope
`observe-monitor`, metrics `monitor.up` (0 for FAIL, else 1), `monitor.result` (0, 1, 2),
`monitor.value` (not for FAIL) and `monitor.latency`. State is read back from the summary
levels and the latest table, never from raw rows. `Store.availability` reads `metric_5m` (windows
up to 48 hours) or `metric_hourly`; `Store.hourly_series`, which feeds forecasts, reads
`metric_hourly`; `Store.last_result` reads `latest`, and `Scheduler.restore` uses it so a
restart does not show everything pending. The cost of these reads does not grow with the poll
count. The host views keep reading the `latest` table, as before.

`observe/state.py` implements section 10.3 of the data design. `CheckResult.unreachable` marks a
failure that means no reply (set by the ping, TCP and HTTP checks and by a pushed host that
missed its batch). The first such failure on a monitor that is not Down moves it to WARN with
`degraded` set and the message "Degraded: not responding". `Scheduler.delay` then returns
`recheck_interval` instead of the polling interval. `recheck_good` consecutive OK results end the
episode in UP; an unreachable failure once `recheck_window` seconds have passed since the start
ends it in DOWN. A reply that is wrong is not a miss and ends the episode into the ordinary
counts. `recheck_interval`, `recheck_window` and `recheck_good` resolve through
`Config.effective`: the monitor's own value, else `defaults`.

Transitions of an episode carry `Transition.degraded`. The scheduler hands them to the alerter,
which sends them only to targets with `notify_degraded` and ignores `notify_on` for them; the Down
that ends an episode is an ordinary alert. For a pushed host, `PushedHostCheck` fails the first
miss, and while `Check.rechecking` is set (the scheduler sets it from the state before each
run) a stale batch is probed by `reachable()`: a ping of `address` or `host`, or a TCP
connect to `recheck_port`. An answer is not a reply from the agent: the result stays an unreachable
failure with `detail.reachable_but_silent`, the message names the agent as silent, and the window
ends in Down with that reason. Only a new batch is a good reply. `reachable()` uses an IP literal
as it is and resolves a name once per 600 seconds (`RESOLVE_TTL_S`), so a 10 second re-check does
no DNS lookup each time. The probe is injectable, so tests use a fake.

`MonitorState.observe` starts the re-check of a monitor that is already WARN without a transition
(WARN to WARN is not logged or alerted). `Store.record` writes `monitor.latency`
only for a result that is not FAIL, and the rule engine is fed the same
way. The `hosts` upsert in `Store.ingest_batch` never replaces a known platform or agent version with
the placeholders `unknown` and `otlp` that `normalize_logs` and `normalize_metrics` give a request
without `os.type` or `service.version`.

Dependencies: `MonitorState.observe(allow_recheck=False)` is used while `Rollup.blocking_parent`
names a Down ancestor, so the child shows Unreachable and starts no re-check. While an ancestor
is itself in a re-check (`Rollup.degraded_parent`), the child's alert is held with a note in its
event; when the ancestor recovers, `_release_children` polls the child again and alerts if it
still fails.

The dashboard shows this. `GET /api/v2/monitors` returns `degraded` for a monitor in its re-check and
`held_by` (the name of the re-checking ancestor, null when none or when an ancestor is Down and
`blocked_by` applies). `app.js` draws a Degraded chip, its own `degraded` role and colour tokens
in the light and both dark blocks, instead of the plain Warning chip, and a "Alert held" note on
the child row.

## Status integration

A pushed host becomes a monitor of type `pushed_host` when it is listed in the
YAML. Listing it is the confirmation; the `hosts.confirmed` column is reserved
for the admin screen's confirm action in a later slice. The check
(`observe/checks/host.py`) takes its state from freshness and readings
rather than from a poll. It reads the newest sample per source, metric and
label set through `Store.latest_host`, reading only samples from the last
`max(stale_after, 900)` seconds (the `window` argument, served by the `(host, ts)` index, so the cost does not grow with
retention; `GET /api/v2/hosts` and `GET /api/v2/hosts/{host}` use the same bound; a series silent for longer than the window is no longer listed, and when nothing is inside the window the single newest sample is returned so a quiet host still reads stale), grades each configured component Good,
Warning or Critical, and returns OK, WARN or FAIL for the worst one. Those go
through `MonitorState.observe` like any other result, so `failures_to_down`
confirmation applies before a host is DOWN or pages. No batch within
`stale_after` seconds (default three intervals), or none ever, is FAIL. A component whose newest sample is older than `stale_after` is graded stale and is also FAIL, so an outbox replay or a lagging agent clock can not read as healthy. The dashboard and host page refresh timers skip a tick while the previous refresh is still running.
Because the result is an ordinary check result, `group`, `depends_on`,
`critical`, rollup, alerts and `/metrics` work unchanged, and `/metrics` adds
`observe_host_age_seconds` and `observe_host_component_state`. The grouped
summary adapted from hostwatch's `integrations/summary.py` is the host views section below.

## Host views

The host page at `/host` is a static page too: `host.js` and `host-control.js` are ES modules, the Control section is a card inside `<main>`, and both use the shared chip, dialog and toast modules. Readings with a unit are formatted by one shared module, `static/js/format.js` (`formatValue(value, unit)`): unit `1` is a ratio shown as a percentage, `By` uses KiB to EiB, `Hz`, `bit/s` and `By/s` use SI prefixes, `s` becomes a duration, and no normal reading is shown in exponent notation. `tests/js/format.test.mjs` covers it (`node --test tests/js`). The dashboard at `/` is a static page whose module script (`app.js`) builds the KPI row, availability tiles and group cards in the browser from `/api/v2/monitors`, `/api/v2/groups`, `/api/v2/status`, `/api/v2/events` and `/api/v2/findings`, using the shared chip and DOM modules. `static/js/api.js` is the console's client for the v2 API; see "The console as a v2 client" below.

Each pushed host has a page at `/host?name=HOST`, linked from the dashboard
row of its `pushed_host` monitor. It is served by two v2 routes, `GET /api/v2/hosts`
(one summary row per host, paged by name) and `GET /api/v2/hosts/{name:path}` (host names may contain slashes; the full document), both
built by `observe/hostview.py` (through `observe/api/hosts.py`, which finds the monitor behind the host and writes the timestamps as RFC 3339) from the newest sample per series in the store.
The list builds its page in a constant number of statements: `Store.host_list_inputs` reads the host rows, then in one read unit
the host resources, every latest row, every source status and the warning and critical events of the alert window (the 50 newest per host, by
`ROW_NUMBER`), and the window, newest-row and silent-series rules of `latest_host` are applied in Python. Measured on 50 hosts with
alerts, a silent host and a configured component, the list took 414 statements with the per host build and takes 5 now (the same 5 for 10 hosts); the single host
document `GET /api/v2/hosts/{name}` still reads one host the old way, and a test compares both builds on the same data byte for byte. The list's ETag is the digest of the shown rows without
`last_seen` and `age_seconds`, plus the 10 second clock bucket, instead of the `hosts`, `metrics` and `events` counters that every batch moves, so a batch that changes nothing the list shows
leaves the ETag, and the cached body, alone. The digest is rebuilt only when a counter, the scheduler fingerprint or the bucket moved.
A reading is matched to its section by the scope name `hostwatch.collector.<source>` and the
OpenTelemetry metric name of docs/DATA-API-DESIGN.md section 3.2, and the point attributes are its
labels; utilization, charge and wear are ratios from 0 to 1, so the built-in limits and a monitor's
`components` limits are ratios too. The memory section adds the computed `system.memory.utilization`.
The golden files of the agent are kept in `tests/fixtures/otel` and drive `tests/test_otel_contract.py`.
The sections are CPU, memory, power, temperatures, fans with the fan controller
state, RAID, ZFS pools, disks, UPS, Home Assistant, containers, alerts and events, plus the boot state and
the list of sources. Each section and each reading carries Good, Warning or
Critical. Built-in limits live in `hostview.py`; thresholds on the monitor in the
YAML override them (a component names the scope and metric, for example `source:
hostwatch.collector.hwmon` and `metric: hw.temperature`), and the agent never sets any.

A `homeassistant` monitor with `mode: host` feeds the same page without an agent. Every 300 s
(or its `interval`) it reads `/api/config` and `/api/states` with the existing non-admin token,
maps them in `observe/checks/ha_host.py` to a `Batch` for the host named `host_name`
(default `homeassistant`), and calls `Store.ingest_batch` in process, so grading, staleness and
retention are the ordinary ones. The points carry the OpenTelemetry names of
`docs/DATA-API-DESIGN.md` section 3.4 under three scopes: `observe.check.homeassistant`
(`observe.ha.running`, `observe.ha.safe_mode`, `observe.ha.recovery_mode`, `observe.ha.version`
with the attributes `observe.ha.component` and `observe.ha.version`, `observe.ha.update.pending`
per update entity, `observe.ha.update.count`, `observe.ha.entity.count` (the total, and one point
per `observe.ha.domain`) and `observe.ha.entity.unavailable`), `observe.check.hassio`
(`container.cpu.utilization` and `container.memory.utilization` as ratios per `container.name`
for Core, Supervisor and add-ons, and the host disk as `system.filesystem.usage` with
`system.filesystem.state` used or free, `system.filesystem.limit` and
`system.filesystem.utilization`, in bytes and as a ratio) and `observe.check.ha_soc`
(`observe.ha.soc.posture_score`, `.open_detections`, `.users_at_risk`, `.suspicious_activity`).
A source with no matching sensor is reported absent, never as zero,
and a source that is absent claims nothing, so a healthy Home Assistant with default settings
(the hassio and HA SOC sensors are opt-in) grades Good. Only the Core and Supervisor sensors
(`sensor.home_assistant_core_*` and `sensor.home_assistant_supervisor_*`) and add-ons are read
as containers; an add-on counts when the same slug also has its hassio `binary_sensor.<slug>_running`
or `update.<slug>_update` entity, so a `*_cpu_percent` sensor from another integration is
ignored. An update entity is pending when its state is on and its installed version differs from
the latest one; every update entity is stored each cycle (1 pending, 0 not), so an installed
update replaces the earlier pending reading. Every read in this check and in the other
`homeassistant`, `unifi_network` and `unifi_protect` modes goes through one capped reader: no
redirect is followed, a body over 16 MB (Home Assistant) or 4 MB (UniFi) is refused, and a UniFi
list stops with an error after 50 pages of 200 rows, the same caps as the plugin client.

The host row's platform and agent version come from one declared producer, not from whichever
batch wrote last. A pushed agent (including ha_Int_soc) outranks the Home Assistant pull
(`observe-ha-host`), which outranks SNMP (`observe-snmp-host`); a lower rank never replaces a
higher one and the newest heartbeat wins within a rank (`PULL_PRODUCER_RANK` in `store.py`).

An `snmp` monitor in mode `cpu`, `memory`, `storage` or `interface` with `host_name` set does the
same for SNMP: after a successful poll `observe/checks/snmp.py` builds a one-source
(`observe.check.snmp`) `Batch` for that host and calls `Store.ingest_batch` in process. The
readings carry the OpenTelemetry names of `docs/DATA-API-DESIGN.md` section 3.5, with ratios from
0 to 1, bytes in `By` and rates in `bit/s`. The host page then shows `system.cpu.utilization`
(the whole host, and one point per core with `cpu.logical_number`) under CPU,
`system.memory.utilization`, `system.memory.limit` and `system.memory.usage` under Memory,
`system.filesystem.utilization` and `system.filesystem.usage` (state used and free) per mount
under Disks, and `observe.network.interface.up`, `.rate` (with `network.io.direction`), `.speed`
and `.utilization` under a Network interfaces section. The grading limits are the old percent
limits as ratios (CPU 0.90 and 0.98, memory 0.90 and 0.97, filesystem 0.85 and 0.95, interface
0.70 and 0.90), so a host grades the same as before; one core is shown and not graded. The old
`snmp` source and its keys (`cpu_pct`, `mem_used_pct`, `if_in_bps` and the rest) are gone. A
pushed host component or saved rule that still names the source `snmp` or one of those metrics is
refused, or reported as invalid, with the new name and unit. The poll is stored in process and does
not pass through the rule engine, so saved threshold rules do not evaluate these series; the host
page grades them with the limits above. After an upgrade the old `snmp` source row and series stay in the store until
retention removes them; the host page sources list hides that row. Several monitors may share
one host name, because each series is keyed by scope, metric and attributes. A failed poll stores
nothing, so the host goes stale instead of showing zeros, and a watched interface that is down is
stored as `observe.network.interface.up` 0, a Warning.
The `storage` mode reads `hrStorageTable` fixed disks (`.1.3.6.1.2.1.25.2.1.4`) and computes
`capacity = hrStorageAllocationUnits * hrStorageSize` and `used = hrStorageAllocationUnits *
hrStorageUsed`, per RFC 2790 and ha_Int_soc `docs/SNMPV3.md`. The host listing and the stale
window use the shortest interval among the SNMP monitors for that name. Use the same
`host_name` as the Home Assistant host mode monitor to see the HA SOC Probe readings beside it.
The hrStorage description strings and unit sizes the Probe reports are unverified against a live
Probe; the test fixture is shaped from the contract documents.
`hostview.py` grades the `ha` section (not running Critical; update pending, safe or recovery
mode, and 25 or more unavailable entities Warning) and the `containers` section (CPU and memory
ratio Warning at 0.85, Critical at 0.95); the `hassio` disk utilization ratio is graded under
disks (Warning at 0.85, Critical at 0.95). The
stale window of that host is three times the monitor's interval, so a 300 s poll is not stale
at 180 s. The entity ids of the hassio and HA SOC sensors are unverified against a live install
and are marked in the module. Richer HA detail arrives from ha_Int_soc pushing batches with its
own ingest key bound to the same host, never from an HA admin token held by Observe.

### Home Assistant push contract

ha_Int_soc (HA SOC, same owner) pushes OTLP/HTTP JSON to `POST /v1/metrics` and `POST /v1/logs`
every 60 s with `Authorization: Bearer <key>`. The key is an ordinary `wpi` ingest key created
for the host name `homeassistant`, so it is bound to that host: a resource whose `host.name` is
anything else is rejected, a request with no resource for `homeassistant` is refused with 403, and
a key bound to another host cannot push as `homeassistant`. A fresh `Idempotency-Key` per cycle is
reused on a resend. Each source is the instrumentation scope name, and the repairs, crash
forensics and watchdog breaches are log records. It must stay inside the ordinary bounds (body
1 MiB, 5000 points, 500 records, 256 sources, 32 attributes per point). A push needs about five
points per container and a handful per other source, so a 60 s cycle is far inside them. Observe
never holds an HA admin token; the pull monitor (`mode: host`) keeps using the non-admin token,
and both write to the same host, because series are keyed by source, metric and labels.

ha_Int_soc names its points as in `docs/DATA-API-DESIGN.md` section 3.4, under the scopes
`ha_soc.collector.<name>`. The golden files it sends are copied into `tests/fixtures/observe_otlp`
(the README there names the source commit) and `tests/test_ha_push.py` sends them through the
real routes. A resource with no `os.type` that sends a metric or log named `observe.ha.*` is
platform `homeassistant`.

| Scope `ha_soc.collector.` | Metrics (unit) | Attributes | Graded under |
| --- | --- | --- | --- |
| `containers` | `container.cpu.utilization`, `container.memory.utilization` (1, ratio), `container.memory.usage` (By), `observe.ha.container.running` (0 or 1) | `container.name` (Supervisor slug, `core`, `supervisor` or an add-on slug) | Containers: ratio Warning at 0.85, Critical at 0.95; not running Warning |
| `watchdog` | `observe.ha.watchdog.breaches` ({breach}) | `observe.ha.watchdog.rule` | Containers: any breach Warning |
| `integrations` | `observe.ha.integration.count` ({integration}) per `observe.ha.integration.category`, `observe.ha.integration.errors` ({error}) | `observe.ha.integration.category`, `observe.ha.integration` | Integration health: category `failing` Critical, `credential`, `communication`, `collection`, `errors` Warning, `debug_logging` and `disabled` Good; a count with no category is shown |
| `repairs` | `observe.ha.repair.issues` ({issue}) | `observe.ha.repair.state` (`open`, `all`), `observe.ha.repair.domain`, `observe.ha.repair.severity` | Repairs: open issues Warning, or the level of the severity (`critical` Critical) when sent; `all` is shown |
| `backup` | `observe.ha.backup.count`, `observe.ha.backup.last_ok` (0 or 1), `observe.ha.backup.last_success_age` (s), `observe.ha.backup.unprotected` (0 or 1) | `observe.ha.backup.password_set` | Backups: age Warning at 36 h, Critical at 72 h; failed or unprotected Warning |
| `supervisor` | `observe.ha.supervisor.healthy`, `observe.ha.supervisor.supported` (0 or 1), `observe.ha.supervisor.unhealthy_reasons` ({reason}) | `observe.ha.supervisor.reason` | Home Assistant: unhealthy Critical, unsupported Warning |

Log records are events. `observe.ha.watchdog.breach` (severity WARN) is a warning event. An
`observe.ha.crash` record is a boot classification: its attribute
`observe.ha.crash.classification` becomes the event kind `boot.<classification>`, so the boot
classifier decides the state of the host (`docs/CRASH-FORENSICS.md` of ha_Int_soc), the boot id is
`observe.boot_id` and the severity the record carries is kept. `kernel_fault` and `silent_stop`
are unclean (the host's boot fields show a crash and the pushed host crash check fires),
`core_restart` and `clean_reboot` are clean (a `core_restart` keeps its warning severity, so it is
still listed and alerts). The host page lists boot events that are not clean under Crash events,
and warning and critical events under Alerts for 24 h. The page sections Integration health,
Repairs and Backups, and the existing Containers and Home Assistant sections, take the
`ha_soc.collector.*` scopes beside the pulled `observe.check.*` ones. A source that has never
reported shows `not_reported`, never zero. The old source and metric names (`ha_container`,
`ha_backup`, `cpu_percent`, `backups_total` and the rest) are gone.

Upgrade note. Stored Home Assistant history under the old (source, metric) keys is not deleted and
not migrated: no view or rule reads it any more, so graphs and history of those series start again
under the new names at the upgrade, and the old rows age out with the retention setting. The units
changed too (percent to ratio, GB to bytes, hours to seconds). A saved `pushed_host` component or
`require_sources` entry that names an old Home Assistant source is refused with the replacement
scope, so a limit cannot sit on a series that no longer exists and read Good for ever, and a
saved threshold rule on an old metric is refused when saved and logged as a warning when loaded,
with the new metric and the unit change. The old source rows are hidden from the host page.

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
The existing optional basic auth is kept, by owner decision, for `/metrics` and the
static page shells only. It can never reach admin, ingest-key, user or future
action routes, which require a session login with CSRF, and it does not open the
`/api/v2` read API (see "The v2 read API" below).

Implemented in `observe/auth.py` and the routes in `observe/web.py`. The
session identifier and CSRF token are never stored as plaintext: the table
holds a SHA-256 digest of the identifier, and the CSRF token is an HMAC of the
identifier, recomputed on each request. Four FastAPI dependencies enforce the
boundary: `session` (any user), `mutating` (session plus CSRF), `admin`, and
`admin_mutating`. None of them looks at the Authorization header, so basic
auth credentials get 401 there. `/metrics` accepts a session or, when
configured, basic auth; the v2 read routes accept a session or a read token. Login is `POST /api/login` (JSON), logout is
`POST /api/logout`, and `GET /api/v2/session` returns the current user, the role and the CSRF token.
A host view that is not stale and not good sets `status_reason` from the worst section: the first
item at that level (`temperatures: <source> <metric>: <item reason>`), the first alert event, or the
section note, so a warning or critical summary is never unexplained.

A login body is at most 4096 bytes however it is framed: a declared Content-Length over the cap
and a chunked body that grows past it (counted as it streams, never fully read) both get 413
before any JSON is parsed. The first admin is created from the command line (`--create-admin`). Failed
logins are counted per account and peer (`observe.auth.LoginLocks`, in memory). After `login_max_failures`
bad passwords that pair is locked for `login_lock_s`, and each further lockout of the same pair doubles
the delay up to 24 hours. A LAN attacker who knows the owner's name therefore locks only their own
address, and a correct password from any other peer is accepted. Login requests are also limited per
peer (`login_rate_per_minute`) and across all peers together (`login_global_per_minute`, default 120), which
bounds the argon2 work a flood from many addresses can cause. An unknown account takes the same time
and answer as a wrong password. Settings live under `server:` in the config.

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
yet, so it has no rows yet. `GET /api/v2/audit` returns rows newest first and is admin only,
session only, so basic auth never reaches it. It takes `limit` (1 to 500),
`kind`, and `before` (a row id, to page backwards).

## Admin screen

A single admin-only page, `/admin` (`static/admin.html` and `admin.js`), lists
users and ingest keys, creates and revokes keys, creates users, disables or
enables them, grants or removes the admin role, and links to the audit page.
It is the first write surface in Observe's web UI, which is why every
route behind it sits behind the login, role, and CSRF dependencies.

The page is a static file with no data, so a visitor without a session is sent
to `/login` by the script. It reads the key and user lists from `GET /api/v2/admin/keys` and
`/api/v2/admin/users` (the legacy lists were removed). The write routes are `POST /api/admin/users`,
`POST /api/admin/users/{id}/disabled` and `/admin` (body `{"value": bool}`),
`POST /api/admin/keys`, and `POST /api/admin/keys/{id}/revoke`. A
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
shell syntax. A fan header is 1 to 32 letters, digits, dashes or underscores and
does not start with a dash, which is hostwatch-control's own rule (`HEADER_ID`),
so a name the wizard accepts is never refused later by the host. Control is refused for Windows until thermal-control has a
Windows path (Q10), and an allowlist needs control. A name that is already
enrolled or already reporting is refused with 409.

The response carries the install command once. Its first line is a comment
that names the host and platform, for example `# Observe install for nas01
(TrueNAS). Run this on nas01 only.`, followed by a `curl ... | sudo sh` line
(a PowerShell `irm ... | iex` line for Windows) with the token in the path
(Q9). The token is `wpe_` plus 256 random bits, valid for 30 minutes and one
redemption (Q8). Only its SHA-256 digest is stored. Redeeming it claims the
row with one conditional `UPDATE`, so two redemptions cannot both win, and then
mints the host-bound keys: a `wpi` key for the agent and a `wpc` key for control.
Until then no key exists. The function is `enrol.redeem`, called by
`POST /api/enrol/redeem` (body `{"token"}`, no session, rate limited per peer, the
token is the credential), which the install script calls only after its guards
pass. `GET /i/{token}` does not redeem: it serves the script with no key in it
(`enrol.preview` builds it from placeholders that only say which keys are wanted)
and leaves the token valid, so a fetch, or a run that a guard refuses, costs the
admin nothing. The route needs no session: the token is the credential. For
`linux` and `raspberry-pi` it answers with the POSIX sh script
rendered by `observe/scripts.py`; `truenas` gets a POSIX sh script and `windows` a
PowerShell script (agent only, `text/plain`); a second fetch, an expired token or
garbage is 410. Control on TrueNAS or Windows, and control when no control plugin is
loaded, is 409, a missing Observe address is 409, and a bad `pool` query is 400, all
at the fetch. A
TrueNAS command carries the pool as `?pool=NAME` (the create body's optional `pool`,
TrueNAS only, a ZFS-style name; default `Apps`). The redemption also
mints a step key (`wps_`), stored as a digest, which authenticates the script's progress
reports.

The address in every command, and the `OBSERVE_URL` in every script, is never taken from the
request's Host header, which the sender controls. It is `server.public_url`, or the address an
admin confirmed in the wizard and saved in `app_settings` (`PUT /api/enrol/public-url`,
admin, CSRF; the file wins and a `PUT` is then 409; the host settings document shows the address
and where it came from). `config.normalise_public_url` accepts only
`http(s)://host[:port]` with no path or shell metacharacters and never a loopback or wildcard
name. With no address, create, regenerate, reissue and the task commands answer 409 with
`code: public_url_required` before a token is made or a key revoked, and the wizard asks once.
A guard that refuses reports to `POST /api/enrol/guard` (token, guard step, the name the machine
gave itself, cut to host name characters). `enrol.record_guard_failure` keeps a fixed-text reason
such as `ran on ai-pi, expected MediaIn-SVR` on the enrolment row without spending the token;
progress returns it as `guard`, with `token_state` (`valid`, `used` or `expired`). `GET /api/v2/waiting-hosts`
returns the enrolled hosts with the agent chosen that have no `hosts` row yet
(`enrol.waiting_hosts`), which the dashboard lists as "waiting for first data" with a link to
the host's enrolment page.

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
each step report, which `GET /api/v2/hosts/{name}/enrolment` returns as `install`.

`GET /api/v2/hosts/{name}/enrolment` (admin session) returns the state machine:
steps `script` (fetched), `data` (first batch, which is the `hosts` row),
`control` (the `wpc` key's first authenticated pull, from its last-use time)
and `ready`, each `done`, `waiting`, `skipped` or `expired`, with the time.
`state` is `waiting`, `script_fetched`, `first_data`, `control_pulled`, `ready`
or `expired`. A token that was never fetched reads as expired from 30 minutes
after creation.

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
the expiry is first observed), `enrol_script_failed`, `enrol_guard_refused`, `enrol_public_url_set`,
`enrol_public_url_failed`, `enrol_step_refused` and
`enrol_install_problem` (a step reported failed or refused). The rows name the host, the actor and the
choices, never the token or a key. The audit redactor also recognises `wpc_`,
`wpe_`, `wps_` and `wpt_` shapes.

### Host settings

`observe/hosttasks.py`, `observe/taskscripts.py` and the `host_tasks` table (schema version 12)
back the host settings page (`GET /hosts/{name}/settings`, the static `host-settings.html`,
`host-settings.js`, `js/settings-logic.js` and `css/settings.css`, admin only like the wizard).

- **Reading.** `GET /api/v2/hosts/{name}/settings` (admin session) returns the identity, the saved
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
  the token is spent, 410 for a used, expired or unknown token, `no-store`. Unlike the install fetch it
  still spends its token at the fetch. A newer task of the
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
  does that, then deletes the enrolment, its tasks and the host's stored rows (`hosts`, the host's
  resource with its series, samples, latest rows and summary rows, `host_sources`, `host_events`,
  `ingest_batches`) in one transaction, and keeps the
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
navigation for admins. It uses the same `GET /api/v2/audit` route, loads the newest 500 rows and
filters them in the browser by actor, kind, status group and time range. The page serves no data,
and a viewer who opens it sees an "admin account needed" notice while the API answers 403.

## The v2 read API

`observe/api` is the one read surface of the console and of scripts (`docs/DATA-API-DESIGN.md`
section 4). `observe/web.py` builds `ApiRuntime` (store, scheduler, clocks, token buckets, the
response cache and the credential lookup) and mounts the sub-application that `observe.api.build`
returns at `/api/v2`. The sub-application has its own exception handlers, so problem details
(`observe/api/problems.py`, RFC 9457, with a `request_id` and no stack trace) change no other
route, and its own OpenAPI schema, committed as `docs/openapi-v2.json` and checked by
`tests/test_api_v2_core.py` (`python -m observe.api.schema` regenerates it).

A resource joins through `ApiRegistry.resource` (`observe/api/registry.py`): a path, a handler, a
pydantic response model, the change domains it depends on, its roles and tags. The registry mounts
the route with one gate that no handler repeats, in this order: the failed-credential limit of the
peer; the credential (`observe/api/principals.py`: a session cookie, or a bearer `wpr_` read token;
basic auth and the other key scopes are refused); the role (`viewer`, `operator`, `admin`, and
`anonymous` only when `server.anonymous_read` is on and the resource allows it); the CSRF header
for a session on an unsafe method; the token bucket of the principal (`observe/api/ratelimit.py`,
a query costs five); then the ETag. The ETag is built from the route, the full path and query, the
role, the committed counters of the resource's change domains (`Storage.change_seqs`, read from
memory) and an optional fingerprint of in-memory state: monitors and groups read the scheduler's
memory, so their fingerprint is `Scheduler.fingerprint`, and a host view shows ages, so its ETag
also changes every 10 seconds. A resource may pass `counters=False` so that the declared domains stay
client metadata and the ETag comes from its fingerprint alone, and the fingerprint may be an async callable (the host list uses both). A matching `If-None-Match` is a 304, and an unchanged page is
served from a bounded response cache (4 MiB), both before any read connection is borrowed for the handler. The one exception is the host list, whose fingerprint is a digest of the rows shown: the first request after a change counter or the 10 second bucket moves reads the database (host rows plus one list read unit, built for all hosts) to rebuild it, so that request is a 503 on a busy database even when a cached body exists, and the page build that follows costs the same again. The digest leaves out `last_seen` and `age_seconds`, so a 304 or cached list may show them up to one 10 second bucket old. The list read is bounded by the alert and latest windows (events and latest rows older than the window are not read, except for the fallback read of hosts or series that have gone silent), measured on the 50 host test fixture as 6 statements for any host count. A
handler that names a `db` parameter runs on the read pool and receives a read-only connection;
`StorageBusy` and `StorageTimeout` become a 503 with `Retry-After`, and any other error a 500
without detail. Lists use opaque cursors (`observe/api/cursor.py`) that carry the sort key of the
last item, so an insert does not move a page.

The credential lookup is remembered for `server.api_auth_cache_s` (5 seconds), so a 304 needs no
database read; logout forgets a session at once, disabling or demoting a user forgets that user's
sessions (`forget_user`), revoking a key on the admin page forgets that token (`forget_token`),
and a session or token revoked any other way stops working within that time. A session's `last_seen` is written at most once a minute and never by a 304.
A read token is a row in `ingest_keys` with scope `wpr` and a `role` column (schema version 19);
its last use is also written at most once a minute.

The resources are `monitors`, `groups`, `status` (`observe/api/monitors.py`), `hosts` and
`waiting-hosts` (`hosts.py`), `events` (`events.py`, which merges the monitor transitions and the
host events until the `logs` table exists), `metrics`, `metrics/latest` and `metrics/query`
(`metrics.py`) and `changes` (`changes.py`, a long poll on the in-memory counters). The query
chooses raw samples, the 5 minute, the hourly or the daily level from the step and the admin's
retention settings, including the per-metric overrides (the shortest retention of any selected metric
decides, so a query never silently returns partial data; when even the daily level no longer holds the
start of the range for a selected metric, the response has `complete: false` and a note), caps a response at 1,000 points per series and 50 series, and uses only
portable SQL (aggregates cast to plain types, `GROUP BY` by position), which
`tests/test_api_v2_metrics.py` runs through the PostgreSQL dialect fake. A plugin adds resources
with an optional `register_api(api)` hook; its registry mounts only under `/<plugin name>`, never
allows anonymous reads, and lets `api.submit_write` bump only the change domains its resources
declared. The legacy read routes were removed with no adapter (no migration path, section 11 of the
design). Slice r6-api-v2-resources moved the map, port, finding, audit and plugin-list reads too;
the session read, the key and user lists of the admin page and the settings reads next to their
`PUT` used their own routes until the admin pages moved in slice r8-ui-client, which removed them.

Slice r6 added these modules. `network.py` serves `/map`, `/map/nodes`, `/map/edges`, `/ports`,
`/ports/{switch_id}/{port}`, `/findings` and `POST /findings/ack`: the map and the port list are
two statements on `map_nodes`, `map_edges` and `port_current`; one port, the findings and the
acknowledgement call the services the console already had (`PortPages`, `Matcher`, `MapService`),
which `observe/web.py` hands to the runtime as `runtime.infra` once they exist. `ha.py` builds
`/ha/instances` from the host view, so an instance is a host with a Home Assistant section
reporting. `admin.py` holds `/session`, `/plugins` (from the loaded plugins and
`runtime.resources`, so the list names the resources each plugin really registered), `/audit`,
`/admin/keys`, `/admin/users`, `/admin/config`, the settings documents
(`/admin/settings/tiers`, `retention`, `recheck`, `rules`, `storage`) and `/resources`. The
settings reads call the same `load` and `describe` functions as the admin pages. `storage` reports
the backend, whether TimescaleDB runs the rollups, the last run of each compaction and rollup
level and the change counters, and never a connection string. A list that no write bumps a change
domain for (keys, users, storage status) sends no ETag.

The UniFi, Pockethernet and control plugins each have an `api.py` and a `register_api` hook
(`/unifi/devices`, `/unifi/clients`, `/unifi/cameras`; `/pockethernet/reports`, `/reports/{source}/{report_id}`,
`/jacks/{key}`; `/control/commands`, `/control/capabilities`). They read their own tables on the
read connection and page by primary key. The control list never writes: a command that expired
without an answer is shown as `unknown` from the clock, where the legacy admin route (removed in
slice r8-ui-client) also stored that state. The Pockethernet resources send no ETag, because a stored but not yet derived report
changes no change domain. The page scripts `app.js`, `audit.js`, `pages/map.js`, `port.js` and
`js/shell.js` read the v2 routes.

## The console as a v2 client

Slice r8-ui-client (design O-9) moved the console pages onto one client module,
`observe/static/js/api.js`, and deleted `js/v2.js`. The module has no build step and no dependency and
looks up `fetch`, `document` and `location` when it uses them, so `tests/js/api.test.mjs` drives it with
a fake fetch and a sleep that the test opens by hand.

* `get(path, params, {signal, redirect})` keeps one ETag and body per URL (at most 200) and sends
  `If-None-Match`; a 304 returns the cached body, and the body is shared, so callers treat it as read
  only. A response without an ETag is not cached. `getAll` follows `next_cursor`.
* Every failure becomes an `ApiError` with `status`, `type`, `detail`, `code` (the machine-readable
  reason of the older routes, such as `public_url_required`) and `retryAfter` from `Retry-After`. A 401
  sends the visitor to `/login?next=...` once per page load; the open dashboard, the layout read and the
  shell pass `redirect: false` because they must stay on their page without a session.
* `api(method, path, csrf, body)` is the call the pages make. A GET goes through `get`. Any other method
  sends the CSRF header, taken from `whoami()` when the page passes none, and a JSON body. `whoami()` is
  `GET /api/v2/session`, read once per page load; a failed read is not remembered.
* `poller(fn, {interval, domains, maxAge, delay, errorHandler})` waits for `fn` to finish before it plans the
  next run (a timer started after completion, never `setInterval`), pauses while `document.hidden`,
  stops on `pagehide` or `stop()`, and after a failure waits `Retry-After` on 429 and 503 or a doubling
  delay (2 seconds up to 60). `interval` is the least time between two runs. With `domains` the next
  run also waits for one of those change domains to move and runs anyway after `maxAge` (six
  intervals by default), so a lost notice never leaves a page stale.
* `changes` is one `/changes` long poll per tab (`wait=25`), started by the first subscriber and
  stopped with the last. It delivers by domain, pauses while the tab is hidden and backs off like the
  poller.

The dashboard, host, port and map pages refresh through the poller with the domains they read
(`monitors`, `events`, `hosts`, `map`, `ports`), the storage page every 30 seconds, the Control history
every 10 seconds, and the enrolment and host settings pages while a command is being watched. A
refresh that fails throws after it writes "observe unreachable, retrying", so the poller backs off. The
shell reads `/api/v2/session` and `/api/v2/plugins`. The legacy `GET /api/session`, `GET
/api/admin/users`, `GET /api/admin/keys`, the `GET` of the tiers, retention and re-check settings and
the control plugin's `GET /commands` and `/capabilities` were removed with no adapter, and so were the
reads listed below.

The admin settings pages share `js/settings-page.js` (sign in, require an admin, read the document from
`/api/v2/admin/settings/<name>`, draw it, save the whole form with the page's `PUT`, draw the response)
and `js/admin-settings-logic.js` (pure functions from form text to the body of each `PUT`, tested by
`tests/js/admin-settings.test.mjs`). Each page is a static HTML file served by a route in
`observe/web.py` that sends no data and no token: `/admin/tiers`, `/admin/retention`, `/admin/recheck`,
`/admin/rules` and `/admin/storage`. A viewer who opens one gets the page and a "Admin account needed" card,
because every document it reads answers 403.

Two properties of the v2 session changed with the move. A user whose account is disabled or demoted
is forgotten by the v2 credential cache at once (`Authenticator.forget_user`), so the change shows on the
next read and not after `server.api_auth_cache_s`. A session that the v2 read finds expired or idle is
marked revoked (`auth.revoke_dead_session`), as the older routes always did, so it stays dead if the
clock moves back. A cookie that names no session writes nothing.

The UniFi and Pockethernet pages read `/api/v2/unifi/*` and `/api/v2/pockethernet/*` through the same
client, and the host settings, enrolment and map admin pages read their documents from
`/api/v2/hosts/{name}/settings`, `/api/v2/hosts/{name}/enrolment`, `/api/v2/infra/dependencies` and
`/api/v2/admin/infra/unlinked` (`observe/api/console.py`). Those four are served by the services
that `observe/web.py` already built (the enrolment store, the dependency mapper, the switch matcher),
which it hands to the API as `runtime.console` and `runtime.infra`, so the logic has one
implementation. They send no ETag, because an enrolment moves with a script fetch or a first report
and no change domain counts that. The legacy routes `GET /api/hosts/{name}/settings`, `GET
/api/hosts/{name}/enrolment`, `GET /api/enrol/public-url`, `GET /api/infra/dependencies`, `GET
/api/admin/infra/unlinked` and the plugin page routes `/api/plugins/unifi/{devices,clients,protect}`
and `/api/plugins/pockethernet/{reports,report,jack}` were removed with no adapter. What stays on the
old paths is every change (a `POST`, `PUT` or `DELETE`), because v2 has no write routes for them yet,
and the control plugin's request and cancel routes, whose answers the page shows as text. A test
(`tests/test_ui_v2_client.py`) fails when any script reads from a path that is not `/api/v2`.

The UniFi status resource, `GET /api/v2/unifi/status`, replaces the stale flags and notes the old page
routes computed. It returns the server clock, the time of the last good devices poll and whether that
is older than twice the interval, the windows after which a connected client or a camera that was not
refreshed is stale, whether Protect and the classic account are set up and the classic note. The page
marks the rows stale itself (`markClientsStale` and `markCamerasStale` in `vlist-core.js`) against
the server clock, so a browser with a wrong clock shows the same state as another. It sends no ETag,
because it depends on the clock. The lists are read with `getAll`, so the old 5,000 row cap and the
`truncated` note are gone. The Pockethernet list pages by cursor (newest first, an Older link) instead
of by offset, so it no longer shows a total.

## Performance, before and after

Slice r10-cleanup reran the architecture review's benchmark on synthetic data with the SQLite
backend. The script is `tests/bench_synthetic.py` (run it with `python -m tests.bench_synthetic
--days 7 --dir <scratch folder> --out <result.json>`; pytest does not collect it). It builds four
pushed hosts with 40 series each at a 30 second push interval, 36 pull monitors with a day of poll
rows, and one admin session, writes the history through `Store.ingest_batch`, and times the paths
through the FastAPI test client on the loopback interface. The "before" column is the review's
measurement of the build it reviewed (30 days of history, x86, Windows). The "after" column is this
build (7 days of history, 80,640 batches, a 112 MB database, the same machine and the same
Windows). Times are p50 in milliseconds.

| Path | Before | After | Note |
| --- | --- | --- | --- |
| One pushed-host poll | 8,365 | 1.8 | reads the latest table inside a window |
| Poll cycle, 40 monitors (36 stubs) | 34,645 | 35.6 | |
| One pull monitor poll (stub) | 1.48 | 1.2 | |
| `Store.ingest_batch`, 40 samples | 0.76 | 2.6 | now also updates the latest table and the three summary levels in the same transaction; p95 14.0 |
| Host list | 41,433 | 7.0 | after a new batch (cold); 2.5 when served from the response cache |
| Host page | 10,292 | 4.1 | cold |
| Monitor list | 3.1 | 3.8 | cold |
| Events, 25 | 3.1 | 3.3 | cold |
| Findings | 13.7 | 2.5 | cold |
| Map | 80.5 | 2.6 | cold, with the empty map of this data set; the review's map held 384 ports |
| Audit, 500 | 5.5 | 2.6 | cold |
| Monitor or metric history, 24 h | 5.8 | 12.0 | 5 minute level, 288 points for each of 8 series, 87 KB |
| Metric history, 7 days | 18.5 | 8.6 | hourly level, 58 KB |
| Dashboard refresh, four reads | 41,453 | 16.7 | sum of the cold reads |

How to read the table. A cold read is the first request after a batch and a poll row arrived, so
the change counters moved and the page was rebuilt; a warm read (the same request again) is a
response cache hit and a 304 costs the same, both 2.3 to 2.5 ms here, which is the floor of the
test client and the gate. The rows that were many seconds long in the review (the pushed-host
poll, the host list and the host page) no longer read the whole history, so their cost does not
grow with retention: they read the `latest` table, one row per series. That is why a 7 day history
is a fair stand-in for the 30 days of the review for those rows; the metric history rows do grow
with the range and are bounded by the 1,000 point cap and the level chosen from the step. The
ingest row is slower than the review's because the write now maintains the latest table and the
summary levels, which is what removed the read cost; at 2.6 ms it leaves room for about 380
batches a second on this machine. The review's numbers for the UniFi classic feed (392 ms and
1,817 commits for 60 devices), the HTTP ingest request and the Raspberry Pi estimates were not
rerun here: the benchmark does not drive the plugins, and the Pi was not available. The after
numbers are for one machine and one run of 30 repetitions each (5 for the pushed-host poll), so
treat differences of a millisecond or two as noise.

### Process signals and test warnings

`observe/__main__.py` stops the server on SIGTERM and SIGINT through `loop.add_signal_handler`.
Where the loop does not implement that (the Windows proactor loop), it falls back to
`signal.signal` and sets the stop flag through `loop.call_soon_threadsafe`.
`tests/test_main_signals.py` covers both paths. The suite emits no warnings from Observe code
or tests; `pytest.ini` ignores only starlette's httpx test client notice and ldap3's pyasn1
renames, which are third-party.

### Static files and response compression

`observe/web.py` sets `Cache-Control: no-cache` on files served by a `StaticFiles` mount (the
console's `/static/` and a plugin's static directory). They carry no data, and Starlette already
sends an ETag and Last-Modified and answers `If-None-Match` with a 304, so after the first load a
page costs a 304 per script and style sheet instead of the whole file (`app.js` is 16,174 bytes).
All other responses keep `no-store`, so the HTML pages, the login page and every session bound
answer are never kept by a browser or proxy. `GZipMiddleware` compresses any response of 1,024
bytes or more for a client that accepts gzip: the 126,194 byte metrics query of
`tests/test_static_cache_and_gzip.py` is about 31,153 bytes, and the decoded body is identical.
Conditional requests are answered inside the application before compression, so a 304 is not
affected. The same code serves SQLite and PostgreSQL; nothing here touches the database.

The two columns are not a like-for-like comparison: the review could not be rebuilt here, so its
numbers are quoted, and they used 30 days of history where this run used 7. A rerun of the same
script with `--days 30` was tried for this record and did not finish loading the history within 15
minutes, so no 30 day figure is recorded. The 36 pull monitors of the script are TCP monitors aimed
at the loopback interface (every pull check is a stub in the timed paths), so a stray recheck
cannot reach another host.

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
`monitor_types` and `map_contribution`, and the optional `register_api(api)` hook that adds
`/api/v2` resources (see "The v2 read API"). `PluginBase` gives each of the others an empty
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
middleware, so they carry the same CSP, which forbids inline script. `tests/test_ui_static.py` enforces the rules of docs/GUI-DESIGN.md section 4.2 over every such file and asserts the header on every page route. Every page loads `/static/css/tokens.css` and `/static/css/base.css` first, then the sheets for its own area (shell, components, and one page sheet such as `dashboard.css`, `host.css`, `admin.css`, `graph.css` or `login.css`). The legacy `app.css` and the old variable aliases were removed in slice S15, and every page title ends with "- Observe". The tokens define light, system dark and manual dark colours as custom properties, and `js/theme.js` stores the Auto, Light or Dark choice in the browser's `localStorage`, never on the server. `tests/test_ui_tokens.py` checks the token blocks and the WCAG contrast.
Monitor type names must start with the plugin name and a dot, and key scope
markers are three to eight lower-case letters that may not be `wpi`.

The core mounts every plugin router under `/api/plugins/<name>/` and chooses
the dependencies itself (`create_app` in `observe/web.py`). A plugin reads through
`register_api` (resources under `/api/v2/<name>`, which get the v2 sign-in, role check, rate limit and
ETag); its routers are for changes and for routes that take a key of the plugin's own scope:

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
routes. `GET /api/v2/plugins` lists loaded plugins and the navigation entries the
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

### Pockethernet report push

A field report is one OTLP log record sent to the core's `POST /v1/logs` with a `wpf` key
(`plugins/pockethernet/observe_pockethernet/otlp.py` is the handler; the plugin registers it for
the event name `observe.pockethernet.report` through `log_handlers()`). The core does everything
that is not about reports: the rate limits and key check, the 1 MiB request cap and 4 MiB gzip
inflation cap, decoding, the `plugin_request` audit row and the partial success of the answer. The
record body is a string holding the report JSON exactly as the phone built it, at most 256 KiB, and
any other body is refused. The schema then validates the report. A record the handler refuses is
counted in the `partial_success` of the 200 answer and the phone does not retry it, because the same
bytes would be refused again. An empty `POST /v1/logs` checks a key (it replaces the ping route).

Reports are stored in the plugin's `field_reports` table (plugin schema versions 1 and 2),
keyed by `(source, report_id)` where the source is the key's device label, so one
phone's key cannot replace another phone's report. The row holds summary columns
and the exact inflated body. A higher revision replaces the row, an equal revision
is a duplicate and a lower one is ignored; the decision and write are one
transaction under the store lock. The `result` is `accepted`,
`replaced`, `duplicate` or `ignored`, and it is recorded in the audit detail of the request.

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

`derive.py` runs after a report that is `accepted` or `replaced`, in the same write unit
that stores the report (`store_and_derive` in `reports.py`), so an upload is one commit. It
picks the LLDP (else CDP) neighbour that names a switch and a port, then calls `InfraTx` (the
transaction-bound form of `InfraService`) to upsert
the switch, the port (role `access`) and the jack, to link jack and port with source
`field_report`, and to append each allowlisted property with the report id and
`recorded_by` set to `<key prefix>:<device>`. `upsert_link` closes the jack's earlier
link when the port changes, and `append_property` turns an unchanged value into a
`last_verified` bump. The time passed as `now` is the report's receive time, which the
store keeps as `updated_at`, so the live path and the rebuild write identical rows.
The derivation runs inside a SAVEPOINT. An error rolls back only the derivation's own rows,
the report stays stored with `derive_status` failed, the error class goes to the audit detail,
and the transaction still commits, so the evidence is never lost and no half-derived switch,
port or link is left behind. On success the map tables (below) are updated before the commit.
A retry derives one failed report per write unit, and the rebuild is one unit with a savepoint
per report.

`POST /api/plugins/pockethernet/rebuild` is an admin-only plugin router. `rebuild()`
checks for dropped bodies, then in one transaction reads the reports and deletes the
plugin's properties and `field_report` links and unpatches their jacks, and finally
replays each report through `derive_report` in `updated_at` order. Plugin schema version
2 adds `field_reports.key_prefix` for that replay.

### Pockethernet pages

`pages.py` holds the queries (the report summary, the report with its body and ports, the jack with its
history, links and reports), and `api.py` serves them as the v2 resources `GET /api/v2/pockethernet/reports`,
`/reports/{source}/{report_id}` and `/jacks/{key}` behind the core's session check and rate limit. The old
plugin router with `GET /reports`, `/report` and `/jack` under `/api/plugins/pockethernet/` was removed.
They read `field_reports`, `port_properties` and `infra_jacks` through the
store's lock and return JSON, never markup. A report is joined to its ports through the
property rows that carry its report id, and a jack to its history through its `jack_label`
rows. The plugin's `pages()` hook registers three static files from `pages/` at
`/plugins/pockethernet`, `/report` and `/jack`, `nav_entries()` registers "Field reports", and
`static_dir()` serves `static/pockethernet.js` at `/plugins/pockethernet/static`. The script
is loaded as a module on each page and imports its helpers from the `infra-common.js` module (which builds on `js/dom.js` and `js/api.js`). It writes every string with `textContent`.
The shell module (`js/shell.js`) reads `GET /api/v2/plugins` for the navigation links.

The console shell: every signed-in page has a `<header id="shell-header">` (holding the
`aria-live` `#summary` region the page scripts write into), a `<nav id="shell-nav">` mount and
`<script type="module" src="/static/js/shell.js">`. The module adds the "O" badge brand, a theme
toggle and the user name to the header, and draws the navigation from its `NAV` table plus the
plugin entries. The user's role comes from `GET /api/v2/session`; admin entries are left out for a
viewer, which is tidiness only because the server enforces every admin route. The login page has
no shell.

Shared components: `css/components.css` plus `js/chips.js`, `js/table.js`, `js/dialog.js` and
`js/toast.js`. They build every node with `el()` and `svg()` from `js/dom.js`, so text always
goes in through `textContent`. Status is an icon and a word, never colour alone. The pure logic
sits in `js/table-core.js`, `js/chip-states.js` and `js/dialog-logic.js` so it can be tested
without a browser. No page loads them yet.

The Network map page (slice S10) offers Graph, Tiers and Table views side by side. `/api/v2/map` marks top-level switches with `anchor`, and `js/graph/infra.js` turns the payload into graph input (switches as nodes, endpoints as a count badge). The graph engine (slice S9) lives in `js/graph/`: `force.js` (a d3-free force layout, run once
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
and `Te1/0/5`, `eth0` and `eth0.100`, `ge-0/0/5` and `ge-0/0/5.1`; a bare `5` stays different
from `Port 5`); `unifi_port_key(index)` builds the key of a UniFi port index, so "Port 5" and
index 5 are one key (the stored key is always scoped by the switch id); an unrecognised name is
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

`InfraTx` holds each of those operations as a plain function of an open transaction connection
(validation and SQL, no commit), and `InfraService` runs each as one write unit, so the
operations are written once. A feed that needs a whole cycle in one transaction runs
`write_cycle(store, fn, now=...)` with `InfraTx` inside `fn` and a `savepoint(db)` around each
item (`observe.storage.savepoint`, the same `SAVEPOINT` statements on SQLite and PostgreSQL): an
item that fails goes back to its savepoint and is counted as skipped, and the rest of the cycle
commits once. `write_cycle` and every `InfraService` write end by bringing the map tables up to
date in the same transaction.

The current map is three tables (schema version 18, `observe/map_tables.py`): `map_nodes` and
`map_edges` hold the unfiltered map, and `port_current` holds one row per port with its current
property values, the monitors it matches and its findings. `map_tables.rebuild` reads the
infrastructure tables, writes only the rows that differ, and bumps the `map` and `ports` change
counters only when something changed, so an unchanged map keeps its counters. The live columns (a
node's monitor, state, blocked_by and findings, a port's matches and findings) are not known to
a write unit, because they come from the configuration and the scheduler: a write-time rebuild
carries the previous values over, and the 60 second map hook (`MapService.tick`) computes them
in one pass and writes them with `MapService.rebuild`. Live state on the map and in
`port_current` is therefore at most one hook interval old, and an admin link or an
acknowledgement rebuilds at once. Link ageing is measured at the cycle's observation time at
write time and at the hook's clock in the hook.

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
`GET /api/v2/admin/infra/unlinked` (admin session), `POST /api/admin/infra/link` (admin session and
CSRF token) and `GET /api/v2/findings` (session).

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
web layer registers `MapService.tick` (`refresh`, then `rebuild`) as a scheduler hook that runs
once a minute; `GET /api/v2/infra/dependencies` still computes the plan on request, and
`GET /api/v2/map` never rebuilds anything. `map_data` reads `map_nodes` and `map_edges` in
two statements (it took 259 per request before) and applies the `site` and `building` filter
and the `anchor` flag in memory, so its cost does not depend on the number of ports.
`rebuild` computes each port's matches once per pass instead of once per port per request.
`decide` records an admin decision and audits it. Routes:
`GET /api/v2/map` and `GET /api/v2/infra/dependencies` (session), and
`POST /api/admin/infra/depends/accept` and `/reject` (admin session and CSRF token).

Map pages. `observe/infra_port.py` (`PortPages`) builds `GET /api/v2/ports/{switch_id}/{port}`
(session): the port's switch, role, the matched monitors with their state and last polled
speed, VLAN and PoE, the current properties, up to 50 history rows per property, and the
findings for that port. A port is `up` only when no matched monitor is worse and no
unacknowledged warning finding exists; the map applies the same rule through the same
acknowledgement rows, so an info finding or an acknowledged warning does not turn it to
Warning there either. Schema version 8 adds `infra_finding_acks`, keyed by
finding kind and port, which stores the message acknowledged; `acknowledge` refuses a finding
that does not exist now, and a finding whose message changed is shown as unacknowledged. The
route is `POST /api/v2/findings/ack` (admin session and CSRF token, audited as
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

### UniFi ports mode

The `unifi_network` check has a `ports` mode that issues one GET for a device detail with redirects disabled and a 1 MB body cap. It returns `detail.ports` keyed by the string port index, with `speed_mbps`, `max_speed_mbps`, `state`, `poe`, and `vlan` and `poe_w` fixed at None. `live_port` in `web.py` reads that detail for a matched UniFi port. The field names under `interfaces.ports` are unverified against a live console.

### UniFi plugin

`plugins/unifi/observe_unifi` is a plugin with session routes and one page (see Clients, Protect and
the UniFi page below). `UniFiSettings` is its
`plugin_settings.unifi` model; every key is listed in the README under "The UniFi plugin", and the setup of each credential is under "Setting up UniFi and Home Assistant sources". The plugin host calls the optional `bind_credentials` hook after
validation with the config's named credentials, so the plugin can check that `credential` names a
`unifi` credential and `classic_credential` a `unifi_classic` one, and fail startup with a
PluginError that never contains a secret. The host now calls `configure` before it reads
`collectors()`, so a collector can take its interval from the settings.

`client.py` is the read-only Integration API client: GET only, `follow_redirects=False`, a 4 MB
cap per response, `offset` and `limit` paging that ends on an empty page, on `totalCount`, or on a
page shorter than the limit when there is no `totalCount`, and at most 50 pages. `records.py` holds
migration 1 (`unifi_devices` and `unifi_clients`, one row per site and id, `first_seen` and
`last_seen`) and the upsert and prune. The `devices` collector (`collect_devices`, interval 120 s)
picks the site by name, or the only site, and replaces each device row. A 401 or 403 sets a pause
of one interval, doubled per consecutive rejection up to 3600 s; calls during the pause raise
`BackedOff` without a request, so the scheduler logs one failure streak. The plugin's `prune` hook
deletes devices and clients with `last_seen` older than `retention_days`. Row fields follow ha_Int_soc
`docs/UNIFI-LOCAL-API-CONTRACT.md`; that `firmwareUpdatable` is on the device list row is unverified, so
an absent value is stored as NULL.

`classic.py` is the optional classic controller client. `ClassicClient` logs in with the
`unifi_classic` credential (`POST /api/auth/login`), keeps the `TOKEN` cookie and `X-CSRF-Token` in
memory only, and reads only `stat/device`, `stat/sta`, `rest/user` and `stat/health` under
`/proxy/network/api/s/{site}` with GET (any other path is refused before a request). A 401 triggers
one re-login and one retry; a second rejection, a rejected login, or a login answered 429, a 5xx
or anything but 200, backs off for one interval, doubled per failure up to 1800 s, and
`ClassicBackedOff` is raised without a request. A 403 on a read raises `ClassicForbidden` without a
re-login and backs off. The failure count resets only when a read succeeds, so a good login with
refused reads keeps growing its pause. The plugin logs a `ClassicForbidden` once until a read works
again. Redirects are
refused and a body is capped at 8 MB. `logout()` posts `/api/auth/logout` and forgets the session;
the plugin's `close()` calls it, but the plugin host has no shutdown hook yet, so nothing calls it
automatically. The parsers return per-port PoE watts and class, native and tagged VLAN fields, the
LLDP neighbour table, the uplink MAC with local and remote port numbers, the WAN health row and the
known clients that are not active, with the console's `first_seen`. A port that is down has
`speed_mbps` None. `UniFiPlugin.classic_snapshot()` returns all of these. Every
classic field name is unverified against a live console, because ha_Int_soc does not read this API.

`clients.py` holds the clients and cameras code. Migration 2 adds `connected`, `connected_at`, `ssid`,
`uplink_mac`, `sw_port` and `enriched` to `unifi_clients` and creates `unifi_cameras`. The `clients`
collector (`collect_clients`, `clients_interval` 300 s) shares the devices collector's site pick and
backoff (`_site_and_rows`), parses the Integration client list, and, when the classic credential is
set, reads only `stat/sta` and `rest/user` (`classic_clients`) to enrich connected clients and list
offline ones. A client is one row keyed by its lower-case MAC (the Integration id when there is no
MAC). One transaction upserts the poll, marks rows it did not write as not connected, and upserts
offline rows, which keep the stored address and kind and never move `last_seen` back. An offline
client last seen before `retention_days` is skipped so the prune does not remove it only for the next
poll to add it back. The uplink MAC from `ap_mac` or `sw_mac` resolves to a device id through
`unifi_devices`. A classic failure (a `UniFiError`, a 401 or an HTTP error) leaves the Integration
rows stored and sets `classic_note`, which the page shows; the write then passes `classic_ok=False`,
so the upsert keeps the stored `ssid`, `uplink_mac`, `sw_port` and `enriched`. Migration 3 adds
`classic_seen`, the time the classic detail was last read, which `/clients` returns. An offline client with
no `last_seen` is stored with `last_seen` set at first sight and not refreshed, and is skipped when
the console's `first_seen` is older than `retention_days`, so the prune can remove it. The `protect` collector (`protect: true`,
`protect_interval` 120 s) reads the unpaginated `GET {protect_base_path}/cameras` with the same key and
its own backoff, so a rejected Protect key does not pause the network collectors. `isRecording` is
stored only when it is a boolean, because `recordingSettings.mode` on other firmwares is unverified.

`pages.py` and `pages/unifi.html` with `static/unifi.js`, `vlist-core.js` and `unifi.css` are the UniFi
page, mounted by the plugin host at `/plugins/unifi` with the nav entry UniFi under Network. The page reads
`/api/v2/unifi/status`, `/devices`, `/clients` and `/cameras` (the core's session check applies). The status
carries `devices_updated` (the last good devices poll, or the newest stored `last_seen` after a restart)
and `devices_stale`, true when that is older than twice `interval`; the Devices tab shows both. The Clients table is
windowed: rows are one fixed height, only the rows in view and a margin are drawn, and spacer rows
set through a `height` attribute stand for the rest, because the static guard forbids inline styles.
`vlist-core.js` holds the pure window and filter rules, tested by `tests/js/unifi.test.mjs`. Every
value is written with `textContent`.

`feed.py` writes the map feed through `InfraTx` only, and each poll is one cycle in one write
unit and so one commit, with a SAVEPOINT per device, port and link: a device or port that fails
(a malformed row, a value the core refuses, a database error) is rolled back to its savepoint,
counted in `skipped` and left behind, and the others are kept. The devices collector calls
`feed_integration` with `save_devices=True`, so the `unifi_devices` rows and the feed share the
unit, and the map tables are updated in it too: each device is a switch keyed by `switch_id` of its
chassis MAC, with its name, address, vendor `Ubiquiti` and model. A device row that names its uplink
device (`uplink.deviceId` or `uplinkDeviceId`, unverified on device rows) gets a `config` link
between a port `uplink` on the child and a port `to-<child mac>` on the parent, because a link joins
ports. The optional `classic` collector, registered only when `classic_credential` is set, calls
`feed_classic`: ports keyed by `unifi_port_key(port_idx)` with `unifi_index`, the properties
`link_speed_mbps` (only for a port that is up), `poe_class`, `poe_load_w` and `vlan` with source
`unifi`, a `config` link for the uplink port numbers, and an `lldp` link for each LLDP neighbour
whose chassis MAC is a UniFi device of the poll or an already known switch. A neighbour that is not
known is never created, so cameras and phones do not become switches. A real port link closes the
device-level placeholder through `InfraTx.close_link`, and the placeholder is not made again
while a real link joins the two devices; the two placeholder ports stay. `append_property` now
compares a value with the newest row of the same source, so the feed and a field test keep their own
histories. `Matcher.findings` excludes source `unifi` from the field side, and fills a live value
the monitor check did not report from a `unifi` property confirmed within 900 seconds, so a stopped
classic collector goes quiet instead of comparing old values.
