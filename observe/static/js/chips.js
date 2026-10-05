// Status chips: an icon, then a word. Built with createElementNS and textContent only.
import { el, svg } from "/static/js/dom.js";
import { ICONS, stateInfo } from "/static/js/chip-states.js";

export function statusIcon(name) {
  const s = svg("svg", { viewBox: "0 0 16 16", width: 14, height: 14, "aria-hidden": "true",
                         focusable: "false", class: "chip-icon" });
  for (const [tag, attrs] of ICONS[name] || ICONS.hollow) s.append(svg(tag, attrs));
  return s;
}

// statusChip("down") gives "Down"; statusChip("down", "Critical") overrides the word.
export function statusChip(state, text) {
  const info = stateInfo(state);
  const chip = el("span", `chip s-${info.role}`);
  chip.append(statusIcon(info.icon), el("span", null, text || info.word));
  return chip;
}

// A neutral chip for tags, and a mono tag for key IDs, ports and MACs.
export function neutralChip(text) {
  return el("span", "chip neutral", text);
}

export function monoTag(text) {
  return el("span", "tag", text);
}
