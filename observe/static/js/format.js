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
  if (Object.prototype.hasOwnProperty.call(UNIT_WORDS, u)) {
    const w = UNIT_WORDS[u];
    return w ? `${plain(value)}${w.startsWith("°") ? "" : " "}${w}` : plain(value);
  }
  const note = /^\{([^{}]*)\}$/.exec(u);
  if (note) return counted(value, note[1]);
  return `${plain(value)}${u ? " " + u : ""}`;
}

// UCUM codes the agents send that read badly as they are: Cel is a temperature, {rpm} a fan
// speed, and {thread}, {count} and {reason} only say the number is a count (a load average or
// a number of things), which the reading's name already tells.
const UNIT_WORDS = {
  Cel: "°C", "{rpm}": "RPM", "{thread}": "", "{count}": "", "{reason}": "", W: "W", V: "V",
};

// Gauges that say which state a thing is in, one point per state: 1 means "in this state". They
// carry the unit "1", so formatValue would print "100 %". Each names the attribute that holds the
// state word.
const STATE_GAUGES = {
  "hw.status": "hw.state", "observe.thermal.mode": "observe.thermal.mode",
  "observe.ups.status": "observe.ups.flag", "observe.mdraid.sync_action": "observe.mdraid.action",
};

// Health levels, not one-hot gauges: 0 healthy, 1 warning, 2 or more critical (`_level_value`
// in observe/hostview.py). The Windows storage collector sends `hw.status` this way, with the
// state word the OS gives in `hw.state`; keyed on the scope, since other collectors send
// `hw.status` as a state gauge.
const LEVEL_GAUGES = {
  "hostwatch.collector.win_storage|hw.status": "hw.state",
  "hostwatch.collector.truenas|observe.zfs.pool.health": "hw.state",
};
const LEVEL_WORDS = ["healthy", "warning", "critical"];

// Readings that are yes or no, 1 or 0. They carry the unit "1", so formatValue would print
// "100 %". The value names the attribute with a word to show instead of "Yes", if any.
const FLAGS = {
  "observe.ha.update.pending": "", "observe.ha.safe_mode": "", "observe.ha.recovery_mode": "",
  "observe.ha.running": "observe.ha.state", "observe.ha.soc.suspicious_activity": "",
  "observe.ha.supervisor.healthy": "", "observe.ha.supervisor.supported": "",
  "observe.ha.container.running": "", "observe.ha.backup.last_ok": "",
  "observe.ha.backup.unprotected": "", "observe.scrutiny.up": "",
  "observe.network.interface.up": "observe.network.interface.status",
};

// Readings whose value is only a marker and whose meaning is in a label (the version string).
const LABEL_VALUES = { "observe.ha.version": "observe.ha.version" };

// The value cell of one host reading: a state gauge as its state word, a level as its word, a
// flag as Yes or No, else the value with its unit in words.
export function readingText(item) {
  if (!item || item.value == null) return "no value";
  const labels = item.labels || {};
  const levelKey = LEVEL_GAUGES[`${item.source}|${item.metric}`];
  if (levelKey) {
    const word = LEVEL_WORDS[Math.max(0, Math.min(2, Math.floor(item.value)))];
    const said = labels[levelKey];
    // The OS word is shown unless it contradicts the level ("Healthy" on a level of 1).
    if (said && (item.value <= 0 || said.toLowerCase() !== "healthy")) return said;
    return word;
  }
  if (Object.prototype.hasOwnProperty.call(LABEL_VALUES, item.metric)) {
    const text = labels[LABEL_VALUES[item.metric]];
    if (text) return text;
  }
  if (Object.prototype.hasOwnProperty.call(FLAGS, item.metric)) {
    const word = FLAGS[item.metric] && labels[FLAGS[item.metric]];
    return item.value ? (word || "Yes") : (word ? `No (${word})` : "No");
  }
  const key = STATE_GAUGES[item.metric];
  if (key) {
    const state = item.labels && item.labels[key];
    if (state) return item.value ? state : `not ${state}`;
    return item.value ? "yes" : "no";
  }
  return formatValue(item.value, item.unit);
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

// An age in seconds as a card headline: one decimal below 10 s ("9.5 s"), whole seconds up to a
// minute ("16 s", never "15.95 s"), then minutes and seconds ("1m 7s").
export function ageText(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return formatValue(seconds, "s");
  const a = Math.abs(seconds);
  if (Math.round(a * 10) / 10 < 10) return `${Math.round(seconds * 10) / 10} s`;
  if (Math.round(a) < 60) return `${Math.round(seconds)} s`;
  return duration(Math.round(seconds));
}

// The value a monitor card shows. A pushed host's value is the age of its newest batch, so it is
// said as such, rounded (ageText); a monitor with no value shows its latency, and a failed probe
// shows nothing.
export function monitorReading(m) {
  if (m.result === "fail" && m.value == null) return "";  // a failed probe has no latency
  if (m.value === null || m.value === undefined) {
    return m.latency_ms != null ? `${Math.round(m.latency_ms)} ms` : "";
  }
  if (m.type === "pushed_host" && (m.unit || "").trim() === "s") return `data ${ageText(m.value)} old`;
  return formatReading(m.value, m.unit);
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

// The one date and time format of the console: "Oct 10, 2026, 2:33:05 PM" in the reader's locale,
// with the year, so no page writes 10/10/26 while another writes 10/10/2026 or the time alone.
// `ts` is unix seconds, an RFC 3339 string or a Date; nothing is "never".
const WHEN = { dateStyle: "medium", timeStyle: "medium" };

export function formatWhen(ts, locale = []) {
  if (ts === null || ts === undefined || ts === "" || ts === 0) return "never";
  const ms = ts instanceof Date ? ts.getTime()
    : typeof ts === "number" ? ts * 1000 : Date.parse(ts);
  if (!Number.isFinite(ms)) return "never";
  return new Date(ms).toLocaleString(locale, WHEN);
}

// The "refreshed ..." footer line, in the same format.
export function refreshedText(now = new Date()) {
  return `refreshed ${formatWhen(now)}`;
}
