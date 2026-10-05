// Ported from trooperthorn/relationship-maps, packages/graph-core (commit 0c4d268), via ha_Int_soc (MIT). No d3.
// A small force simulation with the same model as d3-force: velocity decay, alpha decay and the
// link, charge, centre, collide and radial forces. It is deterministic, because the start is a
// fixed ring and nothing uses random numbers. It runs once, off screen, then the view paints.

export const FORCE_NODE_LIMIT = 300;
export const MAX_TICKS = 360;
export const TICK_BUDGET = 60000;

const ALPHA_MIN = 0.001;
const ALPHA_DECAY = 1 - Math.pow(ALPHA_MIN, 1 / 300);
const VELOCITY_DECAY = 0.4;
const LINK_DISTANCE = 90;
const LINK_STRENGTH = 0.35;
const CHARGE = -220;
const COLLIDE_PAD = 12;
const COLLIDE_ITERATIONS = 2;

// Tick count for n nodes: min(360, 60000 / n), so a larger graph does fewer steps on a weak client.
export function tickCount(n) {
  if (n <= 0) return 0;
  return Math.min(MAX_TICKS, Math.floor(TICK_BUDGET / n));
}

// Node radius from its degree. Anchors are larger.
export function nodeRadius(degree, anchor, sizeScale) {
  return (anchor ? 8 : 5) + Math.sqrt(degree) * (anchor ? 4.2 : 3.2) * (sizeScale || 1);
}

// A tiny repeatable offset used where d3 would use a random jiggle for coincident points.
function jiggle(i) {
  return (((i * 7919) % 1000) / 1000 - 0.5) * 1e-6 || 1e-7;
}

/**
 * Lay out a graph.
 * input: { entities, relations, groups, width, height, sizeScale, anchors, fixed }
 * `fixed` maps an entity id to {x, y}: those nodes stay put and the others settle around them,
 * which is how one expanded switch is re-laid out without moving the rest.
 * Over FORCE_NODE_LIMIT nodes nothing is simulated and `limited` is true, so the page can fall
 * back to the tiered view with a note.
 */
export function layoutForce(input) {
  const { entities, relations, width, height } = input;
  const groups = input.groups || [];
  const sizeScale = input.sizeScale || 1;
  const anchors = new Set(input.anchors || []);
  const fixed = input.fixed || {};
  const cx = width / 2;
  const cy = height / 2;

  if (entities.length > FORCE_NODE_LIMIT) {
    return { nodes: [], links: [], limited: true, ticks: 0 };
  }

  const degree = new Map();
  for (const r of relations) {
    degree.set(r.source, (degree.get(r.source) || 0) + 1);
    degree.set(r.target, (degree.get(r.target) || 0) + 1);
  }
  const groupIndex = new Map(groups.map((g, i) => [g.id, i]));

  const nodes = entities.map((e, i) => {
    const d = degree.get(e.id) || 0;
    const anchor = anchors.has(e.id);
    // Deterministic starting ring: a reload shows the same picture.
    const a = (i / Math.max(1, entities.length)) * Math.PI * 2;
    const ring = anchor ? 0 : Math.min(width, height) * 0.3;
    const pin = fixed[e.id];
    return {
      entityId: e.id,
      state: e.state || "pending",
      group: groupIndex.has(e.group) ? groupIndex.get(e.group) : groups.length,
      degree: d,
      anchor,
      r: nodeRadius(d, anchor, sizeScale),
      x: pin ? pin.x : cx + Math.cos(a) * ring,
      y: pin ? pin.y : cy + Math.sin(a) * ring,
      vx: 0,
      vy: 0,
      fx: pin ? pin.x : null,
      fy: pin ? pin.y : null,
    };
  });
  const index = new Map(nodes.map((n) => [n.entityId, n]));
  const links = relations
    .filter((r) => index.has(r.source) && index.has(r.target))
    .map((r) => ({ s: index.get(r.source), t: index.get(r.target), kind: r.kind, stale: !!r.stale }));

  const count = new Map(nodes.map((n) => [n, 0]));
  for (const l of links) {
    count.set(l.s, count.get(l.s) + 1);
    count.set(l.t, count.get(l.t) + 1);
  }
  const bias = links.map((l) => count.get(l.s) / (count.get(l.s) + count.get(l.t)));
  const radialR = Math.min(width, height) * 0.34;
  const ticks = tickCount(nodes.length);
  let alpha = 1;

  for (let step = 0; step < ticks; step++) {
    alpha += (0 - alpha) * ALPHA_DECAY;
    forceLink(links, bias, alpha);
    forceCharge(nodes, alpha);
    forceCenter(nodes, cx, cy);
    forceCollide(nodes);
    if (anchors.size) forceRadial(nodes, cx, cy, radialR, alpha);
    for (const n of nodes) {
      if (n.fx !== null) {
        n.x = n.fx;
        n.vx = 0;
      } else {
        n.vx *= VELOCITY_DECAY;
        n.x += n.vx;
      }
      if (n.fy !== null) {
        n.y = n.fy;
        n.vy = 0;
      } else {
        n.vy *= VELOCITY_DECAY;
        n.y += n.vy;
      }
    }
  }

  return {
    nodes: nodes.map((n) => ({
      entityId: n.entityId, x: n.x, y: n.y, r: n.r, group: n.group, state: n.state, anchor: n.anchor,
    })),
    links: links.map((l) => ({
      x1: l.s.x, y1: l.s.y, x2: l.t.x, y2: l.t.y,
      source: l.s.entityId, target: l.t.entityId, kind: l.kind, stale: l.stale,
    })),
    limited: false,
    ticks,
  };
}

function forceLink(links, bias, alpha) {
  for (let i = 0; i < links.length; i++) {
    const { s, t } = links[i];
    let x = t.x + t.vx - s.x - s.vx || jiggle(i);
    let y = t.y + t.vy - s.y - s.vy || jiggle(i + 1);
    let l = Math.sqrt(x * x + y * y);
    l = ((l - LINK_DISTANCE) / l) * alpha * LINK_STRENGTH;
    x *= l;
    y *= l;
    const b = bias[i];
    t.vx -= x * b;
    t.vy -= y * b;
    s.vx += x * (1 - b);
    s.vy += y * (1 - b);
  }
}

// Every pair repels. This is O(n^2), which is fine because the caller caps n at FORCE_NODE_LIMIT.
function forceCharge(nodes, alpha) {
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    for (let j = 0; j < nodes.length; j++) {
      if (i === j) continue;
      const b = nodes[j];
      const dx = b.x - a.x || jiggle(i + j);
      const dy = b.y - a.y || jiggle(i * j + 1);
      let l2 = dx * dx + dy * dy;
      if (l2 < 1) l2 = Math.sqrt(l2);
      const w = (CHARGE * alpha) / l2;
      a.vx += dx * w;
      a.vy += dy * w;
    }
  }
}

function forceCenter(nodes, cx, cy) {
  if (!nodes.length) return;
  let sx = 0;
  let sy = 0;
  for (const n of nodes) {
    sx += n.x;
    sy += n.y;
  }
  sx = sx / nodes.length - cx;
  sy = sy / nodes.length - cy;
  for (const n of nodes) {
    if (n.fx === null) n.x -= sx;
    if (n.fy === null) n.y -= sy;
  }
}

function forceCollide(nodes) {
  for (let it = 0; it < COLLIDE_ITERATIONS; it++) {
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      const ra = a.r + COLLIDE_PAD;
      const xi = a.x + a.vx;
      const yi = a.y + a.vy;
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        const rb = b.r + COLLIDE_PAD;
        const rr = ra + rb;
        let x = xi - (b.x + b.vx) || jiggle(i + j);
        let y = yi - (b.y + b.vy) || jiggle(i + j + 1);
        let l = x * x + y * y;
        if (l >= rr * rr) continue;
        l = Math.sqrt(l);
        l = (rr - l) / l;
        x *= l;
        y *= l;
        const share = (rb * rb) / (ra * ra + rb * rb);
        a.vx += x * share;
        a.vy += y * share;
        b.vx -= x * (1 - share);
        b.vy -= y * (1 - share);
      }
    }
  }
}

function forceRadial(nodes, cx, cy, radius, alpha) {
  for (const n of nodes) {
    const dx = n.x - cx || 1e-6;
    const dy = n.y - cy || 1e-6;
    const dist = Math.sqrt(dx * dx + dy * dy);
    const target = n.anchor ? 0 : radius;
    const strength = n.anchor ? 0.35 : 0.06;
    const k = ((target - dist) * strength * alpha) / dist;
    n.vx += dx * k;
    n.vy += dy * k;
  }
}
