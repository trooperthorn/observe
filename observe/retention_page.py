"""The admin retention page (docs/DATA-API-DESIGN.md section 10.2): the global and per-metric
settings in a form, and the last compaction and rollup run from `rollup_state`.

The server writes the page so the CSRF token and the last-run table are in the first response.
Every value is escaped. The page shows the backend name and never the DSN or its password; the
form is saved by admin-retention.js with PUT /api/admin/retention."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Any

LABELS = {
    "raw_days": "Raw samples",
    "rollup_5m_days": "5 minute summaries",
    "hourly_days": "Hourly summaries",
    "daily_days": "Daily summaries",
    "history_days": "Availability history",
    "compress_after_days": "Compress raw chunks after",
    "late_grace_s": "Late sample grace (seconds)",
}
EXTRA_OVERRIDE_ROWS = 3
NL = "\n"


def _when(ts: Any) -> str:
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return "never"
    if value <= 0:
        return "never"
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _state_rows(states: list[tuple[Any, ...]]) -> str:
    if not states:
        return '<tr><td colspan="5">No compaction or rollup has run yet.</td></tr>'
    out = []
    for level, last_run, rows, error in states:
        name = "compaction" if level == "compaction" else f"rollup {level}"
        out.append(
            f"<tr><td>{escape(str(level))}</td><td>{escape(name)}</td>"
            f"<td>{escape(_when(last_run))}</td><td>{escape(str(rows))}</td>"
            f"<td>{escape(str(error)) if error else 'none'}</td></tr>")
    return NL.join(out)


def _fields(settings: dict[str, Any], bounds: dict[str, Any]) -> str:
    out = []
    for name, label in LABELS.items():
        if name not in settings:
            continue
        b = bounds[name]
        out.append(
            f'<label for="f-{name}">{escape(label)}</label>'
            f'<input id="f-{name}" name="{name}" type="number" inputmode="numeric" '
            f'min="{b["min"]}" max="{b["max"]}" value="{escape(str(settings[name]))}" required>'
            f'<span class="muted">{b["min"]} to {b["max"]}, default {b["default"]}</span>')
    return NL.join(out)


def _override_rows(settings: dict[str, Any], bounds: dict[str, Any],
                   names: list[str]) -> str:
    entries = list(settings.get("overrides", {}).items()) + [("", {})] * EXTRA_OVERRIDE_ROWS
    rows = []
    for i, (metric, values) in enumerate(entries, start=1):
        cells = "".join(
            f'<td><input name="{n}" type="number" inputmode="numeric" '
            f'aria-label="{escape(LABELS[n])} for override {i}" '
            f'min="{bounds[n]["min"]}" max="{bounds[n]["max"]}" '
            f'value="{escape(str(values.get(n, "")))}"></td>' for n in names)
        rows.append(
            '<tr class="override-row"><td><input name="metric" type="text" maxlength="64" '
            f'aria-label="Metric name for override {i}" value="{escape(metric)}"></td>{cells}</tr>')
    return NL.join(rows)


def render(described: dict[str, Any], states: list[tuple[Any, ...]], *, backend: str,
           csrf: str) -> str:
    settings = described["settings"]
    bounds = described["bounds"]
    names = list(described["override_fields"])
    head = "".join(f'<th scope="col">{escape(LABELS[n])}</th>' for n in names)
    token = escape(csrf)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="{token}">
<title>Retention - Observe</title>
<link rel="stylesheet" href="/static/css/tokens.css">
<link rel="stylesheet" href="/static/css/base.css">
<link rel="stylesheet" href="/static/css/shell.css">
<link rel="stylesheet" href="/static/css/components.css">
<link rel="stylesheet" href="/static/css/admin.css">
</head>
<body>
<header id="shell-header" class="shell-head">
  <div id="summary" aria-live="polite"></div>
</header>
<nav id="shell-nav" aria-label="Main"></nav>
<main class="admin-main" id="page">
  <div class="admin-title">
    <p class="crumbs muted">Admin / Retention</p>
    <h1>Retention</h1>
  </div>
  <p id="msg" class="msg-line" role="alert"></p>
  <section class="card" aria-labelledby="last-h">
    <h3 id="last-h">Last compaction and rollup run</h3>
    <p class="card-sub">Storage backend: <strong id="backend">{escape(backend)}</strong></p>
    <table id="rollup-state">
      <caption class="muted">Rows processed are the summary rows a rollup level wrote, or the poll rows compaction removed.</caption>
      <thead><tr><th scope="col">Level</th><th scope="col">Run</th><th scope="col">Time</th><th scope="col">Rows processed</th><th scope="col">Error</th></tr></thead>
      <tbody>
{_state_rows(states)}
      </tbody>
    </table>
  </section>
  <form id="retention-form" class="card" method="post" action="/admin/retention" aria-labelledby="set-h">
    <input type="hidden" name="csrf" value="{token}">
    <h3 id="set-h">Global settings</h3>
    <p class="card-sub">Days each level keeps. A change applies at the next compaction run.</p>
    <div class="retention-fields">
{_fields(settings, bounds)}
    </div>
    <h3 id="ov-h">Per-metric overrides</h3>
    <p class="card-sub">At most {described["max_overrides"]} metrics. A row with no metric name is ignored. Leave a level empty to keep the global value.</p>
    <table id="overrides">
      <thead><tr><th scope="col">Metric</th>{head}</tr></thead>
      <tbody>
{_override_rows(settings, bounds, names)}
      </tbody>
    </table>
    <p><button type="submit" class="btn primary">Save settings</button></p>
  </form>
</main>
<script type="module" src="/static/admin-retention.js"></script>
<script type="module" src="/static/js/shell.js"></script>
</body>
</html>
"""
