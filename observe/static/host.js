// Per-host hardware page. Every string came from a host agent, so it is written
// with textContent only, never innerHTML.
"use strict";

const SECTIONS = [
  ["cpu", "CPU"], ["memory", "Memory"], ["power", "Power"], ["temperatures", "Temperatures"],
  ["fans", "Fans and controller"], ["raid", "RAID"], ["zfs", "ZFS pools"], ["disks", "Disks"],
  ["ups", "UPS"], ["alerts", "Alerts"],
];
const STATE_TEXT = {
  stale: "stale", unavailable: "source unavailable", absent: "not present on this host",
  not_reported: "never reported",
};
const page = document.getElementById("page");
const name = new URLSearchParams(location.search).get("name") || "";

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function ago(s) {
  if (s == null) return "never";
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
}

function fmtValue(i) {
  if (i.value == null) return "no value";
  const v = Math.abs(i.value) >= 1e6 ? i.value.toExponential(3)
    : Math.abs(i.value) >= 100 ? Math.round(i.value) : Math.round(i.value * 100) / 100;
  return `${v}${i.unit ? " " + i.unit : ""}`;
}

function labelText(labels) {
  return Object.entries(labels || {}).map(([k, v]) => `${k}=${v}`).join(" ");
}

function pill(status) { return el("span", `pill ${status}`, status); }

function itemsTable(items) {
  const t = el("table", "items");
  const head = el("tr");
  for (const h of ["Reading", "Labels", "Value", "Status", "Seen"]) head.append(el("th", null, h));
  t.append(head);
  for (const i of items) {
    const r = el("tr");
    r.append(el("td", null, `${i.source}.${i.metric}`), el("td", null, labelText(i.labels)),
      el("td", "num", fmtValue(i)));
    const st = el("td");
    st.append(pill(i.status));
    if (i.reason) st.append(" ", el("span", "note", i.reason));
    r.append(st, el("td", i.stale ? "stalemark" : null,
      `${ago(i.age_seconds)}${i.stale ? " (stale)" : ""}`));
    t.append(r);
  }
  return t;
}

function eventsList(events) {
  const ol = el("ol");
  for (const e of events) {
    const li = el("li");
    li.append(el("span", "when", fmtTime(e.ts)), ` ${e.severity} ${e.kind}: ${e.title}`);
    ol.append(li);
  }
  return ol;
}

function section(title, sec, isEvents) {
  const box = el("section", "sec");
  const h = el("h2", null, title);
  h.append(pill(sec.status));
  if (sec.state !== "ok") h.append(el("span", `pill ${sec.state}`, STATE_TEXT[sec.state] || sec.state));
  box.append(h);
  if (sec.note) box.append(el("div", "note", sec.note));
  if (sec.items.length) box.append(isEvents ? eventsList(sec.items) : itemsTable(sec.items));
  return box;
}

function sourcesTable(sources) {
  const box = el("section", "sec");
  box.append(el("h2", null, "Sources"));
  if (!sources.length) {
    box.append(el("div", "note", "No source has reported."));
    return box;
  }
  const t = el("table", "items");
  const head = el("tr");
  for (const h of ["Source", "State", "Reason", "Reported"]) head.append(el("th", null, h));
  t.append(head);
  for (const s of sources) {
    const r = el("tr");
    const state = !s.present ? "absent" : !s.available ? "unavailable" : s.stale ? "stale" : "ok";
    const st = el("td");
    st.append(pill(s.status), " ", el("span", "note", STATE_TEXT[state] || "available"));
    r.append(el("td", null, s.source), st, el("td", null, s.present ? s.reason : ""),
      el("td", s.stale ? "stalemark" : null, ago(s.age_seconds)));
    t.append(r);
  }
  box.append(t);
  return box;
}

function render(h) {
  document.title = `${h.host} - watchpost`;
  document.getElementById("summary").replaceChildren(
    el("span", `pill ${h.status}`, `${h.host}: ${h.status}`));
  const frag = document.createDocumentFragment();
  const banner = el("div", `banner ${h.status}`);
  banner.append(el("strong", null, h.host),
    ` ${h.platform || "unknown platform"}, agent ${h.agent_version || "unknown"}. `,
    h.heard ? `Last batch ${ago(h.age_seconds)}. ` : "No batch has ever arrived. ");
  if (h.status_reason) banner.append(el("div", null, h.status_reason));
  if (!h.monitored) {
    banner.append(el("div", "note", "Not listed as a pushed_host monitor, so it never alerts."));
  } else if (h.monitor) {
    banner.append(el("div", "note", `Monitor ${h.monitor.name}: ${h.monitor.effective_state}.`));
  }
  const b = h.boot;
  if (b.boot_ts) {
    const prev = b.clean_shutdown === true ? "previous shutdown was clean"
      : b.clean_shutdown === false ? "previous shutdown was a crash" : "previous shutdown unknown";
    banner.append(el("div", "note", `Last boot ${fmtTime(b.boot_ts)}: ${prev}`));
  }
  frag.append(banner);
  for (const [key, title] of SECTIONS) frag.append(section(title, h[key], key === "alerts"));
  const ev = el("section", "sec");
  ev.append(el("h2", null, "Recent events"));
  ev.append(h.events.length ? eventsList(h.events) : el("div", "note", "No events reported."));
  frag.append(ev, sourcesTable(h.sources));
  page.replaceChildren(frag);
  document.getElementById("footer").textContent = `refreshed ${new Date().toLocaleTimeString()}`;
}

async function refresh() {
  try {
    const r = await fetch(`/api/hosts/${encodeURIComponent(name)}`);
    if (r.status === 401) { location.assign("/login"); return; }
    if (r.status === 404) { page.replaceChildren(el("p", null, "Unknown host.")); return; }
    if (r.ok) render(await r.json());
  } catch (_) {
    document.getElementById("footer").textContent = "watchpost unreachable, retrying";
  }
}

refresh();
setInterval(refresh, 10000);
