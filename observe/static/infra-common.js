// Shared helpers for the map, port and map admin pages. Every string came from a field
// report, a switch or a monitor name, so it is written with textContent only, never as markup.
import { el } from "/static/js/dom.js";

export { el };
export { api, whoami } from "/static/js/api.js";

export const STATE_WORDS = {
  up: "Up", warn: "Warning", down: "Down", unreachable: "Unreachable", pending: "Pending",
  unknown: "State unknown",
};

export function stateText(node) {
  const word = STATE_WORDS[node.state] || "State unknown";
  return node.state === "unreachable" && node.blocked_by ? `${word}, behind ${node.blocked_by}` : word;
}

export function statePill(node) {
  return el("span", `pill ${node.state || "pending"}`, stateText(node));
}

export function when(ts) {
  return ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "medium" }) : "never";
}

export function portHref(switchId, portKey) {
  return `/port?switch_id=${encodeURIComponent(switchId)}&port=${encodeURIComponent(portKey)}`;
}
