"""Changefeed consumer for raw capture enrichment."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import structlog

from sibyl.api.raw_capture_events import publish_raw_capture_changed
from sibyl.config import settings
from sibyl.jobs import queue as job_queue
from sibyl.persistence.surreal.content import surreal_content_client
from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.observability import elapsed_ms

log = structlog.get_logger()

RAW_CAPTURE_CHANGEFEED_CONSUMER = "raw_capture_enrichment"
RAW_CAPTURE_CHANGEFEED_TABLE = "raw_captures"
RAW_CAPTURE_CHANGEFEED_CURSOR_TABLE = "content_changefeed_cursors"
_MAX_PENDING_RAW_MEMORY_IDS = 500
_VECTOR_FIELD_NAMES = frozenset({"embedding", "embeddings", "vector", "vectors"})
_VECTOR_FIELD_SUFFIXES = ("_embedding", "_embeddings", "_vector", "_vectors")


@dataclass(frozen=True, slots=True)
class RawCaptureChangefeedCursor:
    organization_id: str
    consumer_name: str
    versionstamp: int = 0
    # Ids whose promotion enqueue failed on an earlier poll. They ride in the
    # cursor record so the next poll retries them instead of losing them.
    pending_raw_memory_ids: tuple[str, ...] = ()


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
        # Ids a previous poll could not enqueue come first, so a queue outage
        # delays promotion by a poll instead of dropping it.
        attempt_ids = _dedupe(cursor.pending_raw_memory_ids, raw_memory_ids)
        promotion_job_id: str | None = None
        enqueue_error: str | None = None
        pending_ids: list[str] = []
        dropped_ids = 0
        if attempt_ids:
            # An enqueue failure must not strand the cursor either. Leaving it
            # behind replays the same oldest changes on every poll forever,
            # which never delivers the batch and never advances, so the cursor
            # moves on and carries the undelivered ids instead.
            try:
                promotion_job_id = await queue_raw_capture_changes(
                    organization_id,
                    raw_memory_ids=attempt_ids,
                    rows_seen=len(rows),
                    previous_versionstamp=cursor.versionstamp,
                    next_versionstamp=next_versionstamp,
                )
            except Exception as exc:
                enqueue_error = type(exc).__name__
                pending_ids = attempt_ids[:_MAX_PENDING_RAW_MEMORY_IDS]
                dropped_ids = len(attempt_ids) - len(pending_ids)
                log.warning(
                    "raw_capture_changefeed_enqueue_failed",
                    organization_id=organization_id,
                    consumer_name=consumer_name,
                    error=str(exc),
                    error_type=enqueue_error,
                    raw_memory_count=len(attempt_ids),
                    pending_raw_memory_count=len(pending_ids),
                    dropped_raw_memory_count=dropped_ids,
                    previous_versionstamp=cursor.versionstamp,
                    next_versionstamp=next_versionstamp,
                )
        # Save when the feed advanced, and also when the carried set changed,
        # so a retry queue is never left behind by a quiet poll.
        if next_versionstamp > cursor.versionstamp or tuple(pending_ids) != tuple(
            cursor.pending_raw_memory_ids
        ):
            await _save_cursor(
                client,
                organization_id=organization_id,
                consumer_name=consumer_name,
                versionstamp=next_versionstamp,
                metadata=_cursor_metadata(
                    rows_seen=len(rows),
                    raw_memory_ids=raw_memory_ids,
                    promotion_job_id=promotion_job_id,
                    enqueue_error=enqueue_error,
                    pending_ids=pending_ids,
                    dropped_ids=dropped_ids,
                ),
            )

    status = _poll_status(
        rows_seen=len(rows),
        promotion_job_id=promotion_job_id,
        enqueue_error=enqueue_error,
    )
    result = {
        "organization_id": organization_id,
        "status": status,
        "rows_seen": len(rows),
        "changed_raw_memory_ids": raw_memory_ids,
        "attempted_raw_memory_ids": attempt_ids,
        "pending_raw_memory_ids": pending_ids,
        "dropped_raw_memory_count": dropped_ids,
        "promotion_job_id": promotion_job_id,
        "enqueue_error": enqueue_error,
        "previous_versionstamp": cursor.versionstamp,
        "next_versionstamp": next_versionstamp,
        "duration_ms": elapsed_ms(started_at),
    }
    log.info("raw_capture_changefeed_polled", **result)
    return result


def _poll_status(*, rows_seen: int, promotion_job_id: str | None, enqueue_error: str | None) -> str:
    if enqueue_error:
        return "enqueue_failed"
    if promotion_job_id:
        return "queued"
    return "advanced" if rows_seen else "idle"


def _cursor_metadata(
    *,
    rows_seen: int,
    raw_memory_ids: list[str],
    promotion_job_id: str | None,
    enqueue_error: str | None,
    pending_ids: list[str],
    dropped_ids: int,
) -> dict[str, object]:
    # The whole metadata object is replaced on every save, so anything that
    # must survive the next poll is written here every time.
    metadata: dict[str, object] = {
        "rows_seen": rows_seen,
        "raw_memory_count": len(raw_memory_ids),
        "promotion_job_id": promotion_job_id,
        "pending_raw_memory_ids": pending_ids,
    }
    if enqueue_error:
        metadata["enqueue_error"] = enqueue_error
    if dropped_ids:
        # Past the carry cap the ids are only recoverable from the log, so say
        # so in the record rather than leaving a silent gap.
        metadata["dropped_raw_memory_count"] = dropped_ids
    return metadata


def _dedupe(*id_groups: Iterable[object]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for group in id_groups:
        for value in group:
            text = _optional_str(value)
            if text is None or text in seen:
                continue
            seen.add(text)
            ordered.append(text)
    return ordered


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
    # SHOW CHANGES cannot project fields, so every change arrives with the
    # whole record, embedding vectors included. The consumer only needs ids,
    # so the vectors are dropped here, before anything holds or re-encodes
    # them.
    return [strip_vector_fields(dict(row)) for row in normalize_records(raw)]


def strip_vector_fields(value: Any) -> Any:
    """Return a copy of a change payload without its embedding vectors."""
    if isinstance(value, Mapping):
        return {
            str(key): strip_vector_fields(item)
            for key, item in value.items()
            if not _is_vector_field(str(key))
        }
    if isinstance(value, list | tuple):
        return [strip_vector_fields(item) for item in value]
    return value


def _is_vector_field(name: str) -> bool:
    lowered = name.lower()
    return lowered in _VECTOR_FIELD_NAMES or lowered.endswith(_VECTOR_FIELD_SUFFIXES)


async def _load_cursor(
    client: Any,
    *,
    organization_id: str,
    consumer_name: str,
) -> RawCaptureChangefeedCursor:
    rows = await _execute_records(
        client,
        """
        SELECT versionstamp, metadata FROM content_changefeed_cursors
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
        pending_raw_memory_ids=tuple(_pending_ids_from_metadata(rows[0].get("metadata"))),
    )


def _pending_ids_from_metadata(value: object) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    pending = value.get("pending_raw_memory_ids")
    if not isinstance(pending, list | tuple):
        return []
    return _dedupe(pending)


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


async def _safe_broadcast_raw_capture_changed(result: Mapping[str, object]) -> None:
    organization_id = _optional_str(result.get("organization_id"))
    raw_memory_ids = result.get("changed_raw_memory_ids")
    if not organization_id or not isinstance(raw_memory_ids, list):
        return
    await publish_raw_capture_changed(
        organization_id=organization_id,
        raw_memory_ids=raw_memory_ids,
        promotion_job_id=result.get("promotion_job_id"),
        rows_seen=result.get("rows_seen"),
        previous_versionstamp=result.get("previous_versionstamp"),
        next_versionstamp=result.get("next_versionstamp"),
    )


async def queue_raw_capture_changes(
    organization_id: str,
    *,
    raw_memory_ids: Iterable[object],
    rows_seen: object | None = None,
    previous_versionstamp: object | None = None,
    next_versionstamp: object | None = None,
) -> str | None:
    raw_memory_id_values = [str(raw_memory_id) for raw_memory_id in raw_memory_ids if raw_memory_id]
    if not organization_id or not raw_memory_id_values:
        return None

    promotion_job_id = await job_queue.enqueue_raw_promotion(
        organization_id,
        raw_memory_ids=raw_memory_id_values,
        limit=len(raw_memory_id_values),
    )
    await _safe_broadcast_raw_capture_changed(
        {
            "organization_id": organization_id,
            "changed_raw_memory_ids": raw_memory_id_values,
            "promotion_job_id": promotion_job_id,
            "rows_seen": rows_seen,
            "previous_versionstamp": previous_versionstamp,
            "next_versionstamp": next_versionstamp,
        }
    )
    return promotion_job_id


def _raw_capture_refs_for_org(
    rows: Iterable[Mapping[str, object]],
    *,
    organization_id: str,
) -> list[RawCaptureChangeRef]:
    refs: list[RawCaptureChangeRef] = []
    seen: set[str] = set()
    for row in rows:
        for payload in _change_payloads(row.get("changes")):
            ref = raw_capture_ref_from_payload(payload)
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


def raw_capture_ref_from_payload(payload: Mapping[str, object]) -> RawCaptureChangeRef | None:
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
    "queue_raw_capture_changes",
    "raw_capture_ref_from_payload",
    "strip_vector_fields",
]
