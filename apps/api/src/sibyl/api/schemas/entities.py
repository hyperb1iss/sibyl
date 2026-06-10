"""Entity and raw-capture request/response models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from sibyl_core.models.entities import EntityType


class EntityBase(BaseModel):
    """Base fields for all entities."""

    name: str = Field(..., max_length=200, description="Entity name/title")
    description: str = Field(default="", description="Short description")
    content: str = Field(default="", max_length=50000, description="Full content")
    category: str | None = Field(default=None, description="Category for organization")
    languages: list[str] = Field(default_factory=list, description="Programming languages")
    tags: list[str] = Field(default_factory=list, description="Searchable tags")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class EntityCreate(EntityBase):
    """Schema for creating a new entity."""

    entity_type: EntityType = Field(default=EntityType.EPISODE, description="Type of entity")
    skip_conflicts: bool = Field(
        default=False,
        description="Skip semantic duplicate/conflict detection for latency-sensitive captures",
    )
    related_to: list[str] | None = Field(
        default=None,
        description="Entity IDs to explicitly connect with RELATED_TO edges",
    )


class EntityBulkCreateRequest(BaseModel):
    """Schema for creating many graph entities in one request."""

    entities: list[EntityCreate] = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Entities to create in one bounded batch",
    )
    defer_embeddings: bool = Field(
        default=False,
        description="Persist lexical graph records first and queue embedding backfill",
    )


class EntityUpdate(BaseModel):
    """Schema for updating an entity (all fields optional)."""

    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    content: str | None = Field(default=None, max_length=50000)
    category: str | None = None
    languages: list[str] | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class RelatedEntitySummary(BaseModel):
    """Summary of a related entity for embedding in responses."""

    id: str = Field(..., description="Entity ID")
    name: str = Field(..., description="Entity name")
    entity_type: str = Field(..., description="Entity type")
    relationship: str = Field(..., description="Relationship type connecting to this entity")
    direction: Literal["outgoing", "incoming"] = Field(..., description="Relationship direction")


class EntityResponse(EntityBase):
    """Full entity response with all fields."""

    id: str = Field(..., description="Unique entity ID")
    entity_type: EntityType = Field(..., description="Type of entity")
    source_file: str | None = Field(default=None, description="Source file path")
    created_at: datetime | None = Field(default=None, description="Creation timestamp")
    updated_at: datetime | None = Field(default=None, description="Last update timestamp")
    related: list[RelatedEntitySummary] | None = Field(
        default=None, description="Related entities (when requested via related_limit)"
    )

    model_config = {"from_attributes": True}


class EntityListResponse(BaseModel):
    """Paginated list of entities."""

    entities: list[EntityResponse]
    total: int
    page: int
    page_size: int
    has_more: bool


class EntityBulkCreateResponse(BaseModel):
    """Bulk entity creation response."""

    entities: list[EntityResponse]
    created: int
    failed: int = 0
    background_jobs: dict[str, Any] = Field(default_factory=dict)


class RawCaptureSummary(BaseModel):
    """Summary view of a raw archived capture."""

    id: str = Field(..., description="Raw capture ID")
    entity_id: str | None = Field(default=None, description="Created graph entity ID")
    title: str = Field(..., description="Captured title")
    entity_type: str = Field(..., description="Captured entity type")
    tags: list[str] = Field(default_factory=list, description="Captured tags")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Original request metadata")
    capture_surface: str | None = Field(default=None, description="Where the capture originated")
    review_state: Literal[
        "pending",
        "deferred",
        "archived",
        "promoted",
        "deleted",
        "duplicate",
        "hidden",
        "redacted",
        "sensitive",
        "stale",
        "superseded",
        "wrong",
    ] = Field(
        default="pending",
        description="Review queue state",
    )
    created_by_user_id: str | None = Field(
        default=None, description="User who initiated the capture"
    )
    created_at: datetime = Field(..., description="Archive creation timestamp")


class RawCaptureResponse(RawCaptureSummary):
    """Detailed raw capture response."""

    raw_content: str = Field(..., description="Verbatim captured content")


class RawCaptureListResponse(BaseModel):
    """Paginated raw capture list response."""

    captures: list[RawCaptureSummary]
    limit: int = Field(default=50, description="Results per page")
    offset: int = Field(default=0, description="Current offset")
    has_more: bool = Field(default=False, description="Whether more results exist")


class RawCaptureReviewUpdate(BaseModel):
    """Review-state update for a raw capture."""

    review_state: Literal["pending", "deferred", "archived", "promoted"] = Field(
        ...,
        description="Next review queue state",
    )
