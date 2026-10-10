"""A reading's value as text, in the same units the host page's Value column shows them
(observe/static/js/format.js formatValue), for the reasons built on the server: "92.5 %", not
"0.9248"; "52 °C", not "52"; "40h", not "144000".

Units follow the UCUM names of the data API: "1" is a ratio, "By" bytes, "s" seconds, "Cel" a
temperature, and an annotation such as "{entity}" only names what is counted.
"""

from __future__ import annotations

import math

_BIN = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
_WORDS = {"Cel": "°C", "{rpm}": " RPM", "W": " W", "V": " V", "Hz": " Hz"}


def _plain(v: float) -> str:
    if abs(v) >= 100:
        return str(round(v))
    return f"{round(v, 2):g}"


def _duration(s: float) -> str:
    if abs(s) < 60:
        return f"{_plain(s)} s"
    sign = "-" if s < 0 else ""
    t = round(abs(s))
    d, t = divmod(t, 86400)
    h, t = divmod(t, 3600)
    m, sec = divmod(t, 60)
    parts = [f"{n}{u}" for n, u in ((d, "d"), (h, "h"), (m, "m")) if n]
    if sec and not d:
        parts.append(f"{sec}s")
    return sign + " ".join(parts[:3])


def value_text(value: float, unit: str = "") -> str:
    """`value` with its unit, the way the host page writes it."""
    u = (unit or "").strip()
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return f"{value}{' ' + u if u else ''}"
    if u == "1":
        pct = value * 100
        text = f"{pct:.0f}" if abs(pct) >= 100 else f"{pct:.1f}".removesuffix(".0")
        return f"{text} %"
    if u == "By":
        x, i = float(value), 0
        while i < len(_BIN) - 1 and abs(x) >= 1024:
            x, i = x / 1024, i + 1
        if i == 0:
            return f"{round(x)} B"
        digits = 0 if abs(x) >= 100 else 1 if abs(x) >= 10 else 2
        return f"{x:.{digits}f} {_BIN[i]}"
    if u == "s":
        return _duration(value)
    if u in _WORDS:
        return f"{_plain(value)}{_WORDS[u]}"
    if u.startswith("{") and u.endswith("}"):
        return _plain(value)  # the reading's name already says what is counted
    return f"{_plain(value)}{' ' + u if u else ''}"
