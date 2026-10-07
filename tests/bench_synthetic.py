"""A repeatable benchmark on synthetic data with the SQLite backend (not collected by pytest).

It builds the shape the architecture review measured: four pushed hosts with 40 series each, 36
pull monitors and a history of N days at a 30 second push interval, all written through
`Store.ingest_batch`, then times the ingest, poll and read paths through the FastAPI TestClient.
Nothing leaves the loopback interface and the database lives in a scratch folder.

    python -m tests.bench_synthetic --days 30 --dir <scratch folder> --out <result.json>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
import uuid
from pathlib import Path

from observe.checks.base import CheckResult
from observe.ingest.boot import classify_events
from observe.ingest.schema import Batch

from .api_env import ApiEnv

HOSTS = ["nas01", "nas02", "pve01", "pi01"]
PUSH_INTERVAL = 30.0
random.seed(7)


def series() -> list[tuple[str, str, dict[str, str], str]]:
    s = [("hwmon", "cpu_temp_c", {}, "C")]
    s += [("cpu", "usage_pct", {"core": str(i)}, "%") for i in range(8)]
    s += [("net", m, {"iface": f"eth{i}"}, "B/s") for i in range(4) for m in ("rx_bps", "tx_bps")]
    s += [("disk", "used_pct", {"mount": f"/mnt/d{i}"}, "%") for i in range(5)]
    s += [("smart", "temp_c", {"disk": f"sd{c}"}, "C") for c in "abcd"]
    s += [("fan", "rpm", {"fan": str(i)}, "rpm") for i in range(4)]
    s += [("mem", m, {}, "%") for m in ("used_pct", "swap_pct", "cache_pct", "avail_pct")]
    s += [("rapl", "package_watts", {"package": "0"}, "W"), ("rapl", "dram_watts", {}, "W")]
    s += [("load", f"load{i}", {}, "") for i in (1, 5, 15)]
    s += [("uptime", "seconds", {}, "s")]
    return s[:40]


SERIES = series()


def make_batch(host: str, ts: float) -> Batch:
    return Batch.model_validate({
        "schema_version": 1, "agent_version": "0.9.0", "host": host, "platform": "linux",
        "sent_at": ts, "batch_id": str(uuid.uuid4()),
        "sources": [{"source": s, "available": True}
                    for s in ("hwmon", "disk", "cpu", "net", "smart", "fan", "mem", "rapl")],
        "samples": [{"source": a, "metric": b, "labels": c, "unit": u,
                     "value": round(random.uniform(10, 60), 2), "ts": ts}
                    for a, b, c, u in SERIES],
        "events": []})


def pct(xs: list[float]) -> dict:
    xs = sorted(xs)
    return {"n": len(xs), "p50_ms": round(statistics.median(xs), 2),
            "p95_ms": round(xs[min(len(xs) - 1, int(len(xs) * 0.95))], 2)}


def timed(fn, reps: int) -> list[float]:
    out = []
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--reps", type=int, default=30)
    args = ap.parse_args()
    monitors = [{"name": h, "type": "pushed_host", "host": h, "group": "storage",
                 "stale_after": 3600} for h in HOSTS]
    monitors += [{"name": f"mon{i:02d}", "type": "ping", "host": f"10.1.0.{i + 1}",
                  "group": "net"} for i in range(36)]
    env = ApiEnv(Path(args.dir), monitors=monitors, api_rate_per_second=1_000_000,
                 api_burst=1_000_000)
    out: dict = {"days": args.days, "hosts": len(HOSTS), "series_per_host": len(SERIES)}
    base = float(int(time.time()))  # data is stamped near the real clock the checks read
    try:
        env.wall.now = base
        env.login("bench", admin=True)
        # History, through the real write path.
        start = base - args.days * 86400 - 60
        steps = int(args.days * 86400 / PUSH_INTERVAL)
        t0 = time.perf_counter()
        async def history() -> None:
            for i in range(steps):
                ts = start + i * PUSH_INTERVAL
                for host in HOSTS:
                    b = make_batch(host, ts)
                    await env.store.ingest_batch(b, classify_events(b.events), now=ts)
        asyncio.run(history())  # one event loop: a loop per batch exhausts sockets on Windows
        out["history_load_s"] = round(time.perf_counter() - t0, 1)
        print("history loaded", out["history_load_s"], flush=True)
        out["history_batches"] = steps * len(HOSTS)
        async def poll_rows() -> None:
            for m in monitors:
                if m["type"] != "ping":
                    continue
                for k in range(0, 24 * 120, 6):  # a day of poll rows every 12 minutes
                    await env.store.record(m["name"], base - k * 60,
                                           CheckResult.ok("up", value=1.0, latency_ms=5.0))
        asyncio.run(poll_rows())
        env.wall.now = base
        print("poll rows loaded", flush=True)

        def ingest_store() -> None:
            ts = env.wall.now = env.wall.now + 1
            b = make_batch("nas01", ts)
            asyncio.run(env.store.ingest_batch(b, classify_events(b.events), now=ts))
        out["ingest_batch_store_40_samples"] = pct(timed(ingest_store, args.reps))

        # Pull checks are stubs, as in the review.
        class Stub:
            def thresholds(self):
                return None

            async def run(self):
                return CheckResult.ok("up", value=1.0, latency_ms=1.0)

        sched = env.sched
        pushed = [m for m in sched.monitors if m.type == "pushed_host"]
        for m in sched.monitors:
            if m.type != "pushed_host":
                sched.checks[m.slug] = Stub()

        async def cycle() -> None:
            await asyncio.gather(*(sched.poll_once(m) for m in sched.monitors))
        out["poll_one_pushed_host"] = pct(timed(
            lambda: asyncio.run(sched.poll_once(pushed[0])), 5))
        out["poll_one_pull_monitor_stub"] = pct(timed(
            lambda: asyncio.run(sched.poll_once(sched.monitors[-1])), args.reps))
        out["poll_cycle_40_monitors"] = pct(timed(lambda: asyncio.run(cycle()), 3))

        q = "resource=nas01&scope=cpu&metric=usage_pct"
        endpoints = {
            "dash /monitors": "/monitors", "dash /events?limit=25": "/events?limit=25",
            "dash /findings": "/findings", "dash /hosts": "/hosts",
            "host /hosts/nas01": "/hosts/nas01", "status": "/status", "map": "/map",
            "ports": "/ports", "audit?limit=500": "/audit?limit=500", "session": "/session",
            "plugins": "/plugins", "metrics catalogue": "/metrics",
            "metrics latest (nas01 cpu)": "/metrics/latest?resource=nas01&scope=cpu&metric=usage_pct",
            "metrics query 24h step 300": f"/metrics/query?{q}&from=-24h&step=300",
            "metrics query 7d step 3600": f"/metrics/query?{q}&from=-7d&step=3600",
            "metrics query raw 1h": f"/metrics/query?{q}&from=-1h",
        }
        reads = {}

        def fresh_write() -> None:
            """A new batch and a new poll row, so the change counters move and the page is rebuilt."""
            env.wall.now += 31
            b = make_batch("nas01", env.wall.now)
            asyncio.run(env.store.ingest_batch(b, classify_events(b.events), now=env.wall.now))
            env.poll("mon00", ts=env.wall.now)

        for name, path in endpoints.items():
            r = env.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text[:200])
            cold = []
            for _ in range(args.reps):
                fresh_write()
                t = time.perf_counter()
                env.get(path)
                cold.append((time.perf_counter() - t) * 1000)
            r = env.get(path)
            row = {"cold": pct(cold), "warm": pct(timed(lambda p=path: env.get(p), args.reps)),
                   "bytes": len(r.content)}
            etag = r.headers.get("etag")
            if etag:
                row["p50_304_ms"] = pct(timed(
                    lambda p=path, e=etag: env.get(p, headers={"If-None-Match": e}),
                    args.reps))["p50_ms"]
            reads[name] = row
            print(name, row, flush=True)
        out["reads"] = reads
        dash = ["dash /monitors", "dash /events?limit=25", "dash /findings", "dash /hosts"]
        out["dashboard_refresh_sum_p50_ms"] = round(sum(reads[k]["cold"]["p50_ms"] for k in dash), 1)
        out["db_bytes"] = Path(env.path).stat().st_size
        wal = Path(env.path + "-wal")
        out["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
    finally:
        env.close()
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "reads"}, indent=1))


if __name__ == "__main__":
    main()
