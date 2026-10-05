"""Capacity forecasting: least-squares trend to a threshold crossing.

Method (see docs/PRIOR-ART.md): take the monitor's numeric history over the
lookback window, average it into hourly buckets to damp poll-level noise,
fit an ordinary least-squares line value = a + b*t, and solve for the time t
at which the line reaches the warn and crit thresholds. This is the same
calculation as Prometheus's predict_linear() and as a spreadsheet TREND().

What it is honest about:
  * r-squared is reported with every projection. A low r-squared means the
    series is not behaving like a straight line (seasonal, step changes,
    noise) and the date is labelled low confidence rather than hidden.
  * Too little history, too short a span, a flat or receding trend, or a
    crossing beyond the horizon all return "no projection" with the reason,
    never a fabricated date.
  * A linear fit ignores daily and weekly cycles. Holt-Winters seasonal
    smoothing handles those and is listed in the README as not implemented.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from .config import ForecastSettings, Thresholds


@dataclass
class Fit:
    slope_per_day: float
    intercept: float  # value at t = 0 (epoch seconds)
    r2: float
    n: int
    span_hours: float
    last_value: float

    def value_at(self, ts: float) -> float:
        return self.intercept + self.slope_per_day / 86400 * ts


@dataclass
class Forecast:
    status: str  # "projected", "not_trending", "beyond_horizon", "already_crossed", "insufficient_data"
    reason: str
    warn_at: float | None = None  # epoch seconds of projected crossing
    crit_at: float | None = None
    confidence: str | None = None  # "normal" or "low"
    fit: dict[str, Any] | None = None
    computed_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def least_squares(points: list[tuple[float, float]]) -> Fit | None:
    """Ordinary least squares on (t, v). Returns None if t has no spread."""
    n = len(points)
    if n < 2:
        return None
    # Centre t for numerical stability (epoch seconds are ~1.8e9).
    t0 = points[0][0]
    ts = [t - t0 for t, _ in points]
    vs = [v for _, v in points]
    mt, mv = sum(ts) / n, sum(vs) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    if sxx == 0:
        return None
    sxy = sum((t - mt) * (v - mv) for t, v in zip(ts, vs))
    b = sxy / sxx
    a_centred = mv - b * mt
    ss_tot = sum((v - mv) ** 2 for v in vs)
    ss_res = sum((v - (a_centred + b * t)) ** 2 for t, v in zip(ts, vs))
    r2 = 1.0 if ss_tot == 0 else max(0.0, 1 - ss_res / ss_tot)
    return Fit(
        slope_per_day=b * 86400,
        intercept=a_centred - b * t0,
        r2=round(r2, 4),
        n=n,
        span_hours=(ts[-1] - ts[0]) / 3600,
        last_value=vs[-1],
    )


def _crossing(fit: Fit, level: float, direction: str, now: float) -> tuple[str, float | None]:
    """When the trend line reaches `level`. Returns (kind, ts)."""
    rising = direction == "above"
    current = fit.value_at(now)
    if (rising and current >= level) or (not rising and current <= level):
        return "already", now
    b = fit.slope_per_day / 86400
    if b == 0 or (rising and b < 0) or (not rising and b > 0):
        return "never", None
    return "future", (level - fit.intercept) / b


def project(points: list[tuple[float, float]], th: Thresholds, cfg: ForecastSettings,
            now: float | None = None) -> Forecast:
    now = time.time() if now is None else now
    if th.warn is None and th.crit is None:
        return Forecast("insufficient_data", "no thresholds to project to", computed_at=now)
    if len(points) < cfg.min_points:
        return Forecast("insufficient_data",
                        f"{len(points)} hourly points, need {cfg.min_points}", computed_at=now)
    fit = least_squares(points)
    if fit is None or fit.span_hours < cfg.min_span_hours:
        span = 0 if fit is None else fit.span_hours
        return Forecast("insufficient_data",
                        f"history spans {span:.1f} h, need {cfg.min_span_hours:g} h",
                        computed_at=now)

    confidence = "normal" if fit.r2 >= cfg.min_r2 else "low"
    horizon = now + cfg.horizon_days * 86400
    out = Forecast("not_trending", "", confidence=confidence, fit=asdict(fit), computed_at=now)
    results = {}
    for name, level in (("warn", th.warn), ("crit", th.crit)):
        if level is None:
            continue
        kind, at = _crossing(fit, level, th.direction, now)
        results[name] = (kind, at)
        if kind in ("already", "future") and at is not None and at <= horizon:
            setattr(out, f"{name}_at", at)

    kinds = {k for k, _ in results.values()}
    trend = f"{fit.slope_per_day:+.3g}/day, r2 {fit.r2:.2f}"
    if "already" in kinds:
        out.status, out.reason = "already_crossed", f"trend line is past a threshold ({trend})"
    elif out.warn_at is not None or out.crit_at is not None:
        out.status, out.reason = "projected", trend
    elif "future" in kinds:
        out.status = "beyond_horizon"
        out.reason = f"crossing more than {cfg.horizon_days:g} days out ({trend})"
    else:
        out.reason = f"trend is flat or moving away from thresholds ({trend})"
    return out
