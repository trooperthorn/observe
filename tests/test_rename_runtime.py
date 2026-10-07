"""Runtime names are Observe's: defaults, metric names, compose and Docker files."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
