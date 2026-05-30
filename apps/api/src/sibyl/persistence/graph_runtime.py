"""Graph and search adapters backed by the active graph runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Self

import structlog

from sibyl_core.embeddings.providers import configured_embedding_provider
from sibyl_core.errors import EntityNotFoundError
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import KnowledgeReadService
from sibyl_core.services.graph import (
    EntityManager,
    GraphRuntime,
    RelationshipManager,
    SurrealGraphClient,
    entity_from_surreal_row,
    get_surreal_graph_runtime,
    normalize_records,
    relationship_from_surreal_row,
)
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

log = structlog.get_logger()


async def _get_graph_runtime(group_id: str) -> GraphRuntime:
    return await get_surreal_graph_runtime(
        group_id,
        embedding_provider=configured_embedding_provider(),
    )


def _normalize_result(result: object) -> list[dict[str, object]]:
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        if "result" not in payload or not {"status", "time"} & payload.keys():
            return [payload]
    if isinstance(result, list):
        rows: list[dict[str, object]] = []
        for item in result:
            if not isinstance(item, dict):
                break
            payload = {str(key): value for key, value in item.items()}
            if "result" in payload and {"status", "time"} & payload.keys():
                break
            rows.append(payload)
        else:
            return rows
    return normalize_records(result)


def _driver_for_client(client: Any, group_id: str) -> Any:
    get_org_driver = getattr(client, "get_org_driver", None)
    if callable(get_org_driver):
        return get_org_driver(group_id)
    return client


def _native_driver_for_client(client: Any, group_id: str) -> Any:
    driver = _driver_for_client(client, group_id)
    surreal_driver = _surreal_driver_for(driver)
    if surreal_driver is None:
        raise RuntimeError("Supported graph runtime requires native Surreal graph operations")
    return surreal_driver


def _entity_manager_for(client: Any, group_id: str) -> Any:
    return EntityManager(_native_driver_for_client(client, group_id), group_id=group_id)


def _relationship_manager_for(client: Any, group_id: str) -> Any:
    return RelationshipManager(_native_driver_for_client(client, group_id), group_id=group_id)


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


def _coerce_int(value: object, *, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
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


async def _surreal_group_count(driver: Any, table: str, group_id: str) -> int:
    rows = await _surreal_rows_or_empty(
        driver,
        f"SELECT count() AS cnt FROM {table} WHERE group_id = $group_id GROUP ALL;",  # noqa: S608
        group_id=group_id,
    )
    return _coerce_int(rows[0].get("cnt")) if rows else 0


def _surreal_nested_rows(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [{str(key): item for key, item in value.items()}]
    if isinstance(value, list):
        return [
            {str(key): item for key, item in row.items()} for row in value if isinstance(row, dict)
        ]
    return []


def _surreal_count_number(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _surreal_count_value(value: object) -> int:
    rows = _surreal_nested_rows(value)
    return _surreal_count_number(rows[0].get("cnt", 0)) if rows else 0


def _is_surreal_missing_table_error(exc: Exception) -> bool:
    message = str(getattr(exc, "surreal_message", exc))
    return exc.__class__.__name__ == "SurrealQueryError" and "does not exist" in message


async def _surreal_rows_or_empty(
    driver: Any,
    query: str,
    *,
    group_id: str,
) -> list[dict[str, object]]:
    try:
        return _normalize_result(await driver.execute_query(query, group_id=group_id))
    except Exception as exc:
        if _is_surreal_missing_table_error(exc):
            return []
        raise


def _surreal_driver_for(driver: Any) -> Any | None:
    return driver if isinstance(driver, SurrealGraphClient) else None


def _assert_legacy_graph_query_allowed(driver: Any, operation: str) -> None:
    if _surreal_driver_for(driver) is not None:
        raise RuntimeError(f"SurrealDB {operation} requires native graph operations")


class GraphEntityStore(EntityStore):
    """EntityStore backed by the current native entity manager."""

    def __init__(self, manager: Any, *, driver: Any, group_id: str) -> None:
        self._manager = manager
        self._driver = driver
        self._group_id = group_id

    @classmethod
    def from_client(cls, client: Any, group_id: str) -> Self:
        return cls(
            _entity_manager_for(client, group_id),
            driver=_driver_for_client(client, group_id),
            group_id=group_id,
        )

    @classmethod
    def from_runtime(cls, runtime: GraphRuntime, group_id: str) -> Self:
        return cls(runtime.entity_manager, driver=runtime.client, group_id=group_id)

    async def get(self, entity_id: str) -> Entity | None:
        try:
            return await self._manager.get(entity_id)
        except (EntityNotFoundError, KeyError):
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

    async def find_by_name(
        self, name: str, *, exact: bool = False, limit: int = 20
    ) -> list[Entity]:
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

    def entity_from_node(self, node: Any) -> Entity:
        if isinstance(node, dict):
            return entity_from_surreal_row(node)
        node_to_entity = getattr(self._manager, "node_to_entity", None)
        if callable(node_to_entity):
            return node_to_entity(node)
        model_dump = getattr(node, "model_dump", None)
        if callable(model_dump):
            return entity_from_surreal_row(model_dump(mode="python"))
        try:
            return entity_from_surreal_row(vars(node))
        except TypeError as exc:
            raise TypeError(f"cannot hydrate entity from {type(node).__name__}") from exc

    async def count(self) -> int:
        if _surreal_driver_for(self._driver) is not None:
            rows = _normalize_result(
                await self._driver.execute_query(
                    """
                    SELECT count() AS cnt
                    FROM entity
                    WHERE group_id = $group_id
                    GROUP ALL;
                    """,
                    group_id=self._group_id,
                )
            )
            return _coerce_int(rows[0].get("cnt")) if rows else 0

        rows = _normalize_result(
            await self._driver.execute_query(
                """
                MATCH (n)
                WHERE n.group_id = $group_id AND n.entity_type IS NOT NULL
                RETURN count(n) AS cnt
                """,
                group_id=self._group_id,
            )
        )
        return _coerce_int(rows[0].get("cnt")) if rows else 0


class GraphRelationshipStore(RelationshipStore):
    """RelationshipStore backed by the current native relationship manager."""

    def __init__(self, manager: Any, *, driver: Any, group_id: str) -> None:
        self._manager = manager
        self._driver = driver
        self._group_id = group_id

    @classmethod
    def from_client(cls, client: Any, group_id: str) -> Self:
        return cls(
            _relationship_manager_for(client, group_id),
            driver=_driver_for_client(client, group_id),
            group_id=group_id,
        )

    @classmethod
    def from_runtime(cls, runtime: GraphRuntime, group_id: str) -> Self:
        return cls(runtime.relationship_manager, driver=runtime.client, group_id=group_id)

    async def get(self, relationship_id: str) -> Relationship | None:
        if _surreal_driver_for(self._driver) is not None:
            try:
                return await self._manager.get(relationship_id)
            except KeyError:
                return None

        _assert_legacy_graph_query_allowed(self._driver, "relationship get")

        rows = _normalize_result(
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
        if _surreal_driver_for(self._driver) is not None:
            if existing is not None:
                created_id = await self._manager.create(relationship)
                refreshed = await self.get(created_id)
                if refreshed is None:
                    msg = f"Relationship not found after update: {created_id}"
                    raise LookupError(msg)
                return refreshed
        else:
            _assert_legacy_graph_query_allowed(self._driver, "relationship upsert")

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

    async def get_related_entities(
        self,
        entity_id: str,
        *,
        relationship_types: list[RelationshipType] | None = None,
        limit: int = 50,
    ) -> list[tuple[Entity, Relationship]]:
        return await self._manager.get_related_entities(
            entity_id=entity_id,
            relationship_types=relationship_types,
            limit=limit,
        )

    async def find_between(
        self,
        source_id: str,
        target_id: str,
        *,
        relationship_type: RelationshipType | None = None,
    ) -> list[Relationship]:
        if _surreal_driver_for(self._driver) is not None:
            return await self._manager.find_between(
                source_id,
                target_id,
                relationship_type=relationship_type,
            )

        _assert_legacy_graph_query_allowed(self._driver, "relationship find_between")

        rows = _normalize_result(
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
        if _surreal_driver_for(self._driver) is not None:
            rows = _normalize_result(
                await self._driver.execute_query(
                    """
                    SELECT count() AS cnt
                    FROM relates_to
                    WHERE group_id = $group_id
                    GROUP ALL;
                    """,
                    group_id=self._group_id,
                )
            )
            return _coerce_int(rows[0].get("cnt")) if rows else 0

        rows = _normalize_result(
            await self._driver.execute_query(
                """
                MATCH ()-[r]->()
                WHERE r.group_id = $group_id
                RETURN count(r) AS cnt
                """,
                group_id=self._group_id,
            )
        )
        return _coerce_int(rows[0].get("cnt")) if rows else 0


class GraphSearchIndex(SearchIndex):
    """SearchIndex backed by the current entity search implementation."""

    def __init__(self, client: Any, group_id: str, entities: GraphEntityStore) -> None:
        self._client = client
        self._group_id = group_id
        self._entities = entities

    @classmethod
    def from_client(cls, client: Any, group_id: str, entities: GraphEntityStore) -> Self:
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
        driver = _driver_for_client(self._client, self._group_id)
        if _surreal_driver_for(driver) is not None:
            from sibyl_core.backends.surreal.schema import GRAPH_EDGES, GRAPH_TABLES
            from sibyl_core.migrate.legacy_graph_archive import (
                ARCHIVE_GRAPH_EDGES,
                ARCHIVE_GRAPH_TABLES,
            )

            entity_rows = await _surreal_rows_or_empty(
                driver,
                """
                    SELECT entity_type, count() AS cnt
                    FROM entity
                    WHERE group_id = $group_id
                    GROUP BY entity_type;
                    """,
                group_id=self._group_id,
            )
            relates_to_rows = await _surreal_rows_or_empty(
                driver,
                """
                    SELECT name AS relationship_type, count() AS cnt
                    FROM relates_to
                    WHERE group_id = $group_id
                    GROUP BY name;
                    """,
                group_id=self._group_id,
            )

            entities_by_type = {
                str(row.get("entity_type")): _coerce_int(row.get("cnt"))
                for row in entity_rows
                if row.get("entity_type")
            }
            for table in GRAPH_TABLES:
                if table == "entity" or table in ARCHIVE_GRAPH_TABLES:
                    continue
                count = await _surreal_group_count(driver, table, self._group_id)
                if count:
                    entities_by_type[table] = count

            relationships_by_type = {
                str(row.get("relationship_type") or "RELATED_TO"): _coerce_int(row.get("cnt"))
                for row in relates_to_rows
                if _coerce_int(row.get("cnt"))
            }
            for table in GRAPH_EDGES:
                if table == "relates_to" or table in ARCHIVE_GRAPH_EDGES:
                    continue
                count = await _surreal_group_count(driver, table, self._group_id)
                if count:
                    relationships_by_type[table.upper()] = count

            return GraphStats(
                total_entities=sum(entities_by_type.values()),
                total_relationships=sum(relationships_by_type.values()),
                entities_by_type=entities_by_type,
                relationships_by_type=relationships_by_type,
            )

        node_rows = _normalize_result(
            await driver.execute_query(
                """
                MATCH (n)
                WHERE n.group_id = $group_id AND n.entity_type IS NOT NULL
                RETURN n.entity_type AS entity_type, count(*) AS cnt
                """,
                group_id=self._group_id,
            )
        )
        relationship_rows = _normalize_result(
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
            str(row.get("entity_type") or "unknown"): _coerce_int(row.get("cnt"))
            for row in node_rows
        }
        relationships_by_type = {
            str(row.get("relationship_type") or "RELATED_TO"): _coerce_int(row.get("cnt"))
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
        entities: GraphEntityStore,
        relationships: GraphRelationshipStore,
        search: GraphSearchIndex,
    ) -> None:
        self._entities = entities
        self._relationships = relationships
        self._search = search

    @classmethod
    def from_client(cls, client: Any, group_id: str) -> Self:
        entities = GraphEntityStore.from_client(client, group_id)
        relationships = GraphRelationshipStore.from_client(client, group_id)
        return cls(
            entities=entities,
            relationships=relationships,
            search=GraphSearchIndex.from_client(client, group_id, entities),
        )

    @classmethod
    def from_runtime(cls, runtime: GraphRuntime, group_id: str) -> Self:
        entities = GraphEntityStore.from_runtime(runtime, group_id)
        relationships = GraphRelationshipStore.from_runtime(runtime, group_id)
        return cls(
            entities=entities,
            relationships=relationships,
            search=GraphSearchIndex(runtime.client, group_id, entities),
        )

    @property
    def entities(self) -> GraphEntityStore:
        return self._entities

    @property
    def relationships(self) -> GraphRelationshipStore:
        return self._relationships

    @property
    def search(self) -> GraphSearchIndex:
        return self._search


class GraphReadServiceAdapter(KnowledgeReadService):
    """KnowledgeReadService backed by the current graph runtime."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    @classmethod
    def from_client(cls, client: Any, group_id: str) -> Self:
        return cls(ActiveGraphStore.from_client(client, group_id))

    @classmethod
    def from_runtime(cls, runtime: GraphRuntime, group_id: str) -> Self:
        return cls(ActiveGraphStore.from_runtime(runtime, group_id))

    async def get_entity(self, entity_id: str) -> Entity | None:
        return await self._store.entities.get(entity_id)

    async def get_entity_bundle(self, entity_id: str) -> EntityBundle | None:
        entity = await self.get_entity(entity_id)
        if entity is None:
            return None

        relationship_store: Any = self._store.relationships
        get_related_entities = getattr(relationship_store, "get_related_entities", None)
        if callable(get_related_entities):
            related_pairs = await get_related_entities(entity_id, limit=50)
            return EntityBundle(
                entity=entity,
                relationships=[relationship for _entity, relationship in related_pairs],
                related_entities=[related for related, _relationship in related_pairs],
            )

        relationships = await relationship_store.list_for_entity(entity_id)
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


@dataclass(frozen=True, slots=True)
class TaskGraphRuntime:
    """Scoped graph runtime for task routes on the active backend."""

    client: Any
    entity_manager: Any
    relationship_manager: Any


class GraphQueryAdapter:
    """Thin graph query surface for routes that still need runtime reads."""

    def __init__(
        self,
        client: Any,
        group_id: str,
        *,
        entity_manager: Any | None = None,
        relationship_manager: Any | None = None,
    ) -> None:
        self._client = client
        self._group_id = group_id
        self._driver = _driver_for_client(client, group_id)
        self._entities = entity_manager or _entity_manager_for(client, group_id)
        self._relationships = relationship_manager or _relationship_manager_for(client, group_id)

    @classmethod
    def from_runtime(cls, runtime: GraphRuntime, group_id: str) -> Self:
        return cls(
            runtime.client,
            group_id,
            entity_manager=runtime.entity_manager,
            relationship_manager=runtime.relationship_manager,
        )

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        _assert_legacy_graph_query_allowed(self._driver, "raw query")
        result = await self._driver.execute_query(query, group_id=self._group_id, **params)
        return _normalize_result(result)

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

        if _surreal_driver_for(self._driver) is not None:
            type_clause = "AND name IN $relationship_types" if relationship_types else ""
            rows = _normalize_result(
                await self._driver.execute_query(
                    f"""
                    SELECT
                        id AS record_id,
                        uuid,
                        name,
                        fact,
                        group_id,
                        episodes,
                        attributes,
                        created_at,
                        expired_at,
                        valid_at,
                        invalid_at,
                        source_id AS source_uuid,
                        target_id AS target_uuid
                    FROM relates_to
                    WHERE group_id = $group_id
                      AND source_id IN $entity_ids
                      AND target_id IN $entity_ids
                      {type_clause}
                    ORDER BY uuid DESC
                    LIMIT $limit
                    START $offset;
                    """,  # noqa: S608
                    group_id=self._group_id,
                    entity_ids=sorted(scoped_entity_ids),
                    relationship_types=[rel.value for rel in relationship_types or []],
                    limit=limit,
                    offset=max(offset, 0),
                )
            )
            return [relationship_from_surreal_row(row) for row in rows]

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
        if _surreal_driver_for(self._driver) is not None:
            type_clause = "AND name IN $relationship_types" if relationship_types else ""
            rows = _normalize_result(
                await self._driver.execute_query(
                    f"""
                    SELECT source_id, target_id
                    FROM relates_to
                    WHERE group_id = $group_id
                      AND (source_id IN $entity_ids OR target_id IN $entity_ids)
                      {type_clause};
                    """,  # noqa: S608
                    group_id=self._group_id,
                    entity_ids=sorted(scoped_entity_ids),
                    relationship_types=[rel.value for rel in relationship_types or []],
                )
            )
            for row in rows:
                source_id = str(row.get("source_id") or "")
                target_id = str(row.get("target_id") or "")
                if source_id in counts:
                    counts[source_id] += 1
                if target_id in counts and target_id != source_id:
                    counts[target_id] += 1
            return counts

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
                if (
                    relationship.target_id in counts
                    and relationship.target_id != relationship.source_id
                ):
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
            allow_surreal=False,
            group_id=self._group_id,
            **params,
        )

    async def get_clusters_for_visualization(self, *, force_refresh: bool = False) -> list[Any]:
        from sibyl_core.services.graph_communities import get_clusters_for_visualization

        return await get_clusters_for_visualization(
            self._client,
            self._group_id,
            force_refresh=force_refresh,
        )

    async def get_cluster_nodes(self, cluster_id: str) -> dict[str, Any]:
        from sibyl_core.services.graph_communities import get_cluster_nodes

        return await get_cluster_nodes(self._client, self._group_id, cluster_id)

    async def get_hierarchical_graph(
        self,
        *,
        project_ids: list[str] | None = None,
        entity_types: list[str] | None = None,
        max_nodes: int = 1000,
        max_edges: int = 5000,
        resolution: str = "detail",
        cluster_id: str | None = None,
    ) -> Any:
        from sibyl_core.services.graph_communities import get_hierarchical_graph

        return await get_hierarchical_graph(
            self._client,
            self._group_id,
            project_ids=project_ids,
            entity_types=entity_types,
            max_nodes=max_nodes,
            max_edges=max_edges,
            resolution=resolution,
            cluster_id=cluster_id,
        )


async def get_knowledge_read_adapter(group_id: str) -> GraphReadServiceAdapter:
    runtime = await _get_graph_runtime(group_id)
    return GraphReadServiceAdapter.from_runtime(runtime, group_id)


async def get_graph_store(group_id: str) -> ActiveGraphStore:
    runtime = await _get_graph_runtime(group_id)
    return ActiveGraphStore.from_runtime(runtime, group_id)


async def get_graph_query_adapter(group_id: str) -> GraphQueryAdapter:
    runtime = await _get_graph_runtime(group_id)
    return GraphQueryAdapter.from_runtime(runtime, group_id)


async def execute_surreal_graph_query(
    group_id: str,
    query: str,
    **params: object,
) -> list[dict[str, object]] | None:
    runtime = await _get_graph_runtime(group_id)
    driver = runtime.client
    surreal_driver = _surreal_driver_for(driver)
    if surreal_driver is None:
        return None
    result = await surreal_driver.execute_query(query, group_id=group_id, **params)
    return _normalize_result(result)


async def get_task_graph_runtime(group_id: str) -> TaskGraphRuntime:
    runtime = await _get_graph_runtime(group_id)
    return TaskGraphRuntime(
        client=runtime.client,
        entity_manager=runtime.entity_manager,
        relationship_manager=runtime.relationship_manager,
    )


async def get_entity_graph_runtime(group_id: str) -> TaskGraphRuntime:
    return await get_task_graph_runtime(group_id)


async def update_graph_entity(
    group_id: str,
    entity_id: str,
    patch: dict[str, object],
) -> Entity | None:
    """Update an entity through the current graph runtime."""
    runtime = await _get_graph_runtime(group_id)
    return await runtime.entity_manager.update(entity_id, patch)


async def delete_graph_data(group_id: str) -> None:
    runtime = await _get_graph_runtime(group_id)
    driver = runtime.client
    if _surreal_driver_for(driver) is not None:
        from sibyl_core.backends.surreal.records import raise_on_error
        from sibyl_core.backends.surreal.schema import GRAPH_EDGES, GRAPH_TABLES

        # A failure here must propagate, not silently fall through to the
        # per-table loop: degrading from an atomic clear to a non-atomic sweep
        # on error is how a half-deleted graph used to slip through unnoticed.
        graph_ops = getattr(driver, "graph_ops", None)
        if graph_ops is not None:
            await graph_ops.clear_data(driver, group_ids=[group_id])
            return

        # Table names are fixed module constants; the only runtime value
        # ($group_id) stays parameter-bound. One transaction keeps the
        # namespace-scoped clear atomic.
        statements = "\n".join(
            f"DELETE FROM {table} WHERE group_id = $group_id;"  # noqa: S608
            for table in (*GRAPH_EDGES, *GRAPH_TABLES)
        )
        result = await driver.execute_query_raw(
            f"BEGIN TRANSACTION;\n{statements}\nCOMMIT TRANSACTION;",
            group_id=group_id,
        )
        raise_on_error(result, query="delete_graph_data")
        return

    raise RuntimeError("Supported graph runtime requires native Surreal graph operations")


async def delete_project_graph_data(group_id: str, project_id: str) -> None:
    runtime = await _get_graph_runtime(group_id)
    driver = runtime.client
    if _surreal_driver_for(driver) is not None:
        from sibyl_core.backends.surreal.records import raise_on_error

        query = """
            BEGIN TRANSACTION;
            LET $project_entity_ids = (
                SELECT VALUE uuid FROM entity
                WHERE group_id = $group_id
                  AND (project_id = $project_id OR uuid = $project_id)
            );
            LET $project_episode_ids = (
                SELECT VALUE uuid FROM episode
                WHERE group_id = $group_id AND project_id = $project_id
            );
            DELETE FROM relates_to
            WHERE group_id = $group_id
              AND (source_id IN $project_entity_ids OR target_id IN $project_entity_ids);
            DELETE FROM mentions
            WHERE group_id = $group_id
              AND (source_id IN $project_episode_ids OR target_id IN $project_entity_ids);
            DELETE FROM entity
            WHERE group_id = $group_id
              AND (project_id = $project_id OR uuid = $project_id);
            DELETE FROM episode
            WHERE group_id = $group_id AND project_id = $project_id;
            COMMIT TRANSACTION;
        """
        result = await driver.execute_query_raw(
            query,
            group_id=group_id,
            project_id=project_id,
        )
        raise_on_error(result, query="delete_project_graph_data")
        return

    raise RuntimeError("Supported graph runtime requires native Surreal graph operations")


def graph_stats_payload(stats: GraphStats) -> dict[str, object]:
    entity_counts = {entity_type.value: 0 for entity_type in EntityType}
    entity_counts.update(stats.entities_by_type)
    return {
        "entity_counts": entity_counts,
        "total_entities": stats.total_entities,
    }


async def _graph_stats_payload(group_id: str) -> dict[str, object]:
    from sibyl_core.backends.surreal.schema import GRAPH_TABLES
    from sibyl_core.migrate.legacy_graph_archive import ARCHIVE_GRAPH_TABLES

    runtime = await _get_graph_runtime(group_id)
    rows = normalize_records(
        await runtime.client.execute_query(
            """
            RETURN {
                entity_types: (
                    SELECT entity_type, count() AS cnt
                    FROM entity
                    WHERE group_id = $group_id
                    GROUP BY entity_type
                ),
            };
            """,
            group_id=group_id,
        )
    )
    row = rows[0] if rows else {}
    entity_counts = {entity_type.value: 0 for entity_type in EntityType}
    for entity_row in _surreal_nested_rows(row.get("entity_types")):
        entity_type = entity_row.get("entity_type")
        if entity_type:
            entity_counts[str(entity_type)] = _surreal_count_number(entity_row.get("cnt", 0))
    for table in GRAPH_TABLES:
        if table == "entity" or table in ARCHIVE_GRAPH_TABLES:
            continue
        count = _surreal_count_value(row.get(f"{table}_count"))
        if count:
            entity_counts[table] = count
    return {
        "entity_counts": entity_counts,
        "total_entities": sum(entity_counts.values()),
    }


async def get_graph_stats_payload(group_id: str) -> dict[str, object]:
    try:
        return await _graph_stats_payload(group_id)
    except Exception as exc:
        if not _is_surreal_missing_table_error(exc):
            log.warning("graph_stats_payload_failed", group_id=group_id, error=str(exc))
        service = await get_knowledge_read_adapter(group_id)
        stats = await service.stats()
        return graph_stats_payload(stats)


async def ensure_graph_indexes(group_id: str) -> None:
    await _get_graph_runtime(group_id)


async def reset_graph_runtime() -> None:
    from sibyl_core.services.graph import close_graph_clients

    await close_graph_clients()


async def execute_debug_query(
    cypher: str,
    group_id: str,
    **params: object,
) -> list[dict[str, object]]:
    runtime = await _get_graph_runtime(group_id)
    result = await runtime.client.execute_query(cypher, group_id=group_id, **params)

    rows: list[dict[str, object]] = []
    for record in _normalize_result(result):
        if hasattr(record, "keys"):
            rows.append(dict(record))
        elif isinstance(record, list | tuple):
            rows.append({"value": record})
        else:
            rows.append({"value": record})
    return rows


def _collect_related_ids(entity_id: str, relationships: Sequence[Relationship]) -> Iterable[str]:
    seen: set[str] = set()
    for relationship in relationships:
        candidate = (
            relationship.target_id
            if relationship.source_id == entity_id
            else relationship.source_id
        )
        if not candidate or candidate == entity_id or candidate in seen:
            continue
        seen.add(candidate)
        yield candidate
