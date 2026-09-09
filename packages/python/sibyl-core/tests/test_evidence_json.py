"""Byte provenance survives Unicode, transport spellings, and dictionary aliases."""

import json

import pytest

from sibyl_core.tasks._evidence_json import read_original_json, share_exact_values


def test_original_ranges_use_utf8_bytes_and_original_string_spelling() -> None:
    artifact = ' { "name": "λ", "escaped": "\\u03bb", "items": [true, 1.0] } '.encode()
    parsed = read_original_json(artifact)
    assert parsed.value == {"name": "λ", "escaped": "λ", "items": [True, 1.0]}
    for key, expected in (("name", '"λ"'.encode()), ("escaped", b'"\\u03bb"')):
        start, end = parsed.ranges[(key,)]
        assert artifact[start:end] == expected
    start, end = parsed.ranges[("items", 1)]
    assert artifact[start:end] == b"1.0"


@pytest.mark.parametrize(
    "artifact",
    [
        b'{"a":1,"a":2}',
        b'{"a":{"b":1,"b":2}}',
        b"NaN",
        b"Infinity",
        b"1e9999",
        b"-1e9999",
        b"[1,]",
        b"{}[]",
    ],
)
def test_ambiguous_or_non_json_evidence_is_rejected(artifact: bytes) -> None:
    with pytest.raises(ValueError):
        read_original_json(artifact)


def test_shared_values_round_trip_reference_shaped_literals() -> None:
    repeated = {"code": "print('complete output')" * 20}
    source = [repeated, repeated, {"$ref": 0}, {"$literal": [["$ref", 0]]}, [1, 1.0, True]]
    encoded = share_exact_values(source)

    def decode(value):
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                return decode(encoded["values"][value["$ref"]])
            if set(value) == {"$literal"}:
                return {key: decode(child) for key, child in value["$literal"]}
            return {key: decode(child) for key, child in value.items()}
        if isinstance(value, list):
            return [decode(child) for child in value]
        return value

    assert json.dumps(decode(encoded["sources"])) == json.dumps(source)
    assert len(json.dumps(encoded)) < len(json.dumps(source))
    assert share_exact_values(source) == encoded


def test_largest_finite_float_retains_original_numeric_span() -> None:
    artifact = b"1.7976931348623157e308"
    parsed = read_original_json(artifact)
    assert parsed.value == 1.7976931348623157e308
    assert parsed.ranges[()] == (0, len(artifact))
