from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest

from sibyl.api.dependencies import get_graph_store, get_knowledge_read_service
from sibyl.api.routes.graph import get_graph_stats
from sibyl.persistence.graph_runtime import (
    ActiveGraphStore,
    GraphEntityStore,
    GraphQueryAdapter,
    GraphRelationshipStore,
    _surreal_driver_for,
    delete_graph_data,
    get_graph_stats_payload as get_active_graph_stats_payload,
)
from sibyl.persistence.legacy.graph import (
    LegacyEntityStore,
    LegacyGraphQueryAdapter,
    LegacyGraphStore,
    LegacyKnowledgeReadAdapter,
    LegacySearchIndex,
    get_legacy_entity_runtime,
    get_legacy_graph_query_adapter,
    get_legacy_graph_stats_payload,
    get_legacy_task_runtime,
    graph_stats_payload,
)
from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.backends.surreal.driver import SurrealQueryError
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.native_graph import NativeEntityManager, NativeRelationshipManager
from sibyl_core.storage import GraphStats, SearchFilters


def test_surreal_driver_detection_uses_declared_ops_only() -> None:
    declared_driver = SimpleNamespace(entity_node_ops=object(), execute_query=AsyncMock())
    edge_ops_only = SimpleNamespace(entity_edge_ops=object())

    assert _surreal_driver_for(declared_driver) is declared_driver
    assert _surreal_driver_for(edge_ops_only) is None
    assert _surreal_driver_for(MagicMock()) is None


@pytest.mark.asyncio
async def test_delete_graph_data_uses_surreal_graph_ops_when_available() -> None:
    graph_ops = MagicMock()
    graph_ops.clear_data = AsyncMock()
    driver = MagicMock()
    driver.graph_ops = graph_ops

    with (
        patch(
            "sibyl.persistence.graph_runtime._get_graph_runtime",
            AsyncMock(return_value=SimpleNamespace(client=driver)),
        ),
        patch("sibyl.persistence.graph_runtime._surreal_driver_for", return_value=driver),
    ):
        await delete_graph_data("org-1")

    graph_ops.clear_data.assert_awaited_once_with(driver, group_ids=["org-1"])
    driver.execute_query.assert_not_called()


@pytest.mark.asyncio
async def test_delete_graph_data_falls_back_to_surreal_table_deletes() -> None:
    from sibyl_core.backends.surreal.schema import GRAPH_EDGES, GRAPH_TABLES

    graph_ops = MagicMock()
    graph_ops.clear_data = AsyncMock(side_effect=RuntimeError("clear failed"))
    driver = MagicMock()
    driver.graph_ops = graph_ops
    driver.execute_query = AsyncMock()

    with (
        patch(
            "sibyl.persistence.graph_runtime._get_graph_runtime",
            AsyncMock(return_value=SimpleNamespace(client=driver)),
        ),
        patch("sibyl.persistence.graph_runtime._surreal_driver_for", return_value=driver),
    ):
        await delete_graph_data("org-1")

    graph_ops.clear_data.assert_awaited_once_with(driver, group_ids=["org-1"])
    assert driver.execute_query.await_count == len((*GRAPH_EDGES, *GRAPH_TABLES))
    queries = [call.args[0] for call in driver.execute_query.await_args_list]
    assert [
        query.removeprefix("DELETE FROM ").removesuffix(" WHERE group_id = $group_id;")
        for query in queries
    ] == [*GRAPH_EDGES, *GRAPH_TABLES]
    assert all(
        call.kwargs == {"group_id": "org-1"} for call in driver.execute_query.await_args_list
    )


@pytest.mark.asyncio
async def test_delete_graph_data_uses_legacy_write_for_non_surreal_driver() -> None:
    client = MagicMock()
    client.execute_write_org = AsyncMock()

    with (
        patch(
            "sibyl.persistence.graph_runtime._get_graph_runtime",
            AsyncMock(return_value=SimpleNamespace(client=client)),
        ),
        patch("sibyl.persistence.graph_runtime._surreal_driver_for", return_value=None),
    ):
        await delete_graph_data("org-1")

    client.execute_write_org.assert_awaited_once_with(
        "MATCH (n) DETACH DELETE n RETURN count(n) AS deleted",
        "org-1",
    )


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
async def test_legacy_search_index_aggregates_graph_stats_via_surreal_queries() -> None:
    driver = MagicMock()
    driver.execute_query = AsyncMock(
        side_effect=[
            [
                {"entity_type": "pattern", "cnt": 1},
                {"entity_type": "task", "cnt": 2},
            ],
            [
                {"relationship_type": "RELATED_TO", "cnt": 2},
                {"relationship_type": "DEPENDS_ON", "cnt": 1},
            ],
            [{"cnt": 4}],
            [{"cnt": 0}],
            [{"cnt": 1}],
            [{"cnt": 5}],
            [{"cnt": 0}],
            [{"cnt": 1}],
            [{"cnt": 2}],
        ]
    )
    client = MagicMock()
    client.get_org_driver.return_value = driver
    search = LegacySearchIndex(
        client,
        "org-1",
        LegacyEntityStore(MagicMock(), driver=driver, group_id="org-1"),
    )

    with patch("sibyl.persistence.graph_runtime._surreal_driver_for", return_value=object()):
        stats = await search.stats()

    assert stats.total_entities == 8
    assert stats.total_relationships == 11
    assert stats.entities_by_type == {"pattern": 1, "task": 2, "episode": 4, "saga": 1}
    assert stats.relationships_by_type == {
        "RELATED_TO": 2,
        "DEPENDS_ON": 1,
        "MENTIONS": 5,
        "NEXT_EPISODE": 1,
        "HAS_MEMBER": 2,
    }
    driver.execute_query.assert_has_awaits(
        [
            call(
                """
                    SELECT entity_type, count() AS cnt
                    FROM entity
                    WHERE group_id = $group_id
                    GROUP BY entity_type;
                    """,
                group_id="org-1",
            ),
            call(
                """
                    SELECT name AS relationship_type, count() AS cnt
                    FROM relates_to
                    WHERE group_id = $group_id
                    GROUP BY name;
                    """,
                group_id="org-1",
            ),
        ]
    )


@pytest.mark.asyncio
async def test_legacy_search_index_surreal_stats_treats_missing_tables_as_empty() -> None:
    driver = MagicMock()
    driver.execute_query = AsyncMock(
        side_effect=[
            [{"entity_type": "episode", "cnt": 1}],
            SurrealQueryError(
                "SELECT name AS relationship_type, count() AS cnt FROM relates_to",
                "The table 'relates_to' does not exist",
            ),
            [],
            [],
            [],
            SurrealQueryError(
                "SELECT count() AS cnt FROM mentions WHERE group_id = $group_id GROUP ALL;",
                "The table 'mentions' does not exist",
            ),
            [],
            [],
            [],
        ]
    )
    client = MagicMock()
    client.get_org_driver.return_value = driver
    search = LegacySearchIndex(
        client,
        "org-1",
        LegacyEntityStore(MagicMock(), driver=driver, group_id="org-1"),
    )

    with patch("sibyl.persistence.graph_runtime._surreal_driver_for", return_value=object()):
        stats = await search.stats()

    assert stats.total_entities == 1
    assert stats.total_relationships == 0
    assert stats.entities_by_type == {"episode": 1}
    assert stats.relationships_by_type == {}


@pytest.mark.asyncio
async def test_graph_entity_count_uses_surreal_select_when_driver_detected() -> None:
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[{"cnt": 7}])
    store = GraphEntityStore(MagicMock(), driver=driver, group_id="org-1")

    with patch("sibyl.persistence.graph_runtime._surreal_driver_for", return_value=object()):
        assert await store.count() == 7

    query = driver.execute_query.await_args.args[0]
    assert "FROM entity" in query
    assert "MATCH" not in query


@pytest.mark.asyncio
async def test_graph_relationship_count_uses_surreal_select_when_driver_detected() -> None:
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[{"cnt": 3}])
    store = GraphRelationshipStore(MagicMock(), driver=driver, group_id="org-1")

    with patch("sibyl.persistence.graph_runtime._surreal_driver_for", return_value=object()):
        assert await store.count() == 3

    query = driver.execute_query.await_args.args[0]
    assert "FROM relates_to" in query
    assert "MATCH" not in query


@pytest.mark.asyncio
async def test_active_graph_store_uses_native_managers_for_surreal_driver() -> None:
    driver = SurrealDriver("memory://").clone("org-1")
    client = MagicMock()
    client.get_org_driver.return_value = driver

    store = ActiveGraphStore.from_client(client, "org-1")

    assert isinstance(store.entities._manager, NativeEntityManager)
    assert isinstance(store.relationships._manager, NativeRelationshipManager)


@pytest.mark.asyncio
async def test_graph_relationship_get_uses_native_manager_for_surreal_driver() -> None:
    relationship = Relationship(
        id="rel-1",
        relationship_type=RelationshipType.BELONGS_TO,
        source_id="task-1",
        target_id="project-1",
    )
    driver = MagicMock()
    driver.execute_query = AsyncMock()
    relationships = AsyncMock()
    relationships.get = AsyncMock(return_value=relationship)
    store = GraphRelationshipStore(relationships, driver=driver, group_id="org-1")

    with (
        patch("sibyl.persistence.graph_runtime._surreal_driver_for", return_value=object()),
    ):
        result = await store.get("rel-1")

    assert result is relationship
    relationships.get.assert_awaited_once_with("rel-1")
    driver.execute_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_relationship_find_between_uses_native_manager_for_surreal_driver() -> None:
    relationship = Relationship(
        id="rel-1",
        relationship_type=RelationshipType.BELONGS_TO,
        source_id="task-1",
        target_id="project-1",
    )
    driver = MagicMock()
    driver.execute_query = AsyncMock()
    relationships = AsyncMock()
    relationships.find_between = AsyncMock(return_value=[relationship])
    store = GraphRelationshipStore(relationships, driver=driver, group_id="org-1")

    with (
        patch("sibyl.persistence.graph_runtime._surreal_driver_for", return_value=object()),
    ):
        result = await store.find_between(
            "task-1",
            "project-1",
            relationship_type=RelationshipType.BELONGS_TO,
        )

    assert result == [relationship]
    relationships.find_between.assert_awaited_once_with(
        "task-1",
        "project-1",
        relationship_type=RelationshipType.BELONGS_TO,
    )
    driver.execute_query.assert_not_awaited()


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
async def test_get_graph_store_scopes_to_org() -> None:
    org = MagicMock()
    org.id = uuid4()
    store = MagicMock(spec=LegacyGraphStore)

    with patch(
        "sibyl.persistence.graph_runtime.get_graph_store",
        AsyncMock(return_value=store),
    ) as factory:
        result = await get_graph_store(org=org)

    assert result is store
    factory.assert_awaited_once_with(str(org.id))


@pytest.mark.asyncio
async def test_get_knowledge_read_service_wraps_store() -> None:
    store = MagicMock(spec=LegacyGraphStore)

    service = await get_knowledge_read_service(graph_store=store)

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
async def test_active_graph_stats_payload_uses_entity_only_native_query() -> None:
    client = SimpleNamespace(
        execute_query=AsyncMock(
            return_value={
                "entity_types": [
                    {"entity_type": "task", "cnt": 3},
                    {"entity_type": "pattern", "cnt": 2},
                ],
                "episode_count": [{"cnt": 4}],
                "community_count": [{"cnt": 1}],
                "saga_count": [],
            }
        )
    )

    with patch(
        "sibyl.persistence.graph_runtime._get_graph_runtime",
        AsyncMock(return_value=SimpleNamespace(client=client)),
    ):
        payload = await get_active_graph_stats_payload("org-1")

    client.execute_query.assert_awaited_once()
    query = client.execute_query.await_args.args[0]
    assert "relates_to" not in query
    assert "mentions" not in query
    assert payload["total_entities"] == 10
    assert payload["entity_counts"]["task"] == 3
    assert payload["entity_counts"]["pattern"] == 2
    assert payload["entity_counts"]["episode"] == 4
    assert payload["entity_counts"]["community"] == 1


@pytest.mark.asyncio
async def test_get_legacy_graph_stats_payload_uses_read_adapter() -> None:
    stats = GraphStats(total_entities=5, entities_by_type={"task": 5})
    adapter = AsyncMock()
    adapter.stats.return_value = stats

    with patch(
        "sibyl.persistence.legacy.graph.get_legacy_knowledge_read_adapter",
        AsyncMock(return_value=adapter),
    ):
        payload = await get_legacy_graph_stats_payload("org-1")

    assert payload["total_entities"] == 5
    assert payload["entity_counts"]["task"] == 5


@pytest.mark.asyncio
async def test_legacy_graph_query_adapter_proxies_scoped_reads() -> None:
    client = MagicMock()
    client.execute_read_org = AsyncMock(return_value=[{"value": 1}])
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=[{"id": "node-1"}])
    client.get_org_driver.return_value = driver
    manager = AsyncMock()
    manager.list_by_type.return_value = ["task-1"]
    manager.get.return_value = "entity-1"
    manager.search.return_value = [("entity-2", 0.75)]
    relationships = AsyncMock()
    relationships.list_all.return_value = ["rel-1"]

    with (
        patch("sibyl.persistence.legacy.graph.EntityManager", return_value=manager),
        patch("sibyl.persistence.legacy.graph.RelationshipManager", return_value=relationships),
        patch(
            "sibyl.persistence.legacy.graph.GraphClient.normalize_result",
            return_value=[{"id": "node-1"}],
        ),
    ):
        adapter = LegacyGraphQueryAdapter(client, "org-1")
        entities = await adapter.list_entities_by_type(
            EntityType.TASK,
            limit=50,
            offset=10,
            project_id="proj-1",
        )
        entity = await adapter.get_entity("task-1")
        rels = await adapter.list_relationships(limit=25, offset=5)
        search_results = await adapter.search_entities("task query", limit=15)
        query_rows = await adapter.execute_query("RETURN n.uuid AS id")
        rows = await adapter.execute_read_org(
            "RETURN 1 AS value", now_iso="2026-04-17T00:00:00+00:00"
        )

    assert entities == ["task-1"]
    assert entity == "entity-1"
    assert rels == ["rel-1"]
    assert search_results == [("entity-2", 0.75)]
    assert query_rows == [{"id": "node-1"}]
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
    manager.get.assert_awaited_once_with("task-1")
    manager.search.assert_awaited_once_with("task query", entity_types=None, limit=15)
    relationships.list_all.assert_awaited_once_with(
        relationship_types=None,
        limit=25,
        offset=5,
    )
    driver.execute_query.assert_awaited_once_with("RETURN n.uuid AS id", group_id="org-1")
    client.execute_read_org.assert_awaited_once_with(
        "RETURN 1 AS value",
        "org-1",
        allow_surreal=False,
        group_id="org-1",
        now_iso="2026-04-17T00:00:00+00:00",
    )
    assert rows == [{"value": 1}]


@pytest.mark.asyncio
async def test_graph_query_adapter_refuses_raw_queries_on_surreal_driver() -> None:
    driver = SurrealDriver("memory://").clone("org-1")
    driver.execute_query = AsyncMock()
    client = MagicMock()
    client.get_org_driver.return_value = driver

    adapter = GraphQueryAdapter(client, "org-1")

    with pytest.raises(RuntimeError, match="native graph operations"):
        await adapter.execute_query("MATCH (n) RETURN n")

    driver.execute_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_graph_query_adapter_lists_entities_with_filtered_offset() -> None:
    task_1 = Entity(id="task-1", entity_type=EntityType.TASK, name="Task 1")
    project = Entity(id="project-1", entity_type=EntityType.PROJECT, name="Project 1")
    task_2 = Entity(id="task-2", entity_type=EntityType.TASK, name="Task 2")
    task_3 = Entity(id="task-3", entity_type=EntityType.TASK, name="Task 3")
    entity_manager = AsyncMock()
    entity_manager.list_all = AsyncMock(
        side_effect=[
            [task_1, project, task_2],
            [task_3],
        ]
    )
    relationships = AsyncMock()
    client = MagicMock()
    client.get_org_driver.return_value = MagicMock()

    with (
        patch("sibyl.persistence.legacy.graph.EntityManager", return_value=entity_manager),
        patch("sibyl.persistence.legacy.graph.RelationshipManager", return_value=relationships),
    ):
        adapter = LegacyGraphQueryAdapter(client, "org-1")
        result = await adapter.list_entities(
            entity_types=[EntityType.TASK],
            limit=2,
            offset=1,
            include_archived=True,
        )

    assert [entity.id for entity in result] == ["task-2", "task-3"]
    assert entity_manager.list_all.await_args_list[0].kwargs == {
        "limit": 200,
        "offset": 0,
        "include_archived": True,
    }


@pytest.mark.asyncio
async def test_legacy_graph_query_adapter_scopes_relationship_reads_to_entity_ids() -> None:
    matching = Relationship(
        id="rel-1",
        relationship_type=RelationshipType.BELONGS_TO,
        source_id="task-1",
        target_id="project-1",
    )
    filtered = Relationship(
        id="rel-2",
        relationship_type=RelationshipType.RELATED_TO,
        source_id="task-1",
        target_id="outside",
    )
    matching_second = Relationship(
        id="rel-3",
        relationship_type=RelationshipType.RELATED_TO,
        source_id="task-2",
        target_id="project-1",
    )

    async def list_all(
        *,
        relationship_types: list[RelationshipType] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Relationship]:
        del relationship_types, limit
        if offset == 0:
            return [matching, filtered]
        if offset == 2:
            return [matching_second]
        return []

    relationships = AsyncMock()
    relationships.list_all = AsyncMock(side_effect=list_all)
    client = MagicMock()
    client.get_org_driver.return_value = MagicMock()

    with (
        patch("sibyl.persistence.legacy.graph.EntityManager", return_value=AsyncMock()),
        patch("sibyl.persistence.legacy.graph.RelationshipManager", return_value=relationships),
    ):
        adapter = LegacyGraphQueryAdapter(client, "org-1")
        result = await adapter.list_relationships_for_entities(
            {"task-1", "task-2", "project-1"},
            limit=2,
            offset=0,
        )
        counts = await adapter.get_connection_counts(["task-1", "project-1", "task-2"])

    assert [relationship.id for relationship in result] == ["rel-1", "rel-3"]
    assert counts == {
        "task-1": 2,
        "project-1": 2,
        "task-2": 1,
    }


@pytest.mark.asyncio
async def test_graph_query_adapter_counts_connections_with_surreal_query() -> None:
    driver = MagicMock()
    driver.execute_query = AsyncMock(
        return_value=[
            {"source_id": "task-1", "target_id": "project-1"},
            {"source_id": "task-1", "target_id": "outside"},
            {"source_id": "task-2", "target_id": "project-1"},
            {"source_id": "task-2", "target_id": "task-2"},
        ]
    )
    relationships = AsyncMock()
    relationships.list_all = AsyncMock()
    client = MagicMock()
    client.get_org_driver.return_value = driver

    with (
        patch("sibyl.persistence.legacy.graph.EntityManager", return_value=AsyncMock()),
        patch("sibyl.persistence.legacy.graph.RelationshipManager", return_value=relationships),
        patch("sibyl.persistence.graph_runtime._surreal_driver_for", return_value=driver),
    ):
        adapter = LegacyGraphQueryAdapter(client, "org-1")
        counts = await adapter.get_connection_counts(
            ["task-1", "project-1", "task-2"],
            relationship_types=[RelationshipType.BELONGS_TO],
        )

    assert counts == {
        "task-1": 2,
        "project-1": 2,
        "task-2": 2,
    }
    relationships.list_all.assert_not_awaited()
    query = driver.execute_query.await_args.args[0]
    assert "FROM relates_to" in query
    assert "in.uuid IN $entity_ids OR out.uuid IN $entity_ids" in query
    assert "name IN $relationship_types" in query
    assert driver.execute_query.await_args.kwargs == {
        "group_id": "org-1",
        "entity_ids": ["project-1", "task-1", "task-2"],
        "relationship_types": ["BELONGS_TO"],
    }


@pytest.mark.asyncio
async def test_graph_query_adapter_lists_scoped_relationships_with_surreal_query() -> None:
    driver = MagicMock()
    driver.execute_query = AsyncMock(
        return_value=[
            {
                "record_id": "relates_to:rel-1",
                "uuid": "rel-1",
                "name": "BELONGS_TO",
                "fact": "Task belongs to project",
                "group_id": "org-1",
                "episodes": ["episode-1"],
                "attributes": {"confidence": 0.9, "source_ids": ["raw-1"]},
                "created_at": "2026-05-13T12:00:00+00:00",
                "expired_at": None,
                "valid_at": "2026-05-13T12:00:00+00:00",
                "invalid_at": None,
                "source_uuid": "task-1",
                "target_uuid": "project-1",
            }
        ]
    )
    relationships = AsyncMock()
    relationships.list_all = AsyncMock()
    client = MagicMock()
    client.get_org_driver.return_value = driver

    with (
        patch("sibyl.persistence.legacy.graph.EntityManager", return_value=AsyncMock()),
        patch("sibyl.persistence.legacy.graph.RelationshipManager", return_value=relationships),
        patch("sibyl.persistence.graph_runtime._surreal_driver_for", return_value=driver),
    ):
        adapter = LegacyGraphQueryAdapter(client, "org-1")
        result = await adapter.list_relationships_for_entities(
            {"task-1", "project-1"},
            relationship_types=[RelationshipType.BELONGS_TO],
            limit=25,
            offset=5,
        )

    relationships.list_all.assert_not_awaited()
    assert len(result) == 1
    assert result[0].id == "rel-1"
    assert result[0].source_id == "task-1"
    assert result[0].target_id == "project-1"
    assert result[0].relationship_type is RelationshipType.BELONGS_TO
    assert result[0].metadata["confidence"] == 0.9
    assert result[0].metadata["source_ids"] == ["raw-1"]
    assert result[0].metadata["fact"] == "Task belongs to project"
    assert result[0].metadata["group_id"] == "org-1"
    assert result[0].metadata["episodes"] == ["episode-1"]
    assert result[0].metadata["valid_at"] == "2026-05-13T12:00:00+00:00"
    assert result[0].metadata["record_id"] == "relates_to:rel-1"
    query = driver.execute_query.await_args.args[0]
    assert "FROM relates_to" in query
    assert "id AS record_id" in query
    assert "attributes" in query
    assert "valid_at" in query
    assert "in.uuid AS source_uuid" in query
    assert "out.uuid AS target_uuid" in query
    assert "in.uuid IN $entity_ids" in query
    assert "out.uuid IN $entity_ids" in query
    assert "name IN $relationship_types" in query
    assert "LIMIT $limit" in query
    assert "START $offset" in query
    assert driver.execute_query.await_args.kwargs == {
        "group_id": "org-1",
        "entity_ids": ["project-1", "task-1"],
        "relationship_types": ["BELONGS_TO"],
        "limit": 25,
        "offset": 5,
    }


@pytest.mark.asyncio
async def test_get_legacy_graph_query_adapter_uses_graph_client() -> None:
    client = MagicMock()

    with patch("sibyl.persistence.legacy.graph.get_graph_client", AsyncMock(return_value=client)):
        adapter = await get_legacy_graph_query_adapter("org-1")

    assert isinstance(adapter, LegacyGraphQueryAdapter)
    assert adapter._client is client
    assert adapter._driver is client.get_org_driver.return_value


@pytest.mark.asyncio
async def test_get_legacy_task_runtime_scopes_managers_to_org() -> None:
    client = MagicMock()
    entity_manager = MagicMock()
    relationship_manager = MagicMock()

    with (
        patch("sibyl.persistence.legacy.graph.get_graph_client", AsyncMock(return_value=client)),
        patch("sibyl.persistence.legacy.graph.EntityManager", return_value=entity_manager),
        patch(
            "sibyl.persistence.legacy.graph.RelationshipManager",
            return_value=relationship_manager,
        ),
    ):
        runtime = await get_legacy_task_runtime("org-1")

    assert runtime.client is client
    assert runtime.entity_manager is entity_manager
    assert runtime.relationship_manager is relationship_manager


@pytest.mark.asyncio
async def test_get_legacy_entity_runtime_reuses_task_runtime_factory() -> None:
    runtime = MagicMock()

    with patch(
        "sibyl.persistence.legacy.graph.get_legacy_task_runtime",
        AsyncMock(return_value=runtime),
    ):
        result = await get_legacy_entity_runtime("org-1")

    assert result is runtime
