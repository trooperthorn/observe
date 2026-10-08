"""The session, the audit log, the admin lists, the settings and the plugin list
(docs/DATA-API-DESIGN.md section 4.2), and the raw resource list.

Everything under /admin, and the audit log, needs the admin role. A read token is never admin,
so a script cannot read any of them. None of these lists has a secret in it: a key is shown by
its public prefix, a password hash is never selected, a configuration secret is shown only as
"set" or "not set", and a database connection string is never shown.

The lists that no write bumps a change domain for (users, keys) send no ETag, like the waiting
hosts. The settings change through routes that bump the `admin` domain, so they carry one.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from .. import recheck_settings, retention, rules, tiers
from ..storage import rollups
from .cursor import PageParams, encode
from .models import Page, Ts
from .problems import ApiProblem
from .registry import ApiContext, ApiRegistry

MAX_ATTR_FILTERS = 8
SCAN_BATCH = 500
MAX_SCAN = 5000


class Document(BaseModel):
    """A settings document: the fields named here are always present, others may follow."""

    model_config = ConfigDict(extra="allow")


# ---- session --------------------------------------------------------------------------------

class SessionOut(BaseModel):
    username: str
    kind: str = Field(description="session, token or anonymous")
    role: str
    is_admin: bool
    csrf: str | None = Field(None, description="The CSRF token a session sends on a write.")


def get_session(ctx: ApiContext) -> dict[str, Any]:
    """Who is calling and with what role. A session also gets its CSRF token."""
    p = ctx.principal
    return {"username": p.name, "kind": p.kind, "role": p.role, "is_admin": p.role == "admin",
            "csrf": p.csrf}


# ---- plugins --------------------------------------------------------------------------------

class PluginResource(BaseModel):
    method: str
    path: str
    operation_id: str
    roles: list[str]
    domains: list[str]


class PluginNav(BaseModel):
    label: str
    path: str
    workspace: str


class PluginPageOut(BaseModel):
    path: str
    admin_only: bool


class PluginOut(BaseModel):
    name: str
    version: str
    resources: list[PluginResource]
    nav: list[PluginNav]
    pages: list[PluginPageOut]


class PluginList(BaseModel):
    items: list[PluginOut]


def list_plugins(ctx: ApiContext) -> dict[str, Any]:
    """Loaded plugins, the resources each registered under /api/v2 and the navigation entries
    and pages the console shell builds its menu from. An admin-only entry is shown to an admin."""
    runtime = ctx.runtime
    admin = ctx.principal.role == "admin"
    loaded = runtime.plugins.plugins if runtime.plugins is not None else ()
    owned: dict[str, list[Any]] = {}
    for r in getattr(runtime, "resources", []):
        if r.owner is not None:
            owned.setdefault(r.owner, []).append(r)
    out = []
    for p in loaded:
        out.append({
            "name": p.name, "version": p.plugin.version,
            "resources": [{"method": r.method, "path": r.path, "operation_id": r.operation_id,
                           "roles": list(r.roles), "domains": list(r.domains)}
                          for r in sorted(owned.get(p.name, []), key=lambda r: (r.path, r.method))
                          if admin or "admin" not in r.roles or len(r.roles) > 1],
            "nav": [{"label": n.label, "path": n.path, "workspace": n.workspace}
                    for n in p.nav_entries if admin or not n.admin_only],
            "pages": [{"path": g.path, "admin_only": g.admin_only} for g in p.pages
                      if admin or not g.admin_only]})
    return {"items": out}


# ---- audit ----------------------------------------------------------------------------------

class AuditRow(BaseModel):
    id: int
    ts: Ts
    actor: str
    kind: str
    method: str
    path: str
    status: int
    remote: str
    detail: dict[str, Any]


class AuditPage(Page):
    items: list[AuditRow]


class AuditFilters(BaseModel):
    kind: str | None = Field(None, max_length=64, description="One audit kind")
    actor: str | None = Field(None, max_length=128, description="One actor")


def list_audit(db: Any, page: PageParams, filters: AuditFilters) -> dict[str, Any]:
    """The audit log, newest first. The detail was redacted when it was written."""
    after = page.after(int)
    where, args = ["1=1"], []
    if after:
        where.append("id < ?")
        args.append(after[0])
    for column, value in (("kind", filters.kind), ("actor", filters.actor)):
        if value:
            where.append(f"{column} = ?")
            args.append(value)
    rows = db.execute(
        "SELECT id, ts, actor, kind, method, path, status, remote, detail FROM audit WHERE "
        + " AND ".join(where) + " ORDER BY id DESC LIMIT ?", (*args, page.limit + 1)).fetchall()
    more = len(rows) > page.limit
    rows = rows[:page.limit]
    items = []
    for r in rows:
        try:
            detail = json.loads(r[8])
        except ValueError:
            detail = {}
        items.append({"id": r[0], "ts": r[1], "actor": r[2], "kind": r[3], "method": r[4],
                      "path": r[5], "status": r[6], "remote": r[7],
                      "detail": detail if isinstance(detail, dict) else {}})
    return {"items": items, "next_cursor": encode([rows[-1][0]]) if more else None}


# ---- keys and users -------------------------------------------------------------------------

class KeyOut(BaseModel):
    id: str = Field(description="The public prefix. The secret part is never stored.")
    host: str
    scope: str
    role: str
    created: Ts
    created_by: str
    revoked_at: Ts | None = None
    last_used: Ts | None = None
    active: bool


class KeyPage(Page):
    items: list[KeyOut]


class KeyFilters(BaseModel):
    scope: str | None = Field(None, max_length=8, description="wpi, wpr or a plugin scope")
    active: bool | None = Field(None, description="Only keys that are (not) revoked")


def list_keys(db: Any, page: PageParams, filters: KeyFilters) -> dict[str, Any]:
    """Ingest, read, control and field keys by prefix, host and state. Admin only."""
    after = page.after(int)
    where, args = ["id > ?"], [after[0] if after else 0]
    if filters.scope:
        where.append("scope = ?")
        args.append(filters.scope)
    if filters.active is not None:
        where.append("revoked_at IS NULL" if filters.active else "revoked_at IS NOT NULL")
    rows = db.execute(
        "SELECT id, prefix, host, scope, role, created, created_by, revoked_at, last_used "
        "FROM ingest_keys WHERE " + " AND ".join(where) + " ORDER BY id LIMIT ?",
        (*args, page.limit + 1)).fetchall()
    more = len(rows) > page.limit
    rows = rows[:page.limit]
    items = [{"id": r[1], "host": r[2], "scope": r[3], "role": r[4], "created": r[5],
              "created_by": r[6], "revoked_at": r[7], "last_used": r[8], "active": r[7] is None}
             for r in rows]
    return {"items": items, "next_cursor": encode([rows[-1][0]]) if more else None}


class UserOut(BaseModel):
    id: int
    username: str
    is_admin: bool
    disabled: bool
    created: Ts


class UserPage(Page):
    items: list[UserOut]


def list_users(db: Any, page: PageParams) -> dict[str, Any]:
    """Console users. The password hash and the lockout counters are never selected."""
    after = page.after(int)
    rows = db.execute(
        "SELECT id, username, is_admin, disabled, created FROM users WHERE id > ? "
        "ORDER BY id LIMIT ?", (after[0] if after else 0, page.limit + 1)).fetchall()
    more = len(rows) > page.limit
    rows = rows[:page.limit]
    items = [{"id": r[0], "username": r[1], "is_admin": bool(r[2]), "disabled": bool(r[3]),
              "created": r[4]} for r in rows]
    return {"items": items, "next_cursor": encode([rows[-1][0]]) if more else None}


# ---- effective configuration ----------------------------------------------------------------

SERVER_SHOWN = (
    "listen", "port", "retention_days", "audit_retention_days", "ingest_rate_per_minute",
    "max_concurrency", "session_idle_s", "session_absolute_s", "session_cookie_secure",
    "login_max_failures", "login_lock_s", "login_rate_per_minute", "login_global_per_minute", "plugin_rate_per_minute",
    "public_url", "anonymous_read", "api_rate_per_second", "api_burst", "api_auth_cache_s")


class ConfigOut(BaseModel):
    server: dict[str, Any]
    storage: dict[str, Any]
    defaults: dict[str, Any]
    forecast: dict[str, Any]
    map: dict[str, Any]
    monitors: int
    alerts: list[dict[str, str]]
    credentials: list[dict[str, str]]
    plugins: list[str]


def get_config(ctx: ApiContext) -> dict[str, Any]:
    """The effective configuration. A secret (a password, a token, the database connection
    string) is replaced by whether it is set; credentials are listed by name and type only."""
    c = ctx.config
    server = {k: getattr(c.server, k) for k in SERVER_SHOWN}
    server["basic_auth_set"] = bool(c.server.basic_auth_user or c.server.basic_auth_password)
    storage = {"backend": c.storage.backend, "timescaledb": c.storage.timescaledb,
               "dsn_set": c.storage.dsn is not None,
               "password_file_set": c.storage.password_file is not None}
    return {"server": server, "storage": storage, "defaults": c.defaults.model_dump(),
            "forecast": c.forecast.model_dump(), "map": c.map.model_dump(),
            "monitors": len(c.monitors),
            "alerts": [{"name": a.name, "type": a.type} for a in c.alerts],
            "credentials": [{"name": n, "type": v.type} for n, v in sorted(c.credentials.items())],
            "plugins": list(c.plugins)}


# ---- settings -------------------------------------------------------------------------------

def tier_settings(db: Any) -> dict[str, Any]:
    """The polling rates: the effective global rates, the saved values, the per-host overrides
    and the bounds of each tier."""
    glob, hosts = tiers.load(db)
    return tiers.describe(glob, hosts, tiers.known_hosts(db))


def retention_settings(db: Any, ctx: ApiContext) -> dict[str, Any]:
    """The days each level keeps, with the bounds, the defaults and the per-metric overrides."""
    return retention.describe(rollups.load_levels(db, ctx.config.server.retention_days))


def recheck_view(db: Any, ctx: ApiContext) -> dict[str, Any]:
    """The global re-check window, interval and good-reply count, and the per-monitor overrides."""
    glob, overrides = recheck_settings.load(db)
    return recheck_settings.describe(ctx.config, glob, overrides)


def rule_settings(db: Any) -> dict[str, Any]:
    """The saved threshold rules; one that names a pre-OpenTelemetry metric carries `invalid`."""
    return {"rules": rules.describe(rules.load(db)), "max_rules": rules.MAX_RULES}


class StorageOut(BaseModel):
    backend: str
    timescaledb: bool | None = Field(
        None, description="Whether TimescaleDB runs the rollups. Null on SQLite.")
    incremental_rollups: bool
    levels: list[dict[str, Any]]
    change_seqs: dict[str, int]


async def storage_status(ctx: ApiContext) -> dict[str, Any]:
    """Which backend holds the data, whether TimescaleDB is in use, the last run of each
    compaction and rollup level and the change counters. The connection string is never shown."""
    storage = ctx.store.storage
    rows = await storage.fetchall(rollups.STATE_SQL)
    timescale = getattr(storage, "timescale", None)
    return {"backend": storage.backend,
            "timescaledb": None if storage.backend == "sqlite" else bool(timescale),
            "incremental_rollups": bool(getattr(storage, "incremental_rollups", True)),
            "levels": [{"level": r[0], "last_run": r[1], "last_rows": r[2],
                        "last_error": r[3]} for r in rows],
            "change_seqs": storage.change_seqs()}


class ExporterOut(BaseModel):
    enabled: bool
    endpoint: str | None = None
    protocol: str | None = None
    signals: list[str] = []
    interval: float | None = None
    max_batch_points: int | None = None
    sent: int = 0
    failed: int = 0
    dropped: int = 0
    rejected: int = 0
    lag_seconds: float = 0.0
    last_success: Ts | None = None
    last_error: str = ""
    consecutive_failures: int = 0
    gaps: int = 0
    alert: str = ""


def exporter_status(ctx: ApiContext) -> dict[str, Any]:
    """The OTLP exporter's counters and settings. Header values are never part of it; the
    exporter is set in the configuration file, not through the API."""
    exporter = getattr(ctx.store, "exporter", None)
    if exporter is None:
        return {"enabled": False}
    return exporter.status()


# ---- raw resources --------------------------------------------------------------------------

class ResourceOut(BaseModel):
    id: int
    kind: str
    name: str
    attrs: dict[str, Any]
    first_seen: Ts
    last_seen: Ts


class ResourcePage(Page):
    items: list[ResourceOut]


class SeriesSummary(BaseModel):
    id: int
    scope: str
    metric: str
    unit: str
    attrs: dict[str, Any]
    last_seen: Ts


class ResourceDetail(ResourceOut):
    series: list[SeriesSummary]
    series_total: int


class ResourceFilters(BaseModel):
    kind: str | None = Field(None, max_length=64)
    name: str | None = Field(None, max_length=256, description="The exact resource name")
    seen_since: str | None = Field(None, max_length=40,
                                   description="RFC 3339 or unix seconds")


def _resource(row: Any) -> dict[str, Any]:
    return {"id": row[0], "kind": row[1], "name": row[2], "attrs": json.loads(row[3]),
            "first_seen": row[4], "last_seen": row[5]}


def list_resources(db: Any, ctx: ApiContext, request: Request, page: PageParams,
                   filters: ResourceFilters) -> dict[str, Any]:
    """Resources (a host, a monitor, a device) by kind and name. `attr.<key>=<value>` keeps the
    resources whose attribute equals the value, at most eight of them per request."""
    from .timeparse import parse_time
    attrs = {k[5:]: v for k, v in request.query_params.items() if k.startswith("attr.")}
    if len(attrs) > MAX_ATTR_FILTERS or any(not k or len(k) > 128 or len(v) > 1024
                                            for k, v in attrs.items()):
        raise ApiProblem(400, f"at most {MAX_ATTR_FILTERS} short attr filters")
    where, args = ["id > ?"], [0]
    for column, value in (("kind", filters.kind), ("name", filters.name)):
        if value:
            where.append(f"{column} = ?")
            args.append(value)
    if filters.seen_since:
        where.append("last_seen >= ?")
        args.append(parse_time(filters.seen_since, ctx.now, "seen_since"))
    after = page.after(int)
    last = after[0] if after else 0
    out: list[dict[str, Any]] = []
    scanned, exhausted, more = 0, False, False
    while not more and not exhausted and scanned < MAX_SCAN:
        rows = db.execute(
            "SELECT id, kind, name, attrs, first_seen, last_seen FROM resources WHERE "
            + " AND ".join(where) + " ORDER BY id LIMIT ?",
            (last, *args[1:], SCAN_BATCH)).fetchall()
        exhausted = len(rows) < SCAN_BATCH
        scanned += len(rows)
        for r in rows:
            last = r[0]
            item = _resource(r)
            if all(str(item["attrs"].get(k)) == v for k, v in attrs.items()):
                if len(out) == page.limit:
                    more = True
                    break
                out.append(item)
    cont = more or (not exhausted and scanned >= MAX_SCAN)
    # A page is cut at the item that did not fit, so the next page starts at the last one kept.
    token = None
    if cont:
        token = encode([out[-1]["id"] if more and out else last])
    return {"items": out, "next_cursor": token}


def get_resource(db: Any, rid: int) -> dict[str, Any]:
    """One resource with a summary of its series."""
    row = db.execute("SELECT id, kind, name, attrs, first_seen, last_seen FROM resources "
                     "WHERE id = ?", (rid,)).fetchone()
    if row is None:
        raise ApiProblem(404, "unknown resource")
    total = db.execute("SELECT COUNT(*) FROM series WHERE resource_id = ?", (rid,)).fetchone()[0]
    series = db.execute(
        "SELECT s.id, sc.name, s.metric, s.unit, s.attrs, s.last_seen FROM series s "
        "JOIN scopes sc ON sc.id = s.scope_id WHERE s.resource_id = ? ORDER BY s.id LIMIT 200",
        (rid,)).fetchall()
    return {**_resource(row), "series_total": total,
            "series": [{"id": s[0], "scope": s[1], "metric": s[2], "unit": s[3],
                        "attrs": json.loads(s[4]), "last_seen": s[5]} for s in series]}


def register(api: ApiRegistry) -> None:
    api.resource("/session", get_session, SessionOut, tags=("session",), etag=False,
                 summary="The current caller")
    api.resource("/plugins", list_plugins, PluginList, tags=("plugins",), anonymous=False,
                 etag=False, summary="Loaded plugins and their resources")
    api.resource("/audit", list_audit, AuditPage, domains=("audit",), tags=("audit",),
                 roles=("admin",), paginate=True, filters=AuditFilters, anonymous=False,
                 summary="The audit log")
    api.resource("/admin/keys", list_keys, KeyPage, tags=("admin",), roles=("admin",),
                 paginate=True, filters=KeyFilters, anonymous=False, etag=False,
                 summary="List keys")
    api.resource("/admin/users", list_users, UserPage, tags=("admin",), roles=("admin",),
                 paginate=True, anonymous=False, etag=False, summary="List users")
    api.resource("/admin/config", get_config, ConfigOut, tags=("admin",), roles=("admin",),
                 anonymous=False, memory=lambda: "static", summary="The effective configuration")
    for name, handler, summary in (
            ("tiers", tier_settings, "Polling tier rates"),
            ("retention", retention_settings, "Retention and downsampling"),
            ("recheck", recheck_view, "Re-check settings"),
            ("rules", rule_settings, "Threshold rules")):
        api.resource(f"/admin/settings/{name}", handler, Document, domains=("admin",),
                     tags=("settings",), roles=("admin",), anonymous=False, summary=summary,
                     operation_id=f"settings_{name}")
    api.resource("/admin/settings/storage", storage_status, StorageOut, tags=("settings",),
                 roles=("admin",), anonymous=False, etag=False,
                 summary="Storage backend status", operation_id="settings_storage")
    api.resource("/admin/exporter", exporter_status, ExporterOut, tags=("settings",),
                 roles=("admin",), anonymous=False, etag=False,
                 summary="OTLP exporter status")
    api.resource("/resources", list_resources, ResourcePage, domains=("metrics",),
                 tags=("resources",), paginate=True, filters=ResourceFilters, anonymous=False,
                 summary="List resources")
    api.resource("/resources/{rid}", get_resource, ResourceDetail, domains=("metrics",),
                 tags=("resources",), anonymous=False, summary="One resource")
