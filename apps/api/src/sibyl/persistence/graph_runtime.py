"""Graph and search adapters backed by the active graph runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

from sibyl_core.errors import EntityNotFoundError
from sibyl_core.graph.client import GraphClient, get_graph_client, reset_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.graph.relationships import RelationshipManager
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import KnowledgeReadService, KnowledgeWriteService
from sibyl_core.storage import (
    EntityBundle,
    EntityPatch,
    EntityStore,
    GraphStats,
    GraphStore,
    Page,
    RelationshipPatch,
    RelationshipStore,
    SearchFilters,
    SearchHit,
    SearchIndex,
)

if TYPE_CHECKING:
    from sibyl_core.graph.communities import ClusterSummary, HierarchicalGraphData


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError:
        return 0


def _encode_next_cursor(offset: int, limit: int, item_count: int) -> str | None:
    if item_count < limit:
        return None
    return str(offset + item_count)


def _matches_metadata(metadata: dict[str, object], filters: dict[str, object]) -> bool:
    for key, expected in filters.items():
        actual = metadata.get(key)
        if isinstance(expected, str):
            if str(actual or "").lower() != expected.lower():
                return False
            continue
        if actual != expected:
            return False
    return True


def _coerce_float(value: object, *, default: float = 1.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float | str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _coerce_relationship(row: dict[str, object]) -> Relationship:
    relationship_name = str(row.get("rel_type") or row.get("name") or "RELATED_TO")
    try:
        relationship_type = RelationshipType(relationship_name)
    except ValueError:
        relationship_type = RelationshipType.RELATED_TO

    raw_metadata = row.get("metadata")
    metadata: dict[str, object]
    if isinstance(raw_metadata, dict):
        metadata = {str(key): value for key, value in raw_metadata.items()}
    else:
        metadata = {}

    return Relationship(
        id=str(row.get("id") or row.get("uuid") or ""),
        relationship_type=relationship_type,
        source_id=str(row.get("source_id") or ""),
        target_id=str(row.get("target_id") or ""),
        weight=_coerce_float(row.get("weight")),
        metadata=metadata,
    )


class LegacyEntityStore(EntityStore):
    """EntityStore backed by the current EntityManager."""

    def __init__(self, manager: EntityManager, *, driver: Any, group_id: str) -> None:
        self._manager = manager
        self._driver = driver
        self._group_id = group_id

    @classmethod
    def from_client(cls, client: GraphClient, group_id: str) -> Self:
        return cls(
            EntityManager(client, group_id=group_id),
            driver=client.get_org_driver(group_id),
            group_id=group_id,
        )

    async def get(self, entity_id: str) -> Entity | None:
        try:
            return await self._manager.get(entity_id)
        except EntityNotFoundError:
            return None

    async def get_many(self, entity_ids: list[str]) -> list[Entity]:
        entities = await asyncio.gather(*(self.get(entity_id) for entity_id in entity_ids))
        return [entity for entity in entities if entity is not None]

    async def upsert(self, entity: Entity) -> Entity:
        existing = await self.get(entity.id)
        if existing is None:
            created_id = await self._manager.create_direct(entity)
            created = await self.get(created_id)
            if created is None:
                msg = f"Entity was created but could not be reloaded: {created_id}"
                raise LookupError(msg)
            return created

        patch = EntityPatch(
            name=entity.name,
            description=entity.description,
            content=entity.content,
            metadata=dict(entity.metadata or {}),
            source_file=entity.source_file,
            embedding=entity.embedding,
            updated_at=entity.updated_at,
        )
        return await self.update(entity.id, patch)

    async def update(self, entity_id: str, patch: EntityPatch) -> Entity:
        updated = await self._manager.update(entity_id, patch.model_dump(exclude_none=True))
        if updated is None:
            raise EntityNotFoundError("Entity", entity_id)
        return updated

    async def delete(self, entity_id: str) -> bool:
        try:
            return await self._manager.delete(entity_id)
        except EntityNotFoundError:
            return False

    async def list_by_type(
        self,
        entity_type: EntityType,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[Entity]:
        offset = _decode_cursor(cursor)
        items = await self._manager.list_by_type(entity_type, limit=limit, offset=offset)
        return Page[Entity](
            items=items,
            next_cursor=_encode_next_cursor(offset, limit, len(items)),
        )

    async def find_by_name(self, name: str, *, exact: bool = False, limit: int = 20) -> list[Entity]:
        if exact:
            matches = await self._manager.search_exact_name(name, limit=limit)
        else:
            matches = await self._manager.search(name, limit=limit)
        return [entity for entity, _score in matches[:limit]]

    async def search_entities(
        self,
        query: str,
        *,
        entity_types: list[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        return await self._manager.search(query, entity_types=entity_types, limit=limit)

    async def count(self) -> int:
        rows = GraphClient.normalize_result(
            await self._driver.execute_query(
                """
                MATCH (n)
                WHERE n.group_id = $group_id AND n.entity_type IS NOT NULL
                RETURN count(n) AS cnt
                """,
                group_id=self._group_id,
            )
        )
        return int(rows[0].get("cnt", 0)) if rows else 0


class LegacyRelationshipStore(RelationshipStore):
    """RelationshipStore backed by the current RelationshipManager."""

    def __init__(self, manager: RelationshipManager, *, driver: Any, group_id: str) -> None:
        self._manager = manager
        self._driver = driver
        self._group_id = group_id

    @classmethod
    def from_client(cls, client: GraphClient, group_id: str) -> Self:
        return cls(
            RelationshipManager(client, group_id=group_id),
            driver=client.get_org_driver(group_id),
            group_id=group_id,
        )

    async def get(self, relationship_id: str) -> Relationship | None:
        rows = GraphClient.normalize_result(
            await self._driver.execute_query(
                """
                MATCH (source)-[r]->(target)
                WHERE r.group_id = $group_id AND r.uuid = $relationship_id
                RETURN r.uuid AS id,
                       source.uuid AS source_id,
                       target.uuid AS target_id,
                       type(r) AS rel_type,
                       COALESCE(r.weight, 1.0) AS weight,
                       COALESCE(r.attributes, {}) AS metadata
                LIMIT 1
                """,
                group_id=self._group_id,
                relationship_id=relationship_id,
            )
        )
        if not rows:
            return None
        return _coerce_relationship(rows[0])

    async def upsert(self, relationship: Relationship) -> Relationship:
        existing = await self.get(relationship.id)
        if existing is not None:
            patch = RelationshipPatch(weight=relationship.weight, metadata=relationship.metadata)
            await self._driver.execute_query(
                """
                MATCH ()-[r]->()
                WHERE r.group_id = $group_id AND r.uuid = $relationship_id
                SET r.weight = $weight,
                    r.attributes = $metadata
                RETURN r.uuid AS id
                """,
                group_id=self._group_id,
                relationship_id=relationship.id,
                weight=patch.weight,
                metadata=patch.metadata or {},
            )
            refreshed = await self.get(relationship.id)
            if refreshed is None:
                msg = f"Relationship not found after update: {relationship.id}"
                raise LookupError(msg)
            return refreshed

        created_id = await self._manager.create(relationship)
        created = await self.get(created_id)
        if created is None:
            return relationship.model_copy(update={"id": created_id})
        return created

    async def delete(self, relationship_id: str) -> bool:
        return await self._manager.delete(relationship_id)

    async def list_for_entity(
        self,
        entity_id: str,
        *,
        relationship_types: list[RelationshipType] | None = None,
    ) -> list[Relationship]:
        return await self._manager.get_for_entity(entity_id, relationship_types)

    async def find_between(
        self,
        source_id: str,
        target_id: str,
        *,
        relationship_type: RelationshipType | None = None,
    ) -> list[Relationship]:
        rows = GraphClient.normalize_result(
            await self._driver.execute_query(
                """
                MATCH (source {uuid: $source_id})-[r]-(target {uuid: $target_id})
                WHERE r.group_id = $group_id
                RETURN r.uuid AS id,
                       source.uuid AS source_id,
                       target.uuid AS target_id,
                       type(r) AS rel_type,
                       COALESCE(r.weight, 1.0) AS weight,
                       COALESCE(r.attributes, {}) AS metadata
                """,
                group_id=self._group_id,
                source_id=source_id,
                target_id=target_id,
            )
        )
        relationships = [_coerce_relationship(row) for row in rows]
        if relationship_type is None:
            return relationships
        return [rel for rel in relationships if rel.relationship_type == relationship_type]

    async def count(self) -> int:
        rows = GraphClient.normalize_result(
            await self._driver.execute_query(
                """
                MATCH ()-[r]->()
                WHERE r.group_id = $group_id
                RETURN count(r) AS cnt
                """,
                group_id=self._group_id,
            )
        )
        return int(rows[0].get("cnt", 0)) if rows else 0


class LegacySearchIndex(SearchIndex):
    """SearchIndex backed by the current entity search implementation."""

    def __init__(self, client: GraphClient, group_id: str, entities: LegacyEntityStore) -> None:
        self._client = client
        self._group_id = group_id
        self._entities = entities

    @classmethod
    def from_client(cls, client: GraphClient, group_id: str, entities: LegacyEntityStore) -> Self:
        return cls(client, group_id, entities)

    async def search(
        self,
        query: str,
        *,
        filters: SearchFilters | None = None,
        limit: int = 10,
    ) -> list[SearchHit]:
        if filters and filters.organization_id and filters.organization_id != self._group_id:
            return []

        entity_types = list(filters.entity_types) if filters and filters.entity_types else None
        results = await self._entities.search_entities(
            query,
            entity_types=entity_types,
            limit=limit,
        )

        hits = [
            SearchHit(entity=entity, score=score)
            for entity, score in results
            if not filters or _matches_metadata(entity.metadata, filters.metadata)
        ]
        return hits[:limit]

    async def stats(self) -> GraphStats:
        driver = self._client.get_org_driver(self._group_id)
        node_rows = GraphClient.normalize_result(
            await driver.execute_query(
                """
                MATCH (n)
                WHERE n.group_id = $group_id AND n.entity_type IS NOT NULL
                RETURN n.entity_type AS entity_type, count(*) AS cnt
                """,
                group_id=self._group_id,
            )
        )
        relationship_rows = GraphClient.normalize_result(
            await driver.execute_query(
                """
                MATCH ()-[r]->()
                WHERE r.group_id = $group_id
                RETURN type(r) AS relationship_type, count(*) AS cnt
                """,
                group_id=self._group_id,
            )
        )

        entities_by_type = {
            str(row.get("entity_type") or "unknown"): int(row.get("cnt", 0)) for row in node_rows
        }
        relationships_by_type = {
            str(row.get("relationship_type") or "RELATED_TO"): int(row.get("cnt", 0))
            for row in relationship_rows
        }

        return GraphStats(
            total_entities=sum(entities_by_type.values()),
            total_relationships=sum(relationships_by_type.values()),
            entities_by_type=entities_by_type,
            relationships_by_type=relationships_by_type,
        )


class ActiveGraphStore(GraphStore):
    """GraphStore backed by the current graph runtime."""

    def __init__(
        self,
        *,
        entities: LegacyEntityStore,
        relationships: LegacyRelationshipStore,
        search: LegacySearchIndex,
    ) -> None:
        self._entities = entities
        self._relationships = relationships
        self._search = search

    @classmethod
    def from_client(cls, client: GraphClient, group_id: str) -> Self:
        entities = LegacyEntityStore.from_client(client, group_id)
        relationships = LegacyRelationshipStore.from_client(client, group_id)
        return cls(
            entities=entities,
            relationships=relationships,
            search=LegacySearchIndex.from_client(client, group_id, entities),
        )

    @property
    def entities(self) -> LegacyEntityStore:
        return self._entities

    @property
    def relationships(self) -> LegacyRelationshipStore:
        return self._relationships

    @property
    def search(self) -> LegacySearchIndex:
        return self._search


class GraphReadServiceAdapter(KnowledgeReadService):
    """KnowledgeReadService backed by the current graph runtime."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    @classmethod
    def from_client(cls, client: GraphClient, group_id: str) -> Self:
        return cls(ActiveGraphStore.from_client(client, group_id))

    async def get_entity(self, entity_id: str) -> Entity | None:
        return await self._store.entities.get(entity_id)

    async def get_entity_bundle(self, entity_id: str) -> EntityBundle | None:
        entity = await self.get_entity(entity_id)
        if entity is None:
            return None

        relationships = await self._store.relationships.list_for_entity(entity_id)
        related_ids = list(_collect_related_ids(entity_id, relationships))
        related_entities = await self._store.entities.get_many(related_ids)
        return EntityBundle(
            entity=entity,
            relationships=relationships,
            related_entities=related_entities,
        )

    async def list_entities(
        self,
        entity_type: EntityType,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[Entity]:
        return await self._store.entities.list_by_type(entity_type, limit=limit, cursor=cursor)

    async def search(
        self,
        query: str,
        *,
        filters: SearchFilters | None = None,
        limit: int = 10,
    ) -> list[SearchHit]:
        return await self._store.search.search(query, filters=filters, limit=limit)

    async def get_related(self, entity_id: str) -> list[Relationship]:
        return await self._store.relationships.list_for_entity(entity_id)

    async def stats(self) -> GraphStats:
        return await self._store.search.stats()


class GraphWriteServiceAdapter(KnowledgeWriteService):
    """KnowledgeWriteService backed by the current graph runtime."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    @classmethod
    def from_client(cls, client: GraphClient, group_id: str) -> Self:
        return cls(ActiveGraphStore.from_client(client, group_id))

    async def upsert_entity(self, entity: Entity) -> Entity:
        return await self._store.entities.upsert(entity)

    async def upsert_relationship(self, relationship: Relationship) -> Relationship:
        return await self._store.relationships.upsert(relationship)

    async def delete_entity(self, entity_id: str) -> bool:
        return await self._store.entities.delete(entity_id)

    async def delete_relationship(self, relationship_id: str) -> bool:
        return await self._store.relationships.delete(relationship_id)


@dataclass(frozen=True, slots=True)
class TaskGraphRuntime:
    """Scoped graph runtime for task routes on the active backend."""

    client: GraphClient
    entity_manager: EntityManager
    relationship_manager: RelationshipManager


class GraphQueryAdapter:
    """Thin graph query surface for routes that still need runtime reads."""

    def __init__(self, client: GraphClient, group_id: str) -> None:
        self._client = client
        self._group_id = group_id
        self._driver = client.get_org_driver(group_id)
        self._entities = EntityManager(client, group_id=group_id)
        self._relationships = RelationshipManager(client, group_id=group_id)

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        result = await self._driver.execute_query(query, group_id=self._group_id, **params)
        return GraphClient.normalize_result(result)

    async def list_entities_by_type(
        self,
        entity_type: EntityType,
        *,
        limit: int = 100,
        offset: int = 0,
        project_id: str | None = None,
        epic_id: str | None = None,
        no_epic: bool = False,
        status: str | None = None,
        priority: str | None = None,
        complexity: str | None = None,
        feature: str | None = None,
        tags: list[str] | None = None,
        include_archived: bool = False,
    ) -> list[Entity]:
        return await self._entities.list_by_type(
            entity_type,
            limit=limit,
            offset=offset,
            project_id=project_id,
            epic_id=epic_id,
            no_epic=no_epic,
            status=status,
            priority=priority,
            complexity=complexity,
            feature=feature,
            tags=tags,
            include_archived=include_archived,
        )

    async def get_entity(self, entity_id: str) -> Entity | None:
        try:
            return await self._entities.get(entity_id)
        except EntityNotFoundError:
            return None

    async def list_relationships(
        self,
        *,
        relationship_types: list[RelationshipType] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Relationship]:
        return await self._relationships.list_all(
            relationship_types=relationship_types,
            limit=limit,
            offset=offset,
        )

    async def list_entities(
        self,
        *,
        entity_types: list[EntityType] | None = None,
        limit: int = 100,
        offset: int = 0,
        include_archived: bool = False,
    ) -> list[Entity]:
        allowed_types = set(entity_types or [])
        remaining_offset = max(offset, 0)
        page_offset = 0
        page_size = max(200, min(max(limit, 1) * 2, 1000))
        entities: list[Entity] = []

        while len(entities) < limit:
            batch = await self._entities.list_all(
                limit=page_size,
                offset=page_offset,
                include_archived=include_archived,
            )
            if not batch:
                break

            page_offset += len(batch)
            for entity in batch:
                if allowed_types and entity.entity_type not in allowed_types:
                    continue
                if remaining_offset:
                    remaining_offset -= 1
                    continue
                entities.append(entity)
                if len(entities) >= limit:
                    break

        return entities

    async def list_relationships_for_entities(
        self,
        entity_ids: set[str] | Sequence[str],
        *,
        relationship_types: list[RelationshipType] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Relationship]:
        scoped_entity_ids = {entity_id for entity_id in entity_ids if entity_id}
        if not scoped_entity_ids:
            return []

        remaining_offset = max(offset, 0)
        page_offset = 0
        page_size = max(200, min(max(limit, 1) * 2, 1000))
        relationships: list[Relationship] = []

        while len(relationships) < limit:
            batch = await self._relationships.list_all(
                relationship_types=relationship_types,
                limit=page_size,
                offset=page_offset,
            )
            if not batch:
                break

            page_offset += len(batch)
            for relationship in batch:
                if (
                    relationship.source_id not in scoped_entity_ids
                    or relationship.target_id not in scoped_entity_ids
                ):
                    continue
                if remaining_offset:
                    remaining_offset -= 1
                    continue
                relationships.append(relationship)
                if len(relationships) >= limit:
                    break

        return relationships

    async def get_connection_counts(
        self,
        entity_ids: Sequence[str],
        *,
        relationship_types: list[RelationshipType] | None = None,
    ) -> dict[str, int]:
        scoped_entity_ids = {entity_id for entity_id in entity_ids if entity_id}
        if not scoped_entity_ids:
            return {}

        counts = dict.fromkeys(scoped_entity_ids, 0)
        page_offset = 0
        page_size = 1000

        while True:
            batch = await self._relationships.list_all(
                relationship_types=relationship_types,
                limit=page_size,
                offset=page_offset,
            )
            if not batch:
                break

            page_offset += len(batch)
            for relationship in batch:
                if relationship.source_id in counts:
                    counts[relationship.source_id] += 1
                if relationship.target_id in counts and relationship.target_id != relationship.source_id:
                    counts[relationship.target_id] += 1

        return counts

    async def get_related_entities(
        self,
        *,
        entity_id: str,
        relationship_types: list[RelationshipType] | None = None,
        max_depth: int = 1,
        limit: int = 50,
    ) -> list[tuple[Entity, Relationship]]:
        return await self._relationships.get_related_entities(
            entity_id=entity_id,
            relationship_types=relationship_types,
            max_depth=max_depth,
            limit=limit,
        )

    async def search_entities(
        self,
        query: str,
        *,
        entity_types: list[EntityType] | None = None,
        limit: int = 10,
    ) -> list[tuple[Entity, float]]:
        return await self._entities.search(query, entity_types=entity_types, limit=limit)

    async def execute_read_org(self, query: str, **params: object) -> list[dict[str, object]]:
        return await self._client.execute_read_org(
            query,
            self._group_id,
            group_id=self._group_id,
            **params,
        )

    async def get_clusters_for_visualization(
        self, *, force_refresh: bool = False
    ) -> list[ClusterSummary]:
        from sibyl_core.graph.communities import get_clusters_for_visualization

        return await get_clusters_for_visualization(
            self._client,
            self._group_id,
            force_refresh=force_refresh,
        )

    async def get_cluster_nodes(self, cluster_id: str) -> dict[str, Any]:
        from sibyl_core.graph.communities import get_cluster_nodes

        return await get_cluster_nodes(self._client, self._group_id, cluster_id)

    async def get_hierarchical_graph(
        self,
        *,
        project_ids: list[str] | None = None,
        entity_types: list[str] | None = None,
        max_nodes: int = 1000,
        max_edges: int = 5000,
    ) -> HierarchicalGraphData:
        from sibyl_core.graph.communities import get_hierarchical_graph

        return await get_hierarchical_graph(
            self._client,
            self._group_id,
            project_ids=project_ids,
            entity_types=entity_types,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )


async def get_knowledge_read_adapter(group_id: str) -> GraphReadServiceAdapter:
    client = await get_graph_client()
    return GraphReadServiceAdapter.from_client(client, group_id)


async def get_graph_query_adapter(group_id: str) -> GraphQueryAdapter:
    client = await get_graph_client()
    return GraphQueryAdapter(client, group_id)


async def get_task_graph_runtime(group_id: str) -> TaskGraphRuntime:
    client = await get_graph_client()
    return TaskGraphRuntime(
        client=client,
        entity_manager=EntityManager(client, group_id=group_id),
        relationship_manager=RelationshipManager(client, group_id=group_id),
    )


async def get_entity_graph_runtime(group_id: str) -> TaskGraphRuntime:
    return await get_task_graph_runtime(group_id)


async def update_legacy_entity(
    group_id: str,
    entity_id: str,
    patch: dict[str, object],
) -> Entity | None:
    """Update an entity through the current graph runtime."""
    client = await get_graph_client()
    return await EntityManager(client, group_id=group_id).update(entity_id, patch)


async def update_graph_entity(
    group_id: str,
    entity_id: str,
    patch: dict[str, object],
) -> Entity | None:
    return await update_legacy_entity(group_id, entity_id, patch)


def graph_stats_payload(stats: GraphStats) -> dict[str, object]:
    entity_counts = {entity_type.value: 0 for entity_type in EntityType}
    entity_counts.update(stats.entities_by_type)
    return {
        "entity_counts": entity_counts,
        "total_entities": stats.total_entities,
    }


async def get_graph_stats_payload(group_id: str) -> dict[str, object]:
    service = await get_knowledge_read_adapter(group_id)
    stats = await service.stats()
    return graph_stats_payload(stats)


async def ensure_graph_indexes(group_id: str) -> None:
    client = await get_graph_client()
    await client.ensure_indexes(group_id)


async def reset_graph_runtime() -> None:
    await reset_graph_client()


async def execute_debug_query(
    cypher: str,
    group_id: str,
    **params: object,
) -> list[dict[str, object]]:
    client = await get_graph_client()
    result = await client.execute_read_org(cypher, group_id, **params)

    rows: list[dict[str, object]] = []
    for record in result:
        if hasattr(record, "keys"):
            rows.append(dict(record))
        elif isinstance(record, list | tuple):
            rows.append({"value": record})
        else:
            rows.append({"value": record})
    return rows


LegacyGraphStore = ActiveGraphStore
LegacyKnowledgeReadAdapter = GraphReadServiceAdapter
LegacyKnowledgeWriteAdapter = GraphWriteServiceAdapter
LegacyTaskRuntime = TaskGraphRuntime
LegacyGraphQueryAdapter = GraphQueryAdapter


async def get_legacy_knowledge_read_adapter(group_id: str) -> GraphReadServiceAdapter:
    return await get_knowledge_read_adapter(group_id)


async def get_legacy_graph_query_adapter(group_id: str) -> GraphQueryAdapter:
    return await get_graph_query_adapter(group_id)


async def get_legacy_task_runtime(group_id: str) -> TaskGraphRuntime:
    return await get_task_graph_runtime(group_id)


async def get_legacy_entity_runtime(group_id: str) -> TaskGraphRuntime:
    return await get_legacy_task_runtime(group_id)


async def get_legacy_graph_stats_payload(group_id: str) -> dict[str, object]:
    service = await get_legacy_knowledge_read_adapter(group_id)
    stats = await service.stats()
    return graph_stats_payload(stats)


async def ensure_legacy_graph_indexes(group_id: str) -> None:
    await ensure_graph_indexes(group_id)


async def reset_legacy_graph_runtime() -> None:
    await reset_graph_runtime()


async def execute_legacy_debug_query(
    cypher: str,
    group_id: str,
    **params: object,
) -> list[dict[str, object]]:
    return await execute_debug_query(cypher, group_id, **params)


def _collect_related_ids(entity_id: str, relationships: Sequence[Relationship]) -> Iterable[str]:
    seen: set[str] = set()
    for relationship in relationships:
        candidate = (
            relationship.target_id if relationship.source_id == entity_id else relationship.source_id
        )
        if not candidate or candidate == entity_id or candidate in seen:
            continue
        seen.add(candidate)
        yield candidate
