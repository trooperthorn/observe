"""Runtime names after the rename to Observe, and the old names still read with a warning."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from observe import compat
from observe.config import ConfigError, _resolve_refs

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _fresh_warnings():
    compat._warned.clear()
    yield
    compat._warned.clear()


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_old_env_var_is_read_with_one_warning_naming_the_new_one(monkeypatch, caplog):
    monkeypatch.delenv("OBSERVE_UI_PASSWORD", raising=False)
    monkeypatch.setenv("WATCHPOST_UI_PASSWORD", "old")
    with caplog.at_level(logging.WARNING, logger="observe"):
        assert compat.getenv("OBSERVE_UI_PASSWORD") == "old"
        assert compat.getenv("OBSERVE_UI_PASSWORD") == "old"
    warns = _warnings(caplog)
    assert len(warns) == 1 and "OBSERVE_UI_PASSWORD" in warns[0]


def test_new_env_var_wins_when_both_are_set(monkeypatch, caplog):
    monkeypatch.setenv("OBSERVE_UI_PASSWORD", "new")
    monkeypatch.setenv("WATCHPOST_UI_PASSWORD", "old")
    with caplog.at_level(logging.WARNING, logger="observe"):
        assert compat.getenv("OBSERVE_UI_PASSWORD") == "new"
    assert not _warnings(caplog)


def test_unset_env_var_gives_the_default(monkeypatch):
    monkeypatch.delenv("OBSERVE_NOPE", raising=False)
    monkeypatch.delenv("WATCHPOST_NOPE", raising=False)
    assert compat.getenv("OBSERVE_NOPE", "x") == "x"


def test_config_references_accept_the_old_env_name(monkeypatch, caplog):
    monkeypatch.delenv("OBSERVE_UI_PASSWORD", raising=False)
    monkeypatch.setenv("WATCHPOST_UI_PASSWORD", "old")
    with caplog.at_level(logging.WARNING, logger="observe"):
        assert _resolve_refs("${OBSERVE_UI_PASSWORD}") == "old"
    assert len(_warnings(caplog)) == 1
    monkeypatch.delenv("WATCHPOST_UI_PASSWORD")
    with pytest.raises(ConfigError):
        _resolve_refs("${OBSERVE_UI_PASSWORD}")


def _point_at(monkeypatch, tmp_path):
    monkeypatch.setattr(compat, "DEFAULT_CONFIG", str(tmp_path / "observe.yaml"))
    monkeypatch.setattr(compat, "LEGACY_CONFIG", str(tmp_path / "watchpost.yaml"))
    monkeypatch.setattr(compat, "DEFAULT_DB", str(tmp_path / "observe.db"))
    monkeypatch.setattr(compat, "LEGACY_DB", str(tmp_path / "watchpost.db"))


def test_config_falls_back_to_the_old_file_with_a_warning(monkeypatch, tmp_path, caplog):
    _point_at(monkeypatch, tmp_path)
    old = tmp_path / "watchpost.yaml"
    old.write_text("x: 1\n")
    with caplog.at_level(logging.WARNING, logger="observe"):
        assert compat.resolve_config_path(None) == str(old)
    assert len(_warnings(caplog)) == 1
    assert old.exists() and not (tmp_path / "observe.yaml").exists()  # nothing moved


def test_config_prefers_the_new_file_and_honours_an_explicit_path(monkeypatch, tmp_path, caplog):
    _point_at(monkeypatch, tmp_path)
    (tmp_path / "watchpost.yaml").write_text("x: 1\n")
    (tmp_path / "observe.yaml").write_text("x: 1\n")
    with caplog.at_level(logging.WARNING, logger="observe"):
        assert compat.resolve_config_path(None) == str(tmp_path / "observe.yaml")
        assert compat.resolve_config_path("/elsewhere.yaml") == "/elsewhere.yaml"
    assert not _warnings(caplog)


def test_config_default_when_neither_exists(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)
    assert compat.resolve_config_path(None) == str(tmp_path / "observe.yaml")


def test_database_falls_back_to_the_old_file_without_moving_it(monkeypatch, tmp_path, caplog):
    _point_at(monkeypatch, tmp_path)
    old = tmp_path / "watchpost.db"
    old.write_bytes(b"data")
    with caplog.at_level(logging.WARNING, logger="observe"):
        assert compat.resolve_db_path(compat.DEFAULT_DB) == str(old)
    assert len(_warnings(caplog)) == 1
    assert old.read_bytes() == b"data" and not (tmp_path / "observe.db").exists()


@pytest.mark.parametrize("configured,existing", [("observe.db", "watchpost.db"),
                                                  ("watchpost.db", "observe.db")])
def test_a_renamed_database_is_never_replaced_by_a_new_empty_one(tmp_path, configured, existing):
    # The owner renamed watchpost.db to observe.db but db_path still named the old file, and
    # Observe started on a new empty database: every user, key and host seemed gone.
    (tmp_path / existing).write_bytes(b"real data")
    with pytest.raises(compat.DatabaseMissing) as err:
        compat.resolve_db_path(str(tmp_path / configured))
    assert existing in str(err.value) and "db_path" in str(err.value)
    assert not (tmp_path / configured).exists()


def test_a_missing_database_with_no_sibling_starts_fresh(tmp_path):
    assert compat.resolve_db_path(str(tmp_path / "observe.db")) == str(tmp_path / "observe.db")
    (tmp_path / "watchpost.db").write_bytes(b"")
    assert compat.resolve_db_path(str(tmp_path / "observe.db")) == str(tmp_path / "observe.db")


def test_database_uses_the_new_file_when_present_or_path_is_custom(monkeypatch, tmp_path, caplog):
    _point_at(monkeypatch, tmp_path)
    (tmp_path / "watchpost.db").write_bytes(b"old")
    (tmp_path / "observe.db").write_bytes(b"new")
    with caplog.at_level(logging.WARNING, logger="observe"):
        assert compat.resolve_db_path(compat.DEFAULT_DB) == str(tmp_path / "observe.db")
        assert compat.resolve_db_path("/custom/x.db") == "/custom/x.db"
    assert not _warnings(caplog)


def test_defaults_name_observe():
    from observe import auth
    from observe.config import MqttAlert, ServerConfig
    assert ServerConfig().db_path == "/data/observe.db"
    assert MqttAlert.model_fields["topic_prefix"].default == "observe"
    assert auth.COOKIE == "observe_session"


def test_metric_names_start_with_observe_prefix():
    src = (ROOT / "observe" / "web.py").read_text(encoding="utf-8")
    names = set(re.findall(r"# TYPE (\w+) gauge", src))
    assert names and all(n.startswith("observe_") for n in names)


def test_compose_and_dockerfile_name_observe():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^  observe:$", compose, re.M)
    assert "image: observe:local" in compose and "container_name: observe" in compose
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["python", "-m", "observe"]' in docker
    assert 'CMD ["--config", "/config/observe.yaml"]' in docker
    for text in (compose, docker, (ROOT / ".env.example").read_text(encoding="utf-8"),
                 (ROOT / "config.example.yaml").read_text(encoding="utf-8")):
        assert "watchpost" not in text.lower()


def test_upgrade_guide_gives_the_pi_commands():
    text = (ROOT / "docs" / "UPGRADING-FROM-WATCHPOST.md").read_text(encoding="utf-8")
    for needle in ("~/observe", "git pull", "config/observe.yaml", "data/observe.db",
                   "signing_key_file", "docker compose down", "docker compose build",
                   "docker compose up -d"):
        assert needle in text


def test_docker_cmd_default_path_still_falls_back(monkeypatch, tmp_path, caplog):
    """The Dockerfile CMD passes the default path explicitly; the fallback must still run."""
    _point_at(monkeypatch, tmp_path)
    cmd = re.search(r'^CMD \["--config", "([^"]+)"\]$',
                    (ROOT / "Dockerfile").read_text(encoding="utf-8"), re.M)
    assert cmd and cmd.group(1) == "/config/observe.yaml"
    old = tmp_path / "watchpost.yaml"
    old.write_text("x: 1\n")
    with caplog.at_level(logging.WARNING, logger="observe"):
        assert compat.resolve_config_path(compat.DEFAULT_CONFIG) == str(old)
    assert len(_warnings(caplog)) == 1


def test_mqtt_default_prefix_warns_once(caplog):
    from observe.config import MqttAlert
    explicit = MqttAlert(type="mqtt", name="a", host="h", topic_prefix="watchpost")
    default = MqttAlert(type="mqtt", name="b", host="h")
    assert "topic_prefix" in explicit.model_fields_set
    assert "topic_prefix" not in default.model_fields_set
    with caplog.at_level(logging.WARNING, logger="observe"):
        compat.warn_mqtt_default_prefix()
        compat.warn_mqtt_default_prefix()
    assert len(_warnings(caplog)) == 1
