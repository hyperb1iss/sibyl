from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import KnowledgeReadService, KnowledgeWriteService
from sibyl_core.storage import (
    EntityBundle,
    EntityPatch,
    EntityStore,
    GraphStats,
    GraphStore,
    Page,
    RelationshipStore,
    SearchFilters,
    SearchHit,
    SearchIndex,
)


class FakeEntityStore:
    def __init__(self, entity: Entity) -> None:
        self.entity = entity

    async def get(self, entity_id: str) -> Entity | None:
        return self.entity if self.entity.id == entity_id else None

    async def get_many(self, entity_ids: list[str]) -> list[Entity]:
        return [self.entity] if self.entity.id in entity_ids else []

    async def upsert(self, entity: Entity) -> Entity:
        self.entity = entity
        return entity

    async def update(self, entity_id: str, patch: EntityPatch) -> Entity:
        if entity_id != self.entity.id:
            raise ValueError(entity_id)
        if patch.name is not None:
            self.entity.name = patch.name
        if patch.description is not None:
            self.entity.description = patch.description
        return self.entity

    async def delete(self, entity_id: str) -> bool:
        return entity_id == self.entity.id

    async def list_by_type(
        self, entity_type: EntityType, *, limit: int = 100, cursor: str | None = None
    ) -> Page[Entity]:
        del limit, cursor
        items = [self.entity] if self.entity.entity_type is entity_type else []
        return Page[Entity](items=items, next_cursor=None, total_count=len(items))

    async def find_by_name(
        self, name: str, *, exact: bool = False, limit: int = 20
    ) -> list[Entity]:
        del exact, limit
        return [self.entity] if self.entity.name == name else []

    async def count(self) -> int:
        return 1


class FakeRelationshipStore:
    def __init__(self, relationship: Relationship) -> None:
        self.relationship = relationship

    async def get(self, relationship_id: str) -> Relationship | None:
        return self.relationship if self.relationship.id == relationship_id else None

    async def upsert(self, relationship: Relationship) -> Relationship:
        self.relationship = relationship
        return relationship

    async def delete(self, relationship_id: str) -> bool:
        return relationship_id == self.relationship.id

    async def list_for_entity(
        self,
        entity_id: str,
        *,
        relationship_types: list[RelationshipType] | None = None,
    ) -> list[Relationship]:
        if relationship_types and self.relationship.relationship_type not in relationship_types:
            return []
        if entity_id not in {self.relationship.source_id, self.relationship.target_id}:
            return []
        return [self.relationship]

    async def find_between(
        self,
        source_id: str,
        target_id: str,
        *,
        relationship_type: RelationshipType | None = None,
    ) -> list[Relationship]:
        if relationship_type and self.relationship.relationship_type is not relationship_type:
            return []
        if self.relationship.source_id == source_id and self.relationship.target_id == target_id:
            return [self.relationship]
        return []

    async def count(self) -> int:
        return 1


class FakeSearchIndex:
    def __init__(self, entity: Entity) -> None:
        self.entity = entity

    async def search(
        self, query: str, *, filters: SearchFilters | None = None, limit: int = 10
    ) -> list[SearchHit]:
        del filters, limit
        if query.lower() in self.entity.name.lower():
            return [SearchHit(entity=self.entity, score=0.99, highlights=[self.entity.name])]
        return []

    async def stats(self) -> GraphStats:
        return GraphStats(
            total_entities=1,
            total_relationships=1,
            entities_by_type={self.entity.entity_type.value: 1},
            relationships_by_type={RelationshipType.RELATED_TO.value: 1},
            last_ingestion=datetime.now(UTC),
        )


class FakeGraphStore:
    def __init__(
        self,
        entity_store: FakeEntityStore,
        relationship_store: FakeRelationshipStore,
        search_index: FakeSearchIndex,
    ) -> None:
        self._entities = entity_store
        self._relationships = relationship_store
        self._search = search_index

    @property
    def entities(self) -> EntityStore:
        return self._entities

    @property
    def relationships(self) -> RelationshipStore:
        return self._relationships

    @property
    def search(self) -> SearchIndex:
        return self._search


class FakeKnowledgeService:
    def __init__(self, graph_store: FakeGraphStore) -> None:
        self.graph_store = graph_store

    async def get_entity(self, entity_id: str) -> Entity | None:
        return await self.graph_store.entities.get(entity_id)

    async def get_entity_bundle(self, entity_id: str) -> EntityBundle | None:
        entity = await self.graph_store.entities.get(entity_id)
        if entity is None:
            return None
        relationships = await self.graph_store.relationships.list_for_entity(entity_id)
        return EntityBundle(entity=entity, relationships=relationships, related_entities=[])

    async def list_entities(
        self, entity_type: EntityType, *, limit: int = 100, cursor: str | None = None
    ) -> Page[Entity]:
        return await self.graph_store.entities.list_by_type(entity_type, limit=limit, cursor=cursor)

    async def search(
        self, query: str, *, filters: SearchFilters | None = None, limit: int = 10
    ) -> list[SearchHit]:
        return await self.graph_store.search.search(query, filters=filters, limit=limit)

    async def get_related(self, entity_id: str) -> list[Relationship]:
        return await self.graph_store.relationships.list_for_entity(entity_id)

    async def stats(self) -> GraphStats:
        return await self.graph_store.search.stats()

    async def upsert_entity(self, entity: Entity) -> Entity:
        stored = await self.graph_store.entities.upsert(entity)
        self.graph_store.search.entity = stored
        return stored

    async def upsert_relationship(self, relationship: Relationship) -> Relationship:
        return await self.graph_store.relationships.upsert(relationship)

    async def delete_entity(self, entity_id: str) -> bool:
        return await self.graph_store.entities.delete(entity_id)

    async def delete_relationship(self, relationship_id: str) -> bool:
        return await self.graph_store.relationships.delete(relationship_id)


@pytest.mark.asyncio
async def test_storage_and_service_contracts_round_trip() -> None:
    entity = Entity(
        id="entity-1",
        entity_type=EntityType.PATTERN,
        name="Seam Extraction",
        description="Cut interfaces before swapping backends.",
    )
    relationship = Relationship(
        id="relationship-1",
        relationship_type=RelationshipType.RELATED_TO,
        source_id="entity-1",
        target_id="entity-2",
    )

    entity_store = FakeEntityStore(entity)
    relationship_store = FakeRelationshipStore(relationship)
    search_index = FakeSearchIndex(entity)
    graph_store = FakeGraphStore(entity_store, relationship_store, search_index)
    service = FakeKnowledgeService(graph_store)

    assert isinstance(entity_store, EntityStore)
    assert isinstance(relationship_store, RelationshipStore)
    assert isinstance(search_index, SearchIndex)
    assert isinstance(graph_store, GraphStore)
    assert isinstance(service, KnowledgeReadService)
    assert isinstance(service, KnowledgeWriteService)

    updated = await service.upsert_entity(entity.model_copy(update={"name": "Contract First"}))
    hits = await service.search("contract")
    bundle = await service.get_entity_bundle("entity-1")
    stats = await service.stats()

    assert updated.name == "Contract First"
    assert hits[0].entity.id == "entity-1"
    assert bundle is not None
    assert bundle.relationships == [relationship]
    assert stats.total_entities == 1
