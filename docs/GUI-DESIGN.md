# Observe GUI design: bringing the ha_Int_soc look into Observe

Status: design proposal, 2026-10-05. Source repos were read only.
- Target: the Observe repository (package `observe`).
- Visual source: `C:/Users/sean.LAB/repos/ha_Int_soc` (HA SOC panel, Lit and TypeScript).

Abbreviations: `SRC` = `ha_Int_soc/custom_components/ha_soc/frontend/src`, `OBS` = `ipMontior/observe/static`.

---

## 0. Facts that shape this design (checked in code)

| Fact | Where | Consequence |
| --- | --- | --- |
| The CSP is `default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'` | `ipMontior/observe/web.py:135` | No `style-src` is set, so it falls back to `'self'`. That blocks inline `<style>` and `style="..."` attributes. Setting `el.style.x` from JS through the CSSOM is still allowed, but avoid it: use classes and data attributes. Scripts must be same-origin files. Canvas is allowed. `form-action 'none'` means every form submits through `fetch`, never natively. |
| Pages load classic scripts (`<script src=...>`, `"use strict"`), not modules. Shared helpers go through globals, for example `infra-common.js` before `map.js`. | `OBS/map.html`, `OBS/host.html` | The owner asked for ES modules. `<script type="module" src="/static/...">` is allowed under `'self'`. The move is slice S1, done for the map, port and map admin pages: `js/dom.js`, `js/api.js` and `infra-common.js` are modules, and the map entry module is `pages/map.js`. The other pages still load classic scripts. |
| Theme before slice S2 was `:root` tokens plus `@media (prefers-color-scheme: dark)` in `app.css`; since S2 the tokens live in `css/tokens.css` and the old names are aliases. Status tokens were `--up --warn --down --pending --unreach`. Pills are white text on a filled colour. Monitor cards use a 4px left border. | `OBS/app.css:1-11, 22-35` | The token rename keeps the old names as aliases for one release, so the pages can migrate one slice at a time. |
| The map is a layered (tiered) layout built with DOM boxes. Every node shows its state in words. There is also a link table. | `OBS/pages/map.js:1-60`, `OBS/map.html` | The force graph is added as an extra view. The tier view and the table stay: the table is the accessible equivalent. |
| Reboot already uses a dialog where you type the host name, built with `textContent` only. | `OBS/host-control.js` (around lines 100-120) | This becomes the standard "dangerous confirm" dialog component. |
| Page tests already assert CSP-safe static markup and render hostile strings. | `ipMontior/tests/test_infra_pages.py:1-30` (`HOSTILE`, `PAGES`) | Each slice extends these TestClient and static-scan tests. |
| HA SOC's graph engine is vendored from `trooperthorn/relationship-maps` `packages/graph-core` (commit 0c4d268). It depends on `d3-force` and `d3-quadtree`. | `SRC/graph/VENDOR.md`, `SRC/graph/force.ts:1-12`, `SRC/graph/renderer.ts:1-4` | The licences are covered in section 4. A pure-JS port must replace d3 or vendor it. |

---

## 1. What makes ha_Int_soc's visuals distinctive

The reference screenshots are `ha_Int_soc/docs/screenshots/security-overview.png` (the full overview page) and `unifi-configuration-baseline.png` (a card with tables). The other two screenshots show SSH run views.

1. **A quiet, flat surface with a single border.** Cards have a 1px border in `--soc-border`, a 12px radius, **no shadow** and 16px padding (`SRC/styles.ts:74-81`). The page background is slightly grey and cards are white. In the screenshot, every block is an outlined white card on a light grey page, with roughly 12-16px gaps.
2. **A semantic token vocabulary on top of the host theme.** `--soc-page-bg`, `--soc-surface`, `--soc-surface-subtle` (text colour at 3.5% alpha), `--soc-border`, `--soc-text`, `--soc-text-muted`, `--soc-accent`, `--soc-card-radius` (`SRC/styles.ts:12-22`).
3. **Two separate colour families that are never mixed** (`SRC/styles.ts:24-53`):
   - an 8-step categorical palette `--cat-1..8` plus `--cat-other`, checked for colour-vision deficiency, with separate light and dark steps;
   - reserved status roles `--status-good #0ca30c`, `--status-warning #fab219`, `--status-serious #ec835a`, `--status-critical #d03b3b`, which are "never reused as a plain series color".
   The design doc also separates operational availability from security severity (`docs/FRONTEND-VISUAL-ARCHITECTURE.md`, "Overview rules"). This matches Observe directly: up/down state versus findings and capacity.
4. **The KPI row.** Four equal tiles, each with a 13px muted label, a 32px bold tabular-number value (letter-spacing -0.02em), and a 12.5px context line. A tile that leads somewhere is a real `<button>` and its border turns accent on hover or focus (`SRC/views/dashboard-view.ts:194-238`). The value takes the status colour only when it is non-zero (lines 944-952). The screenshot row reads: Posture score 78 / Open detections 2 / Critical-high 1 / Telemetry 0/0.
5. **Tinted status tiles.** In "Asset availability", each tile has a pale tinted background in its status colour, a coloured label and a large number. Green, amber, red, grey and blue sit together.
6. **Typography.** System font, 13px base in tables, card titles 15px/650 (`styles.ts:82-88`), table headers 11px uppercase with 0.03em tracking in the muted colour (`styles.ts:155-160`), section headings ("Operational detail") at about 17px with a muted subtitle (`.section-subtitle` 13px/1.45). Numbers use `font-variant-numeric: tabular-nums`.
7. **Pills and chips.**
   - `.pill` is a full-radius, 11px/600 tinted neutral pill with a 7px status dot (`styles.ts:172-200`).
   - `.tag` is a 10.5px monospace token on a tinted background (`styles.ts:201-214`).
   - `.chip` is a 5px-radius, 10.5px neutral label.
   - Severity pills in the Priority queue ("Medium", "Low") pair the word with a tint.
   - The `.overview-state` "Live protected data" outline pill sits at the top right of the page heading.
8. **Navigation.** A sticky two-level bar (`SRC/ha-soc-panel.ts:41-101`). The workspace row sits on the surface colour. The subtab row sits on the page background. Tabs are 13px/550 muted buttons with a 9px radius. The active tab shows accent-coloured text, a 10% accent fill and a 24% accent border, and carries `aria-current="page"`. The information architecture lives in one table, `SRC/nav.ts:14-95` (`SOC_WORKSPACES`). Owner-only workspaces show a lock. The header holds a brand mark ("SOC" badge), a title with a context line, an access indicator and a Customize toggle (`ha-soc-panel.ts:363-393`).
9. **Tables.** Collapsed borders, 8px/10px cells, a bottom border per row, and a 3% hover tint. Disabled rows get a red tint and strike-through. Column sorting is accessible: the `<th>` holds a real `<button>`, carries `aria-sort`, and the arrow is `aria-hidden` (`SRC/sortable.ts`, `styles.ts:351-390`). The footer reads "Showing 3 of 3 devices" with a "Show 10" selector (screenshot).
10. **Buttons.** Outline accent buttons with an 8px radius, `danger` (red outline) and `active` (filled) variants (`styles.ts:239-264`). The screenshot shows "Ack", "Resolve" and "Accept current as new baseline".
11. **Error notices.** A 4px left bar in the critical colour, a pale tint, monospace error text, a hint and an action row (`styles.ts:215-238`).
12. **Collapsible cards.** Native `<details class="card">` with the title in `<summary>` and a rotating chevron (`styles.ts:103-142`).
13. **Charts.** Donut gauges with the number in the centre and a legend that lists each value as text, a 30-day area sparkline, and a stacked severity bar with a legend (screenshot). Colour is never the only carrier, because the legend always shows numbers.
14. **Motion is minimal.** A 0.15s chevron rotation, and 0.08s hover lift and shadow on `.clickable` (`dashboard-view.ts:156-163`). There is no page animation.
15. **Customize.** Users can reorder and hide cards per view, with drag-and-drop plus keyboard up and down controls, in a dashed accent box (`SRC/customize.ts`, `SRC/customizable-view.ts`). The rules for fallbacks are firm: a failed load falls back to the declared order, and a failed save never rolls back the screen (`docs/FRONTEND-VISUAL-ARCHITECTURE.md`, "Customize contract").
16. **The graph look** is the one dark-only element (`SRC/graph/theme.ts`, `renderer.ts`):
    - near-black background `#05070a`, accent `#2fe08a`, and a 12-colour neon-ish palette chosen to show up on dark backgrounds;
    - nodes are filled circles sized `5 + sqrt(degree)*3.2` (anchors `8 + sqrt(d)*4.2`), with a dark outline and a white outline on hover or selection;
    - links are drawn in two passes, so highlighted edges sit at alpha 0.85 and dimmed ones at 0.1;
    - selecting a node dims everything outside its focus set to 0.2;
    - labels are screen-space rounded pills with a coloured border, placed largest first, with collision culling and a cap of 55;
    - a small count badge sits beside each label;
    - the initial ring is deterministic, so a reload shows the same picture, and the camera fits the graph without magnifying past 1:1.

---

## 2. Token and component inventory for Observe

### 2.1 Files (no build step, plain ES modules)

```
observe/static/
  css/tokens.css      custom properties, light + dark + data-theme overrides
  css/base.css        reset, body, typography, focus ring, utilities
  css/components.css  shell, card, kpi, chip, table, form, dialog, toast, wizard, tiles
  css/graph.css       graph container, toolbar, legend (canvas itself paints in JS)
  js/dom.js           el(tag, cls, text), clear(), svg(tag, attrs); textContent only
  js/api.js           fetch wrapper: JSON, CSRF header, 401 -> /login, error shape
  js/shell.js         renders nav from NAV table, theme toggle, aria-current, user menu
  js/chips.js         statusChip(state, text?) -> icon+text element
  js/table.js         sortable tables (port of sortable.ts), pager
  js/dialog.js        <dialog> confirm, typed-name confirm (lifted from host-control.js)
  js/toast.js         aria-live toasts
  js/tiles.js         customisable tile order/hide (phase 2, optional)
  js/graph/force.js   small force simulation (own code, see 2.10)
  js/graph/quadtree.js  or grid hit-test (own code)
  js/graph/render.js  canvas renderer port
  js/graph/view.js    camera, pointer/wheel/keyboard, resize observer
  pages/*.js          one entry module per page (dashboard.js, host.js, map.js, ...)
```

Each HTML page loads `tokens.css`, `base.css` and `components.css`, then exactly one `<script type="module" src="/static/pages/x.js">`. `app.css` is kept as a shim of legacy aliases until the last page has migrated, then deleted.

### 2.2 Tokens (`tokens.css`)

The status hues follow HA SOC's reserved roles. Observe's state names stay the same. Every status token has a matching `-bg` tint for tinted tiles and a `-fg` that passes 4.5:1 contrast on that tint.

```css
:root {
  color-scheme: light dark;
  /* surfaces */
  --o-page:#f4f5f7; --o-surface:#ffffff; --o-surface-subtle:rgba(28,34,48,.035);
  --o-border:#e3e6eb; --o-text:#1c2230; --o-text-muted:#5d6676; --o-accent:#2a78d6;
  --o-accent-tint:rgba(42,120,214,.10); --o-accent-line:rgba(42,120,214,.24);
  --o-focus:#2a78d6;
  /* shape + space */
  --o-radius:12px; --o-radius-sm:8px; --o-radius-pill:999px;
  --o-s1:4px; --o-s2:8px; --o-s3:12px; --o-s4:16px; --o-s5:24px; --o-s6:32px;
  --o-maxw:1400px;
  /* type */
  --o-font: system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --o-mono: ui-monospace,"Cascadia Mono",Consolas,monospace;
  --o-fs-xs:11px; --o-fs-sm:12.5px; --o-fs-base:14px; --o-fs-title:15px; --o-fs-h:17px; --o-fs-kpi:32px;
  /* status roles (never used as series colours) */
  --o-up:#0c8a0c;      --o-up-bg:#e6f4e6;
  --o-warn:#9a6700;    --o-warn-bg:#fdf1d6;   /* text-safe amber; #fab219 for fills/dots only */
  --o-serious:#b5501f; --o-serious-bg:#fbe9e0;
  --o-down:#c22f2f;    --o-down-bg:#fbe5e5;
  --o-pending:#5d6676; --o-pending-bg:#eef0f3;
  --o-unreach:#6a4ad8; --o-unreach-bg:#efeafd;
  --o-dot-warn:#fab219;
  /* categorical, from SRC/styles.ts */
  --cat-1:#2a78d6; --cat-2:#eb6834; --cat-3:#1baf7a; --cat-4:#eda100;
  --cat-5:#e87ba4; --cat-6:#008300; --cat-7:#4a3aa7; --cat-8:#e34948; --cat-other:#9aa0a6;
  /* graph canvas (read by render.js via getComputedStyle) */
  --g-bg:#f7f8fa; --g-node-stroke:#ffffff; --g-label-bg:rgba(255,255,255,.92);
  --g-label-fg:#1c2230; --g-dim:.2;
  /* motion */
  --o-dur:120ms;
  /* legacy aliases, removed in the final slice */
  --bg:var(--o-page); --card:var(--o-surface); --fg:var(--o-text); --muted:var(--o-text-muted);
  --line:var(--o-border); --up:var(--o-up); --warn:var(--o-dot-warn); --down:var(--o-down);
  --pending:var(--o-pending); --unreach:var(--o-unreach);
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { /* dark block */ } }
:root[data-theme="dark"] { /* same dark block */ }
/* dark block:
  --o-page:#0f1218; --o-surface:#181c24; --o-surface-subtle:rgba(230,233,239,.04);
  --o-border:#2a303b; --o-text:#e6e9ef; --o-text-muted:#a3acba; --o-accent:#3987e5;
  --o-up:#4cc77f; --o-up-bg:rgba(76,199,127,.14); --o-warn:#f0b429; --o-warn-bg:rgba(240,180,41,.14);
  --o-serious:#f08a5d; --o-serious-bg:rgba(240,138,93,.14); --o-down:#f06a6a; --o-down-bg:rgba(240,106,106,.14);
  --o-pending:#98a2b3; --o-pending-bg:rgba(152,162,179,.12); --o-unreach:#a99bfb; --o-unreach-bg:rgba(169,155,251,.14);
  --cat-1:#3987e5; --cat-2:#d95926; --cat-3:#199e70; --cat-4:#c98500; --cat-5:#d55181;
  --cat-7:#9085e9; --cat-8:#e66767; --cat-other:#7a807f;
  --g-bg:#05070a; --g-node-stroke:rgba(5,7,10,.85); --g-label-bg:rgba(5,7,10,.82); --g-label-fg:#e8eef6;
*/
@media (prefers-reduced-motion: reduce) { :root { --o-dur:0ms; } }
```

The theme toggle cycles Auto, Light and Dark. It sets `document.documentElement.dataset.theme` and stores the choice in `localStorage`, wrapped in try/catch. To avoid a flash of the wrong theme without an inline script, `shell.js` is the first module and sets the attribute before it renders. A short flash on the Pi is acceptable.

Contrast must be checked in both themes by an automated test (slice S2). The test parses `tokens.css` and asserts at least 4.5:1 for every `-fg`/text token against its background, and at least 3:1 for dots and borders.

### 2.3 Layout shell

```
+----------------------------------------------------------------------------+
| [O] Observe   Pi-lab                       (o) 3 down  ! 2 warn   [Auto v] sean v |  header (surface)
+----------------------------------------------------------------------------+
| Overview | Hosts | Network | Reports | Admin                                |  workspace nav (sticky)
+----------------------------------------------------------------------------+
|  Dashboard  Capacity  Findings                                             |  subnav (page bg)
+----------------------------------------------------------------------------+
|  Page title                                              [Live] [action]   |
|  muted subtitle                                                            |
|  ...cards...                                                               |
+----------------------------------------------------------------------------+
|  footer: version, last poll, data age                                      |
```

- The `NAV` table in `shell.js` is a port of `SRC/nav.ts` `SOC_WORKSPACES`, with each item mapped to a URL. Plugins contribute entries, for example Pockethernet under Network ("Cable reports").
  - Overview: Dashboard (`/`).
  - Hosts: All hosts, Add host (admin only).
  - Network: Map (`/map`), Map admin (`/admin/infra`, admin only), Cable reports (`/pockethernet/reports`).
  - Admin (admin only): Users and keys (`/admin`), Audit (`/audit`).
- Nav items are `<a>` elements, not buttons, because these are separate pages. The active item gets `aria-current="page"`. Admin-only items are hidden for viewers. HA SOC showed them with a lock, but there is no need to advertise them here. The server remains the authority on access.
- The header summary pill row ("3 down, 2 warn") replaces today's `#summary`. It sits in an `aria-live="polite"` region and is clickable to filter the dashboard.
- Below 700px both nav rows scroll horizontally (`overflow-x:auto`) and the header wraps. There is no hamburger menu, which keeps the JS small.

### 2.4 Cards

- `.card`: surface background, 1px border, `--o-radius`, 16px padding, no shadow. `h3` is 15px/650 and an optional `.card-sub` subtitle is 12.5px muted.
- `details.card`: collapsible, ported from `styles.ts:103-142`. The chevron is a CSS `::before`, which is allowed because it lives in the stylesheet.
- `.card.notice` is the error notice from `styles.ts:215-238`. Use it for "agent not reporting", "control queue refused" and similar.
- `.kpi-row` holds 4 `.kpi` tiles (`button.kpi` when it links to something). On narrow screens it collapses to 2 columns, then 1. Use container queries where supported and media queries otherwise. Chromium on Pi OS supports container queries.
- `.tiles` is a group of tinted status tiles, as in "Asset availability". Each tile shows the status icon and word and the count. The tile is never just a coloured box.
- Monitor cards (`.mon`) keep their current structure, but the 4px coloured left bar is replaced with the HA SOC style: a border card with a status chip in the header row. The left bar may stay as extra redundancy.

### 2.5 Status chips (icon plus text, never colour alone)

`chips.js` builds `<span class="chip s-down"><svg aria-hidden="true">…</svg><span>Down</span></span>`. The icons are inline SVG built with `createElementNS`, which is allowed because it is DOM, not innerHTML. Each icon has a distinct shape, so the chip still reads in greyscale:

| State | Icon shape | Word | Token |
| --- | --- | --- | --- |
| up / good | check in circle | Up / OK | `--o-up` |
| warn / warning | triangle with ! | Warning | `--o-warn` (dot `--o-dot-warn`) |
| serious (capacity soon) | diamond | Soon full | `--o-serious` |
| down / critical | X in circle | Down / Critical | `--o-down` |
| unreachable | broken link | Unreachable | `--o-unreach` |
| pending / stale / unavailable / absent / not_reported | hollow circle / clock | Pending / Stale / No data | `--o-pending` |

Chip style: the tint background `-bg`, text in the role colour, 11.5px/600, pill radius, 5px gap. This replaces today's white-on-solid `.pill`, whose warn variant already needed an override to dark text (`app.css:27`). A `chip.neutral` variant serves tags, and `.tag` (mono) serves key IDs, ports and MACs.

### 2.6 Tables

Port the `styles.ts:143-171` table rules and `sortable.ts`:
- `sortRows` stable sort, nulls sink.
- `nextSort` cycles ascending then descending.
- `sortableTh` builds a `<th aria-sort>` containing a `<button>` with an `aria-hidden` arrow.

`table.js` exports `sortableTable({columns, rows, empty, pageSizes:[10,25,100]})`. Each column is `{key, label, numeric, get, render(row)->Node}`. Render functions must return Nodes, never strings of HTML.

Below 700px, wide tables sit inside `.table-wrap{overflow-x:auto}` (the HA SOC rule "Tables remain horizontally contained"). Footer: "Showing N of M" plus a page-size `<select>`. Empty state: centred muted text with a single suggested action.

### 2.7 Forms

- Labelled `<label>` wrapping the input, or `for`/`id` pairs. Inputs use a 6px radius, `--o-border`, and focus via `:focus-visible` with a 2px `--o-focus` outline.
- `.form-grid` is 2 columns that collapse to 1. Use a `fieldset` and `legend` for groups such as fan headers and services.
- Inline validation sits in a `<p class="field-err" id=...>` linked with `aria-describedby`. Server errors are rendered with `textContent` from `detail`, as `host-control.js` already does.
- Buttons: `.btn` (outline accent), `.btn.primary` (filled), `.btn.danger`, `.btn.ghost`. Disabled state uses 0.5 opacity.
- Every submit is handled by JS (`preventDefault` then `fetch`), because of `form-action 'none'`.
- Copy-to-clipboard button: `navigator.clipboard.writeText`, with a fallback that selects the text in a readonly `<textarea>` when the page is not served from a secure context.

### 2.8 Dialogs

- Use native `<dialog>` with `showModal()`. Focus trapping and Esc are built in, and the backdrop is styled through `::backdrop` in the stylesheet.
- `dialog.js` exports:
  - `confirmDialog({title, body, confirmText, danger})` returning a Promise of a boolean;
  - `typedConfirm({title, name})`, lifted from the reboot flow in `host-control.js`: the confirm button is enabled only when the typed value equals the host name.
- Focus returns to the element that opened the dialog.

### 2.9 Toasts

- `toast.js` uses a single `<div class="toasts" role="status" aria-live="polite">` region. Errors go to a second `role="alert"` region.
- Each toast has a status icon and text and auto-dismisses after 6s, except errors, which stay until closed. With reduced motion there is no slide.
- Never put secrets in a toast. The one-time API key stays in the `#newkey` section as it does today.

### 2.10 Force-directed graph for the infrastructure map

**Source files and what happens to each:**

| SRC file | Port? | Notes |
| --- | --- | --- |
| `graph/types.ts` | Partly, as JSDoc typedefs in `graph/types.js` | Keep `GraphEntity`, `GraphRelation`, `NodePos`, `LinkPos`, `Layout` and `Category`. Drop `GraphEvent`, `GraphScope`, `DotPos`, `AxisLabel` and `band`, which only the arc layout uses. |
| `graph/theme.ts` | Replace | Read colours from CSS tokens at render time (`getComputedStyle`), so light and dark both work. The source hard-codes `BG='#05070a'` and only works on dark. Keep the idea of `paletteColor(i)`, mapped to `--cat-*`. |
| `graph/force.ts` | Port the logic, not the d3 calls | Keep: the deterministic starting ring, `r = 5 + sqrt(degree)*3.2*scale` (anchors 8 and 4.2), link distance 90 and strength 0.35, charge -220, collide `r+12`, anchors pulled to the centre with a radial force (strength 0.35 for anchors, 0.06 for others). In Observe the anchors are core switches and the router. Write a ~150-line `force.js` with velocity Verlet and alpha decay, matching d3-force's model. |
| `graph/renderer.ts` | Port nearly whole | Keep: `worldToScreen`/`screenToWorld`, `fitCamera` (never magnify past 1:1), the two-pass link drawing, focus dimming, node outline on hover or selection, screen-space label pills with collision culling and `MAX_LABELS`, and `roundRect`. Change: colours come from tokens. The badge shows a state word or count instead of `weight`. Replace `d3-quadtree` with a uniform grid hit-test (`pickNode`), which is enough below 1000 nodes. |
| `graph/arc.ts` | Drop | The timeline arc layout does not apply to infrastructure. |
| `views/entity-map-view.ts` | Use as a behaviour reference only | It covers the canvas, ResizeObserver, wheel zoom (`passive:false`), drag pan and dragging state. Rewrite it in `graph/view.js`. |

**Observe mapping.**
- Entities: switches, routers, hosts, APs, jacks (optional) and endpoints (collapsed into per-switch counts by default), from `/api/infra/map`.
- Group colour comes from `--cat-*` by kind. The state is shown as a **ring and a glyph inside the node** (tick, X, !, broken link), in addition to a status-coloured outline, so state is never shown by colour alone.
- Relations are links. Stale links (the current "dashed lines are links not confirmed for a while") are drawn with `setLineDash`. Typed link kinds are uplink, LLDP and MAC-learned.

**Pi 3 browser performance limits.** The browser may run on the Pi itself, a weak client.
- Run the simulation **once, off-screen**, with a tick budget like the source (`sim.stop().tick(360)`). Then paint statically. No continuous animation loop runs, so the CPU is idle when nothing changes.
- Repaint only on camera change, hover or selection, coalesced through `requestAnimationFrame`. Cap repaints at about 30fps while dragging.
- Cap the ticks at `min(360, 60000 / n)`. Charge is computed naively in O(n^2) only when n ≤ 300. Above that, use a coarse grid approximation, or fall back to the tiered layout with a note that the force view is limited to 300 nodes.
- Endpoints are collapsed by default. Expanding one switch adds its endpoints, and only that subgraph is re-laid out, with the other nodes fixed.
- Keep labels at 55 maximum, `devicePixelRatio` capped at 2, and the canvas sized to its container through ResizeObserver.
- Cache layout positions per filter in `sessionStorage` (try/catch), so returning to the map does not recompute the layout.
- Target: under 300ms layout and under 16ms paint for 200 nodes on a Pi 3 Chromium. Measure this in the slice. Test it with a Python-side fixture that sizes the payload, not in CI.

**Accessibility for the graph.**
- The `<canvas>` has `role="img"` and an `aria-label` summary ("42 devices, 3 down").
- Next to it, a "Selected" side card lists the node details and its links as real links.
- The tier view and the Links table remain one tab away and form the accessible equivalent.
- Keyboard: Tab focuses the canvas, arrow keys move the selection between nodes in reading order, Enter opens the port or host page, and +, - and 0 zoom and fit.

### 2.11 Sortable and customisable dashboard tiles

- **Sortable tables:** yes, everywhere (2.6). The cost is small and they are accessible.
- **Customise (reorder and hide dashboard sections):** worth doing on the Dashboard only, in phase 2. Port the contracts of `effectiveOrder` and `customize.ts`:
  - stale IDs are dropped and new ones appended;
  - hiding never deletes data;
  - a failed load falls back to the declared order;
  - a failed save keeps the screen.
- Storage: **server-side per user** (`GET/PUT /api/ui/layout/{view}`), so it follows the user between devices.
- Interaction: keyboard up and down buttons plus a Hide checkbox. **Skip drag-and-drop at first**, because it is the costliest part and the buttons cover accessibility. Monitor groups are the natural tiles.

---

## 3. Screen by screen

The common frame is shell header, workspace nav, subnav, a page heading row (title, subtitle, state pill, actions), then content. Every page's existing element IDs that tests rely on are kept, or the tests are updated in the same slice.

### 3.1 Login (`login.html`, `login.js`)

```
            +--------------------------------+
            |  [O] Observe                   |
            |  Sign in to continue           |
            |  Username [______________]     |
            |  Password [______________]     |
            |  [ Sign in            ]        |
            |  ! Wrong username or password  |  role=alert, icon+text
            +--------------------------------+
```
A centred card with no nav. The rate-limit message reads "Too many attempts, try again in N s". The theme follows the system or the stored choice.

### 3.2 Dashboard (`index.html`, `app.js`): Overview workspace

```
Overview                                                       ( ) Live, 12 s ago
What is down now, kept separate from capacity and field findings.
+-------------+ +-------------+ +-------------+ +-------------+
| Monitors    | | Down        | | Warnings    | | Capacity    |
| 84          | | 3  (red)    | | 2           | | 1 full <7d  |
| 6 groups    | | 2 hosts     | | 1 stale     | | 4 forecasts |
+-------------+ +-------------+ +-------------+ +-------------+   (Down/Warn/Capacity are buttons -> filter/scroll)
+ Availability ---------------------------------------------------------+
| [v Up 79] [! Warning 2] [x Down 3] [~ Unreachable 0] [o Pending 0]   |  tinted tiles
+-----------------------------------------------------------------------+
Filter: [All states v] [search____]                      [Customize]
+ Group: Core network                                  x 1 down  v 5 up +
| +- edge sw ---------------- [x Down] ---+ +- nas ------- [v Up] --+  |
| | 10.0.0.2  ping        timeout 2.0 s   | | 1.2 ms                |  |
| | ▁▂▁▁▃▁▁█ sparkline (fail bars)         | | ▁▁▁▁▁▁▁▁              |  |
| | 99.1% 24h        host >   events >    | | 100% 24h   host >     |  |
| +---------------------------------------+ +-----------------------+  |
+-----------------------------------------------------------------------+
+ Capacity outlook (details.card) ---------------------------------------+
| Disk /mnt/tank  [◆ Soon full]  full in ~6 d    81% -> 100%            |
+-----------------------------------------------------------------------+
+ Field findings -----+ + Recent events ---------------------------------+
```
- Groups are collapsible cards with a count chip in the summary.
- Sparklines keep the existing SVG. The stroke uses `--o-text-muted` and fail and warn bars use the status `-bg` tints at full opacity.
- The KPI "Down" value turns `--o-down` only when it is above 0, as in the HA SOC rule.
- `Customize` (phase 2) applies to the group cards and the capacity, findings and events cards.

### 3.3 Host page (`host.html`, `host.js`, `host-control.js`): Hosts workspace

```
Hosts / nas01                                [v Up]  last report 8 s ago   [Settings]
TrueNAS SCALE · agent hostwatch 1.4 · control thermal-control 0.9
+-------------+ +-------------+ +-------------+ +-------------+
| CPU 34%     | | Temp 61 °C  | | Fans 3/3 ok | | Disks 6 ok  |
+-------------+ +-------------+ +-------------+ +-------------+
! Warning banner (notice card): "Pool tank 81% used"
+ Sensors (details) ---- sortable table: Sensor | Value | State chip | Age +
+ Disks ---------------- ...                                              +
+ Control -------------------------------------------------------------- +
| Action [Set fan curve v]  Target [fan2 v]   [Queue action]            |
| Queue: ts | action | state chip (queued/pulled/done/refused) | detail  |
| [Reboot host...] (danger) -> typedConfirm("nas01")                     |
+-----------------------------------------------------------------------+
```
- Control moves inside `<main>` as the last card. Today it is a sibling section after `<main>`.
- The action list shows only allowlisted actions. Its wording comes from `ACTION_TEXT`.
- Refusal text appears as an inline `notice` plus an error toast.

### 3.4 Infrastructure map (`map.html`, `map.js`): Network workspace

```
Network map                     Site [All v] Building [All v]   View: (Graph)(Tiers)(Table)
+--------------------------------------------------------+ +- Selected ------------+
|            canvas (force graph)                        | | core-sw1  [v Up]       |
|        (core)●───●(dist)                               | | Switch · 48 ports      |
|           ╲    ╱  ╲                                    | | Links:                 |
|            ●(acc) ●(acc)── ◌ 12 endpoints              | |  gi1/0/1 -> dist-a     |
|  [+][-][fit]  legend: ● switch ● host  ✓ ✕ ! glyphs    | |  ...  [Open port page] |
+--------------------------------------------------------+ +------------------------+
Links (sortable table, unchanged columns: From | To | Source | Seen | State chip)
```
- The view selection is kept in the URL hash (`#graph` / `#tiers` / `#table`). The default is Tiers on narrow screens and whenever there are more than 300 nodes.
- Below 900px the side card stacks under the canvas.
- The existing note about dashed lines moves into the legend.

### 3.5 Port page (`port.html`, `port.js`)

```
Network / core-sw1 / Gi1/0/5                        [x Down]  since 10:42
+ Port --------------+ + Neighbour --------------------------------------+
| Speed 1G  VLAN 20  | | LLDP: ap-hall (Gi0)   MACs: 3 (table)           |
+--------------------+ +-------------------------------------------------+
+ History: events table ------------------------------------------------+
+ Cable test (Pockethernet, if any): latest report link + result chips   +
```

### 3.6 Map admin (`infra-admin.html`, `infra-admin.js`)

There are three cards (Unlinked, Pending, Decided), each with a sortable table. Pending rows have `Accept` (primary) and `Reject` (danger) buttons, and accepting opens a `confirmDialog`. The result appears as a toast, and the row moves to Decided without a page reload.

### 3.7 Admin: users and keys (`admin.html`, `admin.js`)

```
Admin / Users and keys
+ New key (shown once) [notice, accent] ------------------------------+
|  key: [mono readonly ••••••••••] [Copy]  "This is shown only once"  |
+---------------------------------------------------------------------+
+ API keys ---------------------------+ + Users ----------------------+
| form: name [___] scope [v] [Create]  | | form: user [] role [v] [Add]|
| table: Name|Scope|Created|Last used  | | table: User|Role|Last login |
|        |[Revoke] (danger, confirm)   | |   |[Disable]               |
+--------------------------------------+ +-----------------------------+
```

### 3.8 Audit (today a table inside `admin.html`, which becomes its own `/audit` page or keeps its anchor)

The filter bar has Actor, Kind, Status (chips as toggles) and a time range. The sortable table has Time | Actor | Kind | Status chip | Detail. Detail is monospace with `overflow-wrap:anywhere`. Paging: Show 25/100. Recommendation: keep the same API and only move the page (see open question Q5).

### 3.9 Pockethernet pages (`plugins/pockethernet/.../pages/*.html`, `static/pockethernet.js`)

The plugin pages load the core `tokens.css`, `base.css`, `components.css` and `shell.js`, so they inherit the shell. The plugin registers its nav entry through a declared list.
- Status (S8): built as written. Deviations: the Fails column shows one chip per report because a report covers one jack, the report KPI row counts that one jack plus its warning steps, and the wiremap draws the four pairs straight with a state from the pair length and fault text because the report has no per-wire data. The plugin adds `static/pockethernet.css` for the wiremap, served beside its script.
- **Reports list:** a sortable table (Date | Site | Jacks | Fails chip | Uploaded by) and a search box.
- **Report:** a KPI row (Jacks tested / Pass / Fail / Warn), then a table per jack with result chips (Pass ✓ / Fail ✕ / Warn !), each linking to the jack page.
- **Jack:** pair-length table, wiremap as SVG (pairs labelled by number and colour name, never colour alone), history of tests for this jack, and a link to the port when it is matched.

### 3.10 Add host wizard (new, `/hosts/new`, admin only)

A five-step wizard on a single page. Step state is kept in the URL hash, so Back and Forward work. A step list at the top uses `<ol class="steps">` with `aria-current="step"` and shows each step as a number plus its name.

```
Hosts / Add host
(1 Host)───(2 Agent & control)───(3 Allowlist)───(4 Install)───(5 Live)

Step 1  Host
  Host name   [nas01_________]   (lowercase, a-z0-9-, unique; checked live)
  Platform    ( ) Linux server  (•) TrueNAS  ( ) Windows  ( ) Raspberry Pi
  [Next]

Step 2  Agent and control
  Agent       [x] hostwatch agent (metrics)            required
  Control     [x] thermal-control (fans, services, reboot)   (greyed out for Windows, with the reason)
  [Back] [Next]

Step 3  Allowlist  (only if control chosen)
  fieldset Fan headers      [x] fan1  [x] fan2  [ ] fan3   [+ add header ___]
  fieldset Restartable services  [x] smbd  [ ] nfs-server  [+ add ___]
  fieldset Reboot           [ ] Allow reboot (always needs typed host-name confirm)
  [Back] [Create host]  -> POST, server mints enrolment token + command

Step 4  Install
  +-- nas01 · TrueNAS ----------------------------------------------+
  | # Observe install for nas01 (TrueNAS), token expires in 30 min |
  | curl -fsSL https://observe.lan/i/<token> | sudo sh             |
  +---------------------------------------------------------[Copy]--+
  Windows variant: PowerShell one-liner, same header.
  [I have run it ->]

Step 5  Live progress (polls GET /api/hosts/nas01/enrolment every 3 s, aria-live)
  [v] Script fetched           10:41:02
  [v] First data received      10:41:20
  [~] Control first pull       waiting… (only if control chosen)
  [ ] Ready
  On Ready: success notice + [Open host page] [Add another]
  On token expiry: notice "Command expired" + [Regenerate command]
```
- Each progress line is a chip-style row (icon plus word plus time). It is never a spinner alone. With reduced motion, the "waiting" icon is static.
- The command is shown in a `<pre class="cmd">` built with `textContent`. The heading line names the host, as requested. The token appears in the page only. It never goes into a URL query of the console itself, and it is not logged in toasts.
- Platforms set defaults. Raspberry Pi pre-selects fan header `pwm-fan`. TrueNAS warns that the install survives updates only via the documented path. Each platform's install line comes from the server, so the UI does not hard-code commands.

### 3.11 Host settings page (new, `/hosts/{name}/settings`, admin only)

```
Hosts / nas01 / Settings                                   [v Up]
+ Identity ---------------------------------------------------------+
| Platform TrueNAS · agent 1.4 · control 0.9 · enrolled 2026-10-01  |
+-------------------------------------------------------------------+
+ Control allowlist -----------------------------------------------+
| Fan headers   [x] fan1 [x] fan2 [ ] fan3 [+ add]                  |
| Services      [x] smbd [ ] nfs-server [+ add]                     |
| Reboot        [ ] Allow                                            |
| [Save allowlist]  (confirmDialog listing diff: + fan3, - smbd)     |
| Note: the host picks up the new allowlist on its next control pull |
|       (status chip: Pending pull / Applied 10:52).                 |
+-------------------------------------------------------------------+
+ Install command ------------------------------------------------+
| [Regenerate command]  (confirm: old token is revoked)             |
|  -> shows the same headed command block as wizard step 4          |
+-------------------------------------------------------------------+
+ Danger zone (details, closed) -----------------------------------+
| [Revoke host keys] [Remove host]  -> typedConfirm("nas01")        |
+-------------------------------------------------------------------+
```

---

## 4. Accessibility, CSP and theme rules; what must not be copied

### 4.1 Accessibility (from HA SOC's contract, extended)
- State is always icon plus word. Charts carry text values and legends. Graph nodes carry glyphs. Wiremaps label pairs in text.
- Nav uses `<nav aria-label>` and `aria-current`. Sort headers use `aria-sort` with a real `<button>`. Dialogs are native `<dialog>`. Live regions: the header summary, wizard progress and toasts.
- A visible `:focus-visible` ring everywhere. Do not use `all: unset` without restoring focus, as `.head` does today in `app.css:39`, which only partly restores it.
- Hit targets are at least 32px (44px on touch, via `@media (pointer:coarse)`).
- `prefers-reduced-motion` turns the 120ms transitions and the wizard pulse to 0.
- Text contrast is at least 4.5:1 in both themes, enforced by a test. Amber is used as a fill or dot only, and the text uses `--o-warn` (darker).
- Layout works at 320px width without horizontal page scroll. Only tables and the canvas scroll inside their own containers.

### 4.2 CSP rules (enforced by static tests)
- No `<script>` without `src`, no `<style>` element, no `style=` attribute, and no `on*=` attributes in any HTML under `static/` or plugin `pages/`.
- No `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, `new Function`, or string `setTimeout` in any `.js`.
- Avoid `element.style` and `setAttribute('style')`. Use classes, `hidden`, `data-*` and `<progress>`/`<meter>` for bars. The exception is canvas and SVG geometry attributes (`x`, `y`, `width`, `d`), which are not styles.
- All URLs are same-origin. No CDN, no web fonts (system font stack), and icons are inline SVG built through DOM calls.
- Keep the CSP header unchanged. Optionally add explicit `style-src 'self'; script-src 'self'` for clarity. That would be a separate one-line change with a test asserting the header.

### 4.3 Theme rules
- Every colour is a token. Components never hard-code hex values. The graph reads tokens at paint time and repaints on a `matchMedia('(prefers-color-scheme: dark)')` change or a theme toggle.
- Status tokens are never used for categories, and categorical tokens never indicate state (HA SOC rule, `styles.ts:37`).

### 4.4 What must not be copied
- **HA-specific APIs and concepts:** `hass`, `callWS`, `subscribeMessage`, `location-changed` and `navigateToHaPath` (`SRC/nav.ts:131-148`), HA theme variables such as `--primary-color`, `--rgb-*` and `--ha-card-border-radius`, `ha-*` elements, the `HomeAssistant` type, and the WS layout store (`data/ha-soc-ws`). Only the concepts move across.
- **Lit:** `lit`, decorators, the `html`/`css` tagged templates, `:host` and shadow DOM. Observe uses light DOM with real stylesheets. The CSS values are re-expressed, not imported. Lit is BSD-3-Clause, but there is no reason to ship it.
- **Licences checked:**
  - `ha_Int_soc/LICENSE` is **MIT, (c) 2026 trooperthorn**, the same owner. Porting the CSS values, `sortable.ts` logic and `customize.ts` ordering rules is fine. Keep a one-line attribution comment ("ported from ha_Int_soc, MIT").
  - `src/graph/*` is **vendored from `trooperthorn/relationship-maps`**. `C:/Users/sean.LAB/repos/relationship-maps` has **no LICENSE file** and no `license` field in its package.json files. It is the owner's own code, so porting is his call (open question Q2). Note the upstream commit 0c4d268 in the port header, as `VENDOR.md` does.
  - `d3-force` and `d3-quadtree` are **ISC** (Mike Bostock, from `frontend/node_modules/*/LICENSE`). They are permissive, but the recommendation is **not to vendor them**. Write a small force and grid hit-test instead (2.10). If any d3 code is copied verbatim, include the ISC notice in a `static/js/graph/THIRD-PARTY.md` licence file.
  - `@xterm/xterm` and `addon-fit` are **MIT** (xterm.js authors), used by HA SOC's Terminal view only. **Do not copy them.** Observe has no terminal feature, and xterm needs inline styles and CSS that would fight the CSP.
  - `frontend/scripts/gen-xterm-css.mjs`, rollup, TypeScript and the `dist/` bundle are build tooling and are not applicable.
- **Do not copy HA SOC's hard-coded dark graph theme as the only theme.** Observe needs a light graph too.

---

## 5. Slice plan

Each slice is small and lands as one PR. Every slice must pass the existing test suite plus the listed checks. The static checks live in a new `tests/test_ui_static.py`. It walks `observe/static/**` and `plugins/*/**/pages|static/**` and applies the regexes from section 4.2. Page tests extend the `test_infra_pages.py` pattern: TestClient with login, then assert 200, the CSP header, the expected IDs and stylesheet and module links, and that the `HOSTILE` string is never echoed raw.

| # | Slice | Contents | Tests |
| --- | --- | --- | --- |
| S0 | Static guard | Add `test_ui_static.py` (no inline script or style, no `style=`/`on*=`, no innerHTML-family or eval in JS, same-origin URLs only). Fix any existing violation. | Static scan, CSP header assert on every page route. Done: the scan found no existing violation to fix, and it also checks CSS for off-origin URLs. |
| S1 | Modules and DOM helpers | `js/dom.js`, `js/api.js`. Convert `infra-common.js` to a module, switch one page (map) to `type="module"`, no visual change. | Page test: the map HTML references `/static/pages/map.js` with `type="module"`. The static file is served with a JS content type. Existing map tests pass. |
| S2 | Tokens and base CSS | `tokens.css`, `base.css`, `app.css` reduced to legacy aliases. Theme toggle in a small module. | Parse `tokens.css`: every required token is present in light and both dark blocks. Contrast script (pure Python WCAG formula) passes 4.5:1 for text and 3:1 for dots. Done in `tests/test_ui_tokens.py`; see the S2 notes below. |
| S3 | Shell and nav | `shell.js` with a NAV table, header and summary, plus a plugin nav hook (Python returns nav entries). | TestClient: a viewer's `/api/ui/nav` (or nav embedded per page) omits admin entries. Every page includes a `<nav>` mount and `shell.js`. Done in `tests/test_ui_shell.py`; see the S3 notes below. |
| S4 | Chips, cards, KPI, tables | `chips.js`, `components.css`, `table.js` (sortRows and nextSort). | Static check: no `.pill` without text in JS. Optional pure-function tests for `sortRows`, run in Python by mirroring the spec, or by a tiny `node --test` if the owner allows Node in CI (Q6). |
| S5 | Dashboard restyle | KPI row, availability tiles, group cards, details cards. | Page test: IDs present, hostile monitor name rendered as text in an API-to-page round trip. |
| S6 | Host page and Control restyle | Control inside main, `dialog.js` typedConfirm, toasts. | Existing control-action tests, plus page markup and the `<dialog>` present. |
| S7 | Admin, audit, map admin, port | Restyle and sortable tables. Audit gets its own route if Q5 is yes. | Page tests per route, admin-only gating (403/redirect for viewer). |
| S8 | Pockethernet pages | Use the core CSS and shell, result chips, labelled wiremap. | `test_pockethernet_pages.py` extended. |
| S9 | Graph engine | `graph/force.js`, `render.js`, `view.js` with no page wiring. | Static scan. A deterministic layout spec: the same input gives the same positions (pure-JS test needs Node, so Q6; otherwise a manual check documented in the PR). |
| S10 | Map graph view | Graph/Tiers/Table toggle, side card, keyboard, 300-node fallback. | Page test: canvas has `role="img"`, the table still renders, the API payload includes the anchor flag. |
| S11 | Add host API | `POST /api/hosts` (name, platform, agent, control, allowlist) returns a token and command; `GET /api/hosts/{n}/enrolment` returns progress; `GET /i/{token}` serves the script. Admin plus CSRF, token TTL, audit entries. | TestClient: validation (bad name, duplicate, platform enum), admin-only, CSRF required, token single-use and expiry, the progress state machine driven by fake ingest and control pull, the audit row written, the command header contains the host name and the token never appears in logs. |
| S12 | Add host wizard UI | `/hosts/new` page, steps, copy button, progress polling. | Page test for markup and IDs, admin gating, hostile host name rejected server side and rendered as text. |
| S13 | Host settings page | Allowlist edit with diff confirm, regenerate (revokes the old token), danger zone. | TestClient: PUT allowlist validation, regenerate invalidates the previous token, applied/pending status after a control pull. |
| S14 | Customise dashboard (optional) | `/api/ui/layout/{view}` per user, `tiles.js` with up/down and hide. | TestClient: per-user isolation, stale IDs dropped, unknown view rejected, size cap. |
| S15 | Cleanup and rename | Remove legacy aliases and `app.css`, change the old name to "Observe" in titles and brand. | Static scan: no `var(--bg)` and similar remain, every `<title>` ends with "- Observe". |

### S9 notes (done)

- `js/graph/force.js` holds `layoutForce`, a d3-free simulation (velocity decay 0.4, alpha decay for 300 steps, link, charge, centre, collide and radial forces) with the constants of section 2.10. It starts from the fixed ring and uses no random numbers, so the same input gives the same positions. Ticks are `min(360, floor(60000 / n))`. Charge and collide are O(n^2). Over 300 nodes it simulates nothing and returns `limited: true`, so the page falls back to the tiered view with a note (the other option in 2.10, a coarse grid, was not needed). An optional `fixed` map pins nodes, so one expanded switch can be re-laid out while the rest stay put.
- `js/graph/render.js` has `readTheme` (reads `--g-*`, `--cat-1` to `--cat-8`, `--cat-other` and the status tokens with `getComputedStyle` at paint time), `fitCamera`, `worldToScreen`, `screenToWorld`, a uniform grid `pickNode`, and `render` with two-pass links, focus dimming, label pills (55 at most, collision culling) and a badge showing a state word or a count. Each node has a status ring and a glyph (tick, X, !, broken link, hollow dot), and stale links are dashed. The only colour literals are the fallbacks used when a token cannot be read.
- `js/graph/view.js` has `createGraphView(canvas, options)`: one repaint per animation frame, at most about 30 per second while dragging, `devicePixelRatio` capped at 2, `ResizeObserver`, wheel zoom with `passive: false`, drag pan, hover and click selection, keyboard (arrows in reading order, Enter, plus, minus, 0, Escape), and a repaint when the system scheme or `data-theme` changes. The canvas gets `role="img"` and a summary label such as "42 devices, 3 down".
- `js/graph/types.js` holds JSDoc typedefs only. `css/graph.css` holds the container, toolbar and legend styles. No page loads any of them yet (S10 does), and a test asserts that.
- Tests: `tests/test_ui_graph.py` checks the sources, the header, the absence of d3, the token names, the constants and the CSP headers on the served files. `tests/js/graph.test.mjs` holds the layout tests (determinism, anchor near the centre, fixed nodes, tick budget, picking, view helpers) for `node --test tests/js` in CI. Per Q6, Node is not required locally.
- Deviations: nothing was measured on a Pi 3, so the 300 ms layout and 16 ms paint targets are not yet confirmed. A link is highlighted only when both of its ends are in the focus set.

### S7 notes (done)

- `admin.html`, `audit.html`, `infra-admin.html` and `port.html` load `components.css` and `css/admin.css` (tokens only) after `app.css`, with one entry module and then `shell.js`. `admin.js`, `audit.js`, `infra-admin.js` and `port.js` use `sortableTable`, `statusChip`, `confirmDialog` and `toast`; the shared builders are in `js/admin-ui.js`.
- Users and keys: two cards side by side (stacked below 900px), a one-time key notice with a read-only field and a Copy button (with a select fallback), and Revoke and Disable behind a confirm dialog. Audit has its own page, `/audit`, with the Admin nav entry (Q5); the admin page links to it. The API is unchanged.
- Audit filters: actor text, kind select, status toggle chips (OK below 400, Refused 401 and 403, Failed otherwise) and a time range; sortable columns and page sizes 25 and 100. Filtering is in the browser over the newest 500 rows, so no API change was needed.
- Map admin: three cards with sortable tables. Accept and Reject ask for confirmation, a toast reports the result, and the row moves to Decided without a reload. The port page has a breadcrumb, a state chip, and cards for live state, findings, properties and history; the old `statePill` is now `stateChip`.
- Deviations: the key form still takes a host name only (the API has no key name or scope field), and the Users table shows Id, Role and State instead of "Last login" because the API does not return it. The port page does not yet show a Neighbour or Cable test card; the data is not in its API (Pockethernet comes in S8).
- Tests: `tests/test_ui_admin.py` checks markup, stylesheet order, modules, IDs, the viewer 403s and the no-pill and no-innerHTML rules.

### S5 notes (done)

- The dashboard (`index.html`, `app.js`, `css/dashboard.css`) now follows section 3.2: a KPI row (Monitors, Down, Warnings, Capacity), a tinted availability tile row with an icon and a word per state, a state filter and search, collapsible group cards with a count chip per state, and `details.card` panels for the capacity outlook, field findings and recent state changes. The page loads `components.css` after `shell.css`, and `dashboard.css` after `app.css`.
- `app.js` is now an ES module and still writes every device-supplied string with `textContent`. Monitor and group states use `statusChip` and `statusIcon`; the header summary row uses chips too. The old "problems only" checkbox became the "Problems only" option of the state filter. The Down and Warnings KPI buttons set that filter and scroll to the groups, and the Capacity button opens and scrolls to the outlook. The Down value turns `--o-down` only above zero.
- Deviation: the per-monitor card keeps its click-to-expand sparkline and history instead of drawing a sparkline on every card, to avoid one history request per monitor on every refresh. The "Customize" control stays in phase 2 (S14), and the page title is now "Overview - Observe".
- Tests: `tests/test_ui_dashboard.py` checks the IDs and the stylesheet and module order, and sends a hostile monitor and group name through the API to confirm the page markup never contains it and the script only writes text.

### S6 notes (done)

- `host.html` loads `components.css` and `css/host.css` (tokens only) after `app.css`, and `host.js`, `host-control.js` and `shell.js` as ES modules. `host.js` draws a title block with a breadcrumb, a status chip and the last report age, a KPI row (CPU, Temperatures, Fans, Disks, each with an item count and a status chip), a `notice` card when the host is not good, and one `details.card` per section with `table.data` tables and status chips. No `.pill` is built any more.
- The Control section is now a `<section class="card" id="control">` inside `<main>`, after the `#page` mount, so the 10 second refresh of the page never redraws it. It stays hidden for viewers and unknown hosts.
- Control uses `confirmDialog` for the four actions and `typedConfirm` for the reboot, which is now its own danger button, "Reboot host...", outside the action list. The queue button is "Queue action". Command states are status chips (requested and pulled as pending, scheduled as warning, done as up, failed and refused as down). A refusal shows as an inline `notice` with `role="alert"` and an error toast; a queued request shows a status toast.
- Deviations: the "Settings" button is left for S7 and S12 because no settings page exists yet. The KPI tiles show the item count of the section and its status instead of a single headline value such as a percentage, because the host view API does not return one.
- Tests: `tests/test_ui_host.py` checks the markup, stylesheet order, module scripts and tokens-only CSS; `tests/test_control_actions.py` checks the page loads the shared dialog and keeps the typed host name and `confirm_host` rule.

### S4 notes (done)

- `css/components.css` holds cards (including `details.card` and `.card.notice`), the KPI row, status tiles, chips, buttons, tables, dialogs and toasts. It uses tokens only, with no colour literals. It is not yet linked by any page: the pages that adopt it (S5 onwards) add it after `base.css`, and the tests that pin the `tokens, base, app` order will gain it then.
- `js/chips.js` builds `statusChip(state, text)`, `neutralChip` and `monoTag`. The state table lives in `js/chip-states.js` as plain data (role, icon shape, word), so it can be tested without a browser. Unknown states fall back to a hollow "Unknown" chip.
- `js/table.js` exports `sortableTable({columns, rows, empty, pageSizes, caption})` with sortable headers, a page-size select, "Showing N of M" and previous and next buttons. The pure rules (`sortRows`, `nextSort`, `ariaSort`, `pageSlice`) live in `js/table-core.js`.
- `js/dialog.js` exports `confirmDialog` and `typedConfirm`, both returning a Promise of a boolean and returning focus to the opener. The typed-name rule is `typedMatches` in `js/dialog-logic.js`. `host-control.js` now uses these instead of its own copy (S6).
- `js/toast.js` exports `toast(text, kind)` with a polite status region and an alert region for errors. Errors stay until closed; the rest go after six seconds.
- Tests: `tests/test_ui_components.py` checks the sources, mirrors the sort rules in Python and asserts that no `.pill` element is built without text. `tests/js/table-core.test.mjs` holds the same cases for `node --test tests/js` in CI. Per Q6, Node is not required locally.
- Deviation: the pure helpers are split into `table-core.js`, `chip-states.js` and `dialog-logic.js`, because the modules import `dom.js` by an absolute URL that Node cannot resolve.

### S3 notes (done)

- `js/shell.js` and `css/shell.css` are loaded by every signed-in page (not the login page). Each page keeps a `<header id="shell-header">` with its `#summary` live region and any page-specific controls, plus `<nav id="shell-nav">`. Classic page scripts still find `#summary` at load time because the markup is static; the module adds the brand, the theme toggle and the user name around it.
- The `NAV` table lists only pages that exist today: Dashboard (host pages sit under it), Map (port pages sit under it), Map admin and Users and keys. The Hosts and Reports workspaces, Add host and Audit appear when S12 and S7 add their pages; a workspace with no visible item is not drawn.
- Deviation from section 2.3: the workspace row and the subnav are one grouped row (workspace label, then its links), because no workspace has a landing page yet. Below 700px it scrolls sideways.
- The nav data is not a new endpoint. The shell uses `GET /api/session` for the role and `GET /api/plugins` for plugin entries, which already omits admin-only plugin entries for viewers. `NavEntry` gained an optional `workspace` field (default `network`), validated at load.
- The header summary pill row is still written by each page's own script (dashboard and map). Making it a shared, clickable summary needs the dashboard rework in S5.

### S2 notes (done)

- `css/tokens.css` holds the light block, the system dark block (`:root:not([data-theme="light"])` inside `prefers-color-scheme: dark`) and the manual `:root[data-theme="dark"]` block. A test requires the two dark blocks to be identical.
- `css/base.css` holds the reset, body, focus ring, 32px (44px on touch) hit targets, `[hidden]`, reduced motion and a few utilities. `app.css` keeps only the component rules of pages that have not migrated yet, with no colour values of its own. Every page loads `tokens.css`, `base.css` and then `app.css`. `components.css` arrives with S4.
- `js/theme.js` cycles Auto, Light and Dark, applies `data-theme` and stores the choice per browser under the key `observe.theme`, with every storage call in try/catch (Q7). Importing it applies the stored choice. Only the map page imports it so far, because the other pages are still classic scripts. The shell in S3 adds the toggle button to every page.
- Deviations from the section 2.2 sketch, found by the contrast test: the light `--o-accent` and `--o-focus` are `#1d63b8` (the sketch's `#2a78d6` is 4.05:1 on the page and fails as link text), `--o-up` is `#0c7a0c`, `--o-warn` is `#8a5d00` and `--o-serious` is `#a94718`, all to reach 4.5:1 on their own tint. A new `--o-ink` (`#1c2230` in both themes) is the dark text used on amber fills.
- The 3:1 rule is applied to the status colours as dots and to the focus ring. The amber `--o-dot-warn` fill and the card borders are exempt: amber on white cannot reach 3:1 and stay amber, and borders are decoration, because state is always also written in words. A test keeps `--o-dot-warn` from being used as a text colour.
- The old `.pill` text colour is now `--o-page` (light text on dark fills in light mode, dark text on bright fills in dark mode), which fixes the weak white-on-green pills in dark mode.

---

## 6. Owner decisions (2026-10-05: all recommended defaults accepted)

| # | Question | Recommended default |
| --- | --- | --- |
| Q1 | Should the graph replace the tiered map or sit beside it? | **Beside it.** Default to Tiers on phones and above 300 nodes, otherwise Graph. The Table is always available. |
| Q2 | relationship-maps has no LICENSE. Port its renderer and force logic into Observe? | **Yes. It is your own code.** Add an MIT LICENSE to relationship-maps so that all three repos agree, and keep a header naming commit 0c4d268. |
| Q3 | Should the graph stay dark in light theme, matching HA SOC's look? | **No.** Follow the theme: a light canvas in light mode and the HA SOC near-black look in dark mode. |
| Q4 | Where are customise layouts stored? | **Server-side per user**, dashboard only, in phase 2 (S14). Skip drag-and-drop at first. |
| Q5 | Should Audit get its own page? | **Yes**, `/audit` under Admin, using the same API. The admin page keeps a link. |
| Q6 | Is Node allowed in CI only, for JS unit tests (`node --test`, no runtime build)? | **Yes, CI only.** Runtime stays no-build. Otherwise the pure JS logic is covered by documented manual checks. |
| Q7 | Is the theme choice per browser or per user? | **Per browser** (`localStorage`, Auto by default). It costs nothing on the server. |
| Q8 | Enrolment token lifetime and reuse? | **30 minutes, single use for the script fetch.** Agent keys are minted on first contact. Regenerate revokes the old token. |
| Q9 | Should the install command embed the token in the URL path (`/i/<token>`) or require a header? | **Path token.** It is short-lived and single-use, which is needed for a curl one-liner. Never put it in a query string, and never log it. |
| Q10 | Should Windows support control (fans and services) in the wizard? | **Agent only for now.** Control is greyed out with the reason given in text, until thermal-control has a Windows path. |
| Q11 | Brand mark? | **A text badge "O"** in accent on a tint, like HA SOC's "SOC" badge. No image asset is needed. |
| Q12 | Should viewers see admin nav items locked, as HA SOC does, or hidden? | **Hidden.** The server still enforces access. |
