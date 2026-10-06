"""The PostgreSQL connection string and its password never reach a log, an error or a repr.

Nothing here needs a database server: the one connection attempt goes to a closed loopback port.
"""

from __future__ import annotations

import logging

import pytest

from observe.config import Config, ConfigError, StorageConfig, load_config
from observe.storage import StorageError, open_storage
from observe.storage.pg_dsn import check_dsn, read_password
from observe.storage.postgres import PgStorage, Scrubber, _ScrubFilter

PASSWORD = "hunter2-correct-horse"
DSN = "postgresql://observe_user@127.0.0.1:1/observe_db"


def write_config(tmp_path, storage_yaml: str):
    path = tmp_path / "observe.yaml"
    path.write_text(f"storage:\n{storage_yaml}\n", encoding="utf-8")
    return path


# ---- the connection string ------------------------------------------------------------------

@pytest.mark.parametrize("dsn", [
    f"postgresql://u:{PASSWORD}@db:5432/observe",
    f"postgres://u:{PASSWORD}@db/observe",
    f"postgresql://u@db/observe?password={PASSWORD}",
    f"host=db dbname=observe user=u password={PASSWORD}",
    f"host=db password = '{PASSWORD}'",
])
def test_a_connection_string_with_a_password_is_refused_without_repeating_it(dsn):
    with pytest.raises(ValueError) as err:
        check_dsn(dsn)
    assert PASSWORD not in str(err.value) and "password_file" in str(err.value)


@pytest.mark.parametrize("dsn", ["postgresql://observe@postgres:5432/observe",
                                 "host=postgres dbname=observe user=observe"])
def test_a_connection_string_without_a_password_is_accepted(dsn):
    assert check_dsn(dsn) == dsn


def test_a_config_error_for_a_bad_connection_string_does_not_echo_it(tmp_path):
    dsn = f"postgresql://u:{PASSWORD}@db:5432/observe"
    path = write_config(tmp_path, f"  backend: postgres\n  dsn: {dsn}")
    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert PASSWORD not in str(err.value) and dsn not in str(err.value)
    assert "storage.password_file" in str(err.value)


def test_the_config_hides_the_connection_string_in_its_repr(tmp_path):
    cfg = StorageConfig(backend="postgres", dsn=DSN, password_file="/run/secrets/x")
    assert DSN not in repr(cfg) and DSN not in str(cfg.model_dump())
    assert "observe_user" not in repr(Config(storage=cfg).storage)


def test_the_backend_settings_are_consistent():
    with pytest.raises(ValueError, match="needs storage.dsn"):
        StorageConfig(backend="postgres")
    with pytest.raises(ValueError, match="only to"):
        StorageConfig(backend="sqlite", dsn=DSN)
    assert Config().storage.backend == "sqlite"


# ---- the password file ----------------------------------------------------------------------

def test_the_password_comes_from_its_file_without_the_trailing_newline(tmp_path):
    secret = tmp_path / "db_password"
    secret.write_text(PASSWORD + "\n", encoding="utf-8")
    cfg = StorageConfig(backend="postgres", dsn=DSN, password_file=str(secret))
    assert cfg.password() == PASSWORD
    assert read_password(str(secret)) == PASSWORD


def test_an_unreadable_or_empty_password_file_is_a_config_error_that_names_only_the_path(tmp_path):
    missing = tmp_path / "none"
    with pytest.raises(ConfigError) as err:
        StorageConfig(backend="postgres", dsn=DSN, password_file=str(missing)).password()
    assert str(missing) in str(err.value)
    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        StorageConfig(backend="postgres", dsn=DSN, password_file=str(empty)).password()


def test_no_password_file_means_no_password():
    assert StorageConfig(backend="postgres", dsn=DSN).password() is None


# ---- scrubbing ------------------------------------------------------------------------------

def test_the_scrubber_removes_the_string_the_password_and_anything_shaped_like_them():
    scrub = Scrubber(DSN, PASSWORD)
    text = (f"failed {DSN} with {PASSWORD}; also postgresql://other:pw@h/db and "
            "host=h password=abc123 done")
    out = scrub(text)
    for secret in (DSN, PASSWORD, "other:pw", "abc123"):
        assert secret not in out
    assert out.count("[withheld]") >= 3 and out.endswith("done")


def test_a_log_record_from_the_pool_is_scrubbed_before_it_is_emitted(caplog):
    log = logging.getLogger("psycopg.pool")
    flt = _ScrubFilter(Scrubber(DSN, PASSWORD))
    log.addFilter(flt)
    try:
        with caplog.at_level(logging.WARNING, logger="psycopg.pool"):
            log.warning("error connecting in %r: %s", "pool", f"bad {DSN} {PASSWORD}")
            try:
                raise RuntimeError(PASSWORD)
            except RuntimeError:
                log.exception("again %s", DSN)
    finally:
        log.removeFilter(flt)
    assert caplog.records
    for record in caplog.records:
        assert DSN not in record.getMessage() and PASSWORD not in record.getMessage()
        assert not record.exc_info


def test_a_failed_connection_raises_an_error_without_the_string_or_the_password(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(StorageError) as err:
        PgStorage(DSN, password=PASSWORD, connect_timeout_s=1)
    shown = str(err.value) + repr(err.value) + " ".join(r.getMessage() for r in caplog.records)
    assert DSN not in shown and PASSWORD not in shown
    assert err.value.__cause__ is None  # the driver's own error is not chained in


def test_the_postgres_backend_needs_a_connection_string():
    with pytest.raises(StorageError, match="storage.dsn"):
        open_storage("", backend="postgres")
