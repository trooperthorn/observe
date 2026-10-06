// The UniFi page: Devices, Clients and Protect tabs. Every string came from the console or from a
// client on the network, so it is written with textContent only, never as markup. Helpers come
// from the shared console modules.
import { el, whoami, when } from "/static/infra-common.js";
import { statusChip, monoTag } from "/static/js/chips.js";
import { sortableTable } from "/static/js/table.js";
import { attachment, clientState, filterClients, windowFor } from "/plugins/unifi/static/vlist-core.js";

const page = document.getElementById("page");
const footer = document.getElementById("footer");
const API = "/api/plugins/unifi";
const TABS = [["devices", "Devices"], ["clients", "Clients"], ["protect", "Protect"]];
const DEVICE_STATES = { ONLINE: ["up", "Online"], OFFLINE: ["down", "Offline"],
  UPDATING: ["warn", "Updating"], PENDING_ADOPTION: ["pending", "Pending adoption"] };

async function get(path) {
  const r = await fetch(`${API}${path}`);
  if (r.status === 401) { location.assign("/login"); throw new Error("not signed in"); }
  if (!r.ok) throw new Error(`request failed (${r.status})`);
  return r.json();
}

function kpi(value, label) {
  const k = el("div", "kpi");
  k.append(el("span", "kpi-value", String(value)), el("span", "kpi-label", label));
  return k;
}

function section(title, ...kids) {
  const s = el("section", "card");
  s.append(el("h3", null, title), ...kids);
  return s;
}

function note(text) { return el("p", "note", text); }

function mac(v) { return v ? monoTag(v) : el("span", "muted", "unknown"); }

function boolWord(v, yes, no) { return v === true ? yes : v === false ? no : "Not reported"; }

function stateOf(map, raw) {
  const m = map[String(raw || "").toUpperCase()];
  return m ? statusChip(m[0], m[1]) : statusChip("unavailable", raw ? String(raw) : "No data");
}

// ---- Devices ----

function devicesView(d) {
  const rows = d.devices;
  const online = rows.filter((x) => String(x.state).toUpperCase() === "ONLINE").length;
  const updatable = rows.filter((x) => x.firmware_updatable === true).length;
  const kpis = el("div", "kpi-row");
  kpis.append(kpi(rows.length, "Devices"), kpi(online, "Online"),
    kpi(rows.length - online, "Not online"), kpi(updatable, "Firmware updates"));
  const columns = [
    { key: "name", label: "Name", get: (r) => r.name || r.mac, render: (r) => el("span", null, r.name || r.mac || "unnamed") },
    { key: "state", label: "State", get: (r) => r.state, render: (r) => stateOf(DEVICE_STATES, r.state) },
    { key: "model", label: "Model", get: (r) => r.model },
    { key: "ip", label: "Address", get: (r) => r.ip },
    { key: "mac", label: "MAC", get: (r) => r.mac, render: (r) => mac(r.mac) },
    { key: "fw", label: "Firmware", get: (r) => r.firmware,
      render: (r) => el("span", null, r.firmware + (r.firmware_updatable === true ? " (update available)" : "")) },
    { key: "seen", label: "Last seen", get: (r) => r.last_seen, render: (r) => el("span", null, when(r.last_seen)) },
  ];
  const t = sortableTable({ columns, rows, caption: "UniFi devices",
    empty: "No UniFi devices yet. They appear after the first poll of the console." });
  const frag = document.createDocumentFragment();
  frag.append(kpis, section("Devices", t.root));
  return frag;
}

// ---- Clients: a filterable table that draws only the rows in view ----

const ROW_H = 40;
const COLS = ["Name", "State", "Kind", "Address", "MAC", "Connected to", "Network", "Last seen"];

function clientRow(c, index) {
  const tr = el("tr", "vrow");
  tr.setAttribute("aria-rowindex", String(index + 2));
  const state = clientState(c);
  const chip = state === "connected" ? statusChip("up", "Connected")
    : state === "stale" ? statusChip("stale", "Stale")
    : state === "offline" ? statusChip("stale", "Offline") : statusChip("unavailable", "Unknown");
  const cells = [el("span", null, c.name || "unnamed"), chip, el("span", null, c.kind || "unknown"),
    el("span", null, c.ip), mac(c.mac), el("span", null, attachment(c)), el("span", null, c.ssid),
    el("span", null, state === "connected" ? when(c.connected_at || c.last_seen) : when(c.last_seen))];
  for (const x of cells) { const td = el("td"); td.append(x); tr.append(td); }
  return tr;
}

function spacer(height) {
  const tr = el("tr", "vspace");
  tr.setAttribute("aria-hidden", "true");
  const td = el("td");
  td.colSpan = COLS.length;
  td.setAttribute("height", String(height));  // an attribute, not an inline style
  tr.append(td);
  return tr;
}

function clientsView(d) {
  const all = d.clients;
  const connected = all.filter((c) => clientState(c) === "connected").length;
  const frag = document.createDocumentFragment();
  const kpis = el("div", "kpi-row");
  kpis.append(kpi(all.length, "Clients"), kpi(connected, "Connected"),
    kpi(all.filter((c) => clientState(c) === "stale").length, "Stale"),
    kpi(all.filter((c) => c.connected === false).length, "Offline"),
    kpi(all.filter((c) => c.kind === "wireless").length, "Wireless"));
  frag.append(kpis);

  const filters = el("div", "filters");
  const q = el("input");
  q.type = "search";
  q.setAttribute("aria-label", "Filter clients by name, MAC, address, network or device");
  q.placeholder = "Filter clients";
  const kind = el("select");
  kind.setAttribute("aria-label", "Kind");
  const state = el("select");
  state.setAttribute("aria-label", "State");
  for (const [sel, opts] of [[kind, [["all", "All kinds"], ["wired", "Wired"], ["wireless", "Wireless"]]],
    [state, [["all", "Any state"], ["connected", "Connected"], ["stale", "Stale"], ["offline", "Offline"], ["unknown", "Unknown"]]]]) {
    for (const [v, label] of opts) { const o = el("option", null, label); o.value = v; sel.append(o); }
  }
  const count = el("span", "muted");
  count.setAttribute("aria-live", "polite");
  filters.append(q, kind, state, count);

  const scroll = el("div", "vscroll");
  scroll.tabIndex = 0;
  scroll.setAttribute("role", "region");
  scroll.setAttribute("aria-label", "Clients table, scrollable");
  const table = el("table", "data");
  table.append(el("caption", "sr-only", "UniFi clients"));
  const head = el("thead");
  const hr = el("tr");
  hr.setAttribute("aria-rowindex", "1");
  for (const h of COLS) { const th = el("th", null, h); th.scope = "col"; hr.append(th); }
  head.append(hr);
  const body = el("tbody");
  table.append(head, body);
  scroll.append(table);
  const empty = el("p", "empty muted");

  let shown = all;
  let rowH = ROW_H;
  const draw = () => {
    const w = windowFor(scroll.scrollTop, scroll.clientHeight || 480, rowH, shown.length);
    const rows = [];
    if (w.top) rows.push(spacer(w.top));
    for (let i = w.start; i < w.end; i++) rows.push(clientRow(shown[i], i));
    if (w.bottom) rows.push(spacer(w.bottom));
    body.replaceChildren(...rows);
  };
  const refilter = () => {
    shown = filterClients(all, { q: q.value, kind: kind.value, state: state.value });
    table.setAttribute("aria-rowcount", String(shown.length + 1));
    count.textContent = `Showing ${shown.length} of ${all.length}`;
    scroll.scrollTop = 0;
    empty.textContent = all.length ? "No client matches the filter." : "No clients yet. They appear after the first poll of the console.";
    empty.hidden = shown.length > 0;
    scroll.hidden = shown.length === 0;
    draw();
    const first = body.querySelector("tr.vrow");
    if (first) {
      const h = first.getBoundingClientRect().height;
      if (h > 0 && Math.abs(h - rowH) > 0.5) { rowH = h; draw(); }
    }
  };
  scroll.addEventListener("scroll", draw);
  q.addEventListener("input", refilter);
  kind.addEventListener("change", refilter);
  state.addEventListener("change", refilter);

  const notes = [];
  if (d.truncated) notes.push(note(`Showing the first ${all.length} of ${d.total} clients.`));
  if (d.classic_configured && d.classic_note) notes.push(note(d.classic_note));
  if (!d.classic_configured) notes.push(note("Offline clients, switch ports and Wi-Fi names need the optional classic controller account."));
  frag.append(section("Clients", filters, scroll, empty, ...notes));
  refilter();
  return frag;
}

// ---- Protect ----

function protectView(d) {
  const frag = document.createDocumentFragment();
  if (!d.enabled) {
    frag.append(section("Protect cameras", el("p", "empty muted",
      "Protect is not enabled. Set protect to true in the unifi plugin settings to poll the cameras.")));
    return frag;
  }
  const cams = d.cameras;
  const kpis = el("div", "kpi-row");
  kpis.append(kpi(cams.length, "Cameras"), kpi(cams.filter((c) => c.connected === true && !c.stale).length, "Connected"),
    kpi(cams.filter((c) => c.recording === true && !c.stale).length, "Recording"),
    kpi(cams.filter((c) => c.stale).length, "Stale"));
  const columns = [
    { key: "name", label: "Name", get: (r) => r.name || r.mac, render: (r) => el("span", null, r.name || r.mac || "unnamed") },
    { key: "state", label: "State", get: (r) => r.state, render: (r) => el("span", null, r.state || "Not reported") },
    { key: "connected", label: "Connection", get: (r) => (r.connected === null ? null : Number(r.connected)),
      render: (r) => (r.stale ? statusChip("stale", "Stale")
        : r.connected === true ? statusChip("up", "Connected")
        : r.connected === false ? statusChip("down", "Disconnected") : statusChip("unavailable", "Not reported")) },
    { key: "recording", label: "Recording", get: (r) => (r.recording === null ? null : Number(r.recording)),
      render: (r) => el("span", null, r.stale ? "Stale" : boolWord(r.recording, "Recording", "Not recording")) },
    { key: "model", label: "Model", get: (r) => r.model },
    { key: "mac", label: "MAC", get: (r) => r.mac, render: (r) => mac(r.mac) },
    { key: "seen", label: "Last seen", get: (r) => r.last_seen, render: (r) => el("span", null, when(r.last_seen)) },
  ];
  const t = sortableTable({ columns, rows: cams, caption: "Protect cameras",
    empty: "No cameras yet. They appear after the first poll of Protect." });
  frag.append(kpis, section("Protect cameras", t.root,
    note("Recording is shown only when the console reports it. NVR storage is not read, because no route for it is verified.")));
  return frag;
}

// ---- shell ----

const LOADERS = { devices: ["/devices", devicesView], clients: ["/clients", clientsView], protect: ["/protect", protectView] };

function tabFromHash() {
  const h = location.hash.replace("#", "");
  return LOADERS[h] ? h : "devices";
}

async function show(tab) {
  const frag = document.createDocumentFragment();
  const crumbs = el("p", "crumbs", "Network / UniFi");
  const tabs = el("div", "tabs");
  tabs.setAttribute("role", "tablist");
  for (const [id, label] of TABS) {
    const b = el("button", "tab", label);
    b.type = "button";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", id === tab ? "true" : "false");
    b.addEventListener("click", () => { location.hash = id; });
    tabs.append(b);
  }
  const panel = el("div");
  panel.setAttribute("role", "tabpanel");
  frag.append(crumbs, el("h1", null, "UniFi"), tabs, panel);
  page.replaceChildren(frag);
  const [path, view] = LOADERS[tab];
  try {
    panel.append(view(await get(path)));
  } catch (err) {
    panel.append(el("p", "empty muted", err.message === "not signed in" ? "Signing in." : "The UniFi data could not be loaded."));
  }
}

async function main() {
  await whoami();
  footer.textContent = "UniFi data is read from the console on a schedule, not live.";
  window.addEventListener("hashchange", () => show(tabFromHash()));
  await show(tabFromHash());
}

main().catch(() => {});
