// Ported from trooperthorn/relationship-maps, packages/graph-core (commit 0c4d268), via ha_Int_soc (MIT). No d3.
// Canvas view: camera, pointer, wheel and keyboard input, resize handling and repaint scheduling.
// The layout is computed once by force.js. This view only repaints on a camera change, hover or
// selection, coalesced through requestAnimationFrame, so the CPU is idle when nothing changes.

import { fitCamera, pickNode, readTheme, render, screenToWorld } from "./render.js";

export const MAX_DPR = 2;
export const DRAG_FRAME_MS = 33;
export const MIN_ZOOM = 0.2;
export const MAX_ZOOM = 4;

// Text for the canvas aria-label, for example "42 devices, 3 down".
export function graphSummary(entities) {
  const down = entities.filter((e) => e.state === "down").length;
  const noun = entities.length === 1 ? "device" : "devices";
  return entities.length + " " + noun + ", " + down + " down";
}

// Nodes in reading order: top to bottom, then left to right.
export function readingOrder(nodes) {
  return [...nodes].sort((a, b) => a.y - b.y || a.x - b.x || (a.entityId < b.entityId ? -1 : 1));
}

// The id reached by moving `step` places from `current` in reading order, wrapping around.
export function stepSelection(nodes, current, step) {
  const order = readingOrder(nodes);
  if (!order.length) return null;
  const at = order.findIndex((n) => n.entityId === current);
  if (at < 0) return order[step > 0 ? 0 : order.length - 1].entityId;
  return order[(at + step + order.length) % order.length].entityId;
}

// Zoom about a screen point, keeping the world point under it fixed.
export function zoomAt(camera, sx, sy, factor) {
  const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, camera.k * factor));
  const [wx, wy] = screenToWorld(camera, sx, sy);
  return { k, x: sx - wx * k, y: sy - wy * k };
}

// A string that changes when the graph's shape changes: its devices, its links or its anchors.
// Two refreshes with the same key have the same layout, so only the states and stale flags differ.
export function structureKey(entities, relations, anchors) {
  const ids = entities.map((e) => e.id).sort();
  const links = relations.map((r) => r.source + ">" + r.target + ":" + r.kind).sort();
  return JSON.stringify([ids, links, [...(anchors || [])].sort()]);
}

// The previous layout with new device states and stale flags, positions untouched. The caller has
// already checked that the structure key is unchanged, so every node and link still exists.
export function mergeLayout(prev, entities, relations) {
  const state = new Map(entities.map((e) => [e.id, e.state || "pending"]));
  const stale = new Map(relations.map((r) => [r.source + ">" + r.target, !!r.stale]));
  return {
    ...prev,
    nodes: prev.nodes.map((n) => ({ ...n, state: state.has(n.entityId) ? state.get(n.entityId) : n.state })),
    links: prev.links.map((l) => {
      const key = l.source + ">" + l.target;
      return { ...l, stale: stale.has(key) ? stale.get(key) : l.stale };
    }),
  };
}

// The camera after a data refresh. The person's pan and zoom survive when they have moved the
// view or when the graph kept its shape; a changed graph that nobody has moved is fitted again.
export function cameraAfterRefresh(current, fitted, sameStructure, touched) {
  return sameStructure || touched ? current : fitted;
}

// Entities connected to the selection, plus the selection itself. Null when nothing is selected.
export function focusSet(layout, selected) {
  if (!selected) return null;
  const set = new Set([selected]);
  for (const l of layout.links) {
    if (l.source === selected) set.add(l.target);
    if (l.target === selected) set.add(l.source);
  }
  return set;
}

/**
 * Attach a graph to a canvas. The canvas gets role="img", an aria-label and a tab stop.
 * options: { onSelect(id|null), onOpen(id), onHover(id|null), showLabels }
 * Returns { setData, select, fit, zoomBy, refreshTheme, destroy, state }.
 */
export function createGraphView(canvas, options) {
  const opts = options || {};
  const ctx = canvas.getContext("2d");
  const st = {
    layout: { nodes: [], links: [], limited: false, ticks: 0 },
    entities: new Map(),
    camera: { x: 0, y: 0, k: 1 },
    theme: readTheme(canvas),
    hovered: null,
    selected: null,
    w: 300,
    h: 200,
    dpr: 1,
    showLabels: opts.showLabels !== false,
    touched: false, // true once the person has panned or zoomed; fit() clears it
  };
  let frame = 0;
  let lastPaint = 0;
  let dragging = null;
  let destroyed = false;

  canvas.setAttribute("role", "img");
  canvas.tabIndex = 0;

  function paint() {
    frame = 0;
    if (destroyed) return;
    lastPaint = performance.now();
    render(ctx, st.w, st.h, {
      layout: st.layout, entities: st.entities, camera: st.camera, theme: st.theme,
      showLabels: st.showLabels, hovered: st.hovered, selected: st.selected,
      focus: focusSet(st.layout, st.selected), dpr: st.dpr,
    });
  }

  // Coalesce repaints into one animation frame. While dragging, hold them to about 30 per second.
  function schedule() {
    if (frame || destroyed) return;
    frame = window.requestAnimationFrame((now) => {
      if (dragging && now - lastPaint < DRAG_FRAME_MS) {
        frame = 0;
        schedule();
        return;
      }
      paint();
    });
  }

  function resize(width, height) {
    st.w = Math.max(1, Math.floor(width));
    st.h = Math.max(1, Math.floor(height));
    st.dpr = Math.min(MAX_DPR, window.devicePixelRatio || 1);
    canvas.width = Math.floor(st.w * st.dpr);
    canvas.height = Math.floor(st.h * st.dpr);
    st.camera = fitCamera(st.layout, st.w, st.h);
    schedule();
  }

  function describe() {
    canvas.setAttribute("aria-label", graphSummary([...st.entities.values()]));
  }

  function setSelected(id) {
    if (id === st.selected) return;
    st.selected = id;
    if (opts.onSelect) opts.onSelect(id);
    schedule();
  }

  function pointer(ev) {
    const r = canvas.getBoundingClientRect();
    return [ev.clientX - r.left, ev.clientY - r.top];
  }

  function onDown(ev) {
    const [sx, sy] = pointer(ev);
    dragging = { sx, sy, cx: st.camera.x, cy: st.camera.y, moved: false };
    if (canvas.setPointerCapture) canvas.setPointerCapture(ev.pointerId);
  }

  function onMove(ev) {
    const [sx, sy] = pointer(ev);
    if (dragging) {
      if (Math.abs(sx - dragging.sx) + Math.abs(sy - dragging.sy) > 3) dragging.moved = true;
      if (dragging.moved) {
        st.camera = { k: st.camera.k, x: dragging.cx + sx - dragging.sx, y: dragging.cy + sy - dragging.sy };
        st.touched = true;
        schedule();
      }
      return;
    }
    const [wx, wy] = screenToWorld(st.camera, sx, sy);
    const id = pickNode(st.layout, wx, wy);
    if (id !== st.hovered) {
      st.hovered = id;
      if (opts.onHover) opts.onHover(id);
      schedule();
    }
  }

  function onUp(ev) {
    const was = dragging;
    dragging = null;
    if (!was || was.moved) return;
    const [sx, sy] = pointer(ev);
    const [wx, wy] = screenToWorld(st.camera, sx, sy);
    setSelected(pickNode(st.layout, wx, wy));
  }

  function onLeave() {
    if (st.hovered !== null) {
      st.hovered = null;
      if (opts.onHover) opts.onHover(null);
      schedule();
    }
  }

  function onWheel(ev) {
    ev.preventDefault();
    const [sx, sy] = pointer(ev);
    st.camera = zoomAt(st.camera, sx, sy, ev.deltaY < 0 ? 1.15 : 1 / 1.15);
    st.touched = true;
    schedule();
  }

  function onKey(ev) {
    const key = ev.key;
    if (key === "ArrowRight" || key === "ArrowDown") setSelected(stepSelection(st.layout.nodes, st.selected, 1));
    else if (key === "ArrowLeft" || key === "ArrowUp") setSelected(stepSelection(st.layout.nodes, st.selected, -1));
    else if (key === "Enter" && st.selected && opts.onOpen) opts.onOpen(st.selected);
    else if (key === "+" || key === "=") controller.zoomBy(1.25);
    else if (key === "-") controller.zoomBy(1 / 1.25);
    else if (key === "0") controller.fit();
    else if (key === "Escape") setSelected(null);
    else return;
    ev.preventDefault();
  }

  const observer = typeof ResizeObserver === "function"
    ? new ResizeObserver((entries) => {
      const box = entries[0].contentRect;
      resize(box.width, box.height);
    })
    : null;
  const scheme = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const retheme = () => controller.refreshTheme();
  const themeWatch = typeof MutationObserver === "function" ? new MutationObserver(retheme) : null;

  canvas.addEventListener("pointerdown", onDown);
  canvas.addEventListener("pointermove", onMove);
  canvas.addEventListener("pointerup", onUp);
  canvas.addEventListener("pointerleave", onLeave);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("keydown", onKey);
  if (observer) observer.observe(canvas.parentElement || canvas);
  if (scheme && scheme.addEventListener) scheme.addEventListener("change", retheme);
  if (themeWatch) themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  const controller = {
    state: st,
    // layout comes from layoutForce; entities is an array of GraphEntity. With
    // options.sameStructure the layout is the previous one with new states, so the view is kept.
    setData(layout, entities, options) {
      st.layout = layout;
      st.entities = new Map(entities.map((e) => [e.id, e]));
      if (st.selected && !st.entities.has(st.selected)) st.selected = null;
      st.camera = cameraAfterRefresh(st.camera, fitCamera(layout, st.w, st.h),
        !!(options && options.sameStructure), st.touched);
      describe();
      schedule();
    },
    select(id) {
      setSelected(id);
    },
    fit() {
      st.touched = false;
      st.camera = fitCamera(st.layout, st.w, st.h);
      schedule();
    },
    zoomBy(factor) {
      st.touched = true;
      st.camera = zoomAt(st.camera, st.w / 2, st.h / 2, factor);
      schedule();
    },
    // Read the tokens again and repaint, after a theme toggle or a system scheme change.
    refreshTheme() {
      st.theme = readTheme(canvas);
      schedule();
    },
    destroy() {
      destroyed = true;
      if (frame) window.cancelAnimationFrame(frame);
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerup", onUp);
      canvas.removeEventListener("pointerleave", onLeave);
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("keydown", onKey);
      if (observer) observer.disconnect();
      if (themeWatch) themeWatch.disconnect();
      if (scheme && scheme.removeEventListener) scheme.removeEventListener("change", retheme);
    },
  };
  describe();
  return controller;
}
