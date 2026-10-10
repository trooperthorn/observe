// One shared formatter for readings with a unit. Used by the dashboard and the host page.
// Units follow UCUM style names from the data API: "1" is a ratio, "By" is bytes, "Hz",
// "bit/s", "By/s" and "s". Normal readings never use exponent notation. A UCUM annotation such
// as "{entity}" or "{device offline}" is a count of a thing and is shown in words: "3 entities",
// "1 device offline".

const BIN = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"];
const SI = ["", "k", "M", "G", "T", "P", "E"];

function decimals(v) {
  const a = Math.abs(v);
  return a >= 100 ? 0 : a >= 10 ? 1 : 2;
}

function round(v, digits) {
  return v.toFixed(digits != null ? digits : decimals(v));
}

// Divide until the number, as it will be printed, is below the step. Rounding first means
// 1023.9 B rolls over to "1.00 KiB" instead of showing "1024 B".
function scaled(v, base, steps) {
  let i = 0;
  let x = v;
  while (i < steps - 1 && Math.abs(Number(round(x, i ? undefined : 0))) >= base) { x /= base; i += 1; }
  return [x, i];
}

function plain(v) {
  const a = Math.abs(v);
  if (a >= 1e21) return BigInt(Math.round(v)).toString();
  if (a >= 100) return String(Math.round(v));
  return String(Math.round(v * 100) / 100);
}

function duration(s) {
  const a = Math.abs(s);
  if (a < 1 && Math.round(a * 1000) < 1000) return `${plain(s * 1000)} ms`;
  if (a < 60 && Math.round(a * 100) / 100 < 60) return `${plain(s)} s`;
  const sign = s < 0 ? "-" : "";
  let t = Math.round(a);
  const d = Math.floor(t / 86400); t -= d * 86400;
  const h = Math.floor(t / 3600); t -= h * 3600;
  const m = Math.floor(t / 60); t -= m * 60;
  const parts = [];
  if (d) parts.push(`${d}d`);
  if (h) parts.push(`${h}h`);
  if (m) parts.push(`${m}m`);
  if (t && !d) parts.push(`${t}s`);
  return sign + parts.slice(0, 3).join(" ");
}

// The plural of an English noun, for count units. Enough for the nouns checks use.
export function plural(word) {
  if (/[^aeiou]y$/i.test(word)) return `${word.slice(0, -1)}ies`;
  if (/(s|x|z|ch|sh)$/i.test(word)) return `${word}es`;
  return `${word}s`;
}

// "586 entities unavailable" for 586 and "{entity unavailable}": the first word is the noun.
function counted(value, annotation) {
  const words = annotation.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return plain(value);
  if (value !== 1) words[0] = plural(words[0]);
  return `${plain(value)} ${words.join(" ")}`;
}

export function formatValue(value, unit) {
  if (value == null) return "";
  const u = (unit || "").trim();
  // NaN, Infinity or a string from the API is shown as received, never as a blank cell.
  if (typeof value !== "number" || !Number.isFinite(value)) return `${value}${u ? " " + u : ""}`;
  if (u === "1") return `${round(value * 100, Math.abs(value * 100) >= 100 ? 0 : 1).replace(/\.0$/, "")} %`;
  if (u === "By") {
    const [x, i] = scaled(value, 1024, BIN.length);
    return `${i ? round(x) : Math.round(x)} ${BIN[i]}`;
  }
  if (u === "Hz") {
    const [x, i] = scaled(value, 1000, SI.length);
    return `${i ? round(x) : plain(x)} ${SI[i]}Hz`;
  }
  if (u === "bit/s" || u === "By/s") {
    const [x, i] = scaled(value, 1000, SI.length);
    return `${i ? round(x) : plain(x)} ${SI[i]}${u}`;
  }
  if (u === "s") return duration(value);
  const note = /^\{([^{}]*)\}$/.exec(u);
  if (note) return counted(value, note[1]);
  return `${plain(value)}${u ? " " + u : ""}`;
}

// Monitor results store units such as " ms" and " days" with a leading space, and "%" or none.
// Those keep the one-decimal rounding the dashboard always used. Everything else is scaled.
export function formatReading(value, unit) {
  const u = unit || "";
  if (typeof value === "number" && Number.isFinite(value)
      && (!u || u === "%" || u.startsWith(" ") || u === "ms")) {
    const v = Math.abs(value) >= 100 ? Math.round(value) : Math.round(value * 10) / 10;
    return `${v}${u}`;
  }
  return formatValue(value, unit);
}

// The value a monitor card shows. A pushed host's value is the age of its newest batch, so it is
// said as such; a monitor with no value shows its latency, and a failed probe shows nothing.
export function monitorReading(m) {
  if (m.result === "fail" && m.value == null) return "";  // a failed probe has no latency
  if (m.value === null || m.value === undefined) {
    return m.latency_ms != null ? `${Math.round(m.latency_ms)} ms` : "";
  }
  const text = formatReading(m.value, m.unit);
  if (m.type === "pushed_host" && (m.unit || "").trim() === "s") return `data ${text} old`;
  return text;
}

// The text of one event row of the dashboard, with the monitor's display name (from `names`,
// slug -> name) instead of its slug when the monitor is known.
export function eventText(e, names) {
  const a = e.attributes || {};
  const who = (names && names.get(e.resource.name)) || e.resource.name;
  if (e.event_name === "observe.monitor.transition") {
    const from = a["observe.monitor.state.previous"], to = a["observe.monitor.state"];
    return `${who}: ${from} → ${to} (${e.body})`;
  }
  return `${who}: ${e.event_name} (${e.body})`;
}
