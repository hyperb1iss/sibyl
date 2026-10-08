"""cbor2's C decoder must hand back exactly what the SDK's own decoder does."""

from __future__ import annotations

import builtins
import decimal
import uuid
from datetime import UTC, datetime
from typing import Any

import cbor2
import pytest
from surrealdb.data import cbor as sdk_cbor
from surrealdb.data.types.datetime import Datetime
from surrealdb.data.types.duration import Duration
from surrealdb.data.types.geometry import (
    GeometryCollection,
    GeometryLine,
    GeometryMultiLine,
    GeometryMultiPoint,
    GeometryMultiPolygon,
    GeometryPoint,
    GeometryPolygon,
)
from surrealdb.data.types.range import BoundExcluded, BoundIncluded, Range
from surrealdb.data.types.record_id import RecordID
from surrealdb.data.types.table import Table

from sibyl_core.backends.surreal import fast_cbor


@pytest.fixture
def sdk_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from the SDK's own decoder and put it back afterwards."""
    monkeypatch.setattr(sdk_cbor, "loads", sdk_cbor.loads)
    monkeypatch.setattr(fast_cbor, "_installed", False)


def _shape(value: Any) -> Any:
    """Types and values together, so a matching repr of another type still differs."""
    if isinstance(value, dict):
        return ("dict", sorted((_shape(k), _shape(v)) for k, v in value.items()))
    if isinstance(value, list | tuple):
        return (type(value).__name__, [_shape(v) for v in value])
    return (f"{type(value).__module__}.{type(value).__qualname__}", repr(value))


def _frames() -> list[bytes]:
    point = GeometryPoint(1.5, -2.25)
    line = GeometryLine(point, GeometryPoint(3.0, 4.0))
    corner, east, north = GeometryPoint(0.0, 0.0), GeometryPoint(4.0, 0.0), GeometryPoint(4.0, 3.0)
    ring = GeometryLine(corner, east, north, corner)
    polygon = GeometryPolygon(ring, ring)
    encoded = sdk_cbor.encode(
        {
            "none": None,
            "flags": [True, False],
            "numbers": [0, -1, 2**64, 1.25, float("inf")],
            "text": "héllo",
            "bytes": b"\x00\x01",
            "record": RecordID("entity", "abc123"),
            "record_complex": RecordID("entity", ["a", 1]),
            "record_object": RecordID("entity", {"org": "o", "path": ["x", [2, 3]]}),
            "table": Table("entity"),
            "decimal": decimal.Decimal("12.3400"),
            "duration": Duration.parse("1h30m"),
            "datetime": Datetime("2026-10-07T12:34:56.123456789Z"),
            "uuid": uuid.UUID("01961171-9df4-7efd-8533-019611719df4"),
            "range": Range(BoundIncluded(1), BoundExcluded(10)),
            "geometry": [
                point,
                line,
                polygon,
                GeometryMultiPoint(point, GeometryPoint(7.0, 8.0)),
                GeometryMultiLine(line, line),
                GeometryMultiPolygon(polygon, polygon),
                GeometryCollection(point, line),
            ],
            "nested": {"deep": [{"record": RecordID("relates_to", "x")}]},
        }
    )
    # The forms the server itself sends for datetimes, durations and uuids.
    server_forms = cbor2.dumps(
        {
            "compact_datetime": cbor2.CBORTag(12, [1_791_000_000, 123_456_789]),
            "compact_duration": cbor2.CBORTag(14, [5400, 7]),
            "spec_uuid": cbor2.CBORTag(37, uuid.UUID(int=7).bytes),
            "string_datetime": cbor2.CBORTag(0, "2026-10-07T12:34:56Z"),
            "none": cbor2.CBORTag(6, None),
        }
    )
    return [encoded, server_forms]


def test_c_decoder_matches_the_sdk_decoder_for_every_surreal_type(sdk_decoder) -> None:
    frames = _frames()
    expected = [sdk_cbor.decode(frame) for frame in frames]

    assert fast_cbor.install_fast_cbor() is True
    assert sdk_cbor.loads is not cbor2.loads
    actual = [sdk_cbor.decode(frame) for frame in frames]

    assert [_shape(value) for value in actual] == [_shape(value) for value in expected]
    assert actual[1]["compact_datetime"] == datetime.fromtimestamp(1_791_000_000, UTC).replace(
        microsecond=123456
    )


def test_install_is_idempotent(sdk_decoder) -> None:
    assert fast_cbor.install_fast_cbor() is True
    installed = sdk_cbor.loads
    assert fast_cbor.install_fast_cbor() is True
    assert sdk_cbor.loads is installed


def test_without_cbor2_the_sdk_keeps_its_own_decoder(
    sdk_decoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = sdk_cbor.loads
    real_import = builtins.__import__

    def refuse_cbor2(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "cbor2":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_cbor2)

    assert fast_cbor.install_fast_cbor() is False
    assert sdk_cbor.loads is original
