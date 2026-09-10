"""Malformed private history is rejected before archive SQL construction."""

import copy

import pytest

from sibyl_core.services.validation_execution import validation_archive_guard
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.procedure_review import review_digest


def record(request=None):
    if request is None:
        request = {
            "org": "org",
            "principal": "owner",
            "parent": "source",
            "policy": "{}",
            "source_bindings": [{"source_id": "source", "incarnation": "epoch", "generation": 1}],
        }
    identity = review_digest(request)
    return {
        "uuid": identity,
        "request_sha256": identity,
        "request_json": canonical(request),
        "organization_id": "org",
        "principal_id": "owner",
        "parent_id": "source",
        "policy_json": "{}",
        "source_ids": ["source"],
        "purged": False,
    }


@pytest.mark.parametrize("field", ["request_json", "source_ids", "parent_id", "policy_json"])
def test_missing_record_field_is_value_error(field):
    value = record()
    del value[field]
    with pytest.raises(ValueError):
        validation_archive_guard("memory_validation_executions", value)


@pytest.mark.parametrize("payload", [[], None, 4, "request", {}, {"org": "org"}])
def test_nonobject_or_incomplete_request_is_value_error(payload):
    value = record()
    value.update(
        request_json=canonical(payload),
        request_sha256=review_digest(payload),
        uuid=review_digest(payload),
    )
    with pytest.raises(ValueError):
        validation_archive_guard("memory_validation_executions", value)


@pytest.mark.parametrize(
    "binding",
    [
        None,
        [],
        {},
        {"source_id": "source"},
        {"source_id": [], "incarnation": "epoch", "generation": 1},
        {"source_id": "source", "incarnation": "epoch", "generation": True},
    ],
)
def test_malformed_binding_is_value_error(binding):
    import json

    request = json.loads(record()["request_json"])
    request["source_bindings"] = [binding]
    with pytest.raises(ValueError):
        validation_archive_guard("memory_validation_executions", record(request))


def test_valid_guard_retains_source_fences():
    value = record()
    before = copy.deepcopy(value)
    guard = validation_archive_guard("memory_validation_executions", value)
    assert "source_states" in guard and "deleted != false" in guard
    assert "incarnation" in guard and "generation < 1" in guard
    assert value == before


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"execution_id": []},
        {
            "execution_id": "id",
            "organization_id": "org",
            "principal_id": "owner",
            "outcome_json": {},
        },
    ],
)
def test_malformed_attempt_is_value_error(value):
    with pytest.raises(ValueError):
        validation_archive_guard("memory_validation_attempts", value)
