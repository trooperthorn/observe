// DOM helpers for the console. Text always goes in through textContent, never as markup.
const SVG_NS = "http://www.w3.org/2000/svg";

export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function svg(tag, attrs, text) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, String(v));
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
