// The Control card's choices, as pure functions over GET /api/v2/control/capabilities, so
// tests/js/control.test.mjs can run them without a browser. No DOM here; host-control.js turns
// them into nodes with textContent only. The server (plugins/control/observe_control/actions.py
// control_form and validate) works from the same answer and refuses anything else.

// The card is drawn only for a host with a control daemon that has pulled.
export function controlShown(caps) {
  return Boolean(caps && caps.known && caps.available && Array.isArray(caps.actions)
    && caps.actions.length);
}

// The actions in the drop-down; the reboot has its own button.
export function formActions(caps) {
  return (caps.actions || []).filter((a) => a !== "host.reboot");
}

// The host's own controller only.
export function controllerChoices(caps) {
  if (caps.controller) return [caps.controller];
  return caps.controllers || [];
}

// The Header choices: the allowlisted headers (or, for a host whose allowlist Observe does not
// hold, the ones thermalctl reported). The value is the controller's id, which the signed
// command carries; the label also gives the allowlist's own name when it differs (fan1 is pwm1).
// null when nothing is known, so the form asks for a name.
export function headerChoices(caps) {
  if (!Array.isArray(caps.fan_headers)) return null;
  return caps.fan_headers.map((f) => ({
    value: f.controller_id,
    label: f.header === f.controller_id ? f.header : `${f.header} (${f.controller_id})`,
    floor: Number.isInteger(f.floor) ? f.floor : 0,
  }));
}

// The Service choices from the saved allowlist; null when Observe does not hold the allowlist.
export function serviceChoices(caps) {
  return caps.allowlist ? [...caps.allowlist.services] : null;
}

export function componentChoices(caps) {
  return caps.components && caps.components.length ? caps.components : ["agent"];
}

// The floor of one header value, 0 when the header is not listed.
export function floorOf(choices, value) {
  const hit = (choices || []).find((c) => c.value === value);
  return hit ? hit.floor : 0;
}

// What the Minimum duty input starts at: the old default of 20, but never under the floor.
export function defaultDuty(floor) {
  return Math.min(100, Math.max(20, floor || 0));
}

// The duty to send: a whole number from the floor to 100. Text that is not a number becomes
// the floor, so the form cannot produce a duty the host would refuse.
export function clampDuty(text, floor) {
  const low = Math.max(0, Math.min(100, floor || 0));
  const n = Math.round(Number(String(text ?? "").trim()));
  if (String(text ?? "").trim() === "" || !Number.isFinite(n)) return low;
  return Math.min(100, Math.max(low, n));
}

export function floorText(floor) {
  return floor > 0 ? `This host refuses a floor under ${floor}%.` : "";
}
