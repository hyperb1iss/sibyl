"""Backend-agnostic service contracts."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sibyl_core.models.entities import Entity, EntityType, Relationship
from sibyl_core.storage.models import EntityBundle, GraphStats, Page, SearchFilters, SearchHit


@runtime_checkable
class KnowledgeReadService(Protocol):
    async def get_entity(self, entity_id: str) -> Entity | None: ...

    async def get_entity_bundle(self, entity_id: str) -> EntityBundle | None: ...

    async def list_entities(
        self, entity_type: EntityType, *, limit: int = 100, cursor: str | None = None
    ) -> Page[Entity]: ...

    async def search(
        self, query: str, *, filters: SearchFilters | None = None, limit: int = 10
    ) -> list[SearchHit]: ...

    async def get_related(self, entity_id: str) -> list[Relationship]: ...

    async def stats(self) -> GraphStats: ...


@runtime_checkable
class KnowledgeWriteService(Protocol):
    async def upsert_entity(self, entity: Entity) -> Entity: ...

    async def upsert_relationship(self, relationship: Relationship) -> Relationship: ...

    async def delete_entity(self, entity_id: str) -> bool: ...

    async def delete_relationship(self, relationship_id: str) -> bool: ...
