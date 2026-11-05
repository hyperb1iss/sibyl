"""Send datetimes and durations to SurrealDB in their numeric CBOR form.

SDK 2.0.0 writes every datetime as CBOR tag 0 (RFC 3339 text) and its
``Duration`` as tag 13 holding a [seconds, nanos] array. SurrealDB 4 refuses
both tags outright, and 3.x answers a tag 13 array, or datetime text it cannot
parse, with an error frame the SDK cannot route, which strands the socket.
Every server since 1.5 reads tag 12 and tag 14, each a [seconds, nanos] pair,
so this encoder sends those to every server version.

The SDK encodes each request through ``surrealdb.data.cbor.encode``, which
resolves ``dumps`` at call time, so replacing that one function covers every
transport, the embedded engine included. Installing is idempotent and must
happen before the first query; Sibyl does it wherever it opens an SDK
connection.

Replies need one repair too. SurrealDB leaves a duration's trailing zero parts
out, so a zero duration arrives as tag 14 over an empty array. SDK 2.0.0 reads
the seconds unconditionally and raises inside its receive loop, which strands
the socket; SurrealDB 4 sends one in the reply to a read-only ``begin``. The
SDK decodes through ``surrealdb.data.cbor.tag_decoder``, resolved at call time
as well, so the installer wraps it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from io import BytesIO
from typing import Any

from surrealdb.cbor import CBORDecoder, CBOREncoder, CBORTag
from surrealdb.data import cbor as sdk_cbor
from surrealdb.data.types.constants import TAG_DATETIME_COMPACT, TAG_DURATION_COMPACT
from surrealdb.data.types.datetime import Datetime
from surrealdb.data.types.duration import Duration

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NANOS_PER_SECOND = 1_000_000_000
# The datetime text SurrealDB 3.x accepted in tag 0: a date alone (midnight
# UTC), or a date and a full time with Z or a +HH:MM offset. A fraction past
# nine digits is cut to whole nanoseconds.
_DATETIME_TEXT = re.compile(
    r"([+-]?\d{4,})-(\d{2})-(\d{2})"
    r"(?:[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:[Zz]|([+-])(\d{2}):(\d{2})))?",
    re.ASCII,
)

_installed = False


def _epoch_parts(moment: datetime) -> list[int]:
    """Whole seconds since the epoch (floored) and the nanoseconds past them."""
    delta = moment - _EPOCH
    return [delta.days * 86_400 + delta.seconds, delta.microseconds * 1_000]


def _text_parts(text: str) -> list[int]:
    try:
        moment, fraction = _parse_text(text)
    except (ValueError, OverflowError):
        # datetime() raises OverflowError for a year past a C int.
        raise ValueError(f"SurrealDB cannot read {text!r} as a datetime") from None
    seconds, _ = _epoch_parts(moment)
    return [seconds, int(fraction[:9].ljust(9, "0"))]


def _parse_text(text: str) -> tuple[datetime, str]:
    match = _DATETIME_TEXT.fullmatch(text)
    if match is None:
        raise ValueError(text)
    year, month, day, hour, minute, second, fraction, sign, off_hour, off_minute = match.groups()
    offset = timedelta()
    if sign is not None:
        if int(off_minute) >= 60:
            raise ValueError(text)
        offset = timedelta(hours=int(off_hour), minutes=int(off_minute))
        if sign == "-":
            offset = -offset
    # datetime() refuses what the server did: day 30 of February, second 60,
    # years past 9999 (which SDK 2 could not decode back anyway).
    moment = datetime(
        int(year),
        int(month),
        int(day),
        int(hour or 0),
        int(minute or 0),
        int(second or 0),
        tzinfo=timezone(offset),
    )
    return moment, fraction or ""


def _encode_datetime(encoder: CBOREncoder, value: datetime) -> None:
    if value.utcoffset() is None:
        # The SDK names UTC as the zone for naive datetimes.
        value = value.replace(tzinfo=encoder.timezone)
    encoder.encode_semantic(CBORTag(TAG_DATETIME_COMPACT, _epoch_parts(value)))


def _encode_sdk_datetime(encoder: CBOREncoder, value: Datetime) -> None:
    # The SDK's wrapper carries text so it can hold nanoseconds.
    encoder.encode_semantic(CBORTag(TAG_DATETIME_COMPACT, _text_parts(value.dt)))


def _encode_duration(encoder: CBOREncoder, value: Duration) -> None:
    seconds, nanos = divmod(value.elapsed, _NANOS_PER_SECOND)
    if seconds < 0:
        raise ValueError(f"SurrealDB durations cannot be negative: {value!r}")
    encoder.encode_semantic(CBORTag(TAG_DURATION_COMPACT, [seconds, nanos]))


def _refuse_date(encoder: CBOREncoder, value: date) -> None:
    # cbor2 would send tag 1004, which neither 3.x nor 4 reads; 3.x strands
    # the socket over it. SurrealDB has no date type to map it to.
    raise TypeError(f"SurrealDB has no date type; bind a datetime instead of {value!r}")


_COMPACT_ENCODERS: dict[type | tuple[str, str], Callable[[CBOREncoder, Any], None]] = {
    datetime: _encode_datetime,
    date: _refuse_date,
    Datetime: _encode_sdk_datetime,
    Duration: _encode_duration,
}


class _CompactEncoder(CBOREncoder):
    __slots__ = ()

    def __init__(self, fp: BytesIO, **options: Any) -> None:
        super().__init__(fp, **options)
        # Type-table entries win over the SDK's default hook and cost nothing
        # per value; subclasses (pandas timestamps, archive datetimes) find
        # the datetime entry through the table's subclass lookup.
        self._encoders.update(_COMPACT_ENCODERS)


def dumps(obj: object, **options: Any) -> bytes:
    """The SDK's ``dumps``, with compact datetime and duration tags."""
    with BytesIO() as fp:
        _CompactEncoder(fp, **options).encode(obj)
        return fp.getvalue()


_sdk_tag_decoder = sdk_cbor.tag_decoder


def tag_decoder(decoder: CBORDecoder, tag: CBORTag, shareable_index: int | None = None) -> Any:
    """The SDK's tag decoder, reading a zero duration's empty array as zero."""
    # cbor2's C decoder hands arrays over as tuples; anything else malformed
    # still goes to the SDK, which refuses it as before.
    if tag.tag == TAG_DURATION_COMPACT and isinstance(tag.value, list | tuple) and not tag.value:
        return Duration(0)
    return _sdk_tag_decoder(decoder, tag, shareable_index)


def install_compact_cbor() -> None:
    """Route every SDK request through the compact encoder, and replies through its decoder."""
    global _installed
    if _installed:
        return
    # setattr: this deliberately rebinds the SDK module's own functions.
    setattr(sdk_cbor, "dumps", dumps)  # noqa: B010
    setattr(sdk_cbor, "tag_decoder", tag_decoder)  # noqa: B010
    _installed = True


__all__ = ["dumps", "install_compact_cbor", "tag_decoder"]
