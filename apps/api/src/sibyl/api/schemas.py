"""Pydantic schemas for API request/response models.

These map directly to TypeScript interfaces via OpenAPI generation.
"""

from datetime import UTC, datetime
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
    """Unified search request - searches both knowledge graph AND documentation.

    By default, searches both stores and merges results by relevance.
    Use filters to narrow scope.
    """

    query: str = Field(..., min_length=1, description="Natural language search query")
    types: list[str] | None = Field(
        default=None,
        description="Filter by entity types. Options: pattern, rule, template, topic, "
        "episode, task, project, document. 'document' searches crawled docs.",
    )
    language: str | None = Field(default=None, description="Filter by programming language")
    category: str | None = Field(default=None, description="Filter by category")
    status: str | None = Field(default=None, description="Filter tasks by status")
    project: str | None = Field(default=None, description="Filter tasks by project ID")
    source: str | None = Field(default=None, description="Alias for source_name")
    source_id: str | None = Field(default=None, description="Filter documents by source ID")
    source_name: str | None = Field(default=None, description="Filter documents by source name")
    assignee: str | None = Field(default=None, description="Filter tasks by assignee name")
    since: str | None = Field(
        default=None, description="Filter by creation date (ISO: 2024-03-15 or relative: 7d, 2w)"
    )
    limit: int = Field(default=10, ge=1, le=50, description="Maximum results")
    offset: int = Field(default=0, ge=0, description="Offset for pagination")
    include_content: bool = Field(default=True, description="Include full content in results")
    include_documents: bool = Field(
        default=True, description="Include crawled documentation in search"
    )
    include_graph: bool = Field(default=True, description="Include knowledge graph entities")
    use_enhanced: bool = Field(default=True, description="Use enhanced retrieval with reranking")
    boost_recent: bool = Field(default=True, description="Boost recent results in ranking")


class SearchResult(BaseModel):
    """Single search result - unified across graph entities and documents."""

    id: str = Field(..., description="Entity or chunk ID")
    type: str = Field(..., description="Entity type (pattern, rule, episode, etc.) or 'document'")
    name: str = Field(..., description="Entity name or document title")
    content: str = Field(..., description="Matched content")
    score: float = Field(..., description="Relevance score (0-1)")
    source: str | None = Field(default=None, description="Source file path or documentation source")
    url: str | None = Field(default=None, description="URL for documents")
    result_origin: Literal["graph", "document"] = Field(
        default="graph", description="Whether result is from knowledge graph or documents"
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    """Unified search results response."""

    results: list[SearchResult]
    total: int
    query: str
    filters: dict[str, Any]
    graph_count: int = Field(default=0, description="Number of results from knowledge graph")
    document_count: int = Field(default=0, description="Number of results from documents")
    limit: int = Field(default=10, description="Results per page")
    offset: int = Field(default=0, description="Current offset")
    has_more: bool = Field(default=False, description="Whether more results exist")


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
    epic: str | None = Field(default=None, description="Filter by epic ID (for tasks)")
    status: str | None = Field(default=None, description="Filter by status (for tasks)")
    priority: str | None = Field(
        default=None, description="Filter by priority (for tasks): critical, high, medium, low, someday"
    )
    complexity: str | None = Field(
        default=None, description="Filter by complexity (for tasks): trivial, simple, medium, complex, epic"
    )
    feature: str | None = Field(default=None, description="Filter by feature area (for tasks)")
    tags: str | None = Field(
        default=None, description="Filter by tags (comma-separated, matches if task has ANY)"
    )
    include_archived: bool = Field(
        default=False, description="Include archived projects in results"
    )
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, description="Offset for pagination")


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
    limit: int = Field(default=50, description="Results per page")
    offset: int = Field(default=0, description="Current offset")
    has_more: bool = Field(default=False, description="Whether more results exist")
    actual_total: int | None = Field(default=None, description="Total matching before pagination")


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
        "crawl_started",
        "crawl_progress",
        "crawl_complete",
    ]
    data: dict[str, Any]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# =============================================================================
# Crawler Schemas
# =============================================================================


class CrawlSourceCreate(BaseModel):
    """Create a crawl source."""

    name: str = Field(..., description="Human-readable name")
    url: str = Field(..., description="Base URL to crawl")
    source_type: Literal["website", "github", "local", "api_docs"] = Field(
        default="website", description="Type of documentation source"
    )
    description: str | None = Field(default=None, description="Optional description")
    crawl_depth: int = Field(default=2, ge=1, le=5, description="Maximum link depth")
    include_patterns: list[str] = Field(
        default_factory=list, description="URL patterns to include (regex)"
    )
    exclude_patterns: list[str] = Field(
        default_factory=list, description="URL patterns to exclude (regex)"
    )


class CrawlSourceUpdate(BaseModel):
    """Update a crawl source."""

    name: str | None = Field(default=None, description="Human-readable name")
    description: str | None = Field(default=None, description="Optional description")
    crawl_depth: int | None = Field(default=None, ge=1, le=5, description="Maximum link depth")
    include_patterns: list[str] | None = Field(default=None, description="URL patterns to include")
    exclude_patterns: list[str] | None = Field(default=None, description="URL patterns to exclude")


class CrawlSourceResponse(BaseModel):
    """Crawl source with status."""

    id: str
    name: str
    url: str
    source_type: str
    description: str | None = None
    crawl_depth: int
    crawl_status: str  # pending, in_progress, completed, failed, partial
    document_count: int
    chunk_count: int
    last_crawled_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(default_factory=list)


class CrawlSourceListResponse(BaseModel):
    """List of crawl sources."""

    sources: list[CrawlSourceResponse]
    total: int


class CrawlDocumentResponse(BaseModel):
    """Crawled document summary."""

    id: str
    source_id: str
    url: str
    title: str
    word_count: int
    has_code: bool
    is_index: bool
    depth: int
    crawled_at: datetime
    headings: list[str] = Field(default_factory=list)
    code_languages: list[str] = Field(default_factory=list)


class CrawlDocumentListResponse(BaseModel):
    """List of crawled documents."""

    documents: list[CrawlDocumentResponse]
    total: int


class CrawlIngestRequest(BaseModel):
    """Request to start crawling a source."""

    max_pages: int = Field(default=50, ge=1, le=500, description="Maximum pages to crawl")
    max_depth: int = Field(default=3, ge=1, le=5, description="Maximum link depth")
    generate_embeddings: bool = Field(default=True, description="Generate embeddings for chunks")


class CrawlIngestResponse(BaseModel):
    """Response from starting a crawl."""

    source_id: str
    job_id: str | None = None  # Job ID for cancellation
    status: str  # queued, already_running, cancelled
    message: str


class CrawlStatsResponse(BaseModel):
    """Crawler statistics."""

    total_sources: int
    total_documents: int
    total_chunks: int
    chunks_with_embeddings: int
    sources_by_status: dict[str, int]


class CrawlHealthResponse(BaseModel):
    """Crawler health status."""

    postgres_healthy: bool
    postgres_version: str | None = None
    pgvector_version: str | None = None
    crawl4ai_available: bool
    error: str | None = None


class LinkGraphRequest(BaseModel):
    """Request to link document chunks to the knowledge graph."""

    batch_size: int = Field(default=50, ge=1, le=200, description="Chunks per batch")
    dry_run: bool = Field(default=False, description="Preview without processing")


class LinkGraphResponse(BaseModel):
    """Response from graph linking operation."""

    source_id: str | None = None  # None if processing all sources
    status: str  # completed, dry_run, error, no_chunks
    chunks_processed: int = 0
    chunks_remaining: int = 0  # Unprocessed chunks still pending
    entities_extracted: int = 0
    entities_linked: int = 0
    sources_processed: list[str] = Field(default_factory=list)
    message: str | None = None
    error: str | None = None


class LinkGraphStatusResponse(BaseModel):
    """Status of pending graph linking work."""

    total_chunks: int = 0
    chunks_with_entities: int = 0
    chunks_pending: int = 0
    sources: list[dict[str, int | str]] = Field(default_factory=list)  # [{name, pending}]


# =============================================================================
# RAG Search Schemas
# =============================================================================


class RAGSearchRequest(BaseModel):
    """RAG search request for document chunks."""

    query: str = Field(..., min_length=1, description="Natural language search query")
    source_id: str | None = Field(default=None, description="Filter by source ID")
    source_name: str | None = Field(
        default=None, description="Filter by source name (partial match)"
    )
    match_count: int = Field(default=10, ge=1, le=100, description="Number of results")
    similarity_threshold: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Minimum similarity score"
    )
    return_mode: Literal["chunks", "pages"] = Field(
        default="chunks", description="Return chunks or full pages"
    )
    include_context: bool = Field(default=True, description="Include contextual prefix in results")


class RAGChunkResult(BaseModel):
    """Single chunk result from RAG search."""

    chunk_id: str
    document_id: str
    source_id: str
    source_name: str
    url: str
    title: str
    content: str
    context: str | None = None
    similarity: float
    chunk_type: str
    chunk_index: int
    heading_path: list[str] = Field(default_factory=list)
    language: str | None = None


class RAGPageResult(BaseModel):
    """Full page result from RAG search."""

    document_id: str
    source_id: str
    source_name: str
    url: str
    title: str
    content: str
    word_count: int
    has_code: bool
    headings: list[str] = Field(default_factory=list)
    code_languages: list[str] = Field(default_factory=list)
    best_chunk_similarity: float


class RAGSearchResponse(BaseModel):
    """RAG search response."""

    results: list[RAGChunkResult | RAGPageResult]
    total: int
    query: str
    source_filter: str | None = None
    return_mode: str


class CodeExampleRequest(BaseModel):
    """Search for code examples."""

    query: str = Field(..., min_length=1, description="Search query for code")
    language: str | None = Field(default=None, description="Filter by programming language")
    source_id: str | None = Field(default=None, description="Filter by source")
    match_count: int = Field(default=10, ge=1, le=50, description="Number of results")


class CodeExampleResult(BaseModel):
    """Code example result."""

    chunk_id: str
    document_id: str
    source_name: str
    url: str
    title: str
    code: str
    context: str | None = None
    language: str | None = None
    similarity: float
    heading_path: list[str] = Field(default_factory=list)


class CodeExampleResponse(BaseModel):
    """Code example search response."""

    examples: list[CodeExampleResult]
    total: int
    query: str
    language_filter: str | None = None


class FullPageRequest(BaseModel):
    """Request full page content."""

    document_id: str | None = Field(default=None, description="Get by document ID")
    url: str | None = Field(default=None, description="Get by URL")


class FullPageResponse(BaseModel):
    """Full page content response."""

    document_id: str
    source_id: str
    source_name: str
    url: str
    title: str
    content: str
    raw_content: str | None = None
    word_count: int
    token_count: int
    has_code: bool
    headings: list[str] = Field(default_factory=list)
    code_languages: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    crawled_at: datetime


class SourcePagesRequest(BaseModel):
    """Request to list pages for a source."""

    source_id: str = Field(..., description="Source ID")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum pages")
    offset: int = Field(default=0, ge=0, description="Offset for pagination")
    has_code: bool | None = Field(default=None, description="Filter by code presence")
    is_index: bool | None = Field(default=None, description="Filter index pages")


class SourcePagesResponse(BaseModel):
    """List of pages for a source."""

    source_id: str
    source_name: str
    pages: list[CrawlDocumentResponse]
    total: int
    has_more: bool


class DocumentUpdateRequest(BaseModel):
    """Update a crawled document's content."""

    title: str | None = Field(default=None, max_length=512, description="New document title")
    content: str | None = Field(default=None, max_length=500000, description="New document content")


class DocumentRelatedEntity(BaseModel):
    """An entity related to a document through extraction."""

    id: str
    name: str
    entity_type: str
    description: str = ""
    chunk_count: int = Field(default=1, description="Number of chunks mentioning this entity")


class DocumentRelatedEntitiesResponse(BaseModel):
    """Related entities for a document."""

    document_id: str
    entities: list[DocumentRelatedEntity]
    total: int


# === Backup/Restore Schemas ===


class BackupDataSchema(BaseModel):
    """Graph backup data structure."""

    version: str
    created_at: str
    organization_id: str
    entity_count: int
    relationship_count: int
    entities: list[dict]
    relationships: list[dict]


class BackupResponse(BaseModel):
    """Response from backup operation."""

    success: bool
    entity_count: int
    relationship_count: int
    message: str
    duration_seconds: float
    backup_data: BackupDataSchema | None = None


class RestoreRequest(BaseModel):
    """Request to restore from backup."""

    backup_data: BackupDataSchema
    skip_existing: bool = True


class RestoreResponse(BaseModel):
    """Response from restore operation."""

    success: bool
    entities_restored: int
    relationships_restored: int
    entities_skipped: int
    relationships_skipped: int
    errors: list[str]
    duration_seconds: float


class BackfillRequest(BaseModel):
    """Request to backfill missing relationships."""

    dry_run: bool = Field(
        default=False, description="If true, report what would be done without making changes"
    )


class BackfillResponse(BaseModel):
    """Response from relationship backfill operation."""

    success: bool
    relationships_created: int
    tasks_without_project: int
    tasks_already_linked: int
    errors: list[str]
    duration_seconds: float
    dry_run: bool


# =============================================================================
# Metrics Schemas
# =============================================================================


class TaskStatusDistribution(BaseModel):
    """Task counts by status."""

    backlog: int = 0
    todo: int = 0
    doing: int = 0
    blocked: int = 0
    review: int = 0
    done: int = 0


class TaskPriorityDistribution(BaseModel):
    """Task counts by priority."""

    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    someday: int = 0


class AssigneeStats(BaseModel):
    """Stats per assignee."""

    name: str
    total: int = 0
    completed: int = 0
    in_progress: int = 0


class TimeSeriesPoint(BaseModel):
    """Single point in a time series."""

    date: str  # ISO date string (YYYY-MM-DD)
    value: int


class ProjectMetrics(BaseModel):
    """Metrics for a single project."""

    project_id: str
    project_name: str
    total_tasks: int
    status_distribution: TaskStatusDistribution
    priority_distribution: TaskPriorityDistribution
    completion_rate: float  # 0-100
    assignees: list[AssigneeStats]
    tasks_created_last_7d: int
    tasks_completed_last_7d: int
    velocity_trend: list[TimeSeriesPoint]  # completions per day last 14 days


class ProjectMetricsResponse(BaseModel):
    """Response for project metrics."""

    metrics: ProjectMetrics


class OrgMetricsResponse(BaseModel):
    """Organization-level metrics aggregating all projects."""

    total_projects: int
    total_tasks: int
    status_distribution: TaskStatusDistribution
    priority_distribution: TaskPriorityDistribution
    completion_rate: float
    top_assignees: list[AssigneeStats]
    tasks_created_last_7d: int
    tasks_completed_last_7d: int
    velocity_trend: list[TimeSeriesPoint]
    projects_summary: list[dict[str, Any]]  # [{id, name, total, completed, completion_rate}]
