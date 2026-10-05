"""Wire schema: fixture round trips, limits, version and field validation."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from observe.ingest import schema
from observe.ingest.schema import Batch

FIXTURES = Path(__file__).parent / "fixtures" / "hostwatch"
NAMES = ["batch_minimal", "batch_with_events", "batch_legacy_v1"]


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", NAMES)
def test_fixture_round_trip(name):
    raw = load(name)
    batch = Batch.model_validate(raw)
    again = Batch.model_validate_json(batch.model_dump_json())
    assert again == batch
    assert batch.host == raw["host"]
    assert len(batch.samples) == len(raw["samples"])


def test_legacy_batch_defaults():
    batch = Batch.model_validate(load("batch_legacy_v1"))
    assert batch.events == []
    assert batch.batch_id is None


def test_none_value_is_kept_not_zeroed():
    batch = Batch.model_validate(load("batch_minimal"))
    assert batch.samples[1].value is None
    assert batch.sources[1].present is False


def test_events_parsed():
    batch = Batch.model_validate(load("batch_with_events"))
    assert batch.events[0].boot_id == "bbbb"
    assert batch.events[1].detail == {}


@pytest.mark.parametrize("bad", [0, 2, -1])
def test_unknown_version_rejected(bad):
    raw = load("batch_minimal")
    raw["schema_version"] = bad
    with pytest.raises(ValidationError, match="unsupported schema_version"):
        Batch.model_validate(raw)


def test_unknown_field_ignored():
    raw = load("batch_minimal")
    raw["extra"] = 1
    raw["samples"][0]["bonus"] = 1
    batch = Batch.model_validate(raw)
    assert not hasattr(batch, "extra")


@pytest.mark.parametrize("path,value", [
    (("host",), ""),
    (("host",), "h" * 129),
    (("sent_at",), "soon"),
    (("sent_at",), float("inf")),
    (("samples", 0, "value"), "warm"),
    (("samples", 0, "value"), float("nan")),
    (("samples", 0, "ts"), float("inf")),
    (("samples", 0, "labels"), {"a": 1}),
    (("samples", 0, "source"), ""),
    (("sources", 0, "available"), "maybe"),
    (("batch_id",), ""),
    (("batch_id",), "x" * 65),
])
def test_malformed_fields_rejected(path, value):
    raw = load("batch_minimal")
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        Batch.model_validate(raw)


@pytest.mark.parametrize("field", ["agent_version", "host", "platform", "sent_at", "sources", "samples"])
def test_missing_required_field_rejected(field):
    raw = load("batch_minimal")
    del raw[field]
    with pytest.raises(ValidationError):
        Batch.model_validate(raw)


def test_event_requires_dedup_key():
    raw = load("batch_with_events")
    raw["events"][0]["dedup_key"] = ""
    with pytest.raises(ValidationError):
        Batch.model_validate(raw)


def test_sample_count_limit():
    raw = load("batch_minimal")
    sample = raw["samples"][0]
    raw["samples"] = [sample] * schema.MAX_SAMPLES
    Batch.model_validate(raw)
    raw["samples"] = [sample] * (schema.MAX_SAMPLES + 1)
    with pytest.raises(ValidationError):
        Batch.model_validate(raw)


def test_source_and_event_count_limits():
    raw = load("batch_with_events")
    raw["sources"] = [raw["sources"][0]] * (schema.MAX_SOURCES + 1)
    with pytest.raises(ValidationError):
        Batch.model_validate(raw)
    raw = load("batch_with_events")
    raw["events"] = [raw["events"][0]] * (schema.MAX_EVENTS + 1)
    with pytest.raises(ValidationError):
        Batch.model_validate(raw)


def test_label_limits():
    raw = load("batch_minimal")
    raw["samples"][0]["labels"] = {f"k{i}": "v" for i in range(schema.MAX_LABELS + 1)}
    with pytest.raises(ValidationError):
        Batch.model_validate(raw)
    raw["samples"][0]["labels"] = {"k": "v" * (schema.MAX_TEXT + 1)}
    with pytest.raises(ValidationError):
        Batch.model_validate(raw)


def test_event_detail_limits():
    raw = load("batch_with_events")
    event = raw["events"][0]
    event["detail"] = {"blob": "x" * schema.MAX_DETAIL_BYTES}
    with pytest.raises(ValidationError, match="detail too large"):
        Batch.model_validate(raw)
    event["detail"] = {f"k{i}": 1 for i in range(schema.MAX_DETAIL_KEYS + 1)}
    with pytest.raises(ValidationError):
        Batch.model_validate(raw)
    event["detail"] = {"x": float("nan")}
    with pytest.raises(ValidationError):
        Batch.model_validate(raw)


def test_oversize_batch_exceeds_body_cap():
    raw = load("batch_minimal")
    sample = copy.deepcopy(raw["samples"][0])
    sample["labels"] = {f"k{i}": "v" * 200 for i in range(schema.MAX_LABELS)}
    raw["samples"] = [sample] * schema.MAX_SAMPLES
    batch = Batch.model_validate(raw)
    assert len(batch.model_dump_json()) > schema.MAX_BODY_BYTES
