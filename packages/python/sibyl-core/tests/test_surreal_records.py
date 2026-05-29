"""Unit tests for the shared SurrealDB record-coercion helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from sibyl_core.backends.surreal.connection import SurrealQueryError
from sibyl_core.backends.surreal.records import (
    coerce_datetime,
    coerce_uuid,
    normalize_record,
    normalize_records,
    query_error,
    raise_on_error,
    utcnow,
)


def test_utcnow_is_naive_utc() -> None:
    now = utcnow()
    assert now.tzinfo is None


def test_normalize_record_strips_id_and_rejects_envelopes() -> None:
    assert normalize_record({"id": "users:1", "name": "a"}) == {"name": "a"}
    assert normalize_record({"result": [], "status": "OK", "time": "1ms"}) is None
    assert normalize_record("nope") is None


def test_normalize_records_flattens_nested_statement_lists() -> None:
    assert normalize_records([[{"id": "x", "v": 1}], [{"id": "y", "v": 2}]]) == [
        {"v": 1},
        {"v": 2},
    ]


def test_normalize_records_unwraps_raw_query_envelope() -> None:
    raw = {"status": "OK", "time": "1ms", "result": [{"id": "users:1", "v": 9}]}
    assert normalize_records(raw) == [{"v": 9}]


def test_query_error_detects_status_err() -> None:
    err = {"status": "ERR", "detail": "boom"}
    assert query_error(err) == "boom"
    assert query_error([{"status": "OK", "result": []}, err]) == "boom"


def test_query_error_detects_bare_nested_result_error() -> None:
    """The drift the audit caught: an errored statement nested under a bare
    ``result`` list (no top-level ``status``) must still be detected."""
    nested = {"result": [{"status": "ERR", "detail": "nested boom"}]}
    assert query_error(nested) == "nested boom"


def test_query_error_returns_none_on_success() -> None:
    assert query_error([{"status": "OK", "result": [{"v": 1}]}]) is None
    assert query_error(None) is None


def test_raise_on_error_raises_for_nested_error_shape() -> None:
    nested = {"result": [{"status": "ERR", "detail": "nested boom"}]}
    with pytest.raises(SurrealQueryError):
        raise_on_error(nested, query="DELETE FROM x;")


def test_raise_on_error_noop_on_success() -> None:
    raise_on_error([{"status": "OK", "result": []}], query="DELETE FROM x;")


def test_coerce_uuid_accepts_uuid_and_str() -> None:
    value = UUID("11111111-1111-1111-1111-111111111111")
    assert coerce_uuid(value, field_name="x") == value
    assert coerce_uuid(str(value), field_name="x") == value
    with pytest.raises(TypeError):
        coerce_uuid(None, field_name="x")


def test_coerce_datetime_normalizes_to_naive_utc() -> None:
    aware = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    assert coerce_datetime(aware) == datetime(2026, 5, 28, 12, 0)
    assert coerce_datetime("2026-05-28T12:00:00Z") == datetime(2026, 5, 28, 12, 0)
    assert coerce_datetime(None) is None
