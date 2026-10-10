// Pure rules for the UniFi page: which rows a scroll position shows, the filters, the option
// lists, uptime, and the words of the Wi-Fi readiness cards. No DOM here, so
// tests/js/unifi.test.mjs can run it without a browser.

// The slice of a long list to draw. `top` and `bottom` are the pixel heights of the rows left
// out above and below, so the scroll bar still spans the whole list.
export function windowFor(scrollTop, viewportH, rowH, count, overscan = 6) {
  const first = Math.max(0, Math.floor(Math.max(0, scrollTop) / rowH) - overscan);
  const last = Math.min(count, Math.ceil((Math.max(0, scrollTop) + viewportH) / rowH) + overscan);
  const start = Math.min(first, count);
  const end = Math.max(start, last);
  return { start, end, top: start * rowH, bottom: (count - end) * rowH };
}

// A client row's state word: connected, stale (connected but not refreshed lately), offline, or unknown when the console did not say.
export function clientState(c) {
  if (c.connected === true) return c.stale === true ? "stale" : "connected";
  if (c.connected === false) return "offline";
  return "unknown";
}

// Filter by a text query over name, MAC, address, SSID and uplink name, by kind, by state, by
// VLAN (the number as text) and by SSID (exact).
export function filterClients(rows, { q = "", kind = "all", state = "all", vlan = "", ssid = "" } = {}) {
  const needle = String(q).trim().toLowerCase();
  return rows.filter((c) => {
    if (kind !== "all" && c.kind !== kind) return false;
    if (state !== "all" && clientState(c) !== state) return false;
    if (vlan !== "" && vlan !== null && vlan !== undefined && String(c.vlan ?? "") !== String(vlan)) return false;
    if (ssid && c.ssid !== ssid) return false;
    if (!needle) return true;
    return [c.name, c.mac, c.ip, c.ssid, c.uplink_name, c.network].some(
      (v) => typeof v === "string" && v.toLowerCase().includes(needle));
  });
}

// The distinct VLANs of the rows, as text, in numeric order; and the distinct SSIDs, sorted.
export function vlanOptions(rows) {
  const seen = new Set();
  for (const c of rows) if (Number.isInteger(c.vlan)) seen.add(String(c.vlan));
  return [...seen].sort((a, b) => Number(a) - Number(b));
}

export function ssidOptions(rows) {
  const seen = new Set();
  for (const c of rows) if (typeof c.ssid === "string" && c.ssid) seen.add(c.ssid);
  return [...seen].sort((a, b) => a.localeCompare(b));
}

// Where a client sits: "Switch name port 7", "AP name", or an empty string.
export function attachment(c) {
  const where = c.uplink_name || c.uplink_mac || "";
  if (!where) return "";
  return Number.isInteger(c.sw_port) ? `${where} port ${c.sw_port}` : where;
}

// A time from the API is an RFC 3339 string; a number is taken as unix seconds.
export function toSeconds(v) {
  if (typeof v === "number") return v;
  const t = typeof v === "string" ? Date.parse(v) : NaN;
  return Number.isNaN(t) ? null : t / 1000;
}

// A connected client's uptime in seconds: the classic value when the console gave one, else the
// time since connected_at (the Integration field), judged by the server clock. Null when neither
// is known or the client is not connected.
export function uptimeSeconds(c, now) {
  if (clientState(c) === "offline" || clientState(c) === "unknown") return null;
  if (Number.isInteger(c.uptime_s) && c.uptime_s >= 0) return c.uptime_s;
  const since = toSeconds(c.connected_at);
  const at = toSeconds(now);
  if (since === null || at === null || at < since) return null;
  return Math.round(at - since);
}

// A row is stale when it says it is up but was not refreshed within `after` seconds of `now`
// (the server clock from /unifi/status, so the browser clock does not matter). A missing time or
// window never makes a row stale.
function isStale(up, seenAt, now, after) {
  const seen = toSeconds(seenAt);
  const at = toSeconds(now);
  return up && seen !== null && at !== null && typeof after === "number" && at - seen > after;
}

export function markClientsStale(rows, now, after) {
  return rows.map((c) => ({ ...c, stale: isStale(c.connected === true, c.last_seen, now, after) }));
}

export function markCamerasStale(rows, now, after) {
  return rows.map((c) => ({ ...c,
    stale: isStale(c.connected === true || c.recording === true, c.last_seen, now, after) }));
}

// What the "Last seen" column means for a client row. A connected client is seen now: its
// connected_at is when it joined (the Uptime column), never a last-seen time. A stale row (said
// connected but not refreshed lately) and an offline one show when it was last seen; with no
// time the cell is empty. Returns { now: true } or { at: <time or null> }.
export function lastSeenCell(c) {
  if (clientState(c) === "connected") return { now: true };
  return { at: c.last_seen ?? null };
}

// ---- Overview ----

// The site line under Network status: the site's name, else its id, else not polled yet.
export function siteText(o) {
  if (o.site_name) return `site ${o.site_name}`;
  return o.site_id ? `site ${o.site_id}` : "no site polled yet";
}

// The Internet tile, or null to leave it out when the state is not known (an "Unknown" tile
// beside live WAN traffic only confused). `inferred` is set when the state comes from the
// gateway's WAN traffic rather than a state the console reported.
export function internetTile(o) {
  if (o.internet_up !== true && o.internet_up !== false) return null;
  return { up: o.internet_up, inferred: o.internet_source === "wan_traffic" };
}

// The SSID bars worth drawing: only named SSIDs. Without the classic account no client has an
// SSID, so there are none and the card is left out.
export function ssidBarRows(o) {
  return (Array.isArray(o.clients_per_ssid) ? o.clients_per_ssid : []).filter(
    (r) => typeof r.ssid === "string" && r.ssid && r.count > 0);
}

// ---- Wi-Fi readiness words ----

// The SSID the Wi-Fi section opens on: the first whose name says IoT (the HA SOC rule), else all.
export function defaultSsid(wlans) {
  const hit = wlans.find((w) => typeof w.name === "string" && w.name.toLowerCase().includes("iot"));
  return hit ? hit.name : "";
}

// Which access points may broadcast the SSID, in words.
export function permittedApsText(w) {
  const mode = (w.ap_group_mode || "").toLowerCase();
  if (!mode || mode === "all") return "Every access point";
  const names = Array.isArray(w.ap_names) ? w.ap_names.filter((n) => typeof n === "string" && n) : [];
  if (names.length) return names.join(", ");
  return `Restricted to access point groups (mode ${mode}); the groups are not read, so the access points cannot be named`;
}

// Which access points carry a connected client on the SSID now, in words.
export function carryingText(w) {
  const aps = Array.isArray(w.carrying_aps) ? w.carrying_aps : [];
  const n = Number.isInteger(w.client_count) ? w.client_count : 0;
  if (!n) return "none";
  const who = aps.length ? aps.join(", ") : "an access point the poll did not name";
  return `${who} (${n} client${n === 1 ? "" : "s"})`;
}

// The band words of a classic wlan_band value (both, 2g, 5g, 6g; unverified).
export function bandText(band) {
  const b = (band || "").toLowerCase();
  if (!b) return "Not reported";
  if (b === "both") return "2.4 GHz, 5 GHz";
  return b.replace(/(\d)g\b/g, (_, d) => (d === "2" ? "2.4 GHz" : `${d} GHz`));
}
