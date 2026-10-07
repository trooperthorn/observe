// Reading the v2 API (docs/DATA-API-DESIGN.md section 4): JSON, problem details turned into an
// Error that carries the status, and a helper that follows next_cursor through a whole list.
export class V2Error extends Error {
  constructor(status, detail, type) {
    super(detail);
    this.status = status;
    this.type = type || "";
  }
}

// A timestamp from the API is an RFC 3339 string. Older code passes unix seconds, so both work.
export function seconds(ts) {
  if (ts === null || ts === undefined) return null;
  return typeof ts === "number" ? ts : Date.parse(ts) / 1000;
}

export async function getJson(path) {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (!r.ok) {
    let problem = null;
    try { problem = await r.json(); } catch (_) { problem = null; }
    const detail = problem && typeof problem.detail === "string" ? problem.detail : `request failed (${r.status})`;
    throw new V2Error(r.status, detail, problem && problem.type);
  }
  return r.json();
}

// Every item of a list, 500 at a time.
export async function getAll(path, limit = 500) {
  const items = [];
  let cursor = null;
  do {
    const sep = path.includes("?") ? "&" : "?";
    const more = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
    const page = await getJson(`${path}${sep}limit=${limit}${more}`);
    items.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return items;
}
