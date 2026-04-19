"""Compatibility exports for callers still using the legacy graph module."""

from __future__ import annotations

from sibyl.persistence import graph_runtime as _runtime
from sibyl.persistence.graph_runtime import (
    ActiveGraphStore,
    GraphQueryAdapter,
    GraphReadServiceAdapter,
    GraphWriteServiceAdapter,
    LegacyEntityStore,
    LegacyGraphStore,
    LegacyKnowledgeReadAdapter,
    LegacyKnowledgeWriteAdapter,
    LegacyRelationshipStore,
    LegacySearchIndex,
    LegacyTaskRuntime,
    TaskGraphRuntime,
    ensure_graph_indexes,
    ensure_legacy_graph_indexes,
    execute_debug_query,
    execute_legacy_debug_query,
    get_entity_graph_runtime,
    get_graph_query_adapter,
    get_graph_stats_payload,
    get_knowledge_read_adapter,
    get_task_graph_runtime,
    graph_stats_payload,
    reset_graph_runtime,
    reset_legacy_graph_runtime,
    update_graph_entity,
    update_legacy_entity,
)
from sibyl_core.graph.client import GraphClient, get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.graph.relationships import RelationshipManager


class LegacyGraphQueryAdapter(_runtime.GraphQueryAdapter):
    """Compatibility adapter that preserves legacy patch points."""

    def __init__(self, client: GraphClient, group_id: str) -> None:
        self._client = client
        self._group_id = group_id
        self._driver = client.get_org_driver(group_id)
        self._entities = EntityManager(client, group_id=group_id)
        self._relationships = RelationshipManager(client, group_id=group_id)


async def get_legacy_knowledge_read_adapter(group_id: str) -> GraphReadServiceAdapter:
    return await _runtime.get_knowledge_read_adapter(group_id)


async def get_legacy_graph_query_adapter(group_id: str) -> LegacyGraphQueryAdapter:
    client = await get_graph_client()
    return LegacyGraphQueryAdapter(client, group_id)


async def get_legacy_task_runtime(group_id: str) -> TaskGraphRuntime:
    client = await get_graph_client()
    return TaskGraphRuntime(
        client=client,
        entity_manager=EntityManager(client, group_id=group_id),
        relationship_manager=RelationshipManager(client, group_id=group_id),
    )


async def get_legacy_entity_runtime(group_id: str) -> TaskGraphRuntime:
    return await get_legacy_task_runtime(group_id)


async def get_legacy_graph_stats_payload(group_id: str) -> dict[str, object]:
    service = await get_legacy_knowledge_read_adapter(group_id)
    stats = await service.stats()
    return graph_stats_payload(stats)

__all__ = [
    "ActiveGraphStore",
    "GraphQueryAdapter",
    "GraphReadServiceAdapter",
    "GraphWriteServiceAdapter",
    "LegacyEntityStore",
    "LegacyGraphQueryAdapter",
    "LegacyGraphStore",
    "LegacyKnowledgeReadAdapter",
    "LegacyKnowledgeWriteAdapter",
    "LegacyRelationshipStore",
    "LegacySearchIndex",
    "LegacyTaskRuntime",
    "TaskGraphRuntime",
    "ensure_graph_indexes",
    "ensure_legacy_graph_indexes",
    "execute_debug_query",
    "execute_legacy_debug_query",
    "get_entity_graph_runtime",
    "get_graph_query_adapter",
    "get_graph_stats_payload",
    "get_knowledge_read_adapter",
    "get_legacy_entity_runtime",
    "get_legacy_graph_query_adapter",
    "get_legacy_graph_stats_payload",
    "get_legacy_knowledge_read_adapter",
    "get_legacy_task_runtime",
    "get_task_graph_runtime",
    "graph_stats_payload",
    "reset_graph_runtime",
    "reset_legacy_graph_runtime",
    "update_graph_entity",
    "update_legacy_entity",
]
