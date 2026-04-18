from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.api.dependencies import get_legacy_graph_store, get_legacy_knowledge_read_service
from sibyl.api.routes.graph import get_graph_stats
from sibyl.persistence.legacy.graph import (
    LegacyEntityStore,
    LegacyGraphQueryAdapter,
    LegacyGraphStore,
    LegacyKnowledgeReadAdapter,
    LegacySearchIndex,
    get_legacy_graph_query_adapter,
    get_legacy_graph_stats_payload,
    graph_stats_payload,
)
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.storage import GraphStats, SearchFilters


@pytest.mark.asyncio
async def test_legacy_search_index_filters_metadata_and_types() -> None:
    matching = Entity(
        id="entity-1",
        entity_type=EntityType.PATTERN,
        name="Nova Pattern",
        metadata={"tier": "gold"},
    )
    skipped = Entity(
        id="entity-2",
        entity_type=EntityType.PATTERN,
        name="Shadow Pattern",
        metadata={"tier": "silver"},
    )
    manager = MagicMock()
    manager.search = AsyncMock(return_value=[(matching, 0.9), (skipped, 0.4)])
    entity_store = LegacyEntityStore(manager, driver=MagicMock(), group_id="org-1")
    search = LegacySearchIndex(MagicMock(), "org-1", entity_store)

    results = await search.search(
        "nova",
        filters=SearchFilters(
            organization_id="org-1",
            entity_types=[EntityType.PATTERN],
            metadata={"tier": "gold"},
        ),
        limit=5,
    )

    assert [result.entity.id for result in results] == ["entity-1"]
    manager.search.assert_awaited_once_with("nova", entity_types=[EntityType.PATTERN], limit=5)


@pytest.mark.asyncio
async def test_legacy_search_index_aggregates_graph_stats() -> None:
    driver = MagicMock()
    driver.execute_query = AsyncMock(
        side_effect=[
            [
                {"entity_type": "pattern", "cnt": 2},
                {"entity_type": "task", "cnt": 3},
            ],
            [
                {"relationship_type": "RELATED_TO", "cnt": 4},
                {"relationship_type": "DEPENDS_ON", "cnt": 1},
            ],
        ]
    )
    client = MagicMock()
    client.get_org_driver.return_value = driver
    search = LegacySearchIndex(
        client,
        "org-1",
        LegacyEntityStore(MagicMock(), driver=MagicMock(), group_id="org-1"),
    )

    stats = await search.stats()

    assert stats.total_entities == 5
    assert stats.total_relationships == 5
    assert stats.entities_by_type == {"pattern": 2, "task": 3}
    assert stats.relationships_by_type == {"RELATED_TO": 4, "DEPENDS_ON": 1}
    client.get_org_driver.assert_called_once_with("org-1")


@pytest.mark.asyncio
async def test_legacy_knowledge_read_adapter_builds_entity_bundle() -> None:
    entity = Entity(id="entity-1", entity_type=EntityType.TASK, name="Ship it")
    related = Entity(id="entity-2", entity_type=EntityType.PROJECT, name="Sibyl")
    relationship = Relationship(
        id="rel-1",
        relationship_type=RelationshipType.BELONGS_TO,
        source_id="entity-1",
        target_id="entity-2",
    )
    store = MagicMock()
    store.entities.get = AsyncMock(return_value=entity)
    store.entities.get_many = AsyncMock(return_value=[related])
    store.relationships.list_for_entity = AsyncMock(return_value=[relationship])
    service = LegacyKnowledgeReadAdapter(store)

    bundle = await service.get_entity_bundle("entity-1")

    assert bundle is not None
    assert bundle.entity.id == "entity-1"
    assert [item.id for item in bundle.related_entities] == ["entity-2"]


@pytest.mark.asyncio
async def test_get_legacy_graph_store_scopes_to_org() -> None:
    org = MagicMock()
    org.id = uuid4()
    client = MagicMock()
    store = MagicMock(spec=LegacyGraphStore)

    with (
        patch("sibyl.api.dependencies.get_graph_client", return_value=client),
        patch("sibyl.api.dependencies.LegacyGraphStore.from_client", return_value=store) as factory,
    ):
        result = await get_legacy_graph_store(org=org)

    assert result is store
    factory.assert_called_once_with(client, str(org.id))


@pytest.mark.asyncio
async def test_get_legacy_knowledge_read_service_wraps_store() -> None:
    store = MagicMock(spec=LegacyGraphStore)

    service = await get_legacy_knowledge_read_service(graph_store=store)

    assert isinstance(service, LegacyKnowledgeReadAdapter)
    assert service._store is store


@pytest.mark.asyncio
async def test_get_graph_stats_maps_service_stats() -> None:
    service = AsyncMock()
    service.stats.return_value = GraphStats(
        total_entities=7,
        total_relationships=3,
        entities_by_type={"pattern": 4, "task": 3},
    )

    result = await get_graph_stats(service=service)

    assert result == {
        "total_nodes": 7,
        "total_edges": 3,
        "by_type": {"pattern": 4, "task": 3},
    }


def test_graph_stats_payload_initializes_missing_entity_types() -> None:
    payload = graph_stats_payload(
        GraphStats(
            total_entities=2,
            entities_by_type={"pattern": 2},
        )
    )

    assert payload["total_entities"] == 2
    entity_counts = payload["entity_counts"]
    assert entity_counts["pattern"] == 2
    assert entity_counts["task"] == 0


@pytest.mark.asyncio
async def test_get_legacy_graph_stats_payload_uses_read_adapter() -> None:
    stats = GraphStats(total_entities=5, entities_by_type={"task": 5})
    adapter = AsyncMock()
    adapter.stats.return_value = stats

    with patch("sibyl.persistence.legacy.graph.get_legacy_knowledge_read_adapter", AsyncMock(return_value=adapter)):
        payload = await get_legacy_graph_stats_payload("org-1")

    assert payload["total_entities"] == 5
    assert payload["entity_counts"]["task"] == 5


@pytest.mark.asyncio
async def test_legacy_graph_query_adapter_proxies_scoped_reads() -> None:
    client = AsyncMock()
    manager = AsyncMock()
    manager.list_by_type.return_value = ["task-1"]

    with patch("sibyl.persistence.legacy.graph.EntityManager", return_value=manager):
        adapter = LegacyGraphQueryAdapter(client, "org-1")
        entities = await adapter.list_entities_by_type(
            EntityType.TASK,
            limit=50,
            offset=10,
            project_id="proj-1",
        )
        rows = await adapter.execute_read_org("RETURN 1 AS value", now_iso="2026-04-17T00:00:00+00:00")

    assert entities == ["task-1"]
    manager.list_by_type.assert_awaited_once_with(
        EntityType.TASK,
        limit=50,
        offset=10,
        project_id="proj-1",
        epic_id=None,
        no_epic=False,
        status=None,
        priority=None,
        complexity=None,
        feature=None,
        tags=None,
        include_archived=False,
    )
    client.execute_read_org.assert_awaited_once_with(
        "RETURN 1 AS value",
        "org-1",
        group_id="org-1",
        now_iso="2026-04-17T00:00:00+00:00",
    )
    assert rows == client.execute_read_org.return_value


@pytest.mark.asyncio
async def test_get_legacy_graph_query_adapter_uses_graph_client() -> None:
    client = MagicMock()

    with patch("sibyl.persistence.legacy.graph.get_graph_client", AsyncMock(return_value=client)):
        adapter = await get_legacy_graph_query_adapter("org-1")

    assert isinstance(adapter, LegacyGraphQueryAdapter)
    assert adapter._client is client
