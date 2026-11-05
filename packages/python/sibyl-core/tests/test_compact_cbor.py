"""Datetimes and durations leave the SDK as tag 12 and tag 14 [seconds, nanos] pairs."""

from __future__ import annotations

import calendar
import decimal
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import cbor2
import pytest
from surrealdb.cbor import dumps as sdk_dumps
from surrealdb.data import cbor as sdk_cbor
from surrealdb.data.types.datetime import Datetime
from surrealdb.data.types.duration import Duration
from surrealdb.data.types.record_id import RecordID

from sibyl_core.backends.surreal import compact_cbor, fast_cbor
from sibyl_core.migrate.source_integrity import ArchiveDatetime

NEW_YORK = ZoneInfo("America/New_York")


def _tag12(seconds: int, nanos: int) -> bytes:
    return cbor2.dumps(cbor2.CBORTag(12, [seconds, nanos]))


def _expected_parts(value: datetime) -> tuple[int, int]:
    """Epoch seconds and nanos computed a second way, through the UTC time tuple."""
    utc = value.replace(tzinfo=UTC) if value.utcoffset() is None else value
    return calendar.timegm(utc.utctimetuple()), utc.microsecond * 1000


@pytest.fixture
def sdk_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from the SDK's own encoder and put the compact one back afterwards."""
    monkeypatch.setattr(sdk_cbor, "dumps", sdk_dumps)
    monkeypatch.setattr(compact_cbor, "_installed", False)


def test_the_sdk_alone_sends_datetimes_as_text(sdk_encoder) -> None:
    # Why the shim exists: SurrealDB 4 refuses tag 0 and tag 13.
    assert sdk_cbor.encode(datetime(2026, 9, 30, tzinfo=UTC))[:1] == b"\xc0"
    assert sdk_cbor.encode(Duration(5))[:1] == b"\xcd"

    compact_cbor.install_compact_cbor()
    installed = sdk_cbor.dumps
    compact_cbor.install_compact_cbor()

    assert installed is compact_cbor.dumps
    assert sdk_cbor.dumps is installed
    assert sdk_cbor.encode(datetime(2026, 9, 30, tzinfo=UTC))[:1] == b"\xcc"


def test_a_datetime_is_tag_12_seconds_and_nanos_on_the_wire() -> None:
    encoded = sdk_cbor.encode(datetime(2026, 9, 30, 1, 2, 3, 123456, tzinfo=UTC))
    # tag 12, array of two, uint32 seconds, uint32 nanoseconds
    assert encoded.hex() == "cc821a6abc5f8b1a075bca00"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(datetime(2026, 9, 30, 1, 2, 3, 123456), id="naive-is-utc"),
        pytest.param(datetime(2026, 9, 30, 1, 2, 3, 123456, tzinfo=UTC), id="utc"),
        pytest.param(
            datetime(2026, 9, 30, 3, 2, 3, 1, tzinfo=timezone(timedelta(hours=2))), id="plus-2"
        ),
        pytest.param(
            datetime(2026, 9, 29, 19, 32, 3, tzinfo=timezone(-timedelta(hours=5, minutes=30))),
            id="minus-5-30",
        ),
        pytest.param(datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=NEW_YORK), id="zoneinfo-fold"),
        pytest.param(datetime(1970, 1, 1, tzinfo=UTC), id="epoch"),
        pytest.param(datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=UTC), id="pre-1970"),
        pytest.param(datetime(1900, 1, 1, 0, 0, 0, 1, tzinfo=UTC), id="1900"),
        pytest.param(datetime.min, id="min"),
        pytest.param(datetime.max, id="max"),
        pytest.param(datetime.max.replace(tzinfo=timezone(timedelta(hours=5))), id="max-plus-5"),
        pytest.param(ArchiveDatetime.parse("2026-09-30T01:02:03.123456789Z"), id="subclass"),
    ],
)
def test_datetimes_encode_their_exact_instant(value: datetime) -> None:
    assert sdk_cbor.encode(value) == _tag12(*_expected_parts(value))


def test_seconds_floor_so_nanos_stay_non_negative_before_1970() -> None:
    value = datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=UTC)
    assert cbor2.loads(sdk_cbor.encode(value)) == cbor2.CBORTag(12, (-1, 500_000_000))


@pytest.mark.parametrize(
    ("text", "seconds", "nanos"),
    [
        ("2026-09-30T01:02:03.123456789Z", 1_790_730_123, 123_456_789),
        ("2026-09-30T01:02:03.000000001Z", 1_790_730_123, 1),
        ("2026-09-30T01:02:03.5Z", 1_790_730_123, 500_000_000),
        ("2026-09-30T01:02:03Z", 1_790_730_123, 0),
        ("2026-09-30T03:02:03.5+02:00", 1_790_730_123, 500_000_000),
        ("2026-09-29T19:32:03-05:30", 1_790_730_123, 0),
        ("2026-09-30T01:02:03-00:00", 1_790_730_123, 0),
        ("2026-09-30t01:02:03z", 1_790_730_123, 0),
        ("2026-09-30 01:02:03Z", 1_790_730_123, 0),
        ("2026-09-30", 1_790_726_400, 0),
        ("2026-09-30T01:02:03.1234567899Z", 1_790_730_123, 123_456_789),
        ("1969-12-31T23:59:59.999999999Z", -1, 999_999_999),
    ],
)
def test_sdk_datetime_text_keeps_its_nanoseconds(text: str, seconds: int, nanos: int) -> None:
    assert sdk_cbor.encode(Datetime(text)) == _tag12(seconds, nanos)


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-30T01:02:03",  # a time with no zone
        "2026-09-30T01:02Z",  # no seconds
        "2026-09-30T01:02:03+0200",
        "2026-09-30T01:02:03+02:60",
        "2026-12-31T23:59:60Z",
        "2026-02-30T00:00:00Z",
        "+12026-09-30T01:02:03Z",  # past what Python's datetime can decode back
        "12147483647-01-01T00:00:00Z",  # a year past a C int: OverflowError
        "2026-09-30T01:02:03,5Z",
        "٢026-09-30T01:02:03Z",
        "not a date",
    ],
)
def test_datetime_text_the_server_would_not_read_fails_before_sending(text: str) -> None:
    with pytest.raises(ValueError, match="as a datetime"):
        sdk_cbor.encode({"cursor": Datetime(text)})


@pytest.mark.parametrize(
    ("value", "pair"),
    [
        (Duration.parse("1h30m"), [5400, 0]),
        (Duration(1), [0, 1]),
        (Duration(0), [0, 0]),
        # Past 2**53 ns, where the SDK's float split would round.
        (Duration(31_536_000 * 10**9 + 999_999_999), [31_536_000, 999_999_999]),
    ],
)
def test_durations_are_tag_14_seconds_and_nanos(value: Duration, pair: list[int]) -> None:
    assert sdk_cbor.encode(value) == cbor2.dumps(cbor2.CBORTag(14, pair))


def test_a_negative_duration_fails_before_sending() -> None:
    with pytest.raises(ValueError, match="negative"):
        sdk_cbor.encode(Duration(-1))


def test_a_date_fails_before_sending_and_timedelta_keeps_the_sdk_refusal() -> None:
    with pytest.raises(TypeError, match="no date type"):
        sdk_cbor.encode([date(2026, 9, 30)])
    with pytest.raises(BufferError):
        sdk_cbor.encode(timedelta(seconds=1))


def test_nested_values_and_record_id_parts_are_compact() -> None:
    at = datetime(2026, 9, 30, 1, 2, 3, 123456, tzinfo=UTC)
    value = {
        "rows": [at, {"deep": Datetime("2026-09-30T01:02:03.123456789Z")}],
        "id": RecordID("entity", [at, "x"]),
    }

    decoded = cbor2.loads(sdk_cbor.encode(value))

    # cbor2 hands back a tag's array as a tuple.
    pair = cbor2.CBORTag(12, (1_790_730_123, 123_456_000))
    assert decoded["rows"] == [pair, {"deep": cbor2.CBORTag(12, (1_790_730_123, 123_456_789))}]
    assert decoded["id"] == cbor2.CBORTag(8, ("entity", (pair, "x")))


@pytest.mark.parametrize(
    "value",
    [
        None,
        [1, -1, 2**64, 1.25, "héllo", b"\x00", True],
        decimal.Decimal("12.3400"),
        uuid.UUID("01961171-9df4-7efd-8533-019611719df4"),
        RecordID("entity", "abc"),
    ],
)
def test_every_other_value_encodes_as_the_sdk_does(value: Any) -> None:
    assert sdk_cbor.encode(value) == sdk_dumps(
        value, default=sdk_cbor.default_encoder, timezone=UTC
    )


@pytest.mark.parametrize("fast", [False, True], ids=["sdk-decoder", "c-decoder"])
def test_values_round_trip_through_the_decoders(fast: bool, monkeypatch) -> None:
    monkeypatch.setattr(sdk_cbor, "loads", sdk_cbor.loads)
    monkeypatch.setattr(fast_cbor, "_installed", False)
    if fast:
        assert fast_cbor.install_fast_cbor() is True
    aware = datetime(2026, 9, 30, 3, 2, 3, 123456, tzinfo=timezone(timedelta(hours=2)))
    values = {
        "naive": datetime(2026, 9, 30, 1, 2, 3, 123456),
        "aware": aware,
        "pre_1970": datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=UTC),
        "max": datetime.max,
        "nanos": Datetime("2026-09-30T01:02:03.123456789Z"),
        "duration": Duration(31_536_000 * 10**9 + 999_999_999),
    }

    decoded = sdk_cbor.decode(sdk_cbor.encode(values))

    assert decoded["naive"] == datetime(2026, 9, 30, 1, 2, 3, 123456, tzinfo=UTC)
    assert decoded["aware"] == aware
    assert decoded["aware"].tzinfo is UTC
    assert decoded["pre_1970"] == values["pre_1970"]
    assert decoded["max"] == datetime.max.replace(tzinfo=UTC)
    # Python datetimes hold microseconds; the server keeps all nine digits.
    assert decoded["nanos"] == datetime(2026, 9, 30, 1, 2, 3, 123456, tzinfo=UTC)
    assert decoded["duration"] == values["duration"]


def test_the_sdk_alone_cannot_read_a_zero_duration(monkeypatch) -> None:
    # Why the decoder is wrapped: the receive loop dies on this and strands the socket.
    monkeypatch.setattr(sdk_cbor, "tag_decoder", compact_cbor._sdk_tag_decoder)
    with pytest.raises(IndexError):
        sdk_cbor.decode(cbor2.dumps(cbor2.CBORTag(14, [])))


@pytest.mark.parametrize("fast", [False, True], ids=["sdk-decoder", "c-decoder"])
def test_durations_with_their_trailing_zero_parts_left_out_decode(fast: bool, monkeypatch) -> None:
    # SurrealDB 4's answer to a read-only begin, which carries a zero duration.
    monkeypatch.setattr(sdk_cbor, "loads", sdk_cbor.loads)
    monkeypatch.setattr(fast_cbor, "_installed", False)
    if fast:
        assert fast_cbor.install_fast_cbor() is True
    compact_cbor.install_compact_cbor()
    assert sdk_cbor.tag_decoder is compact_cbor.tag_decoder
    txn = uuid.UUID("01a1aa8d-7d6f-700f-9293-4564f1350f32")
    reply = cbor2.dumps(
        {
            "id": cbor2.CBORTag(37, txn.bytes),
            "isolation": "snapshot",
            "snapshot": {"age": cbor2.CBORTag(14, []), "position": cbor2.CBORTag(6, None)},
            "whole": cbor2.CBORTag(14, [5]),
            "both": cbor2.CBORTag(14, [5, 7]),
        }
    )

    decoded = sdk_cbor.decode(reply)

    assert decoded["id"] == txn
    assert decoded["snapshot"]["age"] == Duration(0)
    assert decoded["whole"] == Duration(5 * 10**9)
    assert decoded["both"] == Duration(5 * 10**9 + 7)
