"""Entity and raw-capture request/response models."""

import unicodedata
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from sibyl_core.memory_pipeline.retrieval_keys import (
    MAX_RETRIEVAL_KEY_LENGTH,
    MAX_RETRIEVAL_KEYS,
)
from sibyl_core.memory_pipeline.spans import MAX_AGENT_SPANS, MAX_SPAN_LABEL_CHARS
from sibyl_core.memory_pipeline.structure import MAX_PROBE_CHARS, MAX_PROBES_PER_MEMORY
from sibyl_core.models.entities import EntityType


class MemorySpan(BaseModel):
    """One half-open cut into the stored body, authored by the writing agent."""

    start: int = Field(..., ge=0, description="First character of the span, inclusive")
    end: int = Field(..., ge=1, description="Character after the span, exclusive")
    label: str | None = Field(
        default=None,
        max_length=MAX_SPAN_LABEL_CHARS,
        description="Section name rendered into the passage body and indexed with it",
    )


class MemoryStructureFields(BaseModel):
    """The structure a write may declare for the memory it stores.

    The tiling rules (ordered, gap-free, non-overlapping, whole-body) are checked
    against the stored content by the server, not here: this schema only fixes
    the shape and the counts, because the body a span addresses is not known
    until the route has resolved content, description, and name fallbacks.
    """

    spans: list[MemorySpan] | None = Field(
        default=None,
        max_length=MAX_AGENT_SPANS,
        description=(
            "Agent-authored cut plan over the stored content. Offsets must tile the "
            "whole body with no gap and no overlap; passages carry the exact span text."
        ),
    )
    atomic: bool = Field(
        default=False,
        description="Declare the body one retrievable unit that must not be cut into passages",
    )


class EntityBase(BaseModel):
    """Base fields for all entities."""

    name: str = Field(..., max_length=200, description="Entity name/title")
    description: str = Field(default="", description="Short description")
    content: str = Field(default="", max_length=50000, description="Full content")
    category: str | None = Field(default=None, description="Category for organization")
    languages: list[str] = Field(default_factory=list, description="Programming languages")
    tags: list[str] = Field(default_factory=list, description="Searchable tags")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class EntityCreate(EntityBase, MemoryStructureFields):
    """Schema for creating a new entity."""

    entity_type: EntityType = Field(default=EntityType.EPISODE, description="Type of entity")
    probes: list[Annotated[str, StringConstraints(max_length=MAX_PROBE_CHARS)]] | None = Field(
        default=None,
        max_length=MAX_PROBES_PER_MEMORY,
        description=(
            "Questions this memory must answer later. Each is run through the live "
            "search path once the write lands and the ranks come back in the response. "
            "Supplying probes forces a synchronous write."
        ),
    )
    skip_conflicts: bool = Field(
        default=False,
        description="Skip semantic duplicate/conflict detection for latency-sensitive captures",
    )
    related_to: list[str] | None = Field(
        default=None,
        description="Entity IDs to explicitly connect with RELATED_TO edges",
    )
    defer_embeddings: bool = Field(
        default=False,
        description="Persist lexical graph records first and queue embedding backfill",
    )
    retrieval_keys: list[Annotated[str, Field(max_length=MAX_RETRIEVAL_KEY_LENGTH)]] | None = Field(
        default=None,
        max_length=MAX_RETRIEVAL_KEYS,
        description=(
            "Exact-match identifiers this entity answers to (error strings, symbols, "
            "config flags, aliases). Matched case-insensitively against "
            "identifier-shaped queries, so a key may name something the content "
            "never spells out."
        ),
    )

    @field_validator("retrieval_keys")
    @classmethod
    def _reject_unprintable_retrieval_keys(cls, value: list[str] | None) -> list[str] | None:
        """Refuse a control character here so the caller gets a 422, not a 500.

        The key contract raises on control characters at the write boundary, and
        that exception has no handler on this route, so the one input class the
        length and count bounds do not cover would surface as a server error.
        """
        if value is None:
            return None
        for key in value:
            if any(unicodedata.category(char) == "Cc" for char in key):
                raise ValueError("retrieval keys must not contain control characters")
        return value


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
    """Schema for updating an entity (all fields optional).

    ``spans`` re-declares the cut plan for the body this update stores. A content
    change without fresh spans drops any stored plan and hands the body back to
    the mechanical cutter, because offsets are only meaningful against the text
    they were computed over.
    """

    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    content: str | None = Field(default=None, max_length=50000)
    category: str | None = None
    languages: list[str] | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    spans: list[MemorySpan] | None = Field(
        default=None,
        max_length=MAX_AGENT_SPANS,
        description="Cut plan for the body this update stores, tiling it exactly",
    )
    atomic: bool | None = Field(
        default=None,
        description=(
            "Re-declare (or with false, withdraw) the claim that this body is one "
            "retrievable unit. Omitted leaves any stored claim in place."
        ),
    )


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
    background_jobs: dict[str, Any] = Field(default_factory=dict)
    probe_rehearsal: dict[str, Any] | None = Field(
        default=None,
        description="Per-probe rank-or-absent from the write-time rehearsal, when probes were sent",
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


class EntityBackgroundJobsRequeueRequest(BaseModel):
    """Requeue derived entity work after a broker state loss."""

    entity_ids: list[str] = Field(default_factory=list, max_length=128)
    manifest_id: str | None = Field(default=None, min_length=1, max_length=2048)
    jobs: list[Literal["embedding_backfill", "memory_projection"]] = Field(
        ...,
        min_length=1,
        max_length=2,
    )

    @model_validator(mode="after")
    def validate_recovery_target(self) -> Self:
        if bool(self.entity_ids) == bool(self.manifest_id):
            raise ValueError("provide exactly one of entity_ids or manifest_id")
        if self.manifest_id and set(self.jobs) != {"embedding_backfill"}:
            raise ValueError("manifest recovery only supports embedding_backfill")
        return self


class EntityBackgroundJobsRequeueResponse(BaseModel):
    """Background jobs recreated from persisted graph entities."""

    entity_ids: list[str]
    manifest_id: str | None = None
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
