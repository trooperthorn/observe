// One shared formatter for readings with a unit. Used by the dashboard and the host page.
// Units follow UCUM style names from the data API: "1" is a ratio, "By" is bytes, "Hz",
// "bit/s", "By/s" and "s". Normal readings never use exponent notation.

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
