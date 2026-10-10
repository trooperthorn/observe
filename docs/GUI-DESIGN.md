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
| Theme before slice S2 was `:root` tokens plus `@media (prefers-color-scheme: dark)` in `app.css`; since S2 the tokens live in `css/tokens.css` and the old names were aliases until S15 removed them. Status tokens were `--up --warn --down --pending --unreach`. Pills are white text on a filled colour. Monitor cards use a 4px left border. | `OBS/app.css:1-11, 22-35` | The token rename keeps the old names as aliases for one release, so the pages can migrate one slice at a time. |
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

Each HTML page loads `tokens.css`, `base.css` and `components.css`, then exactly one `<script type="module" src="/static/pages/x.js">`. `app.css` was a shim of legacy aliases and was deleted in S15; its remaining rules moved into `components.css`, `shell.css` and the page sheets, rewritten with `--o-*` tokens.

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

- Labelled `<label>` wrapping the input, or `for`/`id` pairs. Inputs use a 6px radius, `--o-border-input` (3:1 against the page and the card, unlike the quiet `--o-border`), and focus via `:focus-visible` with a 2px `--o-focus` outline.
- `.form-grid` is 2 columns that collapse to 1. Use a `fieldset` and `legend` for groups such as fan headers and services.
- Inline validation sits in a `<p class="field-err" id=...>` linked with `aria-describedby`. Server errors are rendered with `textContent` from `detail`, as `host-control.js` already does.
- Buttons: `.btn` (outline accent), `.btn.primary` (filled), `.btn.danger`, `.btn.ghost`. Disabled state uses 0.5 opacity.
- Every submit is handled by JS (`preventDefault` then `fetch`), because of `form-action 'none'`.
- Copy-to-clipboard button: `navigator.clipboard.writeText`, with a fallback when the page is not served from a secure context (`navigator.clipboard` is then undefined). The fallback selects the text of the field or the command block, a `<pre>` included, and shows the message "Selected, press Ctrl+C to copy.".

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
- Entities: switches, routers, hosts, APs, jacks (optional) and endpoints (collapsed into per-switch counts by default), from `/api/v2/map`.
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

Step 5  Live progress (polls GET /api/v2/hosts/nas01/enrolment every 3 s, aria-live)
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

### 3.12 Hosts list (`hosts.html`, `hosts.js`, `js/hosts-logic.js`, `/hosts`)

```
Hosts                                            12 hosts, 2 need attention, 1 waiting for first data
+ All hosts ---------------------------------------------------------------------------------+
| Search [________]                                                                          |
| Host ↕   | Platform | Status      | Last report | Agent | Monitor      | Detail  | Settings |
| nas01    | linux    | [v Up]      | 8 s ago     | 0.9.0 | [Listed]     |         | Settings |
| pi       | rpi      | [! Warning] | 2 h (stale) | 0.8.0 | Not listed   | stale   | Settings |
| newbox   | windows  | [o Waiting] | never       |       | Not listed   | not run | Enrolment|
+-------------------------------------------------------------------------------------------+
```
- The one place every host is listed. The Hosts item in the nav owns `/hosts` and `/host`, so a
  host page lights the Hosts workspace, and the host and settings breadcrumbs lead back here.
- Rows come from `GET /api/v2/hosts` (every page, through `getAll`) plus `GET /api/v2/waiting-hosts`.
  A waiting host has no host page yet, so its only link is its enrolment page, for admins.
- The Monitor column reports whether the host is listed as a `pushed_host` in the YAML, which is
  what lets it alert. The page reports it and changes nothing.
- The Settings column is drawn only for an admin session; the server still decides.
- The dashboard's link to a host page is a small accent-coloured "Host page" button, not muted text.

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
| S11 | Add host API | `POST /api/hosts` (name, platform, agent, control, allowlist) returns a token and command; `GET /api/v2/hosts/{n}/enrolment` returns progress; `GET /i/{token}` serves the script. Admin plus CSRF, token TTL, audit entries. | TestClient: validation (bad name, duplicate, platform enum), admin-only, CSRF required, token single-use and expiry, the progress state machine driven by fake ingest and control pull, the audit row written, the command header contains the host name and the token never appears in logs. |
| S12 | Add host wizard UI | `/hosts/new` page, steps, copy button, progress polling. | Page test for markup and IDs, admin gating, hostile host name rejected server side and rendered as text. |
| S13 | Host settings page | Allowlist edit with diff confirm, regenerate (revokes the old token), danger zone. | TestClient: PUT allowlist validation, regenerate invalidates the previous token, applied/pending status after a control pull. |
| S14 | Customise dashboard (done) | `/api/ui/layout/{view}` per user, `tiles.js` with up/down and hide. | TestClient: per-user isolation, stale IDs dropped, unknown view rejected, size cap. |
| S15 | Cleanup and rename (done) | Remove legacy aliases and `app.css`, change the old name to "Observe" in titles and brand. | Static scan: no `var(--bg)` and similar remain, every `<title>` ends with "- Observe". |

### S11a notes (done)

- S11 is split. S11a is `POST /api/hosts`, `GET /api/hosts/{name}/enrolment`, the `enrolments` table (schema version 10) and `observe/enrol.py`, including `redeem`, which spends the token and mints the `wpi` and `wpc` keys. S11b is `GET /i/{token}` and the install scripts, which call `redeem`; it is built for Linux and Raspberry Pi (see the S11b notes), and the TrueNAS and Windows scripts are S11c (see the S11c notes).
- The create body is `name`, `platform`, `agent`, `control` and `allowlist` (`fans`, `services`, `reboot`). A fan entry is a name or `{"header", "min_duty_limit"}`, so thermalctl gets a floor limit per header. Windows with control is a 422 with the reason (Q10). The response has `command` (two lines, the first a comment naming the host and platform), `expires_at` and `ttl_s`. The token is only in that response.
- The progress response has `state` (`waiting`, `script_fetched`, `first_data`, `control_pulled`, `ready`, `expired`), `ready`, `expired` and a `steps` list of `{id, label, status, at}` for `script`, `data`, `control` and `ready`, which is what the step 5 chip rows (S12) draw. A step is `done`, `waiting`, `skipped` or `expired`.
- Audit kinds are `enrol_created`, `enrol_create_failed`, `enrol_fetched`, `enrol_fetch_failed` and `enrol_expired`.
- Tests: `tests/test_enrol_api.py`. The control pull is simulated by an authenticated `wpc` key check, because the control plugin is not loaded in these tests.
- Deviations: the `GET /i/{token}` route is not part of this slice (see S11b above). S12 adds a regenerate route for an enrolment whose script was not fetched, so an expired enrolment can be replaced (see the S12 notes).

### S11b notes (done, Linux and Raspberry Pi)

- `observe/scripts.py` renders the script for `linux` and `raspberry-pi`; `GET /i/{token}` in `observe/web.py` serves it as `text/x-shellscript` with `no-store`. Since fix-enrol-flow the fetch no longer spends the token (see the fix-enrol-flow notes below); the redeem endpoint does. A used, expired or unknown token is 410. A platform with no script (TrueNAS, Windows) is 501, and control without a loaded control plugin is 409, both before the token is spent.
- Order inside the script: root check, hostname guard (short, fully qualified or plain hostname, case-insensitive, printing both names on refusal), Observe-host guard (machine-id, then Observe's listen and request addresses, then what the console URL resolves to, against the local addresses), rerun detection, and only then changes. Each refusal changes nothing and is reported.
- Agent: needs Docker, writes `/etc/hostwatch/agent.env` (0600) with the ingest key and `HOSTWATCH_HOST_NAME`, replaces the `hostwatch-agent` container with the same hardening as hostwatch's `deploy/agent/docker-compose.yml`, and mounts `/run/thermalctl` read only when it exists. Control: account, venv install of `hostwatch[control]` from github.com/trooperthorn/hostwatch, `control.toml` and `control.env`, sudoers checked with `visudo -c`, unit started.
- Progress: `POST /api/enrol/step` with `Authorization: Bearer wps_...` (the step key minted at the fetch, digest stored, valid two hours). Steps are `root`, `hostname`, `observe_host`, `rerun`, `agent`, `control_account`, `control_install`, `control_config`, `sudoers`, `control_unit` and `done`; statuses `ok`, `failed`, `skipped`, `refused`. The enrolment progress response gains `install`. Schema version 11 adds the columns.
- Service names are now matched against the control daemon's own pattern (no `@`, no leading dash, no `..`) when a host is created.
- Tests: `tests/test_install_script.py`. The script is only syntax checked (`bash -n` and `sh -n`); it has not been run on a real machine.
- Deviations: `control.toml` has a single `min_duty_floor`, so the largest per-header `min_duty_limit` is used. A run on the wrong machine still spends the token. Regenerate on the settings page (S13) makes a new command and revokes the old keys, and Clean up this machine undoes the mistaken install.
- Also: `machine_id` is written before the first table in `control.toml`, because the daemon reads it only as a top-level key. `control.env` is `root:hostwatch-control` 0640 to match hostwatch's install contract (`deploy-agents.md`), although systemd reads it as root and 0600 would also work. Both files are written beside the live ones and moved into place only after the sudoers rules render and pass `visudo -c`.
- Limits: progress reports are best effort. A missing `curl` or a failed report is ignored, so Observe can show no install steps for a run that did happen; the screen output of the script is the full record. A run replaces any container named `hostwatch-agent`, including one started from hostwatch's compose file; the script says so, and the old agent's data volume is kept, not deleted, while the new agent uses the volume `hostwatch-agent-data`. Refused step reports write at most one audit row per peer per minute, with a count.

### S11c notes (done, TrueNAS and Windows)

- `observe/scripts.py` gains `render_truenas`, `render_windows` and `render`; `GET /i/{token}` now serves all four platforms. The TrueNAS script (POSIX sh) shares the Linux helper functions and guard block by cutting them out of the Linux text. After the guards it checks that `/mnt/<pool>` exists (default pool `Apps`), writes `/mnt/<pool>/hostwatch/agent.env` (0400, `HOSTWATCH_HUB_URL`, `HOSTWATCH_INGEST_KEY`, `HOSTWATCH_HOST_NAME`) and `compose.yaml` (no key; hardening as in hostwatch `deploy/truenas/compose.yaml`; journal group detected on the host; the data folder is `data` beside it, owned by 10001), then prints a banner naming the host with the one manual UI step: Apps, Discover Apps, Install via YAML, name `hostwatch`, paste the file. That step is reported as `app` with status `skipped`.
- The PowerShell script is for `irm ... | iex`: one function called on the last line, no `exit`, ASCII only. Guards: administrator, then the computer name, DNS host name or its full name against the enrolled name (any case), then the Observe-host check (MachineGuid against Observe's machine id, then Observe's addresses and what the console URL resolves to against the local interface addresses). Then it downloads the hostwatch archive to a temporary folder, runs `deploy\windows\install.ps1 -HubUrl -HostName -IngestKey <SecureString> -SourcePath -Force` (after `uninstall.ps1 -Force` when the service already exists), checks the service is running and removes the folder.
- The pool travels as `?pool=NAME` on the command (create body field `pool`, TrueNAS only), because the enrolment row has no pool column. A bad pool is 400 before the token is spent. Install steps `pool`, `download`, `compose` and `app` are new.
- Tests: `tests/test_install_script_agents.py` (`bash -n` and `sh -n` for TrueNAS, the PowerShell parser for Windows when PowerShell is present, guard order, secrets, injection inputs, routes).
- Deviations: control is agent only on TrueNAS as well as Windows, so a TrueNAS enrolment with control is a 409 at fetch rather than a 422 at create (the wizard greys it out in S12). hostwatch's `install.ps1` names the variable `HOSTWATCH_INGEST_TOKEN` for a key that does not start with `hw_`, so a `wpi_` key lands there. The TrueNAS API source and RAPL group are not set up by the script (they need a TrueNAS API key and a host group id); the banner points to hostwatch's `docs/deploy-truenas.md`. Neither script has been run on a real machine.

### S9 notes (done)

- `js/graph/force.js` holds `layoutForce`, a d3-free simulation (velocity decay 0.4, alpha decay for 300 steps, link, charge, centre, collide and radial forces) with the constants of section 2.10. It starts from the fixed ring and uses no random numbers, so the same input gives the same positions. Ticks are `min(360, floor(60000 / n))`. Charge and collide are O(n^2). Over 300 nodes it simulates nothing and returns `limited: true`, so the page falls back to the tiered view with a note (the other option in 2.10, a coarse grid, was not needed). An optional `fixed` map pins nodes, so one expanded switch can be re-laid out while the rest stay put.
- `js/graph/render.js` has `readTheme` (reads `--g-*`, `--cat-1` to `--cat-8`, `--cat-other` and the status tokens with `getComputedStyle` at paint time), `fitCamera`, `worldToScreen`, `screenToWorld`, a uniform grid `pickNode`, and `render` with two-pass links, focus dimming, label pills (55 at most, collision culling) and a badge showing a state word or a count. Each node has a status ring and a glyph (tick, X, !, broken link, hollow dot), and stale links are dashed. The only colour literals are the fallbacks used when a token cannot be read.
- `js/graph/view.js` has `createGraphView(canvas, options)`: one repaint per animation frame, at most about 30 per second while dragging, `devicePixelRatio` capped at 2, `ResizeObserver`, wheel zoom with `passive: false`, drag pan, hover and click selection, keyboard (arrows in reading order, Enter, plus, minus, 0, Escape), and a repaint when the system scheme or `data-theme` changes. The canvas gets `role="img"` and a summary label such as "42 devices, 3 down".
- `js/graph/types.js` holds JSDoc typedefs only. `css/graph.css` holds the container, toolbar and legend styles. No page loads any of them yet (S10 does), and a test asserts that.
- Tests: `tests/test_ui_graph.py` checks the sources, the header, the absence of d3, the token names, the constants and the CSP headers on the served files. `tests/js/graph.test.mjs` holds the layout tests (determinism, anchor near the centre, fixed nodes, tick budget, picking, view helpers) for `node --test tests/js` in CI. Per Q6, Node is not required locally.
- Deviations: nothing was measured on a Pi 3, so the 300 ms layout and 16 ms paint targets are not yet confirmed. A link is highlighted only when both of its ends are in the focus set.

### S10 notes (done)

- `/api/v2/map` switch nodes carry `anchor`: true for a switch with a switch link that is not through its own uplink port. `js/graph/infra.js` builds entities, relations and anchors from the payload and holds the default-view rule.
- `map.html` and `pages/map.js` have a Graph, Tiers and Table toggle kept in the URL hash. Graph sits beside Tiers (Q1). Tiers is the default on screens up to 600px and above 300 switches, with a note when the graph is limited. The Links table is always shown.
- The side card lists the selected device, its type, its state in words and its links as real links to port pages, and has a Clear selection button. Keyboard support comes from `view.js`, and Enter opens the first linked port page. A click on empty canvas or Escape clears the selection. A selection dims the other devices to `--g-dim` (0.5) and keeps their labels; each label has a second line with the state word and the device type.
- Bug plan WP4: the device type comes from `/api/v2/map` (`device_type` on a switch node). `js/map-logic.js` (no DOM, tested in `tests/js/map.test.mjs`) holds the type words, the tier rule (a gateway is Core, an access point Access, the rest by link depth) and the Devices and Links table rows. The Table view shows a Devices table, then the Links table, both `table.data` with a caption.
- Tests: `tests/test_ui_graph.py`, `tests/test_infra_map.py` (anchor flag) and `tests/js/infra.test.mjs`.
- Deviations: endpoints and jacks are not graph nodes (endpoints are a count badge). Layout positions are not cached in sessionStorage yet.

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

### S12 notes (done)

- `GET /hosts/new` serves `static/hosts-new.html`, a static page for admins like the other admin pages. A viewer who opens it gets the "Admin account needed" card, and every API it calls refuses a viewer. `static/hosts-new.js` is the page module, `static/js/wizard-logic.js` holds the pure rules (name, header, service and pool patterns that mirror `observe/enrol.py`, the create body, the hash to step rule and the status to chip table), and `static/css/wizard.css` holds the styles, tokens only. The nav gains Add host under Hosts, for admins only.
- The five steps are static `<section>` panels toggled with `hidden`, and `<ol class="steps">` marks the current one with `aria-current="step"`. The step is kept in the hash, so Back and Forward work. A hash for step 4 or 5 without a created host goes to step 1, because the command is held in memory only and never goes into the URL, storage or a toast.
- Step 1 checks the name live against `GET /api/v2/hosts` and the name pattern, and TrueNAS shows a pool field (default `Apps`). Step 2 greys out control for Windows and TrueNAS with the reason in text. Step 3 offers fan header and service checkboxes with add fields, an optional lowest remote duty per header (sent as `min_duty_limit`), and the reboot checkbox; Raspberry Pi pre-selects `pwm-fan`. Without control the wizard creates the host after step 2. Step 4 shows `<pre class="cmd">` filled with `textContent`, headed by the host name and platform, with a Copy button (clipboard, or a toast asking the person to copy by hand). Step 5 polls `GET /api/v2/hosts/{name}/enrolment` every 3 seconds into an `aria-live` list of chip rows (icon, word and time) and a second list of the install script's own step reports, so a refusal such as the wrong host name shows with its note. Polling stops when the host is ready or the command expired, and a reply that arrives after the person left the step is ignored.
- Regenerate: `POST /api/hosts/{name}/enrolment/regenerate` (admin and CSRF) replaces the token of an enrolment whose script was not fetched, keeping its choices, and returns a new command. The old token is dead at once. It is refused with 404 after the fetch. The "Command expired" notice with the Regenerate button shows in steps 4 and 5. S13 uses it for an install whose script was not fetched, and a new reissue route for one that was.
- Tests: `tests/test_ui_wizard.py` (markup, IDs, style order, modules, text-only writes, the command never in a URL or storage, polling rules, client and server pattern parity, admin gating, hostile name refused and not echoed, regenerate behaviour and audit) and `tests/js/wizard.test.mjs` for CI.
- Deviations: the wizard is not exercised in a real browser in this slice, only through the page and API tests and a syntax check, so the layout and the step flow have not been seen on screen. Going Back from step 4 and pressing Create again reports that the host already exists; the intended way to start over is Add another. The "Settings" button on the host page was added in S13.

### S13 notes (done)

- `GET /hosts/{name}/settings` serves `static/host-settings.html` (admin only like the wizard: a viewer sees the "Admin account needed" card and every API refuses a viewer). `host-settings.js` is the page module, `js/settings-logic.js` holds the pure rules (draft and request body, the diff wording, status chips, polling rule), and `css/settings.css` the few extra styles, tokens only. The host page shows a Settings button to admins.
- Allowlist: the card lists the saved fan headers (with the lowest remote duty), services and reboot, with add fields. Save builds a list of changes, shows it in `confirmDialog` (which gained an optional `lines` list), and only then calls `PUT /api/hosts/{name}/allowlist`. The status chip is Pending, Written (waiting for the next pull) or Applied, each with a word and an icon. Before the install command was run, saving puts the list into that command and makes no update command.
- Update command: `GET /t/{token}` serves a short script that rewrites `control.toml` and the sudoers rules and restarts the control service, with the install script's three guards. Install, update and cleanup commands all show in one command card with the headed text, a Copy button, the expiry and live progress. Progress polls every 3 seconds while a task is waiting or running, or an install command is being watched, and stops when it is done.
- Regenerate: a confirm dialog. Before the script was fetched it uses the regenerate route; after, `POST /api/hosts/{name}/enrolment/reissue`, which also revokes the old agent and control keys, and the dialog says so.
- Clean up this machine: a confirm dialog, then a cleanup command for Linux, Raspberry Pi, TrueNAS or Windows. The script has the root guard and a match guard (an install made for this host must be here) instead of the hostname and Observe-host guards, because the mistaken machine is not the machine with that name.
- Danger zone: a closed `details` with Revoke host keys and Remove host, each behind `typedConfirm` with the host name, checked again on the server.
- Tests: `tests/test_host_settings.py` (API: validation, confirmation, update command and fetch, status after a pull, reissue, cleanup, danger zone, access, audit, no secrets), `tests/test_task_scripts.py` (rendering, syntax checks, guard order, hostile values), `tests/test_ui_settings.py` (markup, modules, text-only writes, diff rules mirrored in Python) and `tests/js/settings.test.mjs` for CI.
- Deviations: the cleanup scripts do not apply the hostname and Observe-host guards (see above), and do not revoke keys; the console offers Regenerate for that. Remove host deletes the host's stored hardware data as well as its enrolment and keys, but keeps the audit log and the control command history. The pages and scripts have not been exercised in a real browser or on a real host in this slice, only through the page and API tests, syntax checks and a JavaScript syntax check. The update and cleanup scripts assume the file locations the install script uses, and the Windows cleanup removes the service with `sc.exe delete` and the `hostwatch` folders itself instead of calling hostwatch's own uninstall script, so it has not been compared with that script on a real machine.

### fix-ui-a11y notes (done)

Copy, focus, contrast, fan names and the map view, from the audit of the console setup and new look.

- Copy on plain HTTP: `copyText` checks `navigator.clipboard` before calling it, so a missing clipboard no longer throws before the fallback. The fallback focuses the field and selects its text (`selectText` uses `select()` for an input and a range for a `<pre>`), then toasts "Selected, press Ctrl+C to copy.". The wizard passes its command block as the field, as the settings page already did.
- Wizard focus: a step change the person makes moves focus to the step heading (`tabindex="-1"`) and writes "Step N of 5: name" into a polite `role="status"` region (`#step-announce`, `stepAnnouncement` in `wizard-logic.js`). The first draw of the page does not move focus.
- Input contrast: new token `--o-border-input` (light `#7a8494`, dark `#6b7686`, 3.5:1 or better on page and card) for the wizard, login, admin and key fields. `tests/test_ui_tokens.py` checks it in both themes and that no field rule uses `--o-border`.
- Fan header names: `^[A-Za-z0-9_][A-Za-z0-9_-]{0,31}$`, hostwatch-control's `HEADER_ID`. It replaces the looser rule that allowed dots and a leading dash in `enrol.py`, `scripts.py`, the control queue and `wizard-logic.js`. `tests/test_fan_header_rule.py` copies hostwatch's rejected names and checks that all four places and the browser pattern are the same rule. An allowlist saved earlier with a dotted header is refused by the new rule when it is next edited, which is right because the host would never have accepted it.
- Map refresh: the 15 second refresh keeps the old layout and camera when the graph has the same devices, links and anchors (`structureKey`), merging only the new states and stale flags (`mergeLayout`). When the shape changes, a view the person has not moved is fitted again and a moved one is kept (`cameraAfterRefresh`; `st.touched` is set by drag, wheel and the zoom buttons, and cleared by Fit).
- Tests: `tests/test_ui_a11y.py`, `tests/test_fan_header_rule.py`, additions to `tests/test_ui_tokens.py`, `tests/js/wizard.test.mjs` and `tests/js/graph.test.mjs`.
- Deviations: the scripts were not run in a browser; the pure functions are covered by `node --test tests/js` and the wiring by source and page tests. A container resize still refits the camera.

### fix-enrol-flow notes (done)

Enrolment survives the wrong machine and explains failures. The audit of the console setup found that a command pasted on the wrong machine burned the token, that the command carried whatever Host header the request had, and that nothing said why an install had stopped.

- Fetch is split from redeem. `GET /i/{token}` serves the guarded script with no key in it and does not spend the token, so it can be fetched any number of times. The script carries the token as `REDEEM_TOKEN` and runs its guards (root, host name, not the Observe host). Only when all pass does it `POST /api/enrol/redeem` with the token in the body (read by curl from standard input, never on a command line). That call spends the token with the same one conditional `UPDATE` as before, mints the keys and returns `{host, agent_key, control_key, step_key}` as `no-store` JSON, which the script reads with `sed` and never evaluates. The PowerShell script does the same with `Invoke-RestMethod`.
- A guard that refuses reports to `POST /api/enrol/guard` with the token, the guard step (`root`, `hostname` or `observe_host`) and, for the host name guard, the name the machine gave itself. Observe keeps only the newest reason on the enrolment row (schema version 14: `guard_step`, `guard_reason`, `guard_at`), built on the server from a fixed set of texts, for example `ran on ai-pi, expected MediaIn-SVR`. The name is cut to host name characters and 64 of them. The token is left valid. Regenerate, reissue and a successful redeem clear the reason. The progress response gains `guard` (`step`, `reason`, `at`, or null) and `token_state` (`valid`, `used` or `expired`).
- The wizard shows a "refused on the machine" notice on steps 4 and 5 with the reason, and the host settings page shows the same text in its Install command card. Both say the command is still valid.
- The command never uses the request's Host header. The address is `server.public_url` from the config, or if that is unset an address an admin confirmed in the wizard and saved (`PUT /api/enrol/public-url`, admin, CSRF, read back in the host settings document, table `app_settings`, schema version 14). It must be `http(s)://host[:port]` with no path, no shell metacharacters and never a loopback or wildcard name (`localhost`, `*.localhost`, `127.0.0.0/8`, `::1`, `0.0.0.0`, `::`), checked by `config.normalise_public_url` and again in `scripts.py` before it is written into a script. The file wins: a `PUT` while `server.public_url` is set is 409. When neither exists, create, regenerate, reissue and the update and cleanup commands answer 409 with `code: public_url_required` before any token is made or any key revoked, and the wizard and the settings page show an "Observe address" box once (prefilled with the page's own origin as a suggestion the admin confirms), save it, and retry. The Observe-host guard's address list is built from the listen address and the host part of this address, not from the request.
- Expired and used commands are shown, not just refused. `noticeFor` (in `wizard-logic.js`) gives the wizard its "Command expired" and "Command already used" notices with a Regenerate command button: before the command was used it calls the regenerate route, after it the reissue route behind a confirm dialog that says the old keys are revoked. The settings page writes the same wording into `#token-state` and keeps its Regenerate button. The settings page polls while the command has not been run, so a refusal appears without a reload.
- An enrolled host with the agent chosen that has no row in `hosts` yet is "waiting for first data". `GET /api/v2/waiting-hosts` returns it (`host`, `platform`, `control`, `created`, `state`, `note`, `enrolment_url`), where the note says whether the command has not been run, was refused on a machine, expired, or was started and is waiting. The dashboard shows a "Waiting for first data" card (hidden when empty and for viewers without a session) with a link to `/hosts/{name}/settings`, the host's enrolment page. The first batch removes the host from `waiting` and lists it with the other hosts.
- Audit kinds added: `enrol_guard_refused`, `enrol_public_url_set` and `enrol_public_url_failed`. The redeem failure row keeps the kind `enrol_fetch_failed`; the fetch of an unknown, used or expired token is audited with that kind too, with path `/i/[token]`, and the redeem call with path `/api/enrol/redeem`.
- Tests: `tests/test_enrol_flow.py` (guard failure leaves the token valid and shows the reason, redeem spends once, a hostile Host header never reaches a command or script, a missing address is asked for and costs nothing, unsafe addresses are refused, expired and used states, a not-yet-reporting host is listed, keyless scripts pass syntax checks and redeem after the guards), with `run_install` in `tests/test_enrol_api.py` standing in for a host (fetch, then redeem). `tests/js/wizard.test.mjs` covers the pure rules.
- Deviations: `GET /t/{token}` (update and cleanup commands) still spends its token at the fetch, because only the install flow was in scope; it does use the configured address. The step label "Script fetched" now means the token was redeemed, which happens after the guards pass. The scripts have not been run on a real machine, only syntax checked.

### S14 notes (done)

- `observe/layout.py`, the `ui_layouts` table (schema version 13) and `GET`, `PUT` and `DELETE /api/ui/layout/{view}` store one layout per user and view. Only `dashboard` exists (404 otherwise). A tile id is `capacity`, `findings`, `events` or `group:<name>`; ids of the wrong shape and repeats are dropped on save and on read, lists are capped at 200 and the body at 32 KiB. The user id always comes from the session, so layouts are isolated per user and follow the user between devices.
- `js/tiles-logic.js` holds the pure rules (`effectiveOrder`, `effectiveHidden`, `moveTile`, `toggleHidden`), ported from ha_Int_soc (MIT, same owner). Saved ids that no longer exist are dropped, new groups are appended, and hiding never deletes data. `js/tiles.js` draws the controls and applies the layout after every dashboard render. The Customize button sits in the filter row, with Reset to default and a status line.
- A failed load keeps the declared order and a failed save keeps the screen, with a message. There is no drag and drop, as decided in Q4.
- Deviation: the cards are ordered by appending them to a new `#sections` container in layout order, not with the CSS `order` property, because the project's static checks and the CSP forbid inline styles. A new group card, and the keyboard focus on a control, are restored after each refresh.
- Deviation: the server checks only the shape of ids. Whether a group still exists is decided in the browser, because the server has no stable list of groups for a user to compare with.
- Tests: `tests/test_ui_customise.py` (per-user isolation, stale and malformed ids dropped, unknown view, size caps, bad bodies, CSRF and login rules, reset, markup and text-only modules, line endings, the pure rules in Python) and `tests/js/tiles.test.mjs` for `node --test` in CI. The page was not exercised in a real browser in this slice.

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
- The `NAV` table lists only pages that exist today: Dashboard (host pages sit under it), Map (port pages sit under it), Map admin and Users and keys. The Hosts and Reports workspaces and Audit appear when their pages are added (Add host arrived with S12); a workspace with no visible item is not drawn.
- Deviation from section 2.3: the workspace row and the subnav are one grouped row (workspace label, then its links), because no workspace has a landing page yet. Below 700px it scrolls sideways.
- The nav data is not a new endpoint. The shell uses `GET /api/v2/session` for the role and `GET /api/v2/plugins` for plugin entries, which already omits admin-only plugin entries for viewers. `NavEntry` gained an optional `workspace` field (default `network`), validated at load.
- The header summary pill row is still written by each page's own script (dashboard and map). Making it a shared, clickable summary needs the dashboard rework in S5.

### S2 notes (done)

- `css/tokens.css` holds the light block, the system dark block (`:root:not([data-theme="light"])` inside `prefers-color-scheme: dark`) and the manual `:root[data-theme="dark"]` block. A test requires the two dark blocks to be identical.
- `css/base.css` holds the reset, body, focus ring, 32px (44px on touch) hit targets, `[hidden]`, reduced motion and a few utilities. `app.css` keeps only the component rules of pages that have not migrated yet, with no colour values of its own. Every page loads `tokens.css`, `base.css` and then `app.css`. `components.css` arrives with S4.
- `js/theme.js` cycles Auto, Light and Dark, applies `data-theme` and stores the choice per browser under the key `observe.theme`, with every storage call in try/catch (Q7). Importing it applies the stored choice. Only the map page imports it so far, because the other pages are still classic scripts. The shell in S3 adds the toggle button to every page.
- Deviations from the section 2.2 sketch, found by the contrast test: the light `--o-accent` and `--o-focus` are `#1d63b8` (the sketch's `#2a78d6` is 4.05:1 on the page and fails as link text), `--o-up` is `#0c7a0c`, `--o-warn` is `#8a5d00` and `--o-serious` is `#a94718`, all to reach 4.5:1 on their own tint. A new `--o-ink` (`#1c2230` in both themes) is the dark text used on amber fills.
- The 3:1 rule is applied to the status colours as dots and to the focus ring. The border of a text field is not exempt: `--o-border-input` must reach 3:1 on the page and the card in both themes (WCAG 1.4.11), and a test fails if a rule for an input, select or textarea uses `--o-border`. The amber `--o-dot-warn` fill and the card borders are exempt: amber on white cannot reach 3:1 and stay amber, and borders are decoration, because state is always also written in words. A test keeps `--o-dot-warn` from being used as a text colour.
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

### S15 notes (done)

- `observe/static/app.css` is deleted and the `--bg`, `--card`, `--fg`, `--muted`, `--line`, `--up`, `--warn`, `--down`, `--pending` and `--unreach` aliases are gone from `tokens.css`. The component rules that pages still used moved as follows: the sticky header row to `shell.css`; the shared status `.pill`, the `.sec` card, `.note`, `ul.findings` and `a.home` to `components.css`; monitor card details, sparkline and lists to `dashboard.css`; the host banner and item table to `host.css`; the map layers, nodes and edges to `graph.css`. The sign-in page has its own `css/login.css`. Rules for the old `.adminpage` layout had no users and were dropped. `map.html` now also loads `components.css`.
- Titles: every page and plugin page title ends with " - Observe" (`Sign in - Observe`, `Map - Observe`, and the Pockethernet pages), and the sign-in heading reads "Observe".
- Screenshots, described in text. Add host (`/hosts/new`): a card with the host name field and platform choice, a chip row for the steps, then a code block headed with the host name and a Copy button, with a live list of Script fetched, First data, Control pulled and Ready. Host settings (`/hosts/{name}/settings`): cards for identity, the control allowlist with a diff dialog, the regenerate command, the clean up command and a danger zone. Overview: a KPI row, state tiles, collapsible group cards with status chips and a Customize button.
- Tests: `tests/test_ui_cleanup.py` checks that `app.css` is gone and unlinked, that no stylesheet or script uses a legacy variable, that every title ends with " - Observe", and that the README setup section mentions `/hosts/new`. `tests/test_ui_tokens.py` now also checks that no stylesheet other than `tokens.css` holds a hex colour.
