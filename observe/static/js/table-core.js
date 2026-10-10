// Pure sorting and paging logic for tables, ported from the sortable helper in ha_Int_soc
// (MIT, same owner). No DOM here, so it can be tested without a browser.
export const PAGE_SIZES = [10, 25, 100];

function isNull(v) {
  return v === null || v === undefined || (typeof v === "number" && Number.isNaN(v));
}

function compare(a, b) {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

// A stable sort: ties keep their input order. Null and undefined values sink to the end in
// both directions. The input array is not changed.
export function sortRows(rows, get, dir) {
  if (dir !== "asc" && dir !== "desc") return rows.slice();
  const sign = dir === "asc" ? 1 : -1;
  return rows
    .map((row, i) => ({ row, i, v: get(row) }))
    .sort((x, y) => {
      const xn = isNull(x.v), yn = isNull(y.v);
      if (xn || yn) return xn === yn ? x.i - y.i : xn ? 1 : -1;
      return sign * compare(x.v, y.v) || x.i - y.i;
    })
    .map((e) => e.row);
}

// The sort state is {key, dir} or null. A click on a column cycles ascending, descending,
// then back to unsorted. A click on a different column starts ascending.
export function nextSort(current, key) {
  if (!current || current.key !== key) return { key, dir: "asc" };
  return current.dir === "asc" ? { key, dir: "desc" } : null;
}

export function ariaSort(current, key) {
  if (!current || current.key !== key) return "none";
  return current.dir === "asc" ? "ascending" : "descending";
}

// The accessible name of a column's sort button: what it sorts and how it is sorted now.
export function sortLabel(label, aria) {
  const now = aria === "ascending" ? "sorted ascending" : aria === "descending" ? "sorted descending"
    : "not sorted";
  return `Sort by ${label}, ${now}`;
}

// The rows of one page. The page number is clamped, so a shrinking result never shows nothing.
// `sizes` is the list the page offers; a size outside it falls back to the first.
export function pageSlice(rows, page, size, sizes = PAGE_SIZES) {
  const n = sizes.includes(size) ? size : sizes[0];
  const pages = Math.max(1, Math.ceil(rows.length / n));
  const p = Math.min(Math.max(0, page | 0), pages - 1);
  return { rows: rows.slice(p * n, p * n + n), page: p, pages, size: n, total: rows.length };
}
