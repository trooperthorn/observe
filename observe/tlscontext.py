"""Shared TLS client contexts for the checks and discovery.

Building an ssl.SSLContext loads a whole set of trusted roots: certifi's bundle when httpx builds
its default context, or the operating system's store for ssl.create_default_context() without a
cafile. That costs several milliseconds of CPU, more than the rest of a loopback HTTP poll. A
builder wrapped with shared() runs once per (verify, ca_bundle) pair, and the context it built is
handed out again after that.

Sharing a context does not share connections. Each poll still opens a new connection and makes a
full handshake, because Python resumes a client session only when one is passed in explicitly,
and nothing here does that.

A context that trusts a ca_bundle file is rebuilt when the file's modification time, size or inode
changes, so a replaced bundle is used from the next poll on. A failed build is not cached, so a
missing or unreadable bundle fails every poll until it is fixed. The operating system's store and
certifi's bundle are read once per process, so a root added to either one is picked up at the
next restart.

A shared context must not be modified. httpcore does write its ALPN list into every context it is
given, so each builder is wrapped once for contexts handed to httpx and once for raw TLS sockets: a
certificate probe must not start offering http/1.1 to a server that refuses unknown protocols.
"""

from __future__ import annotations

import os
import ssl
from collections.abc import Callable

Builder = Callable[[bool, str | None], ssl.SSLContext]
_Stamp = tuple[int, int, int]
_caches: list[dict[tuple[bool, str | None], tuple[_Stamp | None, ssl.SSLContext]]] = []


def shared(build: Builder) -> Builder:
    """Wrap build(verify, ca_bundle) so each pair is built once and then reused. ca_bundle is left
    out of the key when verify is false, because no builder reads it then."""
    cache: dict[tuple[bool, str | None], tuple[_Stamp | None, ssl.SSLContext]] = {}
    _caches.append(cache)

    def get(verify: bool, ca_bundle: str | None) -> ssl.SSLContext:
        bundle = ca_bundle if verify else None
        stamp = None
        if bundle:
            try:
                st = os.stat(bundle)
            except OSError:
                return build(verify, bundle)  # fails the way an uncached build would
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        hit = cache.get((verify, bundle))
        if hit is None or hit[0] != stamp:
            hit = cache[verify, bundle] = (stamp, build(verify, bundle))
        return hit[1]

    return get


def clear() -> None:
    """Forget every shared context, so the next request builds a new one (tests)."""
    for cache in _caches:
        cache.clear()


def default_context(verify: bool, ca_bundle: str | None) -> ssl.SSLContext:
    """ssl.create_default_context(), trusting ca_bundle or, without one, the operating system's
    store. With verify false, the chain and hostname checks are off."""
    if verify:
        return ssl.create_default_context(cafile=ca_bundle)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


http_default_context = shared(default_context)  # handed to httpx: http checks, discovery
socket_default_context = shared(default_context)  # raw sockets: tls_cert checks, discovery
