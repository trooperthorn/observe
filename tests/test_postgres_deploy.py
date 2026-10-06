"""Static checks on the PostgreSQL deployment files: the compose profile and the CI workflow."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))


def test_the_postgres_service_is_opt_in_and_uses_the_timescaledb_image():
    pg = COMPOSE["services"]["postgres"]
    assert pg["profiles"] == ["postgres"]
    image = pg["image"]
    assert image.startswith("timescale/timescaledb:") and not image.endswith(":latest")
    assert re.search(r":\d+\.\d+\.\d+-pg\d+$", image)
    assert "profiles" not in COMPOSE["services"]["observe"]


def test_the_postgres_service_has_a_health_check_and_a_volume():
    pg = COMPOSE["services"]["postgres"]
    hc = pg["healthcheck"]
    assert "pg_isready" in " ".join(hc["test"]) and hc["retries"] >= 3
    assert any(v.startswith("observe-pgdata:") for v in pg["volumes"])
    assert "observe-pgdata" in COMPOSE["volumes"]


def test_the_postgres_service_is_not_published_and_reads_its_password_from_a_file():
    pg = COMPOSE["services"]["postgres"]
    assert "ports" not in pg
    env = pg["environment"]
    assert "POSTGRES_PASSWORD" not in env
    assert env["POSTGRES_PASSWORD_FILE"].startswith("/run/secrets/")
    assert "no-new-privileges:true" in pg["security_opt"]


def test_observe_waits_for_postgres_only_when_that_profile_is_on():
    dep = COMPOSE["services"]["observe"]["depends_on"]["postgres"]
    assert dep == {"condition": "service_healthy", "required": False}


def test_every_workflow_action_is_pinned_to_a_full_commit():
    uses = [step["uses"] for job in WORKFLOW["jobs"].values() for step in job["steps"]
            if "uses" in step]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


def test_the_workflow_runs_the_suite_on_sqlite_and_on_a_timescaledb_service_container():
    jobs = WORKFLOW["jobs"]
    assert set(jobs) == {"sqlite", "postgres"}
    assert "OBSERVE_TEST_PG_DSN" not in jobs["sqlite"].get("env", {})
    pg = jobs["postgres"]
    assert "OBSERVE_TEST_PG_DSN" in pg["env"]
    images = [m["image"] for m in pg["strategy"]["matrix"]["include"]]
    assert any(i.startswith("timescale/timescaledb:") for i in images)
    modes = {m["timescale"] for m in pg["strategy"]["matrix"]["include"]}
    assert modes == {"on", "off"}
    assert "pg_isready" in pg["services"]["postgres"]["options"]
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_the_workflow_runs_on_every_push_and_pull_request():
    triggers = WORKFLOW.get("on", WORKFLOW.get(True))
    assert "push" in triggers and "pull_request" in triggers
