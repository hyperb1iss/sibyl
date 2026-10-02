"""Inert, integrity-bound native values for trusted operator archive tooling.

The digest binds content, not operator identity or permission. Personal archive
intake must never use this envelope to import authority or store history.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from surrealdb import RecordID
from surrealdb.cbor import CBORSimpleValue

from sibyl_core.migrate.source_integrity import ArchiveDatetime, native_archive_parameters

_DOMAIN = b"sibyl-archive-native-value-v1\0"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{9}Z\Z")
Path = tuple[str | int, ...]


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _text(value: object) -> str:
    if type(value) is not str:
        raise ValueError("native envelope text must be a string")
    value.encode("utf-8", errors="strict")
    return value


def _json(value: object, active: set[int] | None = None) -> None:
    """Reject non-JSON/coerced values, nonfinite floats and cyclic containers."""
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        _text(value)
        return
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise ValueError("native envelope integer exceeds native signed 64-bit range")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("native envelope floats must be finite")
        return
    if type(value) not in (dict, list):
        raise ValueError("native envelope values must be strict JSON")
    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        raise ValueError("native envelope values must be acyclic")
    active.add(identity)
    try:
        children = value.items() if type(value) is dict else enumerate(cast(list[object], value))
        for key, child in children:
            if type(value) is dict:
                _text(key)
            _json(child, active)
    finally:
        active.remove(identity)


def _fields(value: object, names: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != names:
        raise ValueError("native envelope has unexpected fields")
    return cast(dict[str, Any], value)


def _date(value: object) -> str:
    text = _text(value)
    if _DATETIME.fullmatch(text) is None:
        raise ValueError("native datetime must be UTC with nine fractional digits")
    ArchiveDatetime.parse(text)
    return text


def _uuid(value: object) -> str:
    text = _text(value)
    if str(UUID(text)) != text:
        raise ValueError("native UUID must be canonical")
    return text


def _typed_tree(value: object, annotations: object) -> object:
    """Validate annotations and produce native parameters without SQL parsing."""
    _json(value)
    if type(annotations) is not list:
        raise ValueError("native types must be an array")
    paths: dict[Path, dict[str, Any]] = {}
    for annotation in annotations:
        if type(annotation) is not dict:
            raise ValueError("native type descriptor must be an object")
        annotation = cast(dict[str, Any], annotation)
        path = annotation.get("path")
        if type(path) is not list or any(
            type(step) not in (str, int) or (type(step) is int and step < 0) for step in path
        ):
            raise ValueError("native type paths require string keys or nonnegative integer indexes")
        key = tuple(cast(list[str | int], path))
        if key in paths:
            raise ValueError("native type paths must be unique")
        paths[key] = annotation
    visited: set[Path] = set()

    def walk(item: object, path: Path) -> object:
        descriptor = paths.get(path)
        if descriptor is not None:
            visited.add(path)
            kind = descriptor.get("kind")
            if kind in ("none", "null"):
                _fields(descriptor, {"path", "kind"})
                if item is not None:
                    raise ValueError("NONE/NULL descriptor must annotate a JSON null")
                return None if kind == "none" else CBORSimpleValue(22)
            if kind in ("datetime", "uuid"):
                _fields(descriptor, {"path", "kind"})
                text = _date(item) if kind == "datetime" else _uuid(item)
                return (
                    native_archive_parameters(ArchiveDatetime.parse(text))
                    if kind == "datetime"
                    else UUID(text)
                )
            if kind == "record":
                _fields(descriptor, {"path", "kind", "table", "identifier"})
                table = _text(descriptor["table"])
                if not table:
                    raise ValueError("native record table must be nonempty")
                identifier = _fields(descriptor["identifier"], {"value", "native_types"})
                literal = _fields(item, {"table", "id"})
                if _canonical(literal) != _canonical({"table": table, "id": identifier["value"]}):
                    raise ValueError("native record descriptor differs from its JSON value")
                native_id = _typed_tree(identifier["value"], identifier["native_types"])
                if type(native_id) not in (str, int, list, dict, UUID):
                    raise ValueError("unsupported native record identifier")
                return RecordID(table, native_id)
            raise ValueError("unsupported native type descriptor")
        if item is None:
            raise ValueError("every JSON null requires a native NONE/NULL descriptor")
        if type(item) is dict:
            return {
                key: walk(child, (*path, key))
                for key, child in cast(dict[str, object], item).items()
            }
        if type(item) is list:
            return [walk(child, (*path, index)) for index, child in enumerate(item)]
        return item

    result = walk(value, ())
    if visited != set(paths):
        raise ValueError("native type path is unused, overlapping, or outside the value")
    return result


def _validated(payload: object) -> dict[str, Any]:
    body = _fields(
        payload, {"version", "value", "native_types", "native_sha256", "envelope_sha256"}
    )
    _json(body)
    if type(body["version"]) is not int or body["version"] != 1:
        raise ValueError("unsupported native envelope version")
    for field in ("native_sha256", "envelope_sha256"):
        if type(body[field]) is not str or _DIGEST.fullmatch(body[field]) is None:
            raise ValueError("native envelope digest must be lowercase SHA-256")
    unsigned = {key: value for key, value in body.items() if key != "envelope_sha256"}
    expected = hashlib.sha256(_DOMAIN + _canonical(unsigned).encode()).hexdigest()
    if body["envelope_sha256"] != expected:
        raise ValueError("native envelope integrity mismatch")
    _typed_tree(body["value"], body["native_types"])
    return body


@dataclass(frozen=True, slots=True)
class PreparedArchiveNativeValue:
    """A serialized snapshot. Accessors return detached JSON, never shared trees."""

    payload_json: str

    def __post_init__(self) -> None:
        if type(self.payload_json) is not str:
            raise ValueError("prepared native envelope must be serialized JSON")
        _validated(json.loads(self.payload_json))

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)

    @property
    def value(self) -> object:
        return self.payload["value"]

    @property
    def native_sha256(self) -> str:
        return self.payload["native_sha256"]


def validate_archive_native_envelope(payload: object) -> PreparedArchiveNativeValue:
    """Validate inert envelope integrity and strict native annotations."""
    try:
        body = _validated(payload)
        return PreparedArchiveNativeValue(_canonical(body))
    except (TypeError, UnicodeError, RecursionError) as error:
        raise ValueError("invalid native envelope") from error


def prepare_archive_native_value(
    *, value: object, native_types: object, native_sha256: str
) -> PreparedArchiveNativeValue:
    """Seal already captured evidence; this function does not certify origin."""
    unsigned = {
        "version": 1,
        "value": value,
        "native_types": native_types,
        "native_sha256": native_sha256,
    }
    try:
        _json(unsigned)
        digest = hashlib.sha256(_DOMAIN + _canonical(unsigned).encode()).hexdigest()
        return validate_archive_native_envelope({**unsigned, "envelope_sha256": digest})
    except (TypeError, UnicodeError, RecursionError) as error:
        raise ValueError("invalid native envelope") from error


def archive_native_value_parameters(envelope: PreparedArchiveNativeValue) -> object:
    """Produce fresh SDK-native bound values. No archived SQL is executable."""
    if type(envelope) is not PreparedArchiveNativeValue:
        raise TypeError("native parameters require a prepared operator envelope")
    body = _validated(envelope.payload)
    return _typed_tree(body["value"], body["native_types"])
