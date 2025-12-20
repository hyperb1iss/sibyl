"""Pydantic schemas for API request/response models.

These map directly to TypeScript interfaces via OpenAPI generation.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from sibyl.models.entities import EntityType, RelationshipType

# =============================================================================
# Entity Schemas
# =============================================================================


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


class EntityUpdate(BaseModel):
    """Schema for updating an entity (all fields optional)."""

    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    content: str | None = Field(default=None, max_length=50000)
    category: str | None = None
    languages: list[str] | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class EntityResponse(EntityBase):
    """Full entity response with all fields."""

    id: str = Field(..., description="Unique entity ID")
    entity_type: EntityType = Field(..., description="Type of entity")
    source_file: str | None = Field(default=None, description="Source file path")
    created_at: datetime | None = Field(default=None, description="Creation timestamp")
    updated_at: datetime | None = Field(default=None, description="Last update timestamp")

    model_config = {"from_attributes": True}


class EntityListResponse(BaseModel):
    """Paginated list of entities."""

    entities: list[EntityResponse]
    total: int
    page: int
    page_size: int
    has_more: bool


# =============================================================================
# Search Schemas
# =============================================================================


class SearchRequest(BaseModel):
    """Search request body."""

    query: str = Field(..., min_length=1, description="Natural language search query")
    types: list[str] | None = Field(default=None, description="Filter by entity types")
    language: str | None = Field(default=None, description="Filter by programming language")
    category: str | None = Field(default=None, description="Filter by category")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum results")
    include_content: bool = Field(default=True, description="Include full content")


class SearchResult(BaseModel):
    """Single search result."""

    id: str
    type: str
    name: str
    content: str
    score: float
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    """Search results response."""

    results: list[SearchResult]
    total: int
    query: str
    filters: dict[str, Any]


# =============================================================================
# Explore Schemas
# =============================================================================


class ExploreRequest(BaseModel):
    """Explore/graph traversal request."""

    mode: Literal["list", "related", "traverse", "dependencies"] = Field(
        default="list", description="Exploration mode"
    )
    types: list[str] | None = Field(default=None, description="Entity types to explore")
    entity_id: str | None = Field(default=None, description="Starting entity for traversal")
    relationship_types: list[str] | None = Field(default=None, description="Filter relationships")
    depth: int = Field(default=1, ge=1, le=3, description="Traversal depth")
    language: str | None = None
    category: str | None = None
    project: str | None = Field(default=None, description="Filter by project ID (for tasks)")
    status: str | None = Field(default=None, description="Filter by status (for tasks)")
    limit: int = Field(default=50, ge=1, le=200)


class RelatedEntity(BaseModel):
    """Entity related through the graph."""

    id: str
    type: str
    name: str
    relationship: str
    direction: Literal["outgoing", "incoming"]
    distance: int = 1


class ExploreResponse(BaseModel):
    """Explore results response."""

    mode: str
    entities: list[dict[str, Any]]  # Can be EntitySummary or RelatedEntity
    total: int
    filters: dict[str, Any]


# =============================================================================
# Graph Visualization Schemas
# =============================================================================


class GraphNode(BaseModel):
    """Node for graph visualization."""

    id: str = Field(..., description="Unique node ID")
    type: str = Field(..., description="Entity type")
    label: str = Field(..., description="Display label")
    color: str = Field(..., description="Node color (hex)")
    size: float = Field(default=1.0, description="Relative node size")
    x: float | None = Field(default=None, description="X position (if pre-computed)")
    y: float | None = Field(default=None, description="Y position (if pre-computed)")
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """Edge for graph visualization."""

    id: str = Field(..., description="Unique edge ID")
    source: str = Field(..., description="Source node ID")
    target: str = Field(..., description="Target node ID")
    type: str = Field(..., description="Relationship type")
    label: str = Field(default="", description="Edge label")
    weight: float = Field(default=1.0, description="Edge weight/thickness")


class GraphData(BaseModel):
    """Full graph data for visualization."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    node_count: int
    edge_count: int


class SubgraphRequest(BaseModel):
    """Request for subgraph around an entity."""

    entity_id: str = Field(..., description="Center entity ID")
    depth: int = Field(default=2, ge=1, le=4, description="Traversal depth")
    relationship_types: list[RelationshipType] | None = Field(
        default=None, description="Filter relationship types"
    )
    max_nodes: int = Field(default=100, ge=1, le=500, description="Maximum nodes to return")


# =============================================================================
# Admin Schemas
# =============================================================================


class HealthResponse(BaseModel):
    """Server health status."""

    status: Literal["healthy", "unhealthy", "unknown"]
    server_name: str
    uptime_seconds: int
    graph_connected: bool
    entity_counts: dict[str, int]
    errors: list[str]


class StatsResponse(BaseModel):
    """Knowledge graph statistics."""

    entity_counts: dict[str, int]
    total_entities: int
    relationship_counts: dict[str, int] | None = None
    total_relationships: int | None = None


class IngestRequest(BaseModel):
    """Ingestion request."""

    path: str | None = Field(default=None, description="Specific path to ingest")
    force: bool = Field(default=False, description="Force re-ingestion")


class IngestResponse(BaseModel):
    """Ingestion result."""

    success: bool
    files_processed: int
    entities_created: int
    entities_updated: int
    duration_seconds: float
    errors: list[str]


# =============================================================================
# WebSocket Event Schemas
# =============================================================================


class WebSocketEvent(BaseModel):
    """Event sent over WebSocket for realtime updates."""

    event: Literal[
        "entity_created",
        "entity_updated",
        "entity_deleted",
        "search_complete",
        "ingest_progress",
        "ingest_complete",
        "health_update",
    ]
    data: dict[str, Any]
    timestamp: datetime = Field(default_factory=datetime.utcnow)
