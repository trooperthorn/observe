"""Schema steps and the SQLite migration runner.

Schema versioning uses a schema_version table. Each step is additive: it only creates
objects, guarded with IF NOT EXISTS, so rerunning a step changes nothing. The layout is
adapted from hostwatch (hostwatch/store.py). The steps are portable SQL; the PostgreSQL backend runs them through observe/storage/pg_dialect.py.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .rollups import METRIC_VIEWS, ROLLUP_TABLES

BASELINE = (
    """CREATE TABLE IF NOT EXISTS results (
    monitor TEXT NOT NULL,
    ts REAL NOT NULL,
    result TEXT NOT NULL,
    value REAL,
    latency_ms REAL,
    message TEXT
)""",
    "CREATE INDEX IF NOT EXISTS results_monitor_ts ON results(monitor, ts)",
    """CREATE TABLE IF NOT EXISTS events (
    monitor TEXT NOT NULL,
    ts REAL NOT NULL,
    previous TEXT NOT NULL,
    current TEXT NOT NULL,
    message TEXT
)""",
    "CREATE INDEX IF NOT EXISTS events_ts ON events(ts)",
)

HOST_TABLES = (
    """CREATE TABLE IF NOT EXISTS hosts (
  host TEXT PRIMARY KEY, platform TEXT NOT NULL DEFAULT '', agent_version TEXT NOT NULL DEFAULT '',
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, boot_id TEXT, boot_ts REAL,
  heartbeat_ts REAL, clean_shutdown INTEGER, confirmed INTEGER NOT NULL DEFAULT 0, confirmed_at REAL
)""",
    """CREATE TABLE IF NOT EXISTS host_sources (
  host TEXT NOT NULL, source TEXT NOT NULL, available INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '', updated REAL NOT NULL, PRIMARY KEY (host, source)
)""",
    """CREATE TABLE IF NOT EXISTS host_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL, ts REAL NOT NULL,
  kind TEXT NOT NULL, severity TEXT NOT NULL, source TEXT NOT NULL, title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}', dedup_key TEXT NOT NULL, boot_id TEXT,
  UNIQUE (host, dedup_key)
)""",
    "CREATE INDEX IF NOT EXISTS host_events_host_ts ON host_events(host, ts)",
    "CREATE INDEX IF NOT EXISTS host_events_ts ON host_events(ts)",
)

ACCESS_TABLES = (
    """CREATE TABLE IF NOT EXISTS ingest_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT, prefix TEXT NOT NULL UNIQUE, hash TEXT NOT NULL,
  host TEXT NOT NULL, created REAL NOT NULL, created_by TEXT NOT NULL DEFAULT '',
  revoked_at REAL, last_used REAL
)""",
    """CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0, locked_until REAL, created REAL NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS sessions (
  id_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), csrf_hash TEXT NOT NULL,
  created REAL NOT NULL, expires REAL NOT NULL, last_seen REAL NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0
)""",
    "CREATE INDEX IF NOT EXISTS sessions_expires ON sessions(expires)",
    """CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
  method TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '', status INTEGER NOT NULL DEFAULT 0,
  remote TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}'
)""",
    "CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts)",
)

BATCH_TABLES = (
    """CREATE TABLE IF NOT EXISTS ingest_batches (
  host TEXT NOT NULL, batch_id TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY (host, batch_id)
)""",
    "CREATE INDEX IF NOT EXISTS ingest_batches_ts ON ingest_batches(ts)",
)

# One row per plugin holds the highest migration version applied for it. Plugin tables
# live beside the core's and are never dropped, so disabling a plugin leaves them as they are.
PLUGIN_TABLES = (
    """CREATE TABLE IF NOT EXISTS plugin_schema (
  plugin TEXT PRIMARY KEY, version INTEGER NOT NULL
)""",
)

# The infrastructure map: switches, their ports, wall jacks, the links between them, endpoints
# seen on ports, and the append-only port property history (docs/FIELD-DATA.md). Keys are the
# normalised forms made by observe/portkey.py. A link names its two ends as (kind, ref) pairs,
# stored in sorted order so an edge has one row whichever way it was reported. All steps are
# additive; no existing table changes.
INFRA_TABLES = (
    """CREATE TABLE IF NOT EXISTS infra_switches (
  switch_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', mgmt_addresses TEXT NOT NULL DEFAULT '[]',
  vendor TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL DEFAULT '', matched_monitor TEXT,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS infra_ports (
  switch_id TEXT NOT NULL REFERENCES infra_switches(switch_id), port_key TEXT NOT NULL,
  raw_port_id TEXT NOT NULL DEFAULT '', if_index INTEGER, unifi_index INTEGER,
  role TEXT NOT NULL DEFAULT 'unknown', first_seen REAL NOT NULL, last_seen REAL NOT NULL,
  PRIMARY KEY (switch_id, port_key)
)""",
    """CREATE TABLE IF NOT EXISTS infra_jacks (
  jack_key TEXT PRIMARY KEY, room TEXT NOT NULL DEFAULT '', site TEXT NOT NULL DEFAULT '',
  switch_id TEXT, port_key TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS infra_endpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ref TEXT NOT NULL,
  mac TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, UNIQUE (kind, ref)
)""",
    """CREATE TABLE IF NOT EXISTS infra_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT, a_kind TEXT NOT NULL, a_ref TEXT NOT NULL,
  b_kind TEXT NOT NULL, b_ref TEXT NOT NULL, source TEXT NOT NULL, confidence REAL NOT NULL,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, closed_at REAL,
  UNIQUE (a_kind, a_ref, b_kind, b_ref, source)
)""",
    """CREATE TABLE IF NOT EXISTS port_properties (
  id INTEGER PRIMARY KEY AUTOINCREMENT, switch_id TEXT NOT NULL, port_key TEXT NOT NULL,
  name TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL, report_id TEXT NOT NULL DEFAULT '', observed_at REAL NOT NULL,
  recorded_at REAL NOT NULL, recorded_by TEXT NOT NULL DEFAULT '', last_verified REAL NOT NULL,
  FOREIGN KEY (switch_id, port_key) REFERENCES infra_ports(switch_id, port_key)
)""",
    "CREATE INDEX IF NOT EXISTS port_properties_lookup ON port_properties(switch_id, port_key, name, id)",
)

# Admin decisions about inferred dependencies between monitors (docs/FIELD-DATA.md). The
# proposals themselves are computed from the links on each read; only the decision is stored.
MAP_DEPENDENCY_TABLES = (
    """CREATE TABLE IF NOT EXISTS infra_dependencies (
  child TEXT NOT NULL, parent TEXT NOT NULL, decision TEXT NOT NULL,
  decided_by TEXT NOT NULL, decided_at REAL NOT NULL, PRIMARY KEY (child, parent)
)""",
)

# An admin's acknowledgement of a field finding. Findings are computed on each read and never
# stored; an acknowledgement names the finding by kind and port and keeps the message it was
# given for, so a finding whose facts changed is shown as new again.
FINDING_ACK_TABLES = (
    """CREATE TABLE IF NOT EXISTS infra_finding_acks (
  kind TEXT NOT NULL, switch_id TEXT NOT NULL, port_key TEXT NOT NULL, message TEXT NOT NULL,
  acked_by TEXT NOT NULL, acked_at REAL NOT NULL, PRIMARY KEY (kind, switch_id, port_key)
)""",
)


# Keys gain a scope: wpi (host ingest, the default for every existing key) or a marker that a
# plugin registers, such as wpf. For a plugin scope the host column holds the device label.
def _add_key_scope(db: sqlite3.Connection) -> None:
    # ALTER TABLE has no IF NOT EXISTS, so check first; every step must be safe to run again.
    columns = {row[1] for row in db.execute("PRAGMA table_info(ingest_keys)")}
    if "scope" not in columns:
        db.execute("ALTER TABLE ingest_keys ADD COLUMN scope TEXT NOT NULL DEFAULT 'wpi'")


KEY_SCOPE_TABLES = (_add_key_scope,)

# Host enrolment (observe/enrol.py): one row per host being added through the console. Only a
# digest of the single-use token is stored, and the keys are minted when it is redeemed.
ENROLMENT_TABLES = (
    """CREATE TABLE IF NOT EXISTS enrolments (
  host TEXT PRIMARY KEY, platform TEXT NOT NULL, agent INTEGER NOT NULL, control INTEGER NOT NULL,
  allowlist TEXT NOT NULL DEFAULT '{}', token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL,
  created_by TEXT NOT NULL DEFAULT '', expires_at REAL NOT NULL, fetched_at REAL,
  expiry_audited INTEGER NOT NULL DEFAULT 0, agent_prefix TEXT, control_prefix TEXT
)""",
)

# The install script reports its steps back (observe/scripts.py). The redeemed token's step key is
# kept as a digest only, and the reports are a short JSON list of step, status and note.
def _add_enrolment_reports(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(enrolments)")}
    if "step_hash" not in columns:
        db.execute("ALTER TABLE enrolments ADD COLUMN step_hash TEXT")
    if "reports" not in columns:
        db.execute("ALTER TABLE enrolments ADD COLUMN reports TEXT NOT NULL DEFAULT '[]'")


ENROLMENT_REPORT_TABLES = (_add_enrolment_reports,)


# Host settings (observe/hosttasks.py). The enrolment row gains a revision of its saved allowlist,
# when it was saved and when its install command was last reissued. host_tasks holds the short
# update and cleanup commands: one row each, a digest of the single-use token, a digest of the
# step key and the install reports, like the enrolment row.
def _add_settings_columns(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(enrolments)")}
    if "allowlist_rev" not in columns:
        db.execute("ALTER TABLE enrolments ADD COLUMN allowlist_rev INTEGER NOT NULL DEFAULT 0")
    if "allowlist_saved_at" not in columns:
        db.execute("ALTER TABLE enrolments ADD COLUMN allowlist_saved_at REAL")
    if "reissued_at" not in columns:
        db.execute("ALTER TABLE enrolments ADD COLUMN reissued_at REAL")


HOST_TASK_TABLES = (
    _add_settings_columns,
    """CREATE TABLE IF NOT EXISTS host_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL, kind TEXT NOT NULL,
  platform TEXT NOT NULL, allowlist TEXT NOT NULL DEFAULT '{}', rev INTEGER NOT NULL DEFAULT 0,
  token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL, created_by TEXT NOT NULL DEFAULT '',
  expires_at REAL NOT NULL, fetched_at REAL, step_hash TEXT, reports TEXT NOT NULL DEFAULT '[]',
  expiry_audited INTEGER NOT NULL DEFAULT 0
)""",
    "CREATE INDEX IF NOT EXISTS host_tasks_host ON host_tasks(host, id)",
)

# Per-user dashboard layouts (observe/layout.py): one JSON row per user and view.
LAYOUT_TABLES = (
    """CREATE TABLE IF NOT EXISTS ui_layouts (
  user_id INTEGER NOT NULL REFERENCES users(id), view TEXT NOT NULL, layout TEXT NOT NULL,
  updated REAL NOT NULL, PRIMARY KEY (user_id, view)
)""",
)

# Enrolment survives the wrong machine (observe/enrol.py). The install script is served without
# keys, so a refused guard leaves the token valid; the refusal is kept on the enrolment row for
# the wizard and the settings page. app_settings holds the Observe address an admin confirmed.
def _add_guard_columns(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(enrolments)")}
    for name, kind in (("guard_step", "TEXT"), ("guard_reason", "TEXT"), ("guard_at", "REAL")):
        if name not in columns:
            db.execute(f"ALTER TABLE enrolments ADD COLUMN {name} {kind}")


ENROLMENT_GUARD_TABLES = (
    _add_guard_columns,
    """CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL
)""",
)

# Series storage (docs/DATA-API-DESIGN.md section 2): resources, scopes, series, the raw samples
# keyed by series and millisecond timestamp, and the latest point of each series. The summary
# levels are in observe/storage/rollups.py.
SERIES_TABLES = (
    """CREATE TABLE IF NOT EXISTS resources (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash BLOB NOT NULL UNIQUE, kind TEXT NOT NULL,
  name TEXT NOT NULL, attrs TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS resources_kind_name ON resources(kind, name)",
    """CREATE TABLE IF NOT EXISTS scopes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, version TEXT NOT NULL DEFAULT '',
  UNIQUE (name, version)
)""",
    """CREATE TABLE IF NOT EXISTS series (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash BLOB NOT NULL UNIQUE,
  resource_id INTEGER NOT NULL REFERENCES resources(id),
  scope_id INTEGER NOT NULL REFERENCES scopes(id), metric TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT '', instrument TEXT NOT NULL DEFAULT 'gauge',
  monotonic INTEGER NOT NULL DEFAULT 0, temporality TEXT NOT NULL DEFAULT '',
  attrs TEXT NOT NULL DEFAULT '{}', first_seen REAL NOT NULL, last_seen REAL NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS series_resource_metric ON series(resource_id, metric)",
    "CREATE INDEX IF NOT EXISTS series_metric ON series(metric)",
    """CREATE TABLE IF NOT EXISTS samples (
  series_id INTEGER NOT NULL, ts INTEGER NOT NULL, value REAL, PRIMARY KEY (series_id, ts)
) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS latest (
  series_id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, value REAL, prev_ts INTEGER,
  prev_value REAL
) WITHOUT ROWID""",
)

# Change sequences (docs/DATA-API-DESIGN.md section 1.2): one counter per domain, bumped inside
# the write unit that changed the domain. The domain names are fixed in observe/storage/base.py.
CHANGE_SEQ_TABLES = (
    "CREATE TABLE IF NOT EXISTS change_seq (domain TEXT PRIMARY KEY, seq INTEGER NOT NULL DEFAULT 0)",
    "INSERT OR IGNORE INTO change_seq (domain, seq) VALUES "
    "('metrics',0), ('hosts',0), ('monitors',0), ('events',0), ('map',0), ('ports',0), "
    "('unifi',0), ('ha',0), ('audit',0), ('admin',0)",
)

# The current map (docs/DATA-API-DESIGN.md section 2.6): nodes, edges and the per-port summary,
# kept up to date inside the write unit that changes the infrastructure tables and by the
# 60 second hook that adds live state. A GET reads these and never rebuilds them.
MAP_TABLES = (
    """CREATE TABLE IF NOT EXISTS map_nodes (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, site TEXT NOT NULL DEFAULT '',
  attrs TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'unknown', seq INTEGER NOT NULL
) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS map_edges (
  id TEXT PRIMARY KEY, a TEXT NOT NULL, b TEXT NOT NULL, kind TEXT NOT NULL,
  attrs TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'active', seq INTEGER NOT NULL
) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS port_current (
  switch_id TEXT NOT NULL, port_key TEXT NOT NULL, attrs TEXT NOT NULL DEFAULT '{}',
  matches TEXT NOT NULL DEFAULT '[]', findings TEXT NOT NULL DEFAULT '[]',
  seq INTEGER NOT NULL, PRIMARY KEY (switch_id, port_key)
) WITHOUT ROWID""",
)

# The step that creates the summary levels. TimescaleDB runs its own version of it.
ROLLUP_STEP = 17

MIGRATIONS: dict[int, tuple[str | Callable[[sqlite3.Connection], None], ...]] = {
    1: BASELINE,
    2: HOST_TABLES,
    3: ACCESS_TABLES,
    4: BATCH_TABLES,
    5: PLUGIN_TABLES,
    6: INFRA_TABLES,
    7: MAP_DEPENDENCY_TABLES,
    8: FINDING_ACK_TABLES,
    9: KEY_SCOPE_TABLES,
    10: ENROLMENT_TABLES,
    11: ENROLMENT_REPORT_TABLES,
    12: HOST_TASK_TABLES,
    13: LAYOUT_TABLES,
    14: ENROLMENT_GUARD_TABLES,
    15: CHANGE_SEQ_TABLES,
    16: SERIES_TABLES,
    # Summary levels, their compaction state and the read views (observe/storage/rollups.py).
    # TimescaleDB runs its own version of this step.
    17: ROLLUP_TABLES + METRIC_VIEWS,
    18: MAP_TABLES,
}
SCHEMA_VERSION = max(MIGRATIONS)


class SchemaTooNewError(RuntimeError):
    """Raised when the database was written by a newer version of Observe."""


class LegacySchemaError(SchemaTooNewError):
    """Raised when the database was created by an earlier build with a different migration
    numbering (it still has the host_samples table). Steps were renumbered in place, so
    running only the steps above its number would skip tables. There is no migration path."""


def refuse_legacy(current: int, has_host_samples: bool) -> None:
    if current and has_host_samples:
        raise LegacySchemaError(
            f"database at schema version {current} was created by an earlier build of Observe "
            "(it has a host_samples table) and cannot be upgraded in place; delete it and "
            "start again with an empty database")


def migrate(db: sqlite3.Connection) -> None:
    """Bring an open database up to SCHEMA_VERSION, one transaction per step."""
    old = db.isolation_level
    db.isolation_level = None  # manual BEGIN and COMMIT so DDL is transactional
    try:
        db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        current = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        latest = max(MIGRATIONS)
        refuse_legacy(current, bool(db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='host_samples'").fetchall()))
        if current > latest:
            raise SchemaTooNewError(
                f"database schema version {current} is newer than this Observe "
                f"supports ({latest}); upgrade Observe or restore an older database")
        for version in range(current + 1, latest + 1):
            db.execute("BEGIN")
            try:
                for stmt in MIGRATIONS[version]:
                    if callable(stmt):
                        stmt(db)
                    else:
                        db.execute(stmt)
                db.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
    finally:
        db.isolation_level = old


class PluginSchemaTooNewError(SchemaTooNewError):
    """Raised when a plugin's tables were written by a newer release of that plugin."""


def migrate_plugins(db: sqlite3.Connection,
                    plugins: Mapping[str, Sequence[Any]]) -> None:
    """Apply each listed plugin's migrations after the core's, one transaction per step.

    `plugins` maps a plugin name to its Migration objects (version, statements), numbered
    from 1. Every plugin is checked before any is changed, so a database that is too new
    for one plugin is refused whole. A plugin that is not listed is not touched at all.
    """
    old = db.isolation_level
    db.isolation_level = None
    try:
        db.execute(PLUGIN_TABLES[0])
        current: dict[str, int] = {}
        for name, migrations in plugins.items():
            row = db.execute("SELECT version FROM plugin_schema WHERE plugin=?",
                             (name,)).fetchone()
            current[name] = row[0] if row else 0
            if current[name] > len(migrations):
                raise PluginSchemaTooNewError(
                    f"plugin {name!r} database schema version {current[name]} is newer than "
                    f"this plugin release supports ({len(migrations)}); upgrade the plugin "
                    "or restore an older database")
        for name, migrations in plugins.items():
            for migration in migrations[current[name]:]:
                db.execute("BEGIN")
                try:
                    for stmt in migration.statements:
                        db.execute(stmt)
                    db.execute("INSERT OR REPLACE INTO plugin_schema (plugin, version) "
                               "VALUES (?, ?)", (name, migration.version))
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                db.execute("COMMIT")
    finally:
        db.isolation_level = old
