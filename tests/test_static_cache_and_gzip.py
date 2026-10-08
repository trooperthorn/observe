"""Static files are cached with revalidation and large API answers are gzipped (October audit,
optimisations 4 and 6).

Measured with the test client. Before: every request for `/static/app.js` returned all 16,174
bytes with `no-store`, and the 2 series by 2,880 point metrics query below was 126,194 bytes on the
wire. After: the second request for the script is a 304 with no body, and the metrics query is
about 31,153 bytes gzipped (75 percent smaller) with the decoded bytes identical."""

from __future__ import annotations

import gzip
import json

import pytest

from .api_env import START, ApiEnv

QUERY = ("/api/v2/metrics/query?metric=monitor.latency&from=-1d&step=30"
         "&agg=avg,min,max,count,sum")


@pytest.fixture
def env(tmp_path):
    e = ApiEnv(tmp_path)
    e.headers = e.token()
    yield e
    e.close()


def seed_big(env):
    n = 2880
    for slug in ("core", "edge"):
        env.poll_many(slug, [(START - (n - i) * 30, float(i) + 0.123456) for i in range(n)])


def test_static_script_has_etag_revalidates_and_second_request_is_304(env):
    first = env.client.get("/static/app.js")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-cache"
    etag = first.headers["etag"]
    second = env.client.get("/static/app.js", headers={"If-None-Match": etag})
    assert second.status_code == 304 and second.content == b""
    assert second.headers["cache-control"] == "no-cache"
    assert second.headers["etag"] == etag


def test_static_css_is_cached_the_same_way(env):
    css = next(p for p in (env.client.get("/static/index.html").text.split('"'))
               if p.startswith("/static/") and p.endswith(".css"))
    first = env.client.get(css)
    assert first.status_code == 200 and first.headers["cache-control"] == "no-cache"
    again = env.client.get(css, headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


def test_a_large_metrics_answer_is_gzipped_when_accepted_and_identical_when_decoded(env):
    seed_big(env)
    gz = env.client.get(QUERY, headers={**env.headers, "Accept-Encoding": "gzip"})
    plain = env.client.get(QUERY, headers={**env.headers, "Accept-Encoding": "identity"})
    assert gz.status_code == plain.status_code == 200
    assert "content-encoding" not in plain.headers
    raw = plain.content
    assert len(raw) >= 120_000, raw[:300]
    assert gz.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in gz.headers["vary"].lower()
    assert gz.content == raw  # httpx decoded it; the bytes match the identity answer
    assert json.loads(gz.content) == json.loads(raw)
    wire = len(gzip.compress(raw, 5))
    assert wire < len(raw) // 3


def test_the_gzipped_answer_still_honours_the_etag(env):
    seed_big(env)
    first = env.client.get(QUERY, headers=env.headers)
    again = env.client.get(QUERY, headers={**env.headers, "If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


def test_small_answers_are_not_compressed(env):
    r = env.client.get("/healthz", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in r.headers


def test_login_and_session_pages_and_data_stay_no_store(env):
    assert env.client.get("/login").headers["cache-control"] == "no-store"
    assert env.client.get("/", headers=env.headers).headers["cache-control"] == "no-store"
    assert env.client.get("/api/session").headers["cache-control"] == "no-store"
    assert env.client.get("/api/v2/hosts", headers=env.headers).headers["cache-control"] == "no-store"
    assert env.client.get("/static/nope.js").headers["cache-control"] == "no-store"
