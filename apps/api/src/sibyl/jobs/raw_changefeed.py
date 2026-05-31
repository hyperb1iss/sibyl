"""Changefeed consumer for raw capture enrichment."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import structlog

from sibyl.config import settings
from sibyl.jobs import queue as job_queue
from sibyl.persistence.surreal.content import surreal_content_client
from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.observability import elapsed_ms

log = structlog.get_logger()

RAW_CAPTURE_CHANGEFEED_CONSUMER = "raw_capture_enrichment"
RAW_CAPTURE_CHANGEFEED_TABLE = "raw_captures"
RAW_CAPTURE_CHANGEFEED_CURSOR_TABLE = "content_changefeed_cursors"


@dataclass(frozen=True, slots=True)
class RawCaptureChangefeedCursor:
    organization_id: str
    consumer_name: str
    versionstamp: int = 0


@dataclass(frozen=True, slots=True)
class RawCaptureChangeRef:
    raw_memory_id: str
    organization_id: str


async def poll_raw_capture_changefeed(
    ctx: dict[str, Any],  # noqa: ARG001
    organization_id: str,
    *,
    limit: int = 100,
    consumer_name: str = RAW_CAPTURE_CHANGEFEED_CONSUMER,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    if not settings.raw_capture_changefeed_poll_enabled:
        return {
            "organization_id": organization_id,
            "status": "disabled",
            "rows_seen": 0,
            "changed_raw_memory_ids": [],
            "duration_ms": elapsed_ms(started_at),
        }

    bounded_limit = max(int(limit), 1)
    async with surreal_content_client() as client:
        cursor = await _load_cursor(
            client,
            organization_id=organization_id,
            consumer_name=consumer_name,
        )
        rows = await _show_raw_capture_changes(
            client,
            since=cursor.versionstamp,
            limit=bounded_limit,
        )
        next_versionstamp = _last_versionstamp(rows, default=cursor.versionstamp)
        changed_refs = _raw_capture_refs_for_org(rows, organization_id=organization_id)
        raw_memory_ids = [ref.raw_memory_id for ref in changed_refs]
        promotion_job_id: str | None = None
        if raw_memory_ids:
            promotion_job_id = await job_queue.enqueue_raw_promotion(
                organization_id,
                raw_memory_ids=raw_memory_ids,
                limit=len(raw_memory_ids),
            )
        if next_versionstamp > cursor.versionstamp:
            await _save_cursor(
                client,
                organization_id=organization_id,
                consumer_name=consumer_name,
                versionstamp=next_versionstamp,
                metadata={
                    "rows_seen": len(rows),
                    "raw_memory_count": len(raw_memory_ids),
                    "promotion_job_id": promotion_job_id,
                },
            )

    status = "queued" if promotion_job_id else "advanced" if rows else "idle"
    result = {
        "organization_id": organization_id,
        "status": status,
        "rows_seen": len(rows),
        "changed_raw_memory_ids": raw_memory_ids,
        "promotion_job_id": promotion_job_id,
        "previous_versionstamp": cursor.versionstamp,
        "next_versionstamp": next_versionstamp,
        "duration_ms": elapsed_ms(started_at),
    }
    log.info("raw_capture_changefeed_polled", **result)
    return result


async def poll_all_raw_capture_changefeeds(
    ctx: dict[str, Any],
    *,
    limit: int = 100,
    organization_limit: int = 100,
) -> dict[str, Any]:
    if not settings.raw_capture_changefeed_poll_enabled:
        return {"status": "disabled", "organizations": 0, "results": []}

    async with surreal_content_client() as client:
        organization_ids = await _raw_capture_organization_ids(
            client,
            limit=max(int(organization_limit), 1),
        )
    results = [
        await poll_raw_capture_changefeed(ctx, organization_id, limit=limit)
        for organization_id in organization_ids
    ]
    return {
        "status": "ok",
        "organizations": len(organization_ids),
        "results": results,
    }


async def _show_raw_capture_changes(
    client: Any,
    *,
    since: int,
    limit: int,
) -> list[dict[str, object]]:
    raw = await client.execute_query_raw(
        f"SHOW CHANGES FOR TABLE {RAW_CAPTURE_CHANGEFEED_TABLE} SINCE {since} LIMIT $limit;",
        limit=limit,
    )
    return [dict(row) for row in normalize_records(raw)]


async def _load_cursor(
    client: Any,
    *,
    organization_id: str,
    consumer_name: str,
) -> RawCaptureChangefeedCursor:
    rows = await _execute_records(
        client,
        """
        SELECT versionstamp FROM content_changefeed_cursors
        WHERE organization_id = $organization_id
            AND table_name = $table_name
            AND consumer_name = $consumer_name
        LIMIT 1;
        """,
        organization_id=organization_id,
        table_name=RAW_CAPTURE_CHANGEFEED_TABLE,
        consumer_name=consumer_name,
    )
    if not rows:
        return RawCaptureChangefeedCursor(
            organization_id=organization_id,
            consumer_name=consumer_name,
        )
    return RawCaptureChangefeedCursor(
        organization_id=organization_id,
        consumer_name=consumer_name,
        versionstamp=_coerce_int(rows[0].get("versionstamp")),
    )


async def _save_cursor(
    client: Any,
    *,
    organization_id: str,
    consumer_name: str,
    versionstamp: int,
    metadata: dict[str, object],
) -> None:
    rows = await _execute_records(
        client,
        """
        UPDATE content_changefeed_cursors SET
            versionstamp = $versionstamp,
            metadata = $metadata,
            updated_at = time::now()
        WHERE organization_id = $organization_id
            AND table_name = $table_name
            AND consumer_name = $consumer_name;
        """,
        organization_id=organization_id,
        table_name=RAW_CAPTURE_CHANGEFEED_TABLE,
        consumer_name=consumer_name,
        versionstamp=versionstamp,
        metadata=metadata,
    )
    if rows:
        return
    await _execute_records(
        client,
        "CREATE content_changefeed_cursors CONTENT $record;",
        record={
            "uuid": _cursor_id(organization_id, consumer_name),
            "organization_id": organization_id,
            "table_name": RAW_CAPTURE_CHANGEFEED_TABLE,
            "consumer_name": consumer_name,
            "versionstamp": versionstamp,
            "metadata": metadata,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        },
    )


async def _raw_capture_organization_ids(client: Any, *, limit: int) -> list[str]:
    rows = await _execute_records(
        client,
        """
        SELECT organization_id FROM raw_captures
        WHERE organization_id != NONE AND organization_id != ''
        GROUP BY organization_id
        LIMIT $limit;
        """,
        limit=limit,
    )
    return [
        organization_id
        for organization_id in (_optional_str(row.get("organization_id")) for row in rows)
        if organization_id
    ]


async def _execute_records(client: Any, query: str, **params: object) -> list[dict[str, object]]:
    return [dict(row) for row in normalize_records(await client.execute_query(query, **params))]


def _raw_capture_refs_for_org(
    rows: Iterable[Mapping[str, object]],
    *,
    organization_id: str,
) -> list[RawCaptureChangeRef]:
    refs: list[RawCaptureChangeRef] = []
    seen: set[str] = set()
    for row in rows:
        for payload in _change_payloads(row.get("changes")):
            ref = _raw_capture_ref(payload)
            if ref is None or ref.organization_id != organization_id:
                continue
            if ref.raw_memory_id in seen:
                continue
            seen.add(ref.raw_memory_id)
            refs.append(ref)
    return refs


def _change_payloads(value: object) -> Iterable[Mapping[str, object]]:
    if isinstance(value, Mapping):
        for key in ("create", "update", "current"):
            payload = value.get(key)
            if isinstance(payload, Mapping):
                yield payload
        for item in value.values():
            if isinstance(item, Mapping | list | tuple):
                yield from _change_payloads(item)
        return
    if isinstance(value, list | tuple):
        for item in value:
            yield from _change_payloads(item)


def _raw_capture_ref(payload: Mapping[str, object]) -> RawCaptureChangeRef | None:
    organization_id = _optional_str(payload.get("organization_id"))
    raw_memory_id = _optional_str(payload.get("uuid")) or _raw_capture_uuid_from_record_id(
        payload.get("id")
    )
    if not organization_id or not raw_memory_id:
        return None
    return RawCaptureChangeRef(raw_memory_id=raw_memory_id, organization_id=organization_id)


def _last_versionstamp(rows: Iterable[Mapping[str, object]], *, default: int) -> int:
    versionstamp = default
    for row in rows:
        versionstamp = max(versionstamp, _coerce_int(row.get("versionstamp")))
    return versionstamp


def _raw_capture_uuid_from_record_id(value: object) -> str | None:
    text = _optional_str(value)
    if text is None or not text.startswith(f"{RAW_CAPTURE_CHANGEFEED_TABLE}:"):
        return None
    return text.split(":", 1)[1].strip("'\"`⟨⟩")


def _cursor_id(organization_id: str, consumer_name: str) -> str:
    digest = sha256(
        f"{organization_id}\0{RAW_CAPTURE_CHANGEFEED_TABLE}\0{consumer_name}".encode()
    ).hexdigest()
    return f"raw_capture_changefeed:{digest}"


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


__all__ = [
    "RAW_CAPTURE_CHANGEFEED_CONSUMER",
    "RAW_CAPTURE_CHANGEFEED_CURSOR_TABLE",
    "RAW_CAPTURE_CHANGEFEED_TABLE",
    "RawCaptureChangeRef",
    "RawCaptureChangefeedCursor",
    "poll_all_raw_capture_changefeeds",
    "poll_raw_capture_changefeed",
]
