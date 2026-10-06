from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.backends.surreal.records import coerce_datetime, normalize_records
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.content_models import RawMemory, raw_memory_record
from sibyl_core.services.content_raw_persistence import replace_raw_memory_records_bulk
from sibyl_core.services.graph import EntityManager, SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.usage import (
    _GRAPH_ENTITY_STAMP_QUERY,
    _RAW_CAPTURE_STAMP_QUERY,
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
        if query.lstrip().startswith("RETURN $targets.map"):
            return [
                {
                    "item_kind": target["item_kind"],
                    "item_id": target["item_id"],
                    **self._stamp_for_item(
                        organization_id=str(target["organization_id"]),
                        item_kind=str(target["item_kind"]),
                        item_id=str(target["item_id"]),
                    ),
                }
                for target in cast("list[dict[str, object]]", params["targets"])
            ]
        if "UPDATE (SELECT VALUE id FROM raw_captures" in query:
            rows = []
            for stamp in cast("list[dict[str, object]]", params["stamps"]):
                item_id = str(stamp["item_id"])
                previous = self.raw_stamps.get(item_id, {})
                merged = _merge_stamp(previous, stamp)
                self.raw_stamps[item_id] = merged
                rows.append(
                    {
                        "uuid": item_id,
                        "organization_id": params["organization_id"],
                        **merged,
                    }
                )
            return rows
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
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> object:
        self.calls.append((query, params))
        if "UPDATE entity SET" not in query:
            raise AssertionError(f"unexpected graph query: {query}")
        rows = []
        for stamp in cast("list[dict[str, object]]", params["stamps"]):
            item_id = str(stamp["item_id"])
            merged = _merge_stamp(self.entity_stamps.get(item_id, {}), stamp)
            self.entity_stamps[item_id] = merged
            rows.append({"uuid": item_id, "group_id": params["organization_id"], **merged})
        return rows


def _merge_stamp(
    previous: dict[str, object],
    stamp: dict[str, object],
) -> dict[str, object]:
    """Mirror the monotonic merge the stamp statements perform."""
    return {
        "retrieval_count": max(
            int(previous.get("retrieval_count") or 0),
            int(stamp["retrieval_count"] or 0),
        ),
        "citation_count": max(
            int(previous.get("citation_count") or 0),
            int(stamp["citation_count"] or 0),
        ),
        "misled_count": max(
            int(previous.get("misled_count") or 0),
            int(stamp["misled_count"] or 0),
        ),
        "last_recalled_at": _max_datetime(
            coerce_datetime(previous.get("last_recalled_at")),
            coerce_datetime(stamp["last_recalled_at"]),
        ),
        "last_used_at": _max_datetime(
            coerce_datetime(previous.get("last_used_at")),
            coerce_datetime(stamp["last_used_at"]),
        ),
    }


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
async def test_record_memory_usage_stamps_a_batch_with_one_write_per_store() -> None:
    """K returned rows cost one event insert, one aggregate read, one write per store.

    The stamp statements leave revision alone: a stamp is telemetry derived
    from the events table, recomputed monotonically on every stamp, so it
    has nothing to announce to a writer's revision fence.
    """
    content_client = _FakeContentClient()
    graph_client = _FakeGraphClient()
    base = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    raw_ids = ["raw-a", "raw-b", "raw-c"]
    graph_ids = ["entity-a", "entity-b"]

    result = await record_memory_usage(
        content_client,
        [
            *(
                MemoryUsageEvent(
                    organization_id="org-a",
                    session_key="session-a",
                    message_key="message-a",
                    source_surface="search",
                    item_kind=MemoryUsageItemKind.RAW_CAPTURE,
                    item_id=item_id,
                    signal_type=MemoryUsageSignal.EXPOSURE,
                    event_at=base,
                )
                for item_id in raw_ids
            ),
            *(
                MemoryUsageEvent(
                    organization_id="org-a",
                    session_key="session-a",
                    message_key="message-a",
                    source_surface="search",
                    item_kind=MemoryUsageItemKind.GRAPH_ENTITY,
                    item_id=item_id,
                    signal_type=MemoryUsageSignal.EXPOSURE,
                    event_at=base,
                )
                for item_id in graph_ids
            ),
        ],
        graph_client=graph_client,
    )

    assert result.events_processed == 5
    assert {(stamp.item_kind, stamp.item_id) for stamp in result.stamps} == {
        *((MemoryUsageItemKind.RAW_CAPTURE, item_id) for item_id in raw_ids),
        *((MemoryUsageItemKind.GRAPH_ENTITY, item_id) for item_id in graph_ids),
    }
    assert all(stamp.retrieval_count == 1 for stamp in result.stamps)

    content_statements = [
        "insert"
        if query.lstrip().startswith("INSERT")
        else "aggregate"
        if query.lstrip().startswith("RETURN $targets.map")
        else "stamp"
        for query, _ in content_client.calls
    ]
    assert content_statements == ["insert", "aggregate", "stamp"], content_statements
    raw_write = content_client.calls[2]
    assert [stamp["item_id"] for stamp in raw_write[1]["stamps"]] == raw_ids
    assert len(graph_client.calls) == 1
    assert [stamp["item_id"] for stamp in graph_client.calls[0][1]["stamps"]] == graph_ids
    for query, _ in [*content_client.calls, *graph_client.calls]:
        assert "revision" not in query


def test_stamp_statements_are_single_block_statements() -> None:
    """One result on every engine: the server answers BEGIN/COMMIT per statement."""
    for query in (_RAW_CAPTURE_STAMP_QUERY, _GRAPH_ENTITY_STAMP_QUERY):
        stripped = query.strip()
        assert stripped.startswith("RETURN {")
        assert stripped.endswith("};")
        assert "BEGIN" not in stripped
        assert "COMMIT" not in stripped


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
        await replace_raw_memory_records_bulk(
            content_client,
            [
                raw_memory_record(
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


@pytest.mark.asyncio
async def test_graph_entity_stamp_leaves_revision_alone() -> None:
    """A stamp is telemetry, so it does not invalidate anyone's revision fence.

    Bumping revision on every recall turned reads into conflicting writes on
    the most popular rows and failed the expected_revision of whoever was
    editing one. The recall fields are derived from the events table and
    recomputed monotonically on every stamp, so a stale copy carried back by
    a writer is repaired by the next stamp instead; the snapshot fold covers
    its own window by never overwriting a key that exists when it writes.
    """
    organization_id = "org-usage-revision"
    graph_client = SurrealGraphClient(group_id=organization_id, url="memory://")
    base = datetime(2026, 8, 13, 12, 0, tzinfo=UTC).replace(tzinfo=None)

    try:
        await prepare_graph_schema(graph_client)
        manager = EntityManager(graph_client, group_id=organization_id)
        await manager.create_direct(
            Entity(
                id="entity-revision",
                entity_type=EntityType.TASK,
                name="Stamped usage entity",
                organization_id=organization_id,
                metadata={"status": "todo"},
            )
        )
        before = await _entity_revision(graph_client, organization_id, "entity-revision")

        await _stamp_graph_entity(
            graph_client,
            organization_id=organization_id,
            stamp=MemoryUsageStamp(
                item_kind=MemoryUsageItemKind.GRAPH_ENTITY,
                item_id="entity-revision",
                retrieval_count=7,
                citation_count=1,
                last_recalled_at=base,
                last_used_at=base,
            ),
        )

        after = await _entity_revision(graph_client, organization_id, "entity-revision")
        assert after == before
        rows = normalize_records(
            await graph_client.execute_query(
                """
                SELECT retrieval_count FROM entity
                WHERE group_id = $organization_id AND uuid = "entity-revision" LIMIT 1;
                """,
                organization_id=organization_id,
            )
        )
        assert rows[0]["retrieval_count"] == 7
    finally:
        await graph_client.close()


@pytest.mark.asyncio
async def test_raw_capture_stamp_leaves_revision_alone() -> None:
    """Same contract on the raw side, where expected_revision guards a full save.

    A full save carries the counters it read; the next stamp raises them back
    to the event-derived values, so the recall is never lost, while the save
    itself is no longer refused because someone recalled the row meanwhile.
    """
    organization_id = "org-usage-raw-revision"
    content_client = SurrealContentClient(url="memory://")
    base = datetime(2026, 8, 13, 12, 0, tzinfo=UTC).replace(tzinfo=None)

    try:
        await bootstrap_content_schema(content_client, reset=True)
        await content_client.execute_query(
            """
            CREATE raw_captures CONTENT {
                uuid: "raw-revision",
                organization_id: $organization_id,
                source_id: "source-revision",
                principal_id: "user-revision",
                title: "Raw revision target",
                raw_content: "raw revision target",
                tags: [],
                metadata: {},
                provenance: {},
                captured_at: $base,
                created_at: $base,
                revision: 1
            };
            """,
            organization_id=organization_id,
            base=base,
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
                    item_id="raw-revision",
                    signal_type=MemoryUsageSignal.EXPOSURE,
                    event_at=base,
                )
            ],
        )

        rows = normalize_records(
            await content_client.execute_query(
                """
                SELECT revision, retrieval_count FROM raw_captures
                WHERE organization_id = $organization_id AND uuid = "raw-revision"
                LIMIT 1;
                """,
                organization_id=organization_id,
            )
        )
        assert rows[0]["retrieval_count"] == 1
        assert rows[0]["revision"] == 1
    finally:
        await content_client.close()


async def _entity_revision(
    graph_client: SurrealGraphClient,
    organization_id: str,
    uuid: str,
) -> int:
    rows = normalize_records(
        await graph_client.execute_query(
            """
            SELECT revision FROM entity
            WHERE group_id = $organization_id AND uuid = $uuid
            LIMIT 1;
            """,
            organization_id=organization_id,
            uuid=uuid,
        )
    )
    return int(cast("int", rows[0]["revision"]))
