"""Read-only, selected-record native capture for trusted operator backups.

Each round binds the entire ordered selection, including physical IDs and
unknown fields. Selection completeness, current authorization and the separate
content/graph cuts remain caller duties; this is not a public backup API.
"""

from __future__ import annotations

import base64
import binascii
import copy
import math
import re
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from surrealdb import RecordID
from surrealdb.cbor import CBORSimpleValue
from surrealdb.data.types.datetime import Datetime

from sibyl_core.backends.surreal.schema import render_surreal_compatible_sql
from sibyl_core.backends.surreal.schema_version import SurrealExecute
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.migrate.archive_native_values import (
    PreparedArchiveNativeValue,
    archive_native_value_parameters,
    prepare_archive_native_value,
)
from sibyl_core.migrate.source_integrity import ArchiveDatetime

# Source rows, absent-source history, associations, graph companions and the
# store-local phase/checked-intake metadata in the operator backup contract.
ARCHIVE_NATIVE_RECORD_TABLES = frozenset(
    {
        "raw_captures",
        "entity",
        "episode",
        "relates_to",
        "mentions",
        "source_states",
        "memory_derivations",
        "archive_phase_controls",
        "archive_phase_receipts",
        "archive_import_runs",
        "archive_import_artifacts",
    }
)

_CUT = """
    IF array::len(array::distinct($record_ids)) != array::len($record_ids) {
        THROW 'Native archive selection contains duplicate physical records';
    };
    LET $rows = $record_ids.map(|$record| (SELECT * FROM $record)[0]);
    IF array::len($rows) != array::len($record_ids)
        OR array::len($rows[WHERE id = NONE]) > 0 {
        THROW 'Native archive selection contains a missing physical record';
    };
    LET $fingerprint = crypto::sha256(type::string($rows));
    IF $expected != NONE AND $fingerprint != $expected {
        THROW 'Native archive selection changed during capture';
    };
"""

_DESCRIBE = (
    """
RETURN {
    """
    + _CUT
    + """
    LET $descriptors = array::combine($requests, [{ rows: $rows }]).map(|$pair| {
        LET $value = array::fold($pair[0], $pair[1].rows, |$current, $step| {
            RETURN IF $step.kind = 'record' THEN record::id($current)
                ELSE $current[$step.value] END;
        });
        RETURN IF $value = NONE THEN {kind: 'none'}
        ELSE IF $value = NULL THEN {kind: 'null'}
        ELSE IF type::is::bool($value) THEN {kind: 'bool', value: $value}
        ELSE IF type::is::int($value) THEN {kind: 'int', value: $value}
        ELSE IF type::is::float($value) THEN {kind: 'float', value: $value}
        ELSE IF type::is::string($value) THEN {
            kind: 'string', text_base64: encoding::base64::encode(<bytes>$value)
        }
        ELSE IF type::is::datetime($value) THEN {kind: 'datetime', text: type::string($value)}
        ELSE IF type::is::uuid($value) THEN {kind: 'uuid', text: type::string($value)}
        ELSE IF type::is::record($value) THEN {
            kind: 'record', table_base64: encoding::base64::encode(<bytes>record::tb($value))
        }
        ELSE IF type::is::object($value) THEN {
            kind: 'object', keys_base64: object::keys($value).map(|$key|
                encoding::base64::encode(<bytes>$key))
        }
        ELSE IF type::is::array($value) THEN {kind: 'array', length: array::len($value)}
        ELSE { THROW 'Unsupported native value in operator archive'; } END;
    });
    RETURN {fingerprint: $fingerprint, descriptors: $descriptors};
};
"""
)

_VERIFY = (
    """
RETURN {
    """
    + _CUT
    + """
    IF crypto::sha256(type::string($reconstructed)) != $fingerprint {
        THROW 'Native archive reconstruction differs from the selected native value';
    };
    RETURN {fingerprint: $fingerprint};
};
"""
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z\Z")


def _sql(query: str, url: str) -> str:
    query = render_surreal_compatible_sql(query, url=url)
    if not is_embedded_surreal_url(url):
        for kind in ("bool", "float", "uuid", "record"):
            query = query.replace("type::is::" + kind, "type::is_" + kind)
    return query


def _object(value: object, fields: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError("native capture returned an invalid descriptor shape")
    return cast(dict[str, Any], value)


def _safe_text(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9+/]*", value) is None:
        raise ValueError("native capture returned invalid base64 text")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
        text = raw.decode("utf-8", errors="strict")
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise ValueError("native capture returned invalid UTF-8 text") from error
    if base64.b64encode(raw).decode().rstrip("=") != value:
        raise ValueError("native capture returned noncanonical base64 text")
    return text


def _response(value: object, *, expected: str | None, fields: set[str]) -> dict[str, Any]:
    result = _object(value, fields)
    fingerprint = result["fingerprint"]
    if type(fingerprint) is not str or _DIGEST.fullmatch(fingerprint) is None:
        raise ValueError("native capture returned an invalid fingerprint")
    if expected is not None and fingerprint != expected:
        raise ValueError("native archive selection changed during capture")
    return result


def _selector_identity(value: object) -> object:
    """A strict typed identity; string/int/UUID identifiers cannot alias."""
    if value is None:
        return ("none",)
    if type(value) is CBORSimpleValue and value.value == 22:
        return ("null",)
    if type(value) is bool:
        return ("bool", value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("native record identifier float must be finite")
        return ("float", value.hex())
    if type(value) is Datetime:
        match = _DATE.fullmatch(value.dt)
        if match is None:
            raise ValueError("unsupported native record identifier datetime")
        text = match[1] + "." + (match[2] or "").ljust(9, "0") + "Z"
        ArchiveDatetime.parse(text)
        return ("datetime", text)
    if type(value) is RecordID:
        if type(value.table_name) is not str or not value.table_name:
            raise ValueError("invalid nested native record table")
        return ("record", _selector_identity(value.table_name), _selector_identity(value.id))
    if type(value) is str:
        value.encode("utf-8", errors="strict")
        return ("str", value)
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise ValueError("native record identifier exceeds signed 64-bit range")
        return ("int", value)
    if type(value) is UUID:
        return ("uuid", str(value))
    if type(value) is list:
        return ("array", tuple(_selector_identity(item) for item in value))
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("native record identifier object keys must be strings")
        return (
            "object",
            tuple(
                sorted(
                    (_selector_identity(key), _selector_identity(item))
                    for key, item in value.items()
                )
            ),
        )
    raise ValueError("unsupported selected native record identifier")


@dataclass(slots=True)
class _Node:
    address: list[dict[str, object]]
    path: list[str | int]
    annotations: list[dict[str, Any]]
    parent: dict[str, Any] | list[Any]
    key: str | int

    def assign(self, value: object) -> None:
        if isinstance(self.parent, list):
            if type(self.key) is not int:
                raise ValueError("invalid internal native array path")
            self.parent[self.key] = value
        else:
            self.parent[str(self.key)] = value


def _expand(node: _Node, descriptor: object) -> list[_Node]:
    if type(descriptor) is not dict:
        raise ValueError("native capture returned a nonobject descriptor")
    descriptor = cast(dict[str, Any], descriptor)
    kind = descriptor.get("kind")
    if type(kind) is not str:
        raise ValueError("native capture descriptor kind must be a string")
    if kind in ("none", "null"):
        _object(descriptor, {"kind"})
        node.assign(None)
        node.annotations.append({"path": node.path, "kind": kind})
    elif kind in ("bool", "int", "float"):
        _object(descriptor, {"kind", "value"})
        required = {"bool": bool, "int": int, "float": float}[kind]
        if type(descriptor["value"]) is not required:
            raise ValueError("native capture scalar has the wrong Python type")
        node.assign(descriptor["value"])
    elif kind == "string":
        _object(descriptor, {"kind", "text_base64"})
        node.assign(_safe_text(descriptor["text_base64"]))
    elif kind in ("datetime", "uuid"):
        _object(descriptor, {"kind", "text"})
        text = descriptor["text"]
        if type(text) is not str:
            raise ValueError("native capture typed text must be a string")
        if kind == "datetime":
            match = _DATE.fullmatch(text)
            if match is None:
                raise ValueError("unsupported native datetime encoding")
            text = match[1] + "." + (match[2] or "").ljust(9, "0") + "Z"
        node.assign(text)
        node.annotations.append({"path": node.path, "kind": kind})
    elif kind in ("array", "object"):
        field = "length" if kind == "array" else "keys_base64"
        _object(descriptor, {"kind", field})
        if kind == "array":
            length = descriptor["length"]
            if type(length) is not int or length < 0:
                raise ValueError("native capture returned an invalid array length")
            container: dict[str, Any] | list[Any] = [None] * length
            keys: list[str] | range = range(length)
        else:
            encoded = descriptor["keys_base64"]
            if type(encoded) is not list:
                raise ValueError("native capture returned invalid object keys")
            keys = [_safe_text(key) for key in encoded]
            if len(set(keys)) != len(keys):
                raise ValueError("native capture returned duplicate object keys")
            container = dict.fromkeys(keys)
        node.assign(container)
        return [
            _Node(
                [*node.address, {"kind": "index" if kind == "array" else "key", "value": key}],
                [*node.path, key],
                node.annotations,
                container,
                key,
            )
            for key in keys
        ]
    elif kind == "record":
        _object(descriptor, {"kind", "table_base64"})
        table = _safe_text(descriptor["table_base64"])
        identifier: dict[str, Any] = {"value": None, "native_types": []}
        literal: dict[str, Any] = {"table": table, "id": identifier}
        node.assign(literal)
        node.annotations.append(
            {"path": node.path, "kind": "record", "table": table, "identifier": identifier}
        )
        # The identifier uses its own annotation root, avoiding overlapping
        # outer paths. The shared placeholder is replaced after traversal.
        return [
            _Node(
                [*node.address, {"kind": "record"}],
                [],
                identifier["native_types"],
                identifier,
                "value",
            )
        ]
    else:
        raise ValueError("unsupported native capture descriptor kind")
    return []


def _record_literals(value: object, annotations: list[dict[str, Any]]) -> None:
    for descriptor in annotations:
        if descriptor["kind"] != "record":
            continue
        identifier = descriptor["identifier"]
        _record_literals(identifier["value"], identifier["native_types"])
        current: Any = value
        path = descriptor["path"]
        for step in path:
            current = current[step]
        # The record JSON literal contains the exact identifier JSON, while
        # its descriptor holds independently typed identifier annotations.
        current["id"] = identifier["value"]


@dataclass(frozen=True, slots=True)
class PreparedArchiveNativeCapture:
    """Immutable selected native rows; no authorization or inventory claim."""

    envelope: PreparedArchiveNativeValue

    @property
    def native_sha256(self) -> str:
        return self.envelope.native_sha256

    @property
    def rows(self) -> object:
        return self.envelope.value


async def capture_archive_native_records(
    execute_query: SurrealExecute,
    *,
    url: str,
    record_ids: tuple[RecordID, ...],
) -> PreparedArchiveNativeCapture:
    """Capture exact ordered physical records without returning lossy raw SDK rows.

    No writes, retries, archived SQL, function installation or namespace selection occur.
    The caller must provide the trusted store-scoped executor and closed record
    inventory. Changes after the final successful read are outside this cut.
    """
    if type(record_ids) is not tuple or not record_ids:
        raise ValueError("native capture requires a nonempty tuple of physical RecordIDs")
    identities: set[object] = set()
    for record in record_ids:
        if (
            type(record) is not RecordID
            or type(record.table_name) is not str
            or record.table_name not in ARCHIVE_NATIVE_RECORD_TABLES
            or type(record.id) not in (str, int, list, dict, UUID)
        ):
            raise ValueError("unsupported operator archive table or physical record")
        try:
            identity = (record.table_name, _selector_identity(record.id))
        except (UnicodeError, RecursionError) as error:
            raise ValueError("invalid native record selector") from error
        if identity in identities:
            raise ValueError("native capture physical records must be unique")
        identities.add(identity)
    selected = copy.deepcopy(record_ids)
    return PreparedArchiveNativeCapture(
        await capture_archive_native_tree(
            execute_query, url=url, selection_sql=_CUT, parameters={"record_ids": list(selected)}
        )
    )


async def capture_archive_native_tree(
    execute_query: SurrealExecute,
    *,
    url: str,
    selection_sql: str,
    parameters: dict[str, object],
    preamble_sql: str = "",
) -> PreparedArchiveNativeValue:
    """Traverse a server-built native root without exposing lossy SDK values.

    The caller owns authorization, the read-only selection program and its
    transaction. Never supply selection SQL from an archive or public request.
    Selected-record capture and the qualified operator root share this codec.
    """
    describe = preamble_sql + "\n" + _DESCRIBE.replace(_CUT, selection_sql)
    verify = preamble_sql + "\n" + _VERIFY.replace(_CUT, selection_sql)
    holder: dict[str, Any] = {"value": None}
    annotations: list[dict[str, Any]] = []
    pending = [_Node([], [], annotations, holder, "value")]
    fingerprint: str | None = None
    while pending:
        response = _response(
            await execute_query(
                _sql(describe, url),
                **parameters,
                requests=[node.address for node in pending],
                expected=fingerprint,
            ),
            expected=fingerprint,
            fields={"fingerprint", "descriptors"},
        )
        fingerprint = response["fingerprint"]
        descriptors = response["descriptors"]
        if type(descriptors) is not list or len(descriptors) != len(pending):
            raise ValueError("native capture descriptor cardinality differs from requested paths")
        following: list[_Node] = []
        for node, descriptor in zip(pending, descriptors, strict=True):
            following.extend(_expand(node, descriptor))
        pending = following
    _record_literals(holder["value"], annotations)
    if fingerprint is None:
        raise ValueError("native capture did not produce evidence")
    envelope = prepare_archive_native_value(
        value=holder["value"], native_types=annotations, native_sha256=fingerprint
    )
    _response(
        await execute_query(
            _sql(verify, url),
            **parameters,
            expected=fingerprint,
            reconstructed=archive_native_value_parameters(envelope),
        ),
        expected=fingerprint,
        fields={"fingerprint"},
    )
    return envelope
