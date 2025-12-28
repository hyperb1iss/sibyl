"""Response models for operations."""

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from sibyl_core.models.entities import Entity, EntityType, Relationship


class SearchResultItem(BaseModel):
    """A single search result item."""

    entity: Entity = Field(description="The matched entity")
    score: float = Field(description="Relevance score (0-1)")
    highlights: list[str] = Field(
        default_factory=list,
        description="Highlighted matching snippets",
    )


class SearchResult(BaseModel):
    """Response from search operations."""

    query: str = Field(description="Original search query")
    results: list[SearchResultItem] = Field(description="List of matching entities")
    total_count: int = Field(description="Total number of matches")
    returned_count: int = Field(description="Number of results returned")
    search_time_ms: float = Field(description="Search execution time in milliseconds")


class EntityResponse(BaseModel):
    """Response for single entity retrieval."""

    entity: Entity = Field(description="The requested entity")
    relationships: list[Relationship] = Field(
        default_factory=list,
        description="Related relationships",
    )
    related_entities: list[Entity] = Field(
        default_factory=list,
        description="Related entities",
    )


class ListResponse(BaseModel):
    """Response for listing entities."""

    entity_type: EntityType = Field(description="Type of entities listed")
    entities: list[Entity] = Field(description="List of entities")
    total_count: int = Field(description="Total count matching filters")
    offset: int = Field(description="Current pagination offset")
    limit: int = Field(description="Items per page")


class MutationResponse(BaseModel):
    """Response for mutation operations."""

    success: bool = Field(description="Whether the operation succeeded")
    entity_id: str | None = Field(default=None, description="ID of created/updated entity")
    message: str = Field(description="Human-readable result message")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the mutation occurred",
    )


class GraphStatsResponse(BaseModel):
    """Response for graph statistics."""

    total_entities: int = Field(description="Total number of entities")
    total_relationships: int = Field(description="Total number of relationships")
    entities_by_type: dict[str, int] = Field(description="Entity count by type")
    relationships_by_type: dict[str, int] = Field(description="Relationship count by type")
    last_ingestion: datetime | None = Field(description="Last ingestion timestamp")
    graph_size_mb: float = Field(description="Approximate graph size in MB")
