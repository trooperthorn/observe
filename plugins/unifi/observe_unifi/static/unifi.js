// The UniFi page: the network overview (five tiles and clients per SSID), then the Clients,
// Devices, Wi-Fi and Protect tabs. Every string came from the console or from a client on the
// network, so it is written with textContent only, never as markup. Helpers come from the shared
// console modules. The layout follows the HA SOC network view (ha_Int_soc, MIT, same owner).
import { el, get, getAll, whoami, when } from "/static/infra-common.js";
import { seconds } from "/static/js/api.js";
import { formatValue } from "/static/js/format.js";
import { neutralChip, statusChip, monoTag } from "/static/js/chips.js";
import { sortableTable } from "/static/js/table.js";
import { attachment, bandText, carryingText, clientState, defaultSsid, filterClients,
  markCamerasStale, markClientsStale, permittedApsText, ssidOptions, uptimeSeconds,
  vlanOptions, windowFor } from "/plugins/unifi/static/vlist-core.js";

const page = document.getElementById("page");
const footer = document.getElementById("footer");
const API = "/api/v2/unifi";
const TABS = [["clients", "Clients"], ["devices", "Devices"], ["wifi", "Wi-Fi join"], ["protect", "Protect"]];
const DEVICE_STATES = { ONLINE: ["up", "Online"], OFFLINE: ["down", "Offline"],
  UPDATING: ["warn", "Updating"], PENDING_ADOPTION: ["pending", "Pending adoption"] };
const DASH = "—";
const DEVICE_PAGES = [25, 50, 100];
const CLASSIC_NOTE = "Offline clients, switch ports, VLANs, uptime, bandwidth, Wi-Fi names and the SSID configuration need the optional classic controller account.";

// The SSID filter chosen on the overview bars; it is applied to the clients table. While the
// Clients tab is open, `applySsid` is that table's setter; otherwise the tab is opened with it.
let ssidFilter = "";
let applySsid = null;

function kpi(value, label, sub) {
  const k = el("div", "kpi");
  const v = el("span", "kpi-value");
  if (value instanceof Node) v.append(value); else v.textContent = String(value);
  k.append(v, el("span", "kpi-label", label));
  if (sub !== undefined && sub !== null && sub !== "") k.append(el("span", "kpi-sub", sub));
  return k;
}

function section(title, ...kids) {
  const s = el("section", "card");
  s.append(el("h3", null, title), ...kids);
  return s;
}

function note(text) { return el("p", "note", text); }

function dash() { return el("span", "dash", DASH); }

function mac(v) { return v ? monoTag(v) : el("span", "muted", "unknown"); }

function boolWord(v, yes, no) { return v === true ? yes : v === false ? no : "Not reported"; }

function stateOf(map, raw) {
  const m = map[String(raw || "").toUpperCase()];
  return m ? statusChip(m[0], m[1]) : statusChip("unavailable", raw ? String(raw) : "No data");
}

// A rate from the console is bytes per second (unverified unit); it is shown in bits per second.
function rateText(bytesPerSecond) {
  return typeof bytesPerSecond === "number" ? formatValue(bytesPerSecond * 8, "bit/s") : DASH;
}

function bytesText(v) { return typeof v === "number" ? formatValue(v, "By") : DASH; }

// "down x up y": two rates or two totals, with the direction as a word for screen readers.
function pair(down, up) {
  const s = el("span", "rates");
  const d = el("span", null);
  d.append(el("span", "dir", "↓ "), document.createTextNode(down));
  d.setAttribute("aria-label", `down ${down}`);
  const u = el("span", null);
  u.append(el("span", "dir", "↑ "), document.createTextNode(up));
  u.setAttribute("aria-label", `up ${up}`);
  s.append(d, u);
  return s;
}

function twoLine(main, sub) {
  const box = el("span");
  box.append(el("span", "cell-main", main));
  const line = el("span", "cell-sub");
  for (const x of sub) {
    if (!x) continue;
    if (line.childNodes.length) line.append(el("span", "dash", "·"));
    line.append(x instanceof Node ? x : el("span", null, x));
  }
  if (line.childNodes.length) box.append(line);
  return box;
}

function vlanText(v) { return Number.isInteger(v) ? String(v) : DASH; }

function uptimeText(c, now) {
  const s = uptimeSeconds(c, now);
  return s === null ? DASH : formatValue(s, "s");
}

// ---- Overview: the five tiles and the clients per SSID ----

function overviewView(o) {
  const frag = document.createDocumentFragment();
  const tiles = el("div", "kpi-row five");
  const status = o.status === "online" ? statusChip("up", "Online")
    : o.status === "offline" ? statusChip("down", "Offline") : statusChip("unavailable", "Unknown");
  tiles.append(kpi(status, "Network status", o.site_id ? `site ${o.site_id}` : "no site polled yet"));
  const inet = o.internet_up === true ? statusChip("up", "Connected")
    : o.internet_up === false ? statusChip("down", "Down") : statusChip("unavailable", "Unknown");
  tiles.append(kpi(inet, "Internet", o.wan.ip ? `WAN ${o.wan.ip}` : (o.wan.port || DASH)));
  const wan = kpi(pair(rateText(o.wan.rx_rate_bps), rateText(o.wan.tx_rate_bps)), "WAN bandwidth",
    o.wan.port ? `port ${o.wan.port}` : DASH);
  wan.querySelector(".kpi-value").classList.add("small");
  tiles.append(wan);
  tiles.append(kpi(o.wireless_clients, "Wireless clients", `${o.wired_clients} wired`));
  tiles.append(kpi(o.total_clients, "Total clients", `${o.device_count} network devices`));
  frag.append(tiles);
  const notes = [];
  if (o.devices_stale) notes.push(el("p", "note stale", `Stale: the console has not been read successfully since ${when(o.devices_updated)}.`));
  if (o.classic_configured && o.classic_stale) notes.push(el("p", "note stale", `Stale: the classic views have not been read successfully since ${when(o.classic_updated)}.`));
  if (o.classic_configured && o.classic_note) notes.push(note(o.classic_note));
  frag.append(...notes);
  if (o.clients_per_ssid.length) frag.append(ssidBars(o.clients_per_ssid));
  return frag;
}

function ssidBars(rows) {
  const list = el("div", "ssid-list");
  list.setAttribute("role", "list");
  const max = Math.max(1, ...rows.map((r) => r.count));
  for (const r of rows) {
    const row = el("div", "ssid-row");
    row.setAttribute("role", "listitem");
    const b = el("button", "ssid-name", r.ssid);
    b.type = "button";
    b.setAttribute("aria-pressed", ssidFilter === r.ssid ? "true" : "false");
    b.title = `Filter the clients table to ${r.ssid}`;
    b.addEventListener("click", () => {
      ssidFilter = ssidFilter === r.ssid ? "" : r.ssid;
      for (const other of list.querySelectorAll(".ssid-name")) {
        other.setAttribute("aria-pressed", other.textContent === ssidFilter ? "true" : "false");
      }
      if (applySsid) applySsid(ssidFilter);
      else location.hash = "clients";
    });
    const bar = el("progress");
    bar.max = max;
    bar.value = r.count;
    bar.setAttribute("aria-label", `${r.count} of ${max} clients`);
    row.append(b, bar, el("span", "ssid-count", String(r.count)));
    list.append(row);
  }
  const head = el("h3", null, "Clients per SSID");
  head.append(el("span", "card-hint", "click to filter the table"));
  const card = el("section", "card");
  card.append(head, list);
  return card;
}

// ---- Devices ----

function devicesView(d) {
  const rows = d.devices;
  const columns = [
    { key: "name", label: "Device", get: (r) => r.name || r.mac,
      render: (r) => twoLine(r.name || r.mac || "unnamed", [r.state ? String(r.state).toLowerCase() : ""]) },
    { key: "ip", label: "IPv4", get: (r) => r.ip, render: (r) => (r.ip ? monoTag(r.ip) : dash()) },
    { key: "mac", label: "MAC", get: (r) => r.mac, render: (r) => mac(r.mac) },
    // The management VLAN is not read from either API, so the column is a dash until it is.
    { key: "vlan", label: "VLAN", numeric: true, get: () => null, render: () => dash() },
    { key: "model", label: "Model", get: (r) => r.model, render: (r) => el("span", null, r.model || DASH) },
    { key: "fw", label: "Firmware", get: (r) => (r.firmware_updatable === null ? null : Number(r.firmware_updatable)),
      render: (r) => (r.firmware_updatable === true ? statusChip("warn", "Update available")
        : r.firmware_updatable === false ? statusChip("up", "Up to date") : statusChip("unavailable", "Not reported")) },
    { key: "bw", label: "Bandwidth", get: (r) => (typeof r.rx_bytes === "number" || typeof r.tx_bytes === "number" ? (r.rx_bytes || 0) + (r.tx_bytes || 0) : null),
      render: (r) => (typeof r.rx_bytes === "number" || typeof r.tx_bytes === "number" ? pair(bytesText(r.rx_bytes), bytesText(r.tx_bytes)) : dash()) },
    { key: "seen", label: "Last seen", get: (r) => r.last_seen, render: (r) => el("span", null, when(r.last_seen)) },
  ];
  const q = el("input");
  q.type = "search";
  q.setAttribute("aria-label", "Search devices by name, address, MAC or model");
  q.placeholder = "Search devices";
  const filters = el("div", "filters");
  const count = el("span", "muted");
  count.setAttribute("aria-live", "polite");
  filters.append(q, count);
  const t = sortableTable({ columns, rows, caption: "UniFi devices", pageSizes: DEVICE_PAGES,
    empty: "No UniFi devices yet. They appear after the first poll of the console." });
  const refilter = () => {
    const needle = q.value.trim().toLowerCase();
    const shown = needle ? rows.filter((r) => [r.name, r.ip, r.mac, r.model].some(
      (v) => typeof v === "string" && v.toLowerCase().includes(needle))) : rows;
    count.textContent = `Showing ${shown.length} of ${rows.length}`;
    t.setRows(shown);
  };
  q.addEventListener("input", refilter);
  refilter();
  const frag = document.createDocumentFragment();
  if (d.stale) {
    frag.append(el("p", "note stale", `Stale: the console has not been read successfully since ${when(d.last_update)}. The devices below may be out of date.`));
  } else if (d.last_update) {
    frag.append(el("p", "note muted", `Updated ${when(d.last_update)}`));
  }
  frag.append(section("Network devices", filters, t.root,
    note("Bandwidth is the cumulative bytes the console reports for the device, when it reports them. The management VLAN is not read from either API.")));
  return frag;
}

// ---- Clients: a filterable table that draws only the rows in view ----

const ROW_H = 48;
const COLS = ["Client", "IPv4", "MAC", "VLAN", "SSID", "Uptime", "Bandwidth", "Last seen"];

function clientRow(c, index, now) {
  const tr = el("tr", "vrow");
  tr.setAttribute("aria-rowindex", String(index + 2));
  const state = clientState(c);
  const chip = state === "stale" ? statusChip("stale", "Stale")
    : state === "offline" ? statusChip("stale", "Offline")
    : state === "unknown" ? statusChip("unavailable", "Unknown") : null;
  const where = attachment(c);
  const sub = [c.kind === "wireless" ? "wireless" : c.kind === "wired" ? "wired" : "", chip,
    where ? `on ${where}` : ""];
  const ssid = c.ssid ? el("span", null, c.ssid) : c.kind === "wired" ? el("span", "muted", "wired") : dash();
  const hasRate = typeof c.rx_rate_bps === "number" || typeof c.tx_rate_bps === "number";
  const cells = [twoLine(c.name || "unnamed", sub), c.ip ? monoTag(c.ip) : dash(), mac(c.mac),
    el("span", null, vlanText(c.vlan)), ssid, el("span", null, uptimeText(c, now)),
    hasRate ? pair(rateText(c.rx_rate_bps), rateText(c.tx_rate_bps)) : dash(),
    el("span", null, state === "connected" ? when(c.connected_at || c.last_seen) : when(c.last_seen))];
  cells.forEach((x, i) => {
    const td = el("td", i === 3 ? "num" : i === 1 || i === 2 ? "mono" : null);
    td.append(x);
    tr.append(td);
  });
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

function select(label, options, value) {
  const wrap = el("label", null, label + " ");
  const sel = el("select");
  sel.setAttribute("aria-label", label);
  for (const [v, text] of options) {
    const o = el("option", null, text);
    o.value = v;
    if (v === value) o.selected = true;
    sel.append(o);
  }
  wrap.append(sel);
  return [wrap, sel];
}

function clientsView(d) {
  const all = d.clients;
  const frag = document.createDocumentFragment();
  const filters = el("div", "filters");
  const q = el("input");
  q.type = "search";
  q.setAttribute("aria-label", "Search clients by name, MAC, address, network, SSID or device");
  q.placeholder = "Search clients";
  const [vlanWrap, vlan] = select("VLAN", [["", "All VLANs"], ...vlanOptions(all).map((v) => [v, v])], "");
  const ssids = ssidOptions(all);
  if (ssidFilter && !ssids.includes(ssidFilter)) ssidFilter = "";
  const [ssidWrap, ssid] = select("SSID", [["", "All SSIDs"], ...ssids.map((s) => [s, s])], ssidFilter);
  const [stateWrap, state] = select("State", [["all", "Any state"], ["connected", "Connected"],
    ["stale", "Stale"], ["offline", "Offline"], ["unknown", "Unknown"]], "all");
  const count = el("span", "muted");
  count.setAttribute("aria-live", "polite");
  filters.append(q, vlanWrap, ssidWrap, stateWrap, count);

  const scroll = el("div", "vscroll");
  scroll.tabIndex = 0;
  scroll.setAttribute("role", "region");
  scroll.setAttribute("aria-label", "Clients table, scrollable");
  const table = el("table", "data");
  table.append(el("caption", "sr-only", "UniFi clients"));
  const head = el("thead");
  const hr = el("tr");
  hr.setAttribute("aria-rowindex", "1");
  for (const h of COLS) { const th = el("th", h === "VLAN" ? "num" : null, h); th.scope = "col"; hr.append(th); }
  head.append(hr);
  const body = el("tbody");
  table.append(head, body);
  scroll.append(table);
  const empty = el("p", "empty muted");

  let shown = all;
  let rowH = ROW_H;
  const draw = () => {
    const w = windowFor(scroll.scrollTop, scroll.clientHeight || 520, rowH, shown.length);
    const rows = [];
    if (w.top) rows.push(spacer(w.top));
    for (let i = w.start; i < w.end; i++) rows.push(clientRow(shown[i], i, d.now));
    if (w.bottom) rows.push(spacer(w.bottom));
    body.replaceChildren(...rows);
  };
  const refilter = () => {
    shown = filterClients(all, { q: q.value, state: state.value, vlan: vlan.value, ssid: ssid.value });
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
  vlan.addEventListener("change", refilter);
  ssid.addEventListener("change", () => { ssidFilter = ssid.value; refilter(); });
  state.addEventListener("change", refilter);
  // A click on an SSID bar above the tabs changes the filter while this tab is open.
  applySsid = (value) => {
    ssid.value = ssids.includes(value) ? value : "";
    refilter();
  };

  const notes = [note("A column shown as a dash is not reported by the console for that row. Bandwidth is the live rate the console reports, shown in bits per second.")];
  if (d.classic_configured && d.classic_note) notes.push(note(d.classic_note));
  if (!d.classic_configured) notes.push(note(CLASSIC_NOTE));
  frag.append(section("Clients", filters, scroll, empty, ...notes));
  refilter();
  return frag;
}

// ---- Wi-Fi join diagnostics ----

const WIFI_INTRO = "No UniFi source records an association attempt or an authentication failure, so nothing here says a client failed. What it shows is the configuration that decides whether a join is permitted, which access points carry each SSID, and the wireless clients the controller knows but is not carrying now.";

function finding(f) {
  const li = el("li", `finding ${f.severity}`);
  li.append(el("span", "sev", f.severity), el("span", null, f.message));
  return li;
}

function ssidCard(w) {
  const card = el("article", "wifi-ssid");
  const head = el("div", "wifi-head");
  head.append(el("strong", null, w.name));
  head.append(w.enabled === true ? statusChip("up", "Enabled") : w.enabled === false ? statusChip("down", "Disabled")
    : statusChip("unavailable", "Not reported"));
  if (w.guest === true) head.append(neutralChip("Guest"));
  if (w.security) head.append(neutralChip(w.security));
  if (w.hidden === true) head.append(neutralChip("Hidden"));
  card.append(head);
  const grid = el("dl", "wifi-grid");
  const row = (k, v) => { grid.append(el("dt", null, k), el("dd", null, v)); };
  row("Network", w.network_name ? (Number.isInteger(w.vlan) ? `${w.network_name} (VLAN ${w.vlan})` : w.network_name)
    : Number.isInteger(w.vlan) ? `VLAN ${w.vlan}` : DASH);
  row("Radios", bandText(w.band));
  row("Permitted APs", permittedApsText(w));
  row("Carrying clients now", carryingText(w));
  card.append(grid);
  if (w.findings.length) {
    const ul = el("ul", "wifi-findings");
    for (const f of w.findings) ul.append(finding(f));
    card.append(ul);
  } else {
    const ok = el("p", "wifi-ok");
    ok.append(statusChip("up", "Ready"), document.createTextNode(" " + w.summary));
    card.append(ok);
  }
  return card;
}

function wifiView(d) {
  const frag = document.createDocumentFragment();
  const card = el("section", "card");
  card.append(el("h3", null, "Wi-Fi Join Diagnostics"), el("p", "intro", WIFI_INTRO));
  if (!d.classic_configured) {
    card.append(note(CLASSIC_NOTE));
  } else if (!d.wlans.length) {
    card.append(note("The classic views have not given any SSID yet. They appear after the first classic poll, when the console answers rest/wlanconf."));
  } else {
    const [wrap, sel] = select("SSID", [["", "All SSIDs"], ...d.wlans.map((w) => [w.name, w.name])], defaultSsid(d.wlans));
    const filters = el("div", "filters");
    filters.append(wrap);
    const list = el("div");
    const draw = () => {
      const chosen = sel.value ? d.wlans.filter((w) => w.name === sel.value) : d.wlans;
      list.replaceChildren(...chosen.map(ssidCard));
    };
    sel.addEventListener("change", draw);
    draw();
    card.append(filters, list);
  }
  card.append(el("h4", null, "Known but not connected"));
  if (!d.classic_configured) {
    card.append(el("p", "muted", "This list needs the classic controller account. A client that has never associated appears in no collection at all, so its absence here is not evidence that it is fine."));
  } else if (!d.absent.length) {
    card.append(el("p", "muted", "Every wireless client the controller knows is connected right now."));
  } else {
    const columns = [
      { key: "name", label: "Client", get: (r) => r.name || r.mac, render: (r) => el("span", null, r.name || r.mac || "unnamed") },
      { key: "mac", label: "MAC", get: (r) => r.mac, render: (r) => mac(r.mac) },
      { key: "ssid", label: "Last SSID", get: (r) => r.ssid, render: (r) => (r.ssid ? el("span", null, r.ssid) : dash()) },
      { key: "seen", label: "Last seen", get: (r) => r.last_seen, render: (r) => el("span", null, when(r.last_seen)) },
    ];
    card.append(sortableTable({ columns, rows: d.absent, caption: "Known but not connected clients",
      pageSizes: DEVICE_PAGES, empty: "Every wireless client the controller knows is connected right now." }).root);
  }
  frag.append(card);
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

// Each tab reads the status (the clock and the stale windows) and its list, then hands its view
// the shape it draws.
const LOADERS = {
  devices: async () => {
    const [status, devices] = await Promise.all([get(`${API}/status`), getAll(`${API}/devices`)]);
    return devicesView({ devices, stale: status.devices_stale, last_update: seconds(status.devices_updated) });
  },
  clients: async () => {
    const [status, clients] = await Promise.all([get(`${API}/status`), getAll(`${API}/clients`)]);
    return clientsView({ clients: markClientsStale(clients, status.now, status.clients_stale_after), now: status.now,
      classic_configured: status.classic_configured, classic_note: status.classic_note });
  },
  wifi: async () => {
    const status = await get(`${API}/status`);
    const [wlans, absent] = status.classic_configured
      ? await Promise.all([get(`${API}/wlans`), getAll(`${API}/absent-clients`)]) : [{ items: [] }, []];
    return wifiView({ classic_configured: status.classic_configured, wlans: wlans.items, absent });
  },
  protect: async () => {
    const status = await get(`${API}/status`);
    const cameras = status.protect_enabled ? await getAll(`${API}/cameras`) : [];
    return protectView({ enabled: status.protect_enabled,
      cameras: markCamerasStale(cameras, status.now, status.protect_stale_after) });
  },
};

function tabFromHash() {
  const h = location.hash.replace("#", "");
  return LOADERS[h] ? h : "clients";
}

async function show(tab) {
  const frag = document.createDocumentFragment();
  const crumbs = el("p", "crumbs", "Network / UniFi");
  const top = el("div");
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
  // The footer line is kept as the last child of the page, inside its container.
  frag.append(crumbs, el("h1", null, "UniFi"), top, tabs, panel, footer);
  applySsid = null;
  page.replaceChildren(frag);
  try {
    const [overview, body] = await Promise.all([get(`${API}/overview`), LOADERS[tab]()]);
    top.append(overviewView(overview));
    panel.append(body);
  } catch (err) {
    panel.append(el("p", "empty muted", err.status === 401 ? "Signing in." : "The UniFi data could not be loaded."));
  }
}

async function main() {
  await whoami();
  footer.textContent = "UniFi data is read from the console on a schedule, not live.";
  window.addEventListener("hashchange", () => show(tabFromHash()));
  await show(tabFromHash());
}

main().catch(() => {});
