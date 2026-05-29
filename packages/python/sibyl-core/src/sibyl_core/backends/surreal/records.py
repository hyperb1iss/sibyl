"""Canonical SurrealDB result-normalization and value-coercion helpers.

These were historically copy-pasted into every persistence module. Keeping a
single source of truth closes a real drift hazard: divergent ``query_error``
copies once missed an error shape, so an errored write could be read as
success. Every persistence module imports from here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sibyl_core.backends.surreal.connection import SurrealQueryError

type SurrealRecord = dict[str, object]


def utcnow() -> datetime:
    """Return the current UTC time as a tz-naive datetime.

    SurrealDB columns in this codebase store naive UTC; mixing a tz-aware
    ``now()`` with values read back would raise on comparison, so the tzinfo
    is dropped deliberately.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def normalize_record(record: object) -> SurrealRecord | None:
    if not isinstance(record, dict):
        return None
    out = {str(key): value for key, value in record.items()}
    if "result" in out and ("status" in out or "time" in out):
        return None
    out.pop("id", None)
    return out


def normalize_records(result: object) -> list[SurrealRecord]:
    if result is None:
        return []
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        if "result" in payload and ("status" in payload or "time" in payload):
            return normalize_records(payload.get("result"))
        record = normalize_record(payload)
        return [record] if record is not None else []
    if not isinstance(result, list):
        return []

    records: list[SurrealRecord] = []
    for item in result:
        records.extend(normalize_records(item))
    return records


def query_error(result: object) -> str | None:
    """Return the error detail if a SurrealDB result reports an error, else None.

    Detects every error shape any caller might receive: a bare error string,
    a statement envelope with ``status == "ERR"``, a raw ``query_raw`` envelope
    whose ``result`` holds the per-statement list, and any list nesting of the
    above. The bare nested-result branch is load-bearing: without it an errored
    statement returned inside ``{"result": [...]}`` is silently read as success.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        if (
            "result" in payload
            and "status" not in payload
            and isinstance(payload.get("result"), list)
        ):
            return query_error(payload["result"])
        status = payload.get("status")
        if isinstance(status, str) and status.upper() == "ERR":
            detail = payload.get("detail") or payload.get("result") or payload
            return str(detail)
        return None
    if not isinstance(result, list):
        return None
    for item in result:
        error = query_error(item)
        if error is not None:
            return error
    return None


def raise_on_error(result: object, *, query: str = "") -> None:
    """Raise ``SurrealQueryError`` if ``result`` reports a SurrealDB error.

    Destructive call sites use this so an errored statement can never be
    mistaken for a successful write.
    """
    error = query_error(result)
    if error is not None:
        raise SurrealQueryError(query, error)


def coerce_uuid(value: object | None, *, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value:
        return UUID(value)
    msg = f"{field_name} is required"
    raise TypeError(msg)


def coerce_datetime(value: object | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    return None


__all__ = [
    "SurrealQueryError",
    "SurrealRecord",
    "coerce_datetime",
    "coerce_uuid",
    "normalize_record",
    "normalize_records",
    "query_error",
    "raise_on_error",
    "utcnow",
]
