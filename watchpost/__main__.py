"""Entry point.

  python -m watchpost --config /config/watchpost.yaml          run the service
  python -m watchpost --config ... --validate                  check config and exit
  python -m watchpost --config ... --once [--only SLUG]        poll once, print, exit
  python -m watchpost --config ... --discover [--target 192.0.2.0/24 ...]
        [--credential NAME ...] [--out proposals.yaml] [--report report.json]
                                                               propose monitors, exit
  python -m watchpost --config ... --ingest-key-create HOST    print a new ingest key once
  python -m watchpost --config ... --ingest-key-revoke ID      revoke an ingest key
  python -m watchpost --config ... --ingest-key-list           list keys, never the secrets
  python -m watchpost --config ... --create-admin USERNAME     create an admin user
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time

import uvicorn

from . import __version__
from .alerts import Alerter
from .config import ConfigError, load_config
from .plugins import PluginError, load_plugins
from .scheduler import Scheduler
from .store import Store
from .web import create_app

log = logging.getLogger("watchpost")


async def _once(config, only: str | None) -> int:  # type: ignore[no-untyped-def]
    store = Store(":memory:")
    sched = Scheduler(config, store, Alerter(config))
    monitors = [m for m in sched.monitors if only in (None, m.slug)]
    if not monitors:
        print(f"no enabled monitor matches {only!r}", file=sys.stderr)
        return 2
    results = await asyncio.gather(*(sched.poll_once(m) for m in monitors))
    worst = 0
    for mon, res in zip(monitors, results):
        print(f"{res.result.value.upper():5} {mon.slug:32} {res.message}")
        worst = max(worst, {"ok": 0, "warn": 1, "fail": 2}[res.result.value])
    return worst


async def _serve(config, plugins) -> None:  # type: ignore[no-untyped-def]
    store = Store(config.server.db_path)
    alerter = Alerter(config)
    sched = Scheduler(config, store, alerter)
    app = create_app(config, store, sched, alerter, plugins=plugins)
    server = uvicorn.Server(uvicorn.Config(
        app, host=config.server.listen, port=config.server.port,
        log_level="warning", access_log=False, proxy_headers=False,
    ))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: setattr(server, "should_exit", True))
    sched.start()
    log.info("watchpost %s: %d monitors, %d alert targets, listening on %s:%d",
             __version__, len(sched.monitors), len(config.alerts),
             config.server.listen, config.server.port)
    try:
        await server.serve()
    finally:
        await sched.stop()
        store.close()


def _discover(config, args) -> int:  # type: ignore[no-untyped-def]
    import json
    from pathlib import Path

    from .discovery import DiscoveryError, discover

    try:
        text, report = asyncio.run(discover(config, args.target, args.credential,
                                            use_directory=not args.no_directory))
    except (DiscoveryError, ValueError) as err:
        print(f"discovery error: {err}", file=sys.stderr)
        return 2
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, default=str),
                                     encoding="utf-8")
    if args.known_hosts_out and report["new_ssh_host_keys"]:
        Path(args.known_hosts_out).write_text("\n".join(report["new_ssh_host_keys"]) + "\n",
                                              encoding="utf-8")
    if args.certs_out and report["unverified_certs"]:
        out_dir = Path(args.certs_out)
        out_dir.mkdir(parents=True, exist_ok=True)
        for ep, c in report["unverified_certs"].items():
            (out_dir / (ep.replace(":", "_") + ".pem")).write_text(c["pem"], encoding="utf-8")
    st = report["stats"]
    print(f"scanned {st['scanned']}, responded {st['responded']}, proposed {st['proposed']}, "
          f"skipped {st['skipped']} already configured", file=sys.stderr)
    if report["new_ssh_host_keys"]:
        print(f"{len(report['new_ssh_host_keys'])} SSH host keys seen for the first time; "
              "verify before trusting (listed at the end of the proposals)", file=sys.stderr)
    if report["directory"]["skipped"]:
        print(f"directory: {len(report['directory']['skipped'])} accounts skipped "
              "(disabled, stale, or filtered; see --report)", file=sys.stderr)
    if st["retired_credentials"]:
        print("credentials retired after repeated auth failures: "
              + ", ".join(st["retired_credentials"]), file=sys.stderr)
    return 0


def _ingest_keys(config, args) -> int:  # type: ignore[no-untyped-def]
    """Manage ingest keys directly in the database until the admin screen exists."""
    from . import audit
    from .ingest.keys import IngestKeyError, create_key, list_keys, revoke_key

    async def run(store: Store) -> int:
        if args.ingest_key_create:
            try:
                key, info = await create_key(store, args.ingest_key_create, created_by="cli")
            except IngestKeyError as err:
                await audit.record(store, "key_create_failed", actor="cli",
                                   detail={"host": args.ingest_key_create[:128],
                                           "reason": str(err)})
                print(f"error: {err}", file=sys.stderr)
                return 2
            await audit.record(store, "key_created", actor="cli",
                               detail={"host": info.host, "key_id": info.prefix})
            print(key)
            print(f"bound to host {info.host}, id {info.prefix}. This is the only time the "
                  "key is shown; it is stored hashed.", file=sys.stderr)
            return 0
        if args.ingest_key_revoke:
            if await revoke_key(store, args.ingest_key_revoke):
                await audit.record(store, "key_revoked", actor="cli",
                                   detail={"key_id": args.ingest_key_revoke[:64]})
                print(f"revoked {args.ingest_key_revoke}", file=sys.stderr)
                return 0
            await audit.record(store, "key_revoke_failed", actor="cli",
                               detail={"key_id": args.ingest_key_revoke[:64],
                                       "reason": "no active key with that id"})
            print("error: no active key with that id", file=sys.stderr)
            return 2
        for k in await list_keys(store):
            state = "active" if k.active else "revoked"
            used = "never" if k.last_used is None else time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(k.last_used))
            print(f"{k.prefix}  {state:7}  {k.host}  last used {used}")
        return 0

    store = Store(config.server.db_path)
    try:
        return asyncio.run(run(store))
    finally:
        store.close()


def _create_admin(config, args) -> int:  # type: ignore[no-untyped-def]
    """Create an admin user. The password comes from WATCHPOST_ADMIN_PASSWORD or a prompt,
    never from the command line, so it does not land in shell history or the process list."""
    import getpass
    import os

    from . import audit
    from .auth import AuthError, create_user

    password = os.environ.get("WATCHPOST_ADMIN_PASSWORD")
    if password is None:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat password: ") != password:
            print("error: passwords do not match", file=sys.stderr)
            return 2

    async def run(store: Store) -> int:
        try:
            await create_user(store, config, args.create_admin, password, is_admin=True)
        except AuthError as err:
            await audit.record(store, "user_create_failed", actor="cli",
                               detail={"username": args.create_admin.strip()[:64],
                                       "is_admin": True, "reason": str(err)})
            print(f"error: {err}", file=sys.stderr)
            return 2
        await audit.record(store, "user_created", actor="cli",
                           detail={"username": args.create_admin.strip(), "is_admin": True})
        print(f"created admin {args.create_admin.strip()}", file=sys.stderr)
        return 0

    store = Store(config.server.db_path)
    try:
        return asyncio.run(run(store))
    finally:
        store.close()


def _plugins(config):  # type: ignore[no-untyped-def]
    """The listed plugins, or None after printing why startup must stop."""
    try:
        return load_plugins(config)
    except PluginError as err:
        print(f"plugin error: {err}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(prog="watchpost")
    ap.add_argument("--config", default="/config/watchpost.yaml")
    ap.add_argument("--validate", action="store_true", help="validate config and exit")
    ap.add_argument("--once", action="store_true", help="poll every monitor once and exit")
    ap.add_argument("--only", help="with --once, poll only this monitor slug")
    ap.add_argument("--discover", action="store_true",
                    help="scan targets and write proposed monitors; changes nothing")
    ap.add_argument("--target", action="append",
                    help="with --discover: CIDR, a-b range, IP, or hostname (repeatable; "
                         "overrides discovery.targets)")
    ap.add_argument("--credential", action="append",
                    help="with --discover: credential name to try (repeatable; overrides "
                         "discovery.credentials)")
    ap.add_argument("--out", help="with --discover: write proposals here instead of stdout")
    ap.add_argument("--report", help="with --discover: also write a JSON findings report")
    ap.add_argument("--known-hosts-out",
                    help="with --discover: write first-seen SSH host keys here for review")
    ap.add_argument("--certs-out",
                    help="with --discover: directory to save certificates that did not "
                         "validate, one PEM per endpoint, for review and pinning")
    ap.add_argument("--no-directory", action="store_true",
                    help="with --discover: do not query discovery.directory")
    ap.add_argument("--ingest-key-create", metavar="HOST",
                    help="create an ingest key bound to HOST, print it once, and exit")
    ap.add_argument("--ingest-key-revoke", metavar="ID",
                    help="revoke the ingest key with this id (see --ingest-key-list) and exit")
    ap.add_argument("--ingest-key-list", action="store_true",
                    help="list ingest keys with host, state and last use, and exit")
    ap.add_argument("--create-admin", metavar="USERNAME",
                    help="create an admin user (password from WATCHPOST_ADMIN_PASSWORD or a "
                         "prompt) and exit")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per HTTP probe is noise
    try:
        config = load_config(args.config)
    except ConfigError as err:
        print(f"config error: {err}", file=sys.stderr)
        return 2
    if args.validate:
        plugins = _plugins(config)
        if plugins is None:
            return 2
        print(f"ok: {len(config.monitors)} monitors, {len(config.credentials)} credentials, "
              f"{len(config.alerts)} alert targets, {len(plugins.plugins)} plugins")
        return 0
    if args.ingest_key_create or args.ingest_key_revoke or args.ingest_key_list:
        return _ingest_keys(config, args)
    if args.create_admin:
        return _create_admin(config, args)
    if args.once:
        return asyncio.run(_once(config, args.only))
    if args.discover:
        return _discover(config, args)
    plugins = _plugins(config)
    if plugins is None:
        return 2
    asyncio.run(_serve(config, plugins))
    return 0


if __name__ == "__main__":
    sys.exit(main())
