// Pure rules for the UniFi clients table: which rows a scroll position shows, and the filters.
// No DOM here, so tests/js/unifi.test.mjs can run it without a browser.

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

// Filter by a text query over name, MAC, address, SSID and uplink name, by kind and by state.
export function filterClients(rows, { q = "", kind = "all", state = "all" } = {}) {
  const needle = String(q).trim().toLowerCase();
  return rows.filter((c) => {
    if (kind !== "all" && c.kind !== kind) return false;
    if (state !== "all" && clientState(c) !== state) return false;
    if (!needle) return true;
    return [c.name, c.mac, c.ip, c.ssid, c.uplink_name].some(
      (v) => typeof v === "string" && v.toLowerCase().includes(needle));
  });
}

// Where a client sits: "Switch name port 7", "AP name", or an empty string.
export function attachment(c) {
  const where = c.uplink_name || c.uplink_mac || "";
  if (!where) return "";
  return Number.isInteger(c.sw_port) ? `${where} port ${c.sw_port}` : where;
}

// A time from the API is an RFC 3339 string; a number is taken as unix seconds.
function toSeconds(v) {
  if (typeof v === "number") return v;
  const t = typeof v === "string" ? Date.parse(v) : NaN;
  return Number.isNaN(t) ? null : t / 1000;
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
