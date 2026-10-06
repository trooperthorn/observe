"""The admin re-check page (docs/DATA-API-DESIGN.md section 10.3): the global window, interval
and good-reply count, and one optional override per monitor.

The server writes the page so the CSRF token is in the first response. Every value is escaped.
The form is saved by admin-recheck.js with PUT /api/admin/recheck."""

from __future__ import annotations

from html import escape
from typing import Any

from .recheck_settings import FIELDS, LABELS

NL = "\n"


def _num(value: Any) -> str:
    return "" if value is None else f"{value:g}"


def _step(name: str) -> str:
    return "1" if name == "good" else "any"


def _fields(described: dict[str, Any]) -> str:
    out = []
    for name in FIELDS:
        b = described["bounds"][name]
        out.append(
            f'<label for="f-{name}">{escape(LABELS[name])}</label>'
            f'<input id="f-{name}" name="{name}" type="number" step="{_step(name)}" '
            f'min="{b["min"]:g}" max="{b["max"]:g}" '
            f'value="{escape(_num(described["settings"][name]))}" required>'
            f'<span class="muted">{b["min"]:g} to {b["max"]:g}, default {b["default"]:g}</span>')
    return NL.join(out)


def _rows(described: dict[str, Any]) -> str:
    rows = []
    for m in described["monitors"]:
        slug = m["slug"]
        values = described["overrides"].get(slug, {})
        cells = "".join(
            f'<td><input name="{n}" type="number" step="{_step(n)}" '
            f'aria-label="{escape(LABELS[n])} for {escape(m["name"])}" '
            f'min="{described["bounds"][n]["min"]:g}" max="{described["bounds"][n]["max"]:g}" '
            f'value="{escape(_num(values.get(n)))}"></td>' for n in FIELDS)
        rows.append(f'<tr class="override-row" data-slug="{escape(slug)}">'
                    f'<th scope="row">{escape(m["name"])}</th>{cells}</tr>')
    return NL.join(rows)


def render(described: dict[str, Any], *, csrf: str) -> str:
    head = "".join(f'<th scope="col">{escape(LABELS[n])}</th>' for n in FIELDS)
    token = escape(csrf)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="{token}">
<title>Re-check - Observe</title>
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
    <p class="crumbs muted">Admin / Re-check</p>
    <h1>Re-check</h1>
  </div>
  <p id="msg" class="msg-line" role="alert"></p>
  <form id="recheck-form" class="card" method="post" action="/admin/recheck" aria-labelledby="set-h">
    <input type="hidden" name="csrf" value="{token}">
    <h3 id="set-h">Global settings</h3>
    <p class="card-sub">After a missed reply a monitor is Degraded and is checked every interval. It returns to Up after the good replies in a row, and goes Down when the window ends first. A window of 0 turns the re-check off. A change applies at the next result of each monitor.</p>
    <div class="recheck-fields">
{_fields(described)}
    </div>
    <h3 id="ov-h">Per-monitor overrides</h3>
    <p class="card-sub">A value set here beats the global value for that monitor. Leave a cell empty to use the global value.</p>
    <table id="overrides">
      <thead><tr><th scope="col">Monitor</th>{head}</tr></thead>
      <tbody>
{_rows(described)}
      </tbody>
    </table>
    <p><button type="submit" class="btn primary">Save settings</button></p>
  </form>
</main>
<script type="module" src="/static/admin-recheck.js"></script>
<script type="module" src="/static/js/shell.js"></script>
</body>
</html>
"""
