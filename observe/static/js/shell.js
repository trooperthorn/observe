// The console shell: brand, theme toggle, user name and the navigation, drawn into the
// header and nav mounts every signed-in page carries. Ported in spirit from the workspace
// table in ha_Int_soc (MIT, same owner); the links are real pages, not HA panel paths.
// Admin entries are left out for viewers. That is only tidiness: the server still decides.
import { el } from "/static/js/dom.js";
import { applyStoredTheme, currentTheme, cycleTheme } from "/static/js/theme.js";
import { api, get, getAll, poller, whoami } from "/static/js/api.js";
import { statusChip } from "/static/js/chips.js";
import { stateInfo } from "/static/js/chip-states.js";
import { monitorCounts, summaryLabel } from "/static/js/summary-logic.js";

export const WORKSPACES = [
  ["overview", "Overview"], ["hosts", "Hosts"], ["network", "Network"],
  ["reports", "Reports"], ["admin", "Admin"],
];

// One row per page that exists. `also` lists pages that belong under the same item.
// Workspaces with no visible item are not drawn.
export const NAV = [
  { workspace: "overview", label: "Dashboard", href: "/", also: [], admin: false },
  { workspace: "hosts", label: "Hosts", href: "/hosts", also: ["/host"], admin: false },
  { workspace: "hosts", label: "Add host", href: "/hosts/new", also: [], admin: true },
  { workspace: "network", label: "Map", href: "/map", also: ["/port"], admin: false },
  { workspace: "network", label: "Map admin", href: "/admin/infra", also: [], admin: true },
  { workspace: "admin", label: "Users and keys", href: "/admin", also: [], admin: true },
  { workspace: "admin", label: "Audit", href: "/audit", also: [], admin: true },
  { workspace: "admin", label: "Polling tiers", href: "/admin/tiers", also: [], admin: true },
  { workspace: "admin", label: "Retention", href: "/admin/retention", also: [], admin: true },
  { workspace: "admin", label: "Re-check", href: "/admin/recheck", also: [], admin: true },
  { workspace: "admin", label: "Threshold rules", href: "/admin/rules", also: [], admin: true },
  { workspace: "admin", label: "Storage", href: "/admin/storage", also: [], admin: true },
  { workspace: "admin", label: "Updates", href: "/admin/updates", also: [], admin: true },
];

const WORKSPACE_IDS = new Set(WORKSPACES.map(([id]) => id));

// The visible items: the core table plus the plugin entries the server already filtered,
// with admin entries dropped for a viewer. Plugin entries with an unknown workspace go to Network.
export function visibleItems(isAdmin, pluginNav) {
  const items = NAV.filter((n) => isAdmin || !n.admin).map((n) => ({ ...n, plugin: false }));
  for (const p of pluginNav || []) {
    if (typeof p.label !== "string" || typeof p.path !== "string" || !p.path.startsWith("/")) continue;
    items.push({
      workspace: WORKSPACE_IDS.has(p.workspace) ? p.workspace : "network",
      label: p.label, href: p.path, also: [], admin: false, plugin: true,
    });
  }
  return items;
}

function matches(item, path) {
  if (path === item.href || item.also.includes(path)) return item.href.length;
  if (item.plugin && path.startsWith(item.href + "/")) return item.href.length;
  return -1;
}

// The one item whose page this is: the longest matching href wins.
export function activeItem(items, path) {
  let best = null, len = -1;
  for (const i of items) {
    const m = matches(i, path);
    if (m > len) { best = i; len = m; }
  }
  return best;
}

export function renderNav(mount, items, path) {
  const active = activeItem(items, path);
  const groups = WORKSPACES
    .map(([id, title]) => [title, items.filter((i) => i.workspace === id)])
    .filter(([, list]) => list.length);
  const ul = el("ul", "shell-groups");
  for (const [title, list] of groups) {
    const li = el("li", "shell-group");
    const inner = el("ul", "shell-links");
    for (const i of list) {
      const a = el("a", "shell-link", i.label);
      a.href = i.href;
      if (i === active) a.setAttribute("aria-current", "page");
      const row = el("li");
      row.append(a);
      inner.append(row);
    }
    li.append(el("span", "shell-ws", title), inner);
    ul.append(li);
  }
  mount.replaceChildren(ul);
}

function themeLabel(value) {
  return `Theme: ${value.charAt(0).toUpperCase()}${value.slice(1)}`;
}

function brand() {
  const h1 = el("h1", "shell-brand");
  const a = el("a", "shell-home");
  a.href = "/";
  a.append(el("span", "shell-badge", "O"), el("span", "shell-name", "Observe"));
  h1.append(a);
  return h1;
}

function themeButton() {
  const b = el("button", "shell-theme", themeLabel(currentTheme()));
  b.type = "button";
  b.addEventListener("click", () => { b.textContent = themeLabel(cycleTheme()); });
  return b;
}

// Sign out is on every page for a signed-in visitor, not only on /admin.
function signOutButton() {
  const b = el("button", "btn ghost shell-signout", "Sign out");
  b.type = "button";
  b.addEventListener("click", async () => {
    b.disabled = true;
    try { await api("POST", "/api/logout"); } catch (_) { /* fall through to the login page */ }
    window.location.assign("/login");
  });
  return b;
}

// One read of the session or the plugin list. A 401 means the session ended (the client has
// already sent the visitor to sign in), any other failure leaves the nav as the cached role drew it.
async function read(path) {
  try {
    return await get(path, null, { redirect: false });
  } catch (err) {
    return err && err.status === 401 ? { expired: true } : null;
  }
}

// The last known role, so the nav is drawn at once on every page instead of after the session
// round trip. It only decides which links are drawn; the server still checks every request.
const ROLE_KEY = "observe.nav.admin";

function cachedAdmin() {
  try { return window.sessionStorage.getItem(ROLE_KEY) === "1"; } catch (_) { return false; }
}

function rememberAdmin(isAdmin) {
  try { window.sessionStorage.setItem(ROLE_KEY, isAdmin ? "1" : "0"); } catch (_) { /* private mode */ }
}

// Send an expired session to sign in, coming back to this page afterwards.
export function toLogin() {
  const back = window.location.pathname + window.location.search;
  window.location.assign(`/login?next=${encodeURIComponent(back)}`);
}

// The navigation entries of every plugin, from GET /api/v2/plugins.
function pluginNav(plugins) {
  if (!plugins || !Array.isArray(plugins.items)) return null;
  return plugins.items.flatMap((p) => p.nav.map((n) => ({ plugin: p.name, ...n })));
}

// The header summary, the same on every page: how many monitors are in each effective state,
// linked to the dashboard. Page-specific status (hosts, map devices, one host) is in the page
// body. A reader who may not list monitors simply sees no summary.
const WORDS = Object.fromEntries(["down", "unreachable", "warn", "pending", "up"]
  .map((s) => [s, stateInfo(s).word]));

function drawSummary(mount, monitors) {
  const counts = monitorCounts(monitors);
  if (!counts.length) { mount.replaceChildren(); return; }
  const a = el("a", "shell-summary");
  a.href = "/";
  a.setAttribute("aria-label", summaryLabel(counts, WORDS));
  a.title = "Monitors by state; open the dashboard";
  a.append(...counts.map(([s, n]) => statusChip(s, `${n} ${WORDS[s]}`)));
  mount.replaceChildren(a);
}

function mountSummary() {
  const mount = document.getElementById("summary");
  if (!mount) return;
  poller(async () => {
    let monitors;
    try {
      monitors = await getAll("/api/v2/monitors", 500, { fields: "slug,effective_state" },
        { redirect: false });
    } catch (err) {
      if (err && (err.status === 401 || err.status === 403)) { mount.replaceChildren(); return; }
      throw err;
    }
    drawSummary(mount, monitors);
  }, { interval: 30000, domains: ["monitors"] });
}

export async function mountShell() {
  const header = document.getElementById("shell-header");
  const nav = document.getElementById("shell-nav");
  if (!header || !nav) return;
  applyStoredTheme();
  header.prepend(brand());
  header.append(themeButton());
  renderNav(nav, visibleItems(cachedAdmin(), null), window.location.pathname);
  mountSummary();
  // The session comes from whoami(), the one read every page module shares, so a page load asks
  // for it once, not once for the shell and once for the page.
  const session = whoami().catch((err) => (err && err.status === 401 ? { expired: true } : null));
  const [me, plugins] = await Promise.all([session, read("/api/v2/plugins")]);
  if (me && me.expired) { toLogin(); return; }
  const isAdmin = !!(me && me.is_admin);
  rememberAdmin(isAdmin);
  if (me && typeof me.username === "string") {
    header.append(el("span", "shell-user", me.username), signOutButton());
  }
  renderNav(nav, visibleItems(isAdmin, pluginNav(plugins)), window.location.pathname);
}

mountShell();
