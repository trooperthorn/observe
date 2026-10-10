"""The context cache in observe/tlscontext.py: one context per (verify, ca_bundle), rebuilt when
the bundle file changes, never cached on failure, and kept apart for httpx and raw sockets."""

import os
import ssl

import pytest

from observe import tlscontext
from observe.checks.platforms import http_api_context, socket_api_context
from observe.tlscontext import (default_context, http_default_context, shared,
                                socket_default_context)

from .test_net import _self_signed


@pytest.fixture
def counted():
    """A shared default_context and the (verify, ca_bundle) of each build it ran."""
    calls = []

    def build(verify, ca_bundle):
        calls.append((verify, ca_bundle))
        return default_context(verify, ca_bundle)

    return shared(build), calls


def test_one_context_per_verify_and_bundle(tmp_path, counted):
    get, calls = counted
    ca = str(_self_signed(tmp_path, 90)[0])
    pinned, system, unverified = get(True, ca), get(True, None), get(False, None)
    assert get(True, ca) is pinned and get(True, None) is system
    assert get(False, None) is unverified
    assert len({id(pinned), id(system), id(unverified)}) == 3
    assert calls == [(True, ca), (True, None), (False, None)]


def test_bundle_is_not_read_without_verification(tmp_path, counted):
    get, calls = counted
    unverified = get(False, str(tmp_path / "absent.pem"))
    assert get(False, None) is unverified
    assert unverified.verify_mode == ssl.CERT_NONE and not unverified.check_hostname
    assert calls == [(False, None)]


def test_changed_bundle_is_rebuilt(tmp_path, counted):
    get, calls = counted
    ca = _self_signed(tmp_path, 90)[0]
    first = get(True, str(ca))
    st = ca.stat()
    os.utime(ca, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    second = get(True, str(ca))
    assert second is not first and get(True, str(ca)) is second
    assert len(calls) == 2


def test_failed_build_is_not_cached(tmp_path, counted):
    get, calls = counted
    path = tmp_path / "ca.pem"
    for _ in range(2):
        with pytest.raises(FileNotFoundError):
            get(True, str(path))
    path.write_text("not a certificate")
    for _ in range(2):
        with pytest.raises(ssl.SSLError):
            get(True, str(path))
    path.write_bytes(_self_signed(tmp_path, 90)[0].read_bytes())
    assert get(True, str(path)) is get(True, str(path))
    assert len(calls) == 5


def test_httpx_and_socket_copies_are_separate(tmp_path):
    ca = str(_self_signed(tmp_path, 90)[0])
    assert socket_default_context(True, ca) is socket_default_context(True, ca)
    assert http_default_context(True, ca) is not socket_default_context(True, ca)
    assert http_api_context(True, ca) is not socket_api_context(True, ca)
    assert http_api_context(False, None) is not socket_api_context(False, None)


def test_clear_forgets_every_shared_context(tmp_path):
    ca = str(_self_signed(tmp_path, 90)[0])
    before = socket_default_context(True, ca), http_api_context(False, None)
    tlscontext.clear()
    after = socket_default_context(True, ca), http_api_context(False, None)
    assert before[0] is not after[0] and before[1] is not after[1]
