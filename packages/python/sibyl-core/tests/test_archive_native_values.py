"""Strict envelope boundaries, independent of native store access."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest
from surrealdb import RecordID
from surrealdb.cbor import CBORSimpleValue
from surrealdb.data.types.datetime import Datetime

from sibyl_core.migrate.archive_native_values import (
    archive_native_value_parameters,
    prepare_archive_native_value,
    validate_archive_native_envelope,
)

SHA = "a" * 64
DATE = "2026-09-30T01:02:03.123456789Z"
UID = "12345678-1234-4234-8234-123456789abc"


def seal(value: object, types: object) -> object:
    return prepare_archive_native_value(value=value, native_types=types, native_sha256=SHA)


def resign(payload: dict[str, object]) -> dict[str, object]:
    unsigned = {key: value for key, value in payload.items() if key != "envelope_sha256"}
    encoded = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    payload["envelope_sha256"] = hashlib.sha256(
        b"sibyl-archive-native-value-v1\0" + encoded.encode()
    ).hexdigest()
    return payload


def test_archive_native_values_exact_scalar_binding_and_detached_snapshot() -> None:
    key = "a.b" + chr(96) + "$[0]\0雪"
    value = {key: [None, None, DATE, UID, "entity:7", False, 7, -0.0]}
    types = [
        {"path": [key, 0], "kind": "none"},
        {"path": [key, 1], "kind": "null"},
        {"path": [key, 2], "kind": "datetime"},
        {"path": [key, 3], "kind": "uuid"},
    ]
    envelope = seal(value, types)
    value[key][0] = "changed"
    types[0]["kind"] = "uuid"
    first = archive_native_value_parameters(envelope)
    assert first[key][0] is None
    assert first[key][1] == CBORSimpleValue(22)
    assert type(first[key][2]) is Datetime and first[key][2].dt == DATE
    assert type(first[key][3]) is UUID and str(first[key][3]) == UID
    assert first[key][4:] == ["entity:7", False, 7, -0.0]
    payload = envelope.payload
    payload["value"][key][1] = "changed"
    first[key].append("changed")
    assert len(archive_native_value_parameters(envelope)[key]) == 8
    assert envelope.native_sha256 == SHA
    with pytest.raises(FrozenInstanceError):
        envelope.payload_json = "{}"


def test_archive_native_values_record_typed_identifier_nested_annotation_root() -> None:
    identifier = {
        "value": [UID, {"null": None, "none": None, "clock": DATE}],
        "native_types": [
            {"path": [0], "kind": "uuid"},
            {"path": [1, "null"], "kind": "null"},
            {"path": [1, "none"], "kind": "none"},
            {"path": [1, "clock"], "kind": "datetime"},
        ],
    }
    table = "query-looking table; RETURN 'caller text'"
    value = {"ref": {"table": table, "id": identifier["value"]}}
    envelope = seal(
        value, [{"path": ["ref"], "kind": "record", "table": table, "identifier": identifier}]
    )
    record = archive_native_value_parameters(envelope)["ref"]
    assert type(record) is RecordID and record.table_name == table
    assert type(record.id[0]) is UUID
    assert record.id[1]["null"] == CBORSimpleValue(22)
    assert record.id[1]["none"] is None
    assert record.id[1]["clock"].dt == DATE


@pytest.mark.parametrize(
    "value,types",
    [
        (None, []),
        ([None], []),
        ({1: "key"}, []),
        ((1,), []),
        (float("inf"), []),
        (float("nan"), []),
        (2**63, []),
        ("\ud800", []),
        ({"x": 1}, [{"path": ["x"], "kind": "null"}]),
        ([None], [{"path": [True], "kind": "none"}]),
        ([None], [{"path": [-1], "kind": "none"}]),
        ([None], [{"path": [1], "kind": "none"}]),
        ([None], [{"path": ["0"], "kind": "none"}]),
        (None, [{"path": [], "kind": "none"}, {"path": [], "kind": "null"}]),
        (None, [{"path": [], "kind": "none", "extra": "claim"}]),
        (None, [{"path": [], "kind": "decimal"}]),
        ("2026-09-30T01:02:03.123Z", [{"path": [], "kind": "datetime"}]),
        ("2026-02-30T01:02:03.123456789Z", [{"path": [], "kind": "datetime"}]),
        (UID.upper(), [{"path": [], "kind": "uuid"}]),
    ],
)
def test_archive_native_values_reject_ambiguous_or_unsupported_trees(value, types) -> None:
    with pytest.raises(ValueError):
        seal(value, types)


def test_archive_native_values_reject_record_relabel_overlap_and_bad_top_identifier() -> None:
    descriptor = {
        "path": [],
        "kind": "record",
        "table": "entity",
        "identifier": {"value": 7, "native_types": []},
    }
    with pytest.raises(ValueError):
        seal({"table": "entity", "id": "7"}, [descriptor])
    with pytest.raises(ValueError):
        seal({"table": "entity", "id": 7}, [descriptor, {"path": ["id"], "kind": "none"}])
    descriptor["identifier"] = {"value": True, "native_types": []}
    with pytest.raises(ValueError):
        seal({"table": "entity", "id": True}, [descriptor])


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("version", True),
        ("version", 2),
        ("native_sha256", "b" * 64),
        ("value", "replaced"),
        ("native_types", [{"path": [], "kind": "uuid"}]),
    ],
)
def test_archive_native_values_integrity_binds_all_evidence(field, replacement) -> None:
    payload = seal("plain", []).payload
    payload[field] = replacement
    with pytest.raises(ValueError):
        validate_archive_native_envelope(payload)


def test_archive_native_values_digest_is_integrity_not_authenticity() -> None:
    payload = seal("plain", []).payload
    payload["native_sha256"] = "b" * 64
    forged = validate_archive_native_envelope(resign(payload))
    assert forged.native_sha256 == "b" * 64
    payload["version"] = True
    with pytest.raises(ValueError):
        validate_archive_native_envelope(resign(payload))


def test_archive_native_values_reject_cycles_and_unprepared_binding() -> None:
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError):
        seal(cyclic, [])
    with pytest.raises(TypeError):
        archive_native_value_parameters({"value": "caller"})
