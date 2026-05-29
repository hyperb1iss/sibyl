"""Crawler and source-import request/response models."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .common import MemoryScopeLiteral


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


class SourceAdapterResponse(BaseModel):
    """Registered source adapter contract."""

    name: str
    version: str
    source_type: str
    display_name: str
    capabilities: list[str] = Field(default_factory=list)
    default_privacy_class: str
    transform_behavior: str
    metadata_schema: dict[str, Any] = Field(default_factory=dict)
    supports_incremental: bool = False


class SourceAdapterListResponse(BaseModel):
    """Registered source adapter list."""

    adapters: list[SourceAdapterResponse]


SourceImportStatusLiteral = Literal[
    "pending",
    "running",
    "paused",
    "completed",
    "failed",
    "canceled",
]


class SourceImportStartRequest(BaseModel):
    """Start a bounded source import run."""

    source_uri: str = Field(..., min_length=1, max_length=2000)
    adapter_name: str = Field(default="mbox", min_length=1, max_length=80)
    target_memory_scope: MemoryScopeLiteral = Field(default="private")
    target_scope_key: str | None = Field(default=None, max_length=500)
    options: dict[str, Any] = Field(default_factory=dict)
    batch_size: int = Field(default=100, ge=1, le=1000)
    promotion_preview_approved: bool = False


class SourceImportResumeRequest(BaseModel):
    """Resume a bounded source import run from its persisted checkpoint."""

    batch_size: int | None = Field(default=None, ge=1, le=1000)
    promotion_preview_approved: bool | None = None


class SourceImportProgressResponse(BaseModel):
    """Source-safe import progress counters."""

    imported_count: int = 0
    skipped_count: int = 0
    dedupe_count: int = 0
    error_count: int = 0
    attachment_count: int = 0
    extraction_pending_count: int = 0
    raw_memory_count: int = 0


class SourceImportStatusResponse(BaseModel):
    """Source import status without raw source content."""

    import_id: str
    adapter_name: str
    adapter_version: str | None = None
    source_identity: str | None = None
    source_version: str | None = None
    status: SourceImportStatusLiteral
    privacy_class: str | None = None
    target_memory_scope: MemoryScopeLiteral | None = None
    target_scope_key: str | None = None
    checkpoint: dict[str, Any] | None = None
    progress: SourceImportProgressResponse
    raw_memory_ids: list[str] = Field(default_factory=list)
    dedupe_keys: list[str] = Field(default_factory=list)
    duplicate_dedupe_keys: list[str] = Field(default_factory=list)
    skipped_records: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


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
    # Only populated in detail view, not list view
    raw_content: str | None = None
    markdown_content: str | None = None  # Assembled from chunks


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

    relational_backend_enabled: bool
    relational_backend_healthy: bool
    relational_backend_version: str | None = None
    vector_extension_version: str | None = None
    crawl4ai_available: bool
    error: str | None = None


class LinkGraphRequest(BaseModel):
    """Request to link document chunks to the knowledge graph."""

    batch_size: int = Field(default=50, ge=1, le=200, description="Chunks per batch")
    dry_run: bool = Field(default=False, description="Preview without processing")
    create_new_entities: bool = Field(
        default=False,
        description="Create graph entities for unlinked extractions",
    )


class LinkGraphResponse(BaseModel):
    """Response from graph linking operation."""

    source_id: str | None = None  # None if processing all sources
    status: str  # completed, dry_run, error, no_chunks
    chunks_processed: int = 0
    chunks_remaining: int = 0  # Unprocessed chunks still pending
    entities_extracted: int = 0
    entities_linked: int = 0
    new_entities_created: int = 0
    sources_processed: list[str] = Field(default_factory=list)
    message: str | None = None
    error: str | None = None


class LinkGraphSourceStatus(BaseModel):
    """Pending graph-linking work for a single source."""

    source_id: str
    name: str
    pending: int


class LinkGraphStatusResponse(BaseModel):
    """Status of pending graph linking work."""

    total_chunks: int = 0
    chunks_with_entities: int = 0
    chunks_pending: int = 0
    sources: list[LinkGraphSourceStatus] = Field(default_factory=list)
