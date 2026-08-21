"""Shared query boundary for native graph domain stores."""

from __future__ import annotations

from typing import Any, cast

from sibyl_core.backends.surreal.records import raise_on_error
from sibyl_core.services.graph_client import SurrealGraphClient

type SurrealRecord = dict[str, object]


async def select_one(
    client: SurrealGraphClient,
    query: str,
    **params: object,
) -> SurrealRecord | None:
    rows = normalize_graph_records(await client.execute_query(query, **params))
    return rows[0] if rows else None


async def execute_graph_transaction(
    client: SurrealGraphClient,
    query: str,
    **params: object,
) -> list[SurrealRecord]:
    execute_query_raw = getattr(client, "execute_query_raw", None)
    if callable(execute_query_raw):
        result = await cast("Any", execute_query_raw)(query, **params)
    else:
        result = await client.execute_query(query, **params)
    raise_on_error(result, query=query)
    return normalize_graph_records(result)


def _normalize_record(record: object) -> SurrealRecord | None:
    if not isinstance(record, dict):
        return None
    payload = {str(key): value for key, value in record.items()}
    if "result" in payload and ("status" in payload or "time" in payload):
        return None
    raw_id = payload.pop("id", None)
    if raw_id is not None and payload.get("record_id") is None:
        payload["record_id"] = raw_id
    if (
        raw_id is not None
        and payload.get("uuid") is None
        and payload.get("entity_id") is None
        and (text_id := _first_text(raw_id))
        and ":" not in text_id
    ):
        payload["uuid"] = text_id
    return payload


def normalize_graph_records(result: object) -> list[SurrealRecord]:
    """Normalize driver envelopes while preserving Surreal record identity."""

    if result is None:
        return []
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        if (
            "result" in payload
            and "status" not in payload
            and isinstance(payload.get("result"), list)
        ):
            return normalize_graph_records(payload["result"])
        if "result" in payload and ("status" in payload or "time" in payload):
            return normalize_graph_records(payload.get("result"))
        record = _normalize_record(payload)
        return [record] if record is not None else []
    if not isinstance(result, list):
        return []

    records: list[SurrealRecord] = []
    for item in result:
        records.extend(normalize_graph_records(item))
    return records


def _first_text(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


__all__ = ["SurrealRecord", "execute_graph_transaction", "normalize_graph_records", "select_one"]
