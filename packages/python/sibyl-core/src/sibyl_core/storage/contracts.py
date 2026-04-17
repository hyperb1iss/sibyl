"""Backend-agnostic graph storage contracts."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.storage.models import EntityPatch, GraphStats, Page, SearchFilters, SearchHit


@runtime_checkable
class EntityStore(Protocol):
    async def get(self, entity_id: str) -> Entity | None: ...

    async def get_many(self, entity_ids: list[str]) -> list[Entity]: ...

    async def upsert(self, entity: Entity) -> Entity: ...

    async def update(self, entity_id: str, patch: EntityPatch) -> Entity: ...

    async def delete(self, entity_id: str) -> bool: ...

    async def list_by_type(
        self, entity_type: EntityType, *, limit: int = 100, cursor: str | None = None
    ) -> Page[Entity]: ...

    async def find_by_name(self, name: str, *, exact: bool = False, limit: int = 20) -> list[Entity]: ...

    async def count(self) -> int: ...


@runtime_checkable
class RelationshipStore(Protocol):
    async def get(self, relationship_id: str) -> Relationship | None: ...

    async def upsert(self, relationship: Relationship) -> Relationship: ...

    async def delete(self, relationship_id: str) -> bool: ...

    async def list_for_entity(
        self,
        entity_id: str,
        *,
        relationship_types: list[RelationshipType] | None = None,
    ) -> list[Relationship]: ...

    async def find_between(
        self,
        source_id: str,
        target_id: str,
        *,
        relationship_type: RelationshipType | None = None,
    ) -> list[Relationship]: ...

    async def count(self) -> int: ...


@runtime_checkable
class SearchIndex(Protocol):
    async def search(
        self, query: str, *, filters: SearchFilters | None = None, limit: int = 10
    ) -> list[SearchHit]: ...

    async def stats(self) -> GraphStats: ...


@runtime_checkable
class GraphStore(Protocol):
    @property
    def entities(self) -> EntityStore: ...

    @property
    def relationships(self) -> RelationshipStore: ...

    @property
    def search(self) -> SearchIndex: ...
