// Theme choice for the console: Auto, Light or Dark, kept per browser in localStorage.
// Auto removes the data-theme attribute so the system preference decides.
const KEY = "observe.theme";
export const THEMES = ["auto", "light", "dark"];

function read() {
  try {
    const v = window.localStorage.getItem(KEY);
    return THEMES.includes(v) ? v : "auto";
  } catch (e) {
    return "auto";
  }
}

function write(value) {
  try {
    if (value === "auto") window.localStorage.removeItem(KEY);
    else window.localStorage.setItem(KEY, value);
  } catch (e) {
    // Storage can be blocked. The theme still applies for this page view.
  }
}

export function currentTheme() {
  return read();
}

export function applyTheme(value) {
  const root = document.documentElement;
  if (value === "light" || value === "dark") root.dataset.theme = value;
  else delete root.dataset.theme;
  return value;
}

export function nextTheme(value) {
  return THEMES[(THEMES.indexOf(value) + 1) % THEMES.length];
}

// Advance Auto, Light, Dark, then back to Auto. Stores the choice and applies it.
export function cycleTheme() {
  const next = nextTheme(read());
  write(next);
  return applyTheme(next);
}

// Apply the stored choice. Importing this module does it once, before anything renders.
export function applyStoredTheme() {
  return applyTheme(read());
}

applyStoredTheme();
