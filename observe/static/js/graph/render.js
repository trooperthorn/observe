// Ported from trooperthorn/relationship-maps, packages/graph-core (commit 0c4d268), via ha_Int_soc (MIT). No d3.
// Canvas painter. Colours are read from the CSS tokens at paint time, so light and dark both
// work. State is shown as a ring and a glyph inside the node and as a word under its label, never
// by colour alone. A selection dims the rest of the graph but leaves every label readable.

export const MAX_LABELS = 55;
const LABEL_FONT = "600 11px ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif";
const BADGE_FONT = "600 9px ui-sans-serif, system-ui, sans-serif";
const SUB_FONT = "500 10px ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif";
// Links outside the selection stay visible: the person still needs to see where they go.
const DIM_LINK_ALPHA = 0.3;
const CATEGORY_COUNT = 8;
const GRID_CELL = 48;
const STATE_TOKENS = {
  up: "--o-up", warn: "--o-warn", serious: "--o-serious", down: "--o-down",
  pending: "--o-pending", unreach: "--o-unreach",
};
const LINK_WIDTH = { uplink: 2, lldp: 1.4, mac: 1 };

// Fallbacks apply only when a token cannot be read, for example in a test without a stylesheet.
const FALLBACK = {
  bg: "#f7f8fa", stroke: "#ffffff", labelBg: "rgba(255,255,255,.92)", labelFg: "#1c2230", dim: 0.5,
  link: "#5d6676", accent: "#1d63b8", other: "#9aa0a6",
};

function token(cs, name, fallback) {
  const v = cs && cs.getPropertyValue ? cs.getPropertyValue(name).trim() : "";
  return v || fallback;
}

// Read every colour the painter needs from the element's computed style.
export function readTheme(element) {
  let cs = null;
  try {
    cs = window.getComputedStyle(element);
  } catch (e) {
    cs = null;
  }
  const dim = parseFloat(token(cs, "--g-dim", String(FALLBACK.dim)));
  const cats = [];
  for (let i = 1; i <= CATEGORY_COUNT; i++) cats.push(token(cs, "--cat-" + i, FALLBACK.other));
  const states = {};
  for (const [name, tok] of Object.entries(STATE_TOKENS)) states[name] = token(cs, tok, FALLBACK.link);
  return {
    bg: token(cs, "--g-bg", FALLBACK.bg),
    stroke: token(cs, "--g-node-stroke", FALLBACK.stroke),
    labelBg: token(cs, "--g-label-bg", FALLBACK.labelBg),
    labelFg: token(cs, "--g-label-fg", FALLBACK.labelFg),
    dim: Number.isFinite(dim) ? dim : FALLBACK.dim,
    link: token(cs, "--o-text-muted", FALLBACK.link),
    accent: token(cs, "--o-accent", FALLBACK.accent),
    other: token(cs, "--cat-other", FALLBACK.other),
    cats,
    states,
  };
}

// Colour for group number i: the --cat-* tokens in order, then the neutral one.
export function paletteColor(theme, i) {
  return i >= 0 && i < theme.cats.length ? theme.cats[i] : theme.other;
}

export function stateColor(theme, state) {
  return theme.states[state] || theme.states.pending;
}

export function worldToScreen(c, x, y) {
  return [x * c.k + c.x, y * c.k + c.y];
}

export function screenToWorld(c, x, y) {
  return [(x - c.x) / c.k, (y - c.y) / c.k];
}

// Camera that fits the whole layout into w by h with a margin, never magnifying past 1:1.
export function fitCamera(layout, w, h) {
  if (!layout.nodes.length) return { x: 0, y: 0, k: 1 };
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  // Pad above each node: labels are drawn in screen space above the dot.
  for (const p of layout.nodes) {
    const pad = p.r + 46;
    minX = Math.min(minX, p.x - pad);
    minY = Math.min(minY, p.y - pad);
    maxX = Math.max(maxX, p.x + pad);
    maxY = Math.max(maxY, p.y + pad);
  }
  const k = Math.min(1, Math.min(w / (maxX - minX), h / (maxY - minY)) * 0.94);
  return { k, x: w / 2 - ((minX + maxX) / 2) * k, y: h / 2 - ((minY + maxY) / 2) * k };
}

// A uniform grid over the nodes, enough for hit tests below about 1000 nodes.
export function buildGrid(nodes, cell) {
  const size = cell || GRID_CELL;
  const cells = new Map();
  for (const n of nodes) {
    const key = Math.floor(n.x / size) + "," + Math.floor(n.y / size);
    const list = cells.get(key);
    if (list) list.push(n);
    else cells.set(key, [n]);
  }
  return { size, cells };
}

const GRIDS = new WeakMap();

// The node under a world point, or null. The grid is built once per layout.
export function pickNode(layout, wx, wy) {
  let grid = GRIDS.get(layout);
  if (!grid) {
    grid = buildGrid(layout.nodes);
    GRIDS.set(layout, grid);
  }
  const reach = 40;
  const x0 = Math.floor((wx - reach) / grid.size);
  const x1 = Math.floor((wx + reach) / grid.size);
  const y0 = Math.floor((wy - reach) / grid.size);
  const y1 = Math.floor((wy + reach) / grid.size);
  let best = null;
  let bestD = Infinity;
  for (let gx = x0; gx <= x1; gx++) {
    for (let gy = y0; gy <= y1; gy++) {
      for (const n of grid.cells.get(gx + "," + gy) || []) {
        const d = Math.hypot(n.x - wx, n.y - wy);
        if (d <= n.r + 8 && d < bestD) {
          best = n;
          bestD = d;
        }
      }
    }
  }
  return best ? best.entityId : null;
}

// Paint one frame. scene: { layout, entities (Map), camera, theme, showLabels, hovered, selected,
// focus (Set or null), dpr }. w and h are CSS pixels.
export function render(ctx, w, h, scene) {
  const { layout, entities, camera, theme, focus, hovered, selected, dpr } = scene;
  const toWorld = () => ctx.setTransform(camera.k * dpr, 0, 0, camera.k * dpr, camera.x * dpr, camera.y * dpr);
  const toScreen = () => ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  toScreen();
  ctx.fillStyle = theme.bg;
  ctx.fillRect(0, 0, w, h);
  toWorld();
  ctx.lineCap = "round";
  ctx.setLineDash([]);

  const lit = (id) => !focus || focus.has(id);

  // Two passes so highlighted links land on top of dimmed ones.
  const drawLinks = (highlighted) => {
    for (const l of layout.links) {
      const on = lit(l.source) && lit(l.target);
      if (on !== highlighted) continue;
      ctx.beginPath();
      ctx.moveTo(l.x1, l.y1);
      ctx.lineTo(l.x2, l.y2);
      ctx.strokeStyle = theme.link;
      ctx.globalAlpha = highlighted ? 0.85 : DIM_LINK_ALPHA;
      ctx.lineWidth = ((LINK_WIDTH[l.kind] || 1) * (highlighted ? 1 : 0.7)) / camera.k;
      ctx.setLineDash(l.stale ? [5 / camera.k, 4 / camera.k] : []);
      ctx.stroke();
    }
  };
  drawLinks(false);
  drawLinks(true);
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;

  for (const p of [...layout.nodes].sort((a, b) => a.r - b.r)) {
    ctx.globalAlpha = lit(p.entityId) ? 1 : theme.dim;
    drawNode(ctx, p, theme, camera, p.entityId === hovered || p.entityId === selected);
  }
  ctx.globalAlpha = 1;

  if (!scene.showLabels) return;

  // Every node keeps its label; the ones in the selection are placed first, then the biggest.
  const labelled = [...layout.nodes]
    .sort((a, b) => Number(lit(b.entityId)) - Number(lit(a.entityId)) || b.r - a.r)
    .slice(0, MAX_LABELS);
  for (const id of [hovered, selected]) {
    if (!id) continue;
    const p = layout.nodes.find((q) => q.entityId === id);
    if (p && !labelled.includes(p)) labelled.push(p);
  }
  const isForced = (p) => p.entityId === hovered || p.entityId === selected;
  labelled.sort((a, b) => Number(isForced(b)) - Number(isForced(a)));

  toScreen();
  ctx.font = LABEL_FONT;
  ctx.textBaseline = "middle";
  // Hovered and selected first, then biggest first. A label goes above its node, else below, right
  // or left of it, and is dropped only when all four collide with labels already placed.
  const placed = [];
  const hit = (boxes, x, y, bw, bh) =>
    boxes.some(([px, py, pw, ph]) => x < px + pw && x + bw > px && y < py + ph && y + bh > py);
  const collides = (x, y, bw, bh) => hit(placed, x, y, bw, bh);
  // The dots too, so a label keeps clear of other devices when it has a free side.
  const dots = layout.nodes.map((q) => {
    const [qx, qy] = worldToScreen(camera, q.x, q.y);
    const qr = (q.r + 3) * camera.k;
    return [qx - qr, qy - qr, qr * 2, qr * 2];
  });
  for (const p of labelled) {
    const e = entities.get(p.entityId);
    if (!e) continue;
    const [sx, sy] = worldToScreen(camera, p.x, p.y);
    // The second line is the state word and the device type, for example "Up · Gateway".
    let subW = 0;
    if (e.sub) {
      ctx.font = SUB_FONT;
      subW = ctx.measureText(e.sub).width;
      ctx.font = LABEL_FONT;
    }
    const bw = Math.max(ctx.measureText(e.name).width, subW) + 14;
    const bh = e.sub ? 30 : 18;
    const near = (p.r + 4) * camera.k + 6;
    const room = bh + (e.badge ? 16 : 4);
    const forced = isForced(p);
    const spots = [
      [sx - bw / 2, sy - near - bh], [sx - bw / 2, sy + near],
      [sx + near, sy - bh / 2], [sx - near - bw, sy - bh / 2],
    ].filter(([x, y]) => !(x + bw < 0 || x > w || y + bh < 0 || y > h));
    const free = ([x, y]) => forced || !collides(x - 2, y - 2, bw + 4, room);
    const spot = spots.find((xy) => free(xy) && !hit(dots, xy[0], xy[1], bw, bh)) || spots.find(free);
    if (!spot) continue;
    const [bx, by] = spot;
    placed.push([bx - 2, by - 2, bw + 4, room]);
    ctx.globalAlpha = lit(p.entityId) ? 1 : theme.dim;
    ctx.fillStyle = theme.labelBg;
    ctx.strokeStyle = paletteColor(theme, p.group);
    ctx.lineWidth = 1;
    roundRect(ctx, bx, by, bw, bh, 5);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = theme.labelFg;
    if (e.sub) {
      ctx.fillText(e.name, bx + 7, by + 9.5);
      ctx.font = SUB_FONT;
      ctx.fillText(e.sub, bx + 7, by + 22);
      ctx.font = LABEL_FONT;
    } else {
      ctx.fillText(e.name, bx + 7, by + bh / 2 + 0.5);
    }

    // The badge shows a state word or a count.
    if (e.badge) {
      ctx.font = BADGE_FONT;
      const cw = ctx.measureText(e.badge).width + 8;
      const cx = sx - (p.r + 4) * camera.k - cw + 2;
      ctx.fillStyle = theme.labelBg;
      ctx.strokeStyle = stateColor(theme, p.state);
      roundRect(ctx, cx, by + bh - 2, cw, 13, 6);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = theme.labelFg;
      ctx.fillText(e.badge, cx + 4, by + bh + 4.5);
      ctx.font = LABEL_FONT;
    }
  }
  ctx.globalAlpha = 1;
}

function drawNode(ctx, p, theme, camera, emphasised) {
  const sc = stateColor(theme, p.state);
  ctx.beginPath();
  ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
  ctx.fillStyle = paletteColor(theme, p.group);
  ctx.fill();
  ctx.strokeStyle = emphasised ? theme.labelFg : theme.stroke;
  ctx.lineWidth = (emphasised ? 2 : 1.2) / camera.k;
  ctx.stroke();
  // State ring around the node, in the status colour.
  ctx.beginPath();
  ctx.arc(p.x, p.y, p.r + 2.5, 0, Math.PI * 2);
  ctx.strokeStyle = sc;
  ctx.lineWidth = 2 / camera.k;
  ctx.stroke();
  drawGlyph(ctx, p, theme, camera);
}

// A small glyph inside the node: tick, X, !, broken link, or a hollow dot. Skipped when the node
// is too small to hold one.
function drawGlyph(ctx, p, theme, camera) {
  if (p.r < 7) return;
  const g = p.r * 0.45;
  ctx.strokeStyle = theme.stroke;
  ctx.fillStyle = theme.stroke;
  ctx.lineWidth = 1.8 / camera.k;
  ctx.beginPath();
  switch (p.state) {
    case "up":
      ctx.moveTo(p.x - g, p.y);
      ctx.lineTo(p.x - g * 0.25, p.y + g * 0.7);
      ctx.lineTo(p.x + g, p.y - g * 0.7);
      ctx.stroke();
      break;
    case "down":
      ctx.moveTo(p.x - g * 0.8, p.y - g * 0.8);
      ctx.lineTo(p.x + g * 0.8, p.y + g * 0.8);
      ctx.moveTo(p.x + g * 0.8, p.y - g * 0.8);
      ctx.lineTo(p.x - g * 0.8, p.y + g * 0.8);
      ctx.stroke();
      break;
    case "warn":
    case "serious":
      ctx.moveTo(p.x, p.y - g);
      ctx.lineTo(p.x, p.y + g * 0.2);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(p.x, p.y + g * 0.75, 1.2 / camera.k + 0.4, 0, Math.PI * 2);
      ctx.fill();
      break;
    case "unreach":
      // Two link halves with a gap between them.
      ctx.moveTo(p.x - g, p.y + g * 0.6);
      ctx.lineTo(p.x - g * 0.25, p.y - g * 0.1);
      ctx.moveTo(p.x + g * 0.25, p.y + g * 0.1);
      ctx.lineTo(p.x + g, p.y - g * 0.6);
      ctx.stroke();
      break;
    default:
      ctx.arc(p.x, p.y, g * 0.6, 0, Math.PI * 2);
      ctx.stroke();
  }
}

export function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
