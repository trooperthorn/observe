"""Host-bound ingest keys: create, verify, revoke, host binding, hashed storage."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from observe.ingest.keys import IngestKeyError, create_key, list_keys, revoke_key, verify_key
from observe.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "w.db"))
    yield s
    s.close()


def run(coro):
    return asyncio.run(coro)


def test_create_and_verify_records_last_used(store):
    key, info = run(create_key(store, "nas01", created_by="tester"))
    assert info.host == "nas01" and info.active and info.last_used is None
    assert run(verify_key(store, key, "nas01", now=1234.0))
    (listed,) = run(list_keys(store))
    assert listed.prefix == info.prefix and listed.last_used == 1234.0
    assert listed.created_by == "tester"


def test_revoked_key_is_rejected(store):
    key, info = run(create_key(store, "nas01"))
    assert run(revoke_key(store, info.prefix))
    assert not run(verify_key(store, key, "nas01"))
    assert not run(revoke_key(store, info.prefix))  # already revoked
    assert not run(revoke_key(store, "nosuchprefix"))
    (listed,) = run(list_keys(store))
    assert not listed.active and listed.last_used is None


def test_key_bound_to_another_host_is_rejected(store):
    key, _ = run(create_key(store, "nas01"))
    assert not run(verify_key(store, key, "nas02"))
    assert not run(verify_key(store, key, "NAS01"))
    assert not run(verify_key(store, key, ""))
    assert run(list_keys(store))[0].last_used is None  # failures do not count as use


def test_plaintext_is_never_stored(store, tmp_path):
    key, _ = run(create_key(store, "nas01"))
    secret = key.split("_", 2)[2]
    db = sqlite3.connect(str(tmp_path / "w.db"))
    try:
        rows = db.execute("SELECT prefix, hash, host, created_by FROM ingest_keys").fetchall()
        stored_hash = rows[0][1]
    finally:
        db.close()
    assert key not in [c for r in rows for c in r]
    assert all(secret not in str(c) for r in rows for c in r)
    assert stored_hash != secret and len(stored_hash) == 64
    store.close()  # the last connection closing folds the write-ahead log into the file
    raw = (tmp_path / "w.db").read_bytes()
    assert secret.encode() not in raw and key.encode() not in raw


@pytest.mark.parametrize("bad", ["", "garbage", "wpi_", "wpi_a_", "wpi__b", "x_aa_bb",
                                 "wpi_aa_bb_cc"])
def test_malformed_and_unknown_keys_are_rejected(store, bad):
    run(create_key(store, "nas01"))
    assert not run(verify_key(store, bad, "nas01"))


def test_wrong_secret_with_valid_prefix_is_rejected(store):
    key, _ = run(create_key(store, "nas01"))
    forged = key[:-1] + ("A" if key[-1] != "A" else "B")
    assert not run(verify_key(store, forged, "nas01"))


def test_keys_are_unique_and_carry_ingest_marker(store):
    a, _ = run(create_key(store, "nas01"))
    b, _ = run(create_key(store, "nas01"))
    assert a != b and a.startswith("wpi_") and b.startswith("wpi_")
    assert run(verify_key(store, a, "nas01")) and run(verify_key(store, b, "nas01"))


@pytest.mark.parametrize("host", ["", "a b", "x" * 129, "a\nb"])
def test_invalid_host_names_are_refused(store, host):
    with pytest.raises(IngestKeyError):
        run(create_key(store, host))


def test_secret_containing_underscores_still_verifies(store, monkeypatch):
    monkeypatch.setattr("observe.ingest.keys.secrets.token_urlsafe", lambda n: "a_b-c_d")
    key, _ = run(create_key(store, "nas01"))
    assert key.endswith("_a_b-c_d")
    assert run(verify_key(store, key, "nas01"))
