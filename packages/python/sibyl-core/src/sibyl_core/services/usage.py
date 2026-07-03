"""Usage feedback events and memory stamp maintenance."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

from sibyl_core.backends.surreal.records import coerce_datetime, normalize_records


class MemoryUsageSignal(StrEnum):
    EXPOSURE = "exposure"
    CITATION = "citation"


class MemoryUsageItemKind(StrEnum):
    GRAPH_ENTITY = "graph_entity"
    RAW_CAPTURE = "raw_capture"


class UsageContentClient(Protocol):
    async def execute_query(self, query: str, **params: object) -> object: ...


class UsageGraphClient(Protocol):
    async def execute_query(self, query: str, **params: object) -> object: ...


@dataclass(frozen=True, slots=True)
class MemoryUsageEvent:
    organization_id: str
    session_key: str
    message_key: str
    source_surface: str
    item_kind: MemoryUsageItemKind | str
    item_id: str
    signal_type: MemoryUsageSignal | str
    principal_id: str | None = None
    project_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    event_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MemoryUsageStamp:
    item_kind: MemoryUsageItemKind
    item_id: str
    retrieval_count: int
    citation_count: int
    last_recalled_at: datetime | None
    last_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class MemoryUsageWriteResult:
    events_processed: int
    stamps: tuple[MemoryUsageStamp, ...]


_EVENT_INSERT_QUERY = """
INSERT INTO memory_usage_events $rows ON DUPLICATE KEY UPDATE
    uuid = uuid,
    organization_id = organization_id,
    session_key = session_key,
    message_key = message_key,
    source_surface = source_surface,
    item_kind = item_kind,
    item_id = item_id,
    signal_type = signal_type,
    principal_id = principal_id,
    project_id = project_id,
    metadata = metadata,
    event_at = event_at,
    created_at = created_at;
"""

_RAW_CAPTURE_STAMP_QUERY = """
BEGIN TRANSACTION;
LET $exposure_events = (
    SELECT event_at
    FROM memory_usage_events
    WHERE organization_id = $organization_id
        AND item_kind = "raw_capture"
        AND item_id = $item_id
        AND signal_type = "exposure"
);
LET $citation_events = (
    SELECT event_at
    FROM memory_usage_events
    WHERE organization_id = $organization_id
        AND item_kind = "raw_capture"
        AND item_id = $item_id
        AND signal_type = "citation"
);
LET $last_recalled_at = (
    SELECT VALUE event_at
    FROM memory_usage_events
    WHERE organization_id = $organization_id
        AND item_kind = "raw_capture"
        AND item_id = $item_id
        AND signal_type = "exposure"
    ORDER BY event_at DESC
    LIMIT 1
)[0];
LET $last_used_at = (
    SELECT VALUE event_at
    FROM memory_usage_events
    WHERE organization_id = $organization_id
        AND item_kind = "raw_capture"
        AND item_id = $item_id
        AND signal_type = "citation"
    ORDER BY event_at DESC
    LIMIT 1
)[0];
LET $retrieval_count = array::len($exposure_events);
LET $citation_count = array::len($citation_events);
UPDATE raw_captures SET
    last_recalled_at = IF last_recalled_at != NONE
        AND ($last_recalled_at = NONE OR last_recalled_at > $last_recalled_at)
        THEN last_recalled_at ELSE $last_recalled_at END,
    last_used_at = IF last_used_at != NONE
        AND ($last_used_at = NONE OR last_used_at > $last_used_at)
        THEN last_used_at ELSE $last_used_at END,
    retrieval_count = math::max([retrieval_count ?? 0, $retrieval_count]),
    citation_count = math::max([citation_count ?? 0, $citation_count]),
    metadata.last_recalled_at = IF last_recalled_at != NONE
        AND ($last_recalled_at = NONE OR last_recalled_at > $last_recalled_at)
        THEN last_recalled_at ELSE $last_recalled_at END,
    metadata.last_used_at = IF last_used_at != NONE
        AND ($last_used_at = NONE OR last_used_at > $last_used_at)
        THEN last_used_at ELSE $last_used_at END,
    metadata.retrieval_count = math::max([retrieval_count ?? 0, $retrieval_count]),
    metadata.citation_count = math::max([citation_count ?? 0, $citation_count])
WHERE organization_id = $organization_id
    AND uuid = $item_id
RETURN AFTER;
COMMIT TRANSACTION;
"""

_EVENTS_FOR_ITEM_QUERY = """
SELECT signal_type, event_at
FROM memory_usage_events
WHERE organization_id = $organization_id
    AND item_kind = $item_kind
    AND item_id = $item_id;
"""

_GRAPH_ENTITY_STAMP_QUERY = """
UPDATE entity SET
    last_recalled_at = IF last_recalled_at != NONE
        AND ($last_recalled_at = NONE OR last_recalled_at > $last_recalled_at)
        THEN last_recalled_at ELSE $last_recalled_at END,
    last_used_at = IF last_used_at != NONE
        AND ($last_used_at = NONE OR last_used_at > $last_used_at)
        THEN last_used_at ELSE $last_used_at END,
    retrieval_count = math::max([retrieval_count ?? 0, $retrieval_count]),
    citation_count = math::max([citation_count ?? 0, $citation_count]),
    attributes.last_recalled_at = IF last_recalled_at != NONE
        AND ($last_recalled_at = NONE OR last_recalled_at > $last_recalled_at)
        THEN last_recalled_at ELSE $last_recalled_at END,
    attributes.last_used_at = IF last_used_at != NONE
        AND ($last_used_at = NONE OR last_used_at > $last_used_at)
        THEN last_used_at ELSE $last_used_at END,
    attributes.retrieval_count = math::max([retrieval_count ?? 0, $retrieval_count]),
    attributes.citation_count = math::max([citation_count ?? 0, $citation_count])
WHERE group_id = $organization_id
    AND uuid = $item_id
RETURN AFTER;
"""


async def record_memory_usage(
    content_client: UsageContentClient,
    events: Sequence[MemoryUsageEvent],
    *,
    graph_client: UsageGraphClient | None = None,
) -> MemoryUsageWriteResult:
    rows = list(_dedupe_event_records(_event_record(event) for event in events))
    if not rows:
        return MemoryUsageWriteResult(events_processed=0, stamps=())

    await content_client.execute_query(_EVENT_INSERT_QUERY, rows=rows)
    stamps: list[MemoryUsageStamp] = []
    for organization_id, item_kind, item_id in _unique_targets(rows):
        if item_kind is MemoryUsageItemKind.RAW_CAPTURE:
            stamp = await _stamp_raw_capture(
                content_client,
                organization_id=organization_id,
                item_id=item_id,
            )
            stamps.append(stamp)
        elif graph_client is not None:
            content_stamp = await _usage_stamp_for_item(
                content_client,
                organization_id=organization_id,
                item_kind=item_kind,
                item_id=item_id,
            )
            stamp = await _stamp_graph_entity(
                graph_client,
                organization_id=organization_id,
                stamp=content_stamp,
            )
            stamps.append(stamp)
        else:
            stamps.append(
                await _usage_stamp_for_item(
                    content_client,
                    organization_id=organization_id,
                    item_kind=item_kind,
                    item_id=item_id,
                )
            )

    return MemoryUsageWriteResult(events_processed=len(rows), stamps=tuple(stamps))


def _event_record(event: MemoryUsageEvent) -> dict[str, object]:
    organization_id = _required_text(event.organization_id, "organization_id")
    session_key = _required_text(event.session_key, "session_key")
    message_key = _required_text(event.message_key, "message_key")
    source_surface = _required_text(event.source_surface, "source_surface")
    item_kind = MemoryUsageItemKind(str(event.item_kind))
    item_id = _required_text(event.item_id, "item_id")
    signal_type = MemoryUsageSignal(str(event.signal_type))
    event_at = event.event_at or datetime.now(UTC)
    uuid = _usage_event_uuid(
        organization_id=organization_id,
        session_key=session_key,
        message_key=message_key,
        source_surface=source_surface,
        item_kind=item_kind.value,
        item_id=item_id,
        signal_type=signal_type.value,
    )
    return {
        "uuid": uuid,
        "organization_id": organization_id,
        "session_key": session_key,
        "message_key": message_key,
        "source_surface": source_surface,
        "item_kind": item_kind.value,
        "item_id": item_id,
        "signal_type": signal_type.value,
        "principal_id": _optional_text(event.principal_id),
        "project_id": _optional_text(event.project_id),
        "metadata": {str(key): value for key, value in dict(event.metadata).items()},
        "event_at": event_at,
        "created_at": event_at,
    }


async def _usage_stamp_for_item(
    content_client: UsageContentClient,
    *,
    organization_id: str,
    item_kind: MemoryUsageItemKind,
    item_id: str,
) -> MemoryUsageStamp:
    rows = normalize_records(
        await content_client.execute_query(
            _EVENTS_FOR_ITEM_QUERY,
            organization_id=organization_id,
            item_kind=item_kind.value,
            item_id=item_id,
        )
    )
    retrieval_count = 0
    citation_count = 0
    last_recalled_at: datetime | None = None
    last_used_at: datetime | None = None
    for row in rows:
        signal = str(row.get("signal_type") or "")
        event_at = coerce_datetime(row.get("event_at"))
        if signal == MemoryUsageSignal.EXPOSURE.value:
            retrieval_count += 1
            last_recalled_at = _max_datetime(last_recalled_at, event_at)
        elif signal == MemoryUsageSignal.CITATION.value:
            citation_count += 1
            last_used_at = _max_datetime(last_used_at, event_at)
    return MemoryUsageStamp(
        item_kind=item_kind,
        item_id=item_id,
        retrieval_count=retrieval_count,
        citation_count=citation_count,
        last_recalled_at=last_recalled_at,
        last_used_at=last_used_at,
    )


async def _stamp_raw_capture(
    content_client: UsageContentClient,
    *,
    organization_id: str,
    item_id: str,
) -> MemoryUsageStamp:
    return _stamp_from_rows(
        MemoryUsageItemKind.RAW_CAPTURE,
        item_id,
        await content_client.execute_query(
            _RAW_CAPTURE_STAMP_QUERY,
            organization_id=organization_id,
            item_id=item_id,
        ),
    )


async def _stamp_graph_entity(
    graph_client: UsageGraphClient,
    *,
    organization_id: str,
    stamp: MemoryUsageStamp,
) -> MemoryUsageStamp:
    return _stamp_from_rows(
        MemoryUsageItemKind.GRAPH_ENTITY,
        stamp.item_id,
        await graph_client.execute_query(
            _GRAPH_ENTITY_STAMP_QUERY,
            organization_id=organization_id,
            item_id=stamp.item_id,
            last_recalled_at=stamp.last_recalled_at,
            last_used_at=stamp.last_used_at,
            retrieval_count=stamp.retrieval_count,
            citation_count=stamp.citation_count,
        ),
    )


def _dedupe_event_records(rows: Iterable[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
    rows_by_uuid: dict[str, Mapping[str, object]] = {}
    for row in rows:
        rows_by_uuid.setdefault(str(row["uuid"]), row)
    return tuple(rows_by_uuid.values())


def _stamp_from_rows(
    item_kind: MemoryUsageItemKind,
    item_id: str,
    result: object,
) -> MemoryUsageStamp:
    rows = normalize_records(result)
    row = rows[0] if rows else {}
    return MemoryUsageStamp(
        item_kind=item_kind,
        item_id=item_id,
        retrieval_count=_int_count(row.get("retrieval_count")),
        citation_count=_int_count(row.get("citation_count")),
        last_recalled_at=coerce_datetime(row.get("last_recalled_at")),
        last_used_at=coerce_datetime(row.get("last_used_at")),
    )


def _unique_targets(
    rows: Iterable[Mapping[str, object]],
) -> tuple[tuple[str, MemoryUsageItemKind, str], ...]:
    seen: dict[tuple[str, MemoryUsageItemKind, str], None] = {}
    for row in rows:
        organization_id = str(row["organization_id"])
        item_kind = MemoryUsageItemKind(str(row["item_kind"]))
        item_id = str(row["item_id"])
        seen.setdefault((organization_id, item_kind, item_id), None)
    return tuple(seen.keys())


def _usage_event_uuid(
    *,
    organization_id: str,
    session_key: str,
    message_key: str,
    source_surface: str,
    item_kind: str,
    item_id: str,
    signal_type: str,
) -> str:
    payload = "\0".join(
        (
            organization_id,
            session_key,
            message_key,
            source_surface,
            item_kind,
            item_id,
            signal_type,
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _max_datetime(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate)


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _int_count(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    return int(text) if text else 0


__all__ = [
    "MemoryUsageEvent",
    "MemoryUsageItemKind",
    "MemoryUsageSignal",
    "MemoryUsageStamp",
    "MemoryUsageWriteResult",
    "record_memory_usage",
]
