from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.backends.surreal.records import coerce_datetime, normalize_records
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph import EntityManager, SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.surreal_content import (
    RawMemory,
    _raw_memory_record,
    _replace_raw_memory_records_bulk,
)
from sibyl_core.services.usage import (
    MemoryUsageEvent,
    MemoryUsageItemKind,
    MemoryUsageSignal,
    MemoryUsageStamp,
    _stamp_graph_entity,
    record_memory_usage,
)


class _FakeContentClient:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, object]] = {}
        self.raw_stamps: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> object:
        self.calls.append((query, params))
        if "INSERT INTO memory_usage_events" in query:
            for row in cast("list[dict[str, object]]", params["rows"]):
                self.events.setdefault(str(row["uuid"]), row)
            return list(self.events.values())
        if "UPDATE raw_captures SET" in query:
            stamp = self._stamp_for_item(
                organization_id=str(params["organization_id"]),
                item_kind=MemoryUsageItemKind.RAW_CAPTURE.value,
                item_id=str(params["item_id"]),
            )
            self.raw_stamps[str(params["item_id"])] = stamp
            return [stamp]
        if "FROM memory_usage_events" in query:
            return [
                row
                for row in self.events.values()
                if row["organization_id"] == params["organization_id"]
                and row["item_kind"] == params["item_kind"]
                and row["item_id"] == params["item_id"]
            ]
        raise AssertionError(f"unexpected content query: {query}")

    def _stamp_for_item(
        self,
        *,
        organization_id: str,
        item_kind: str,
        item_id: str,
    ) -> dict[str, object]:
        retrieval_count = 0
        citation_count = 0
        misled_count = 0
        last_recalled_at: datetime | None = None
        last_used_at: datetime | None = None
        for row in self.events.values():
            if (
                row["organization_id"] != organization_id
                or row["item_kind"] != item_kind
                or row["item_id"] != item_id
            ):
                continue
            event_at = coerce_datetime(row["event_at"])
            if row["signal_type"] == MemoryUsageSignal.EXPOSURE.value:
                retrieval_count += 1
                last_recalled_at = _max_datetime(last_recalled_at, event_at)
            elif row["signal_type"] == MemoryUsageSignal.CITATION.value:
                citation_count += 1
                last_used_at = _max_datetime(last_used_at, event_at)
            elif row["signal_type"] == MemoryUsageSignal.MISLED.value:
                misled_count += 1
        return {
            "retrieval_count": retrieval_count,
            "citation_count": citation_count,
            "misled_count": misled_count,
            "last_recalled_at": last_recalled_at,
            "last_used_at": last_used_at,
        }


class _FakeGraphClient:
    def __init__(self) -> None:
        self.entity_stamps: dict[str, dict[str, object]] = {}

    async def execute_query(self, query: str, **params: object) -> object:
        if "UPDATE entity SET" not in query:
            raise AssertionError(f"unexpected graph query: {query}")
        item_id = str(params["item_id"])
        previous = self.entity_stamps.get(item_id, {})
        stamp = {
            "retrieval_count": max(
                int(previous.get("retrieval_count") or 0),
                int(params["retrieval_count"] or 0),
            ),
            "citation_count": max(
                int(previous.get("citation_count") or 0),
                int(params["citation_count"] or 0),
            ),
            "misled_count": max(
                int(previous.get("misled_count") or 0),
                int(params["misled_count"] or 0),
            ),
            "last_recalled_at": _max_datetime(
                coerce_datetime(previous.get("last_recalled_at")),
                coerce_datetime(params["last_recalled_at"]),
            ),
            "last_used_at": _max_datetime(
                coerce_datetime(previous.get("last_used_at")),
                coerce_datetime(params["last_used_at"]),
            ),
        }
        self.entity_stamps[item_id] = stamp
        return [stamp]


def _max_datetime(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate)


@pytest.mark.asyncio
async def test_record_memory_usage_recomputes_stamps_from_unique_events() -> None:
    content_client = _FakeContentClient()
    graph_client = _FakeGraphClient()
    base = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)

    result = await record_memory_usage(
        content_client,
        [
            MemoryUsageEvent(
                organization_id="org-a",
                session_key="session-a",
                message_key="message-a",
                source_surface="context_pack",
                item_kind=MemoryUsageItemKind.RAW_CAPTURE,
                item_id="raw-a",
                signal_type=MemoryUsageSignal.EXPOSURE,
                event_at=base,
            ),
            MemoryUsageEvent(
                organization_id="org-a",
                session_key="session-a",
                message_key="message-a",
                source_surface="context_pack",
                item_kind=MemoryUsageItemKind.RAW_CAPTURE,
                item_id="raw-a",
                signal_type=MemoryUsageSignal.EXPOSURE,
                event_at=base + timedelta(minutes=10),
            ),
            MemoryUsageEvent(
                organization_id="org-a",
                session_key="session-a",
                message_key="message-b",
                source_surface="completion",
                item_kind=MemoryUsageItemKind.RAW_CAPTURE,
                item_id="raw-a",
                signal_type=MemoryUsageSignal.CITATION,
                event_at=base + timedelta(minutes=5),
            ),
            MemoryUsageEvent(
                organization_id="org-a",
                session_key="session-a",
                message_key="message-c",
                source_surface="completion",
                item_kind=MemoryUsageItemKind.RAW_CAPTURE,
                item_id="raw-a",
                signal_type=MemoryUsageSignal.MISLED,
                event_at=base + timedelta(minutes=7),
            ),
            MemoryUsageEvent(
                organization_id="org-a",
                session_key="session-a",
                message_key="message-d",
                source_surface="context_pack",
                item_kind=MemoryUsageItemKind.GRAPH_ENTITY,
                item_id="entity-a",
                signal_type=MemoryUsageSignal.EXPOSURE,
                event_at=base + timedelta(minutes=1),
            ),
        ],
        graph_client=graph_client,
    )

    assert result.events_processed == 4
    assert len(content_client.events) == 4
    raw_stamp = content_client.raw_stamps["raw-a"]
    assert raw_stamp["retrieval_count"] == 1
    assert raw_stamp["citation_count"] == 1
    assert raw_stamp["misled_count"] == 1
    assert raw_stamp["last_recalled_at"] == base.replace(tzinfo=None)
    assert raw_stamp["last_used_at"] == (base + timedelta(minutes=5)).replace(tzinfo=None)
    graph_stamp = graph_client.entity_stamps["entity-a"]
    assert graph_stamp["retrieval_count"] == 1
    assert graph_stamp["citation_count"] == 0
    assert graph_stamp["misled_count"] == 0
    assert graph_stamp["last_recalled_at"] == (base + timedelta(minutes=1)).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_record_memory_usage_rejects_missing_required_identity() -> None:
    content_client = _FakeContentClient()

    with pytest.raises(ValueError, match="session_key is required"):
        await record_memory_usage(
            content_client,
            [
                MemoryUsageEvent(
                    organization_id="org-a",
                    session_key="",
                    message_key="message-a",
                    source_surface="context_pack",
                    item_kind=MemoryUsageItemKind.RAW_CAPTURE,
                    item_id="raw-a",
                    signal_type=MemoryUsageSignal.EXPOSURE,
                )
            ],
        )

    assert content_client.calls == []


@pytest.mark.asyncio
async def test_record_memory_usage_persists_events_and_stamps_surreal_records() -> None:
    organization_id = "org-usage-integration"
    content_client = SurrealContentClient(url="memory://")
    graph_client = SurrealGraphClient(group_id=organization_id, url="memory://")
    base = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)

    try:
        await bootstrap_content_schema(content_client, reset=True)
        await prepare_graph_schema(graph_client)
        await content_client.execute_query(
            """
            CREATE raw_captures CONTENT {
                uuid: "raw-usage",
                organization_id: $organization_id,
                source_id: "source-usage",
                principal_id: "user-usage",
                title: "Usage target",
                raw_content: "usage integration target",
                tags: [],
                metadata: {},
                provenance: {},
                captured_at: $base,
                created_at: $base
            };
            """,
            organization_id=organization_id,
            base=base.replace(tzinfo=None),
        )
        manager = EntityManager(graph_client, group_id=organization_id)
        await manager.create_direct(
            Entity(
                id="entity-usage",
                entity_type=EntityType.TASK,
                name="Usage entity",
                organization_id=organization_id,
                metadata={"status": "todo"},
            )
        )

        await record_memory_usage(
            content_client,
            [
                MemoryUsageEvent(
                    organization_id=organization_id,
                    session_key="session-a",
                    message_key="message-a",
                    source_surface="context_pack",
                    item_kind=MemoryUsageItemKind.RAW_CAPTURE,
                    item_id="raw-usage",
                    signal_type=MemoryUsageSignal.EXPOSURE,
                    event_at=base,
                ),
                MemoryUsageEvent(
                    organization_id=organization_id,
                    session_key="session-a",
                    message_key="message-a",
                    source_surface="context_pack",
                    item_kind=MemoryUsageItemKind.RAW_CAPTURE,
                    item_id="raw-usage",
                    signal_type=MemoryUsageSignal.EXPOSURE,
                    event_at=base,
                ),
                MemoryUsageEvent(
                    organization_id=organization_id,
                    session_key="session-a",
                    message_key="message-b",
                    source_surface="completion",
                    item_kind=MemoryUsageItemKind.RAW_CAPTURE,
                    item_id="raw-usage",
                    signal_type=MemoryUsageSignal.CITATION,
                    event_at=base + timedelta(minutes=5),
                ),
                MemoryUsageEvent(
                    organization_id=organization_id,
                    session_key="session-a",
                    message_key="message-c",
                    source_surface="completion",
                    item_kind=MemoryUsageItemKind.RAW_CAPTURE,
                    item_id="raw-usage",
                    signal_type=MemoryUsageSignal.MISLED,
                    event_at=base + timedelta(minutes=7),
                ),
                MemoryUsageEvent(
                    organization_id=organization_id,
                    session_key="session-a",
                    message_key="message-d",
                    source_surface="context_pack",
                    item_kind=MemoryUsageItemKind.GRAPH_ENTITY,
                    item_id="entity-usage",
                    signal_type=MemoryUsageSignal.EXPOSURE,
                    event_at=base + timedelta(minutes=1),
                ),
                MemoryUsageEvent(
                    organization_id=organization_id,
                    session_key="session-a",
                    message_key="message-e",
                    source_surface="completion",
                    item_kind=MemoryUsageItemKind.GRAPH_ENTITY,
                    item_id="entity-usage",
                    signal_type=MemoryUsageSignal.CITATION,
                    event_at=base + timedelta(minutes=6),
                ),
                MemoryUsageEvent(
                    organization_id=organization_id,
                    session_key="session-a",
                    message_key="message-f",
                    source_surface="completion",
                    item_kind=MemoryUsageItemKind.GRAPH_ENTITY,
                    item_id="entity-usage",
                    signal_type=MemoryUsageSignal.MISLED,
                    event_at=base + timedelta(minutes=8),
                ),
            ],
            graph_client=graph_client,
        )
        await record_memory_usage(
            content_client,
            [
                MemoryUsageEvent(
                    organization_id=organization_id,
                    session_key="session-a",
                    message_key="message-a",
                    source_surface="context_pack",
                    item_kind=MemoryUsageItemKind.RAW_CAPTURE,
                    item_id="raw-usage",
                    signal_type=MemoryUsageSignal.EXPOSURE,
                    event_at=base + timedelta(hours=1),
                )
            ],
            graph_client=graph_client,
        )
        await _replace_raw_memory_records_bulk(
            content_client,
            [
                _raw_memory_record(
                    RawMemory(
                        id="raw-usage",
                        organization_id=organization_id,
                        source_id="source-rewrite",
                        principal_id="user-usage",
                        title="Usage target rewritten",
                        raw_content="usage integration target rewritten",
                        metadata={"fresh": True},
                        captured_at=base.replace(tzinfo=None),
                        created_at=base.replace(tzinfo=None),
                    )
                )
            ],
        )
        await manager.create_direct(
            Entity(
                id="entity-usage",
                entity_type=EntityType.TASK,
                name="Usage entity rewritten",
                organization_id=organization_id,
                metadata={"status": "done"},
            )
        )

        events = normalize_records(
            await content_client.execute_query(
                """
                SELECT uuid
                FROM memory_usage_events
                WHERE organization_id = $organization_id;
                """,
                organization_id=organization_id,
            )
        )
        raw_rows = normalize_records(
            await content_client.execute_query(
                """
                SELECT last_recalled_at, last_used_at, retrieval_count,
                    citation_count, misled_count, metadata
                FROM raw_captures
                WHERE organization_id = $organization_id AND uuid = "raw-usage"
                LIMIT 1;
                """,
                organization_id=organization_id,
            )
        )
        entity_rows = normalize_records(
            await graph_client.execute_query(
                """
                SELECT last_recalled_at, last_used_at, retrieval_count,
                    citation_count, misled_count, attributes
                FROM entity
                WHERE group_id = $organization_id AND uuid = "entity-usage"
                LIMIT 1;
                """,
                organization_id=organization_id,
            )
        )

        assert len(events) == 6
        assert raw_rows[0]["retrieval_count"] == 1
        assert raw_rows[0]["citation_count"] == 1
        assert raw_rows[0]["misled_count"] == 1
        assert coerce_datetime(raw_rows[0]["last_recalled_at"]) == base.replace(tzinfo=None)
        assert coerce_datetime(raw_rows[0]["last_used_at"]) == (
            base + timedelta(minutes=5)
        ).replace(tzinfo=None)
        raw_metadata = cast("dict[str, object]", raw_rows[0]["metadata"])
        assert raw_metadata["fresh"] is True
        assert raw_metadata["retrieval_count"] == 1
        assert raw_metadata["citation_count"] == 1
        assert raw_metadata["misled_count"] == 1
        assert coerce_datetime(raw_metadata["last_recalled_at"]) == base.replace(tzinfo=None)
        assert coerce_datetime(raw_metadata["last_used_at"]) == (
            base + timedelta(minutes=5)
        ).replace(tzinfo=None)

        assert entity_rows[0]["retrieval_count"] == 1
        assert entity_rows[0]["citation_count"] == 1
        assert entity_rows[0]["misled_count"] == 1
        assert coerce_datetime(entity_rows[0]["last_recalled_at"]) == (
            base + timedelta(minutes=1)
        ).replace(tzinfo=None)
        assert coerce_datetime(entity_rows[0]["last_used_at"]) == (
            base + timedelta(minutes=6)
        ).replace(tzinfo=None)
        attributes = cast("dict[str, object]", entity_rows[0]["attributes"])
        assert attributes["status"] == "done"
        assert attributes["retrieval_count"] == 1
        assert attributes["citation_count"] == 1
        assert attributes["misled_count"] == 1
        assert coerce_datetime(attributes["last_recalled_at"]) == (
            base + timedelta(minutes=1)
        ).replace(tzinfo=None)
        assert coerce_datetime(attributes["last_used_at"]) == (base + timedelta(minutes=6)).replace(
            tzinfo=None
        )
    finally:
        await content_client.close()
        await graph_client.close()


@pytest.mark.asyncio
async def test_graph_entity_stamp_update_is_monotonic() -> None:
    organization_id = "org-usage-monotonic"
    graph_client = SurrealGraphClient(group_id=organization_id, url="memory://")
    base = datetime(2026, 7, 3, 12, 0, tzinfo=UTC).replace(tzinfo=None)

    try:
        await prepare_graph_schema(graph_client)
        manager = EntityManager(graph_client, group_id=organization_id)
        await manager.create_direct(
            Entity(
                id="entity-monotonic",
                entity_type=EntityType.TASK,
                name="Monotonic usage entity",
                organization_id=organization_id,
                metadata={"status": "todo"},
            )
        )

        await _stamp_graph_entity(
            graph_client,
            organization_id=organization_id,
            stamp=MemoryUsageStamp(
                item_kind=MemoryUsageItemKind.GRAPH_ENTITY,
                item_id="entity-monotonic",
                retrieval_count=3,
                citation_count=2,
                last_recalled_at=base + timedelta(minutes=10),
                last_used_at=base + timedelta(minutes=11),
            ),
        )
        await _stamp_graph_entity(
            graph_client,
            organization_id=organization_id,
            stamp=MemoryUsageStamp(
                item_kind=MemoryUsageItemKind.GRAPH_ENTITY,
                item_id="entity-monotonic",
                retrieval_count=1,
                citation_count=1,
                last_recalled_at=base + timedelta(minutes=1),
                last_used_at=base + timedelta(minutes=2),
            ),
        )

        rows = normalize_records(
            await graph_client.execute_query(
                """
                SELECT last_recalled_at, last_used_at, retrieval_count,
                    citation_count, attributes
                FROM entity
                WHERE group_id = $organization_id AND uuid = "entity-monotonic"
                LIMIT 1;
                """,
                organization_id=organization_id,
            )
        )

        assert rows[0]["retrieval_count"] == 3
        assert rows[0]["citation_count"] == 2
        assert coerce_datetime(rows[0]["last_recalled_at"]) == base + timedelta(minutes=10)
        assert coerce_datetime(rows[0]["last_used_at"]) == base + timedelta(minutes=11)
        attributes = cast("dict[str, object]", rows[0]["attributes"])
        assert attributes["retrieval_count"] == 3
        assert attributes["citation_count"] == 2
        assert coerce_datetime(attributes["last_recalled_at"]) == base + timedelta(minutes=10)
        assert coerce_datetime(attributes["last_used_at"]) == base + timedelta(minutes=11)
    finally:
        await graph_client.close()


@pytest.mark.asyncio
async def test_raw_capture_stamp_update_is_monotonic() -> None:
    organization_id = "org-usage-raw-monotonic"
    content_client = SurrealContentClient(url="memory://")
    base = datetime(2026, 7, 3, 12, 0, tzinfo=UTC).replace(tzinfo=None)

    try:
        await bootstrap_content_schema(content_client, reset=True)
        await content_client.execute_query(
            """
            CREATE raw_captures CONTENT {
                uuid: "raw-monotonic",
                organization_id: $organization_id,
                source_id: "source-monotonic",
                principal_id: "user-monotonic",
                title: "Raw monotonic target",
                raw_content: "raw monotonic target",
                tags: [],
                metadata: {
                    last_recalled_at: $last_recalled_at,
                    last_used_at: $last_used_at,
                    retrieval_count: 3,
                    citation_count: 2
                },
                provenance: {},
                captured_at: $base,
                created_at: $base,
                last_recalled_at: $last_recalled_at,
                last_used_at: $last_used_at,
                retrieval_count: 3,
                citation_count: 2
            };
            """,
            organization_id=organization_id,
            base=base,
            last_recalled_at=base + timedelta(minutes=10),
            last_used_at=base + timedelta(minutes=11),
        )

        await record_memory_usage(
            content_client,
            [
                MemoryUsageEvent(
                    organization_id=organization_id,
                    session_key="session-a",
                    message_key="message-a",
                    source_surface="context_pack",
                    item_kind=MemoryUsageItemKind.RAW_CAPTURE,
                    item_id="raw-monotonic",
                    signal_type=MemoryUsageSignal.EXPOSURE,
                    event_at=base + timedelta(minutes=1),
                ),
                MemoryUsageEvent(
                    organization_id=organization_id,
                    session_key="session-a",
                    message_key="message-b",
                    source_surface="completion",
                    item_kind=MemoryUsageItemKind.RAW_CAPTURE,
                    item_id="raw-monotonic",
                    signal_type=MemoryUsageSignal.CITATION,
                    event_at=base + timedelta(minutes=2),
                ),
            ],
        )

        rows = normalize_records(
            await content_client.execute_query(
                """
                SELECT last_recalled_at, last_used_at, retrieval_count,
                    citation_count, metadata
                FROM raw_captures
                WHERE organization_id = $organization_id AND uuid = "raw-monotonic"
                LIMIT 1;
                """,
                organization_id=organization_id,
            )
        )

        assert rows[0]["retrieval_count"] == 3
        assert rows[0]["citation_count"] == 2
        assert coerce_datetime(rows[0]["last_recalled_at"]) == base + timedelta(minutes=10)
        assert coerce_datetime(rows[0]["last_used_at"]) == base + timedelta(minutes=11)
        metadata = cast("dict[str, object]", rows[0]["metadata"])
        assert metadata["retrieval_count"] == 3
        assert metadata["citation_count"] == 2
        assert coerce_datetime(metadata["last_recalled_at"]) == base + timedelta(minutes=10)
        assert coerce_datetime(metadata["last_used_at"]) == base + timedelta(minutes=11)
    finally:
        await content_client.close()
