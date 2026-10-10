// Sortable, paged tables. Columns are {key, label, numeric, get, render(row) -> Node}; a render
// function must return a Node, never markup text.
import { el } from "/static/js/dom.js";
import { PAGE_SIZES, ariaSort, nextSort, pageSlice, sortLabel, sortRows } from "/static/js/table-core.js";

export { PAGE_SIZES, ariaSort, nextSort, pageSlice, sortRows };

function sortableTh(col, sort, onSort) {
  const th = el("th");
  if (col.numeric) th.className = "num";
  th.scope = "col";
  if (!col.get) { th.textContent = col.label; return th; }
  th.setAttribute("aria-sort", ariaSort(sort, col.key));
  const b = el("button", "th-sort");
  b.type = "button";
  // aria-sort is "none" until a column is sorted, which many accessibility trees leave out, so
  // the button's name says the state too.
  b.setAttribute("aria-label", sortLabel(col.label, ariaSort(sort, col.key)));
  const arrow = sort && sort.key === col.key ? (sort.dir === "asc" ? "▲" : "▼") : "↕";
  const a = el("span", "th-arrow", arrow);
  a.setAttribute("aria-hidden", "true");
  b.append(el("span", null, col.label), a);
  b.addEventListener("click", () => onSort(col.key));
  th.append(b);
  return th;
}

function cell(col, row) {
  const td = el("td");
  if (col.numeric) td.className = "num";
  const out = col.render ? col.render(row) : (col.get ? col.get(row) : "");
  if (out instanceof Node) td.append(out);
  else td.textContent = out === null || out === undefined ? "" : String(out);
  return td;
}

// Returns {root, setRows}. The root holds the scroll wrapper and the footer.
// Every table offers the same page sizes (PAGE_SIZES); a long list may start on a larger one
// with `defaultSize`.
export function sortableTable({ columns, rows, empty, pageSizes, caption, defaultSize }) {
  const sizes = pageSizes || PAGE_SIZES;
  let data = rows || [];
  let sort = null, page = 0, size = sizes.includes(defaultSize) ? defaultSize : sizes[0];
  const root = el("div", "table-block");
  const draw = () => {
    const col = sort && columns.find((c) => c.key === sort.key);
    const sorted = col && col.get ? sortRows(data, col.get, sort.dir) : data;
    const view = pageSlice(sorted, page, size, sizes);
    page = view.page;
    const wrap = el("div", "table-wrap");
    const table = el("table", "data");
    if (caption) table.append(el("caption", "sr-only", caption));
    const head = el("thead");
    const tr = el("tr");
    for (const c of columns) tr.append(sortableTh(c, sort, (k) => { sort = nextSort(sort, k); draw(); }));
    head.append(tr);
    const body = el("tbody");
    for (const r of view.rows) {
      const row = el("tr");
      for (const c of columns) row.append(cell(c, r));
      body.append(row);
    }
    table.append(head, body);
    wrap.append(table);
    root.replaceChildren(wrap);
    if (!data.length) {
      const p = el("p", "empty muted");
      p.append(empty instanceof Node ? empty : document.createTextNode(empty || "Nothing to show."));
      root.append(p);
      return;
    }
    const foot = el("div", "table-foot");
    foot.append(el("span", "muted", `Showing ${view.rows.length} of ${view.total}`));
    const sel = el("select");
    sel.setAttribute("aria-label", "Rows per page");
    for (const n of sizes) {
      const o = el("option", null, String(n));
      o.value = String(n);
      if (n === view.size) o.selected = true;
      sel.append(o);
    }
    sel.addEventListener("change", () => { size = Number(sel.value); page = 0; draw(); });
    const prev = el("button", "btn ghost", "Previous");
    const next = el("button", "btn ghost", "Next");
    prev.type = next.type = "button";
    prev.disabled = view.page === 0;
    next.disabled = view.page >= view.pages - 1;
    prev.addEventListener("click", () => { page -= 1; draw(); });
    next.addEventListener("click", () => { page += 1; draw(); });
    foot.append(sel, prev, el("span", "muted", `Page ${view.page + 1} of ${view.pages}`), next);
    root.append(foot);
  };
  draw();
  // setEmpty swaps the empty note, so a page can say "Loading" until its first answer and only
  // then say that there is nothing.
  return { root, setRows(r) { data = r || []; draw(); },
    setEmpty(e) { empty = e; draw(); } };
}
