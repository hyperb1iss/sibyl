"""Backend-agnostic storage domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType


class Page[T](BaseModel):
    """Cursor-based page of contract models."""

    items: list[T] = Field(default_factory=list)
    next_cursor: str | None = None
    total_count: int | None = None


class EntityPatch(BaseModel):
    """Patch payload for entity updates."""

    name: str | None = None
    description: str | None = None
    content: str | None = None
    metadata: dict[str, Any] | None = None
    source_file: str | None = None
    embedding: list[float] | None = None
    updated_at: datetime | None = None


class RelationshipPatch(BaseModel):
    """Patch payload for relationship updates."""

    weight: float | None = None
    metadata: dict[str, Any] | None = None


class SearchFilters(BaseModel):
    """Cross-backend filters for knowledge search."""

    entity_types: list[EntityType] = Field(default_factory=list)
    relationship_types: list[RelationshipType] = Field(default_factory=list)
    organization_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchHit(BaseModel):
    """A scored search result independent of transport."""

    entity: Entity
    score: float
    highlights: list[str] = Field(default_factory=list)


class GraphStats(BaseModel):
    """Portable graph statistics."""

    total_entities: int = 0
    total_relationships: int = 0
    entities_by_type: dict[str, int] = Field(default_factory=dict)
    relationships_by_type: dict[str, int] = Field(default_factory=dict)
    last_ingestion: datetime | None = None
    graph_size_mb: float | None = None


class EntityBundle(BaseModel):
    """Entity with its directly-related graph neighborhood."""

    entity: Entity
    relationships: list[Relationship] = Field(default_factory=list)
    related_entities: list[Entity] = Field(default_factory=list)
