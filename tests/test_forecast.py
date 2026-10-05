import math
import time

from observe.config import ForecastSettings, Thresholds
from observe.forecast import least_squares, project
from observe.store import Store

CFG = ForecastSettings()
NOW = 1_800_000_000.0
DAY = 86400


def series(days=7, start=50.0, per_day=2.0, noise=0.0):
    pts = []
    for h in range(int(days * 24)):
        t = NOW - days * DAY + h * 3600
        pts.append((t, start + per_day * h / 24 + noise * math.sin(h * 1.7)))
    return pts


def test_least_squares_recovers_slope():
    fit = least_squares(series(per_day=2.0))
    assert abs(fit.slope_per_day - 2.0) < 1e-9 and fit.r2 == 1.0


def test_projects_warn_and_crit_dates():
    # Ends near 64 at NOW, rising 2/day: warn 80 in ~8 days, crit 90 in ~13.
    th = Thresholds(direction="above", warn=80, crit=90)
    f = project(series(), th, CFG, now=NOW)
    assert f.status == "projected" and f.confidence == "normal"
    assert abs((f.warn_at - NOW) / DAY - 8.0) < 0.1
    assert abs((f.crit_at - NOW) / DAY - 13.0) < 0.1


def test_below_direction_for_depleting_resource():
    th = Thresholds(direction="below", warn=20, crit=10)   # e.g. free space
    f = project(series(start=60, per_day=-3.0), th, CFG, now=NOW)
    assert f.status == "projected" and f.warn_at < f.crit_at


def test_flat_or_receding_is_not_a_date():
    th = Thresholds(direction="above", warn=80)
    assert project(series(per_day=0.0), th, CFG, now=NOW).status == "not_trending"
    assert project(series(per_day=-1.0), th, CFG, now=NOW).status == "not_trending"


def test_beyond_horizon():
    th = Thresholds(direction="above", warn=80)
    f = project(series(per_day=0.1), th, CFG, now=NOW)
    assert f.status == "beyond_horizon" and f.warn_at is None


def test_already_crossed():
    f = project(series(start=85, per_day=0.5), Thresholds(warn=80, crit=95), CFG, now=NOW)
    assert f.status == "already_crossed" and f.warn_at == NOW


def test_noisy_series_is_labelled_low_confidence():
    th = Thresholds(direction="above", warn=80)
    f = project(series(per_day=0.5, noise=15), th, CFG, now=NOW)
    assert f.fit["r2"] < 0.5 and f.confidence == "low"


def test_insufficient_history():
    th = Thresholds(warn=80)
    assert project(series(days=0.5), th, CFG, now=NOW).status == "insufficient_data"


async def test_store_hourly_buckets_skip_failures_and_nulls():
    from observe.checks.base import CheckResult, Result
    st = Store(":memory:")
    base = (time.time() // 3600 - 2) * 3600
    for i, (res, val) in enumerate([(Result.OK, 10.0), (Result.OK, 20.0),
                                    (Result.FAIL, 999.0), (Result.OK, None)]):
        await st.record("m", base + i * 60, CheckResult(res, "", value=val))
    await st.record("m", base + 3600 + 5, CheckResult(Result.WARN, "", value=40.0))
    assert await st.hourly_series("m", 1) == [(base + 1800, 15.0), (base + 5400, 40.0)]
