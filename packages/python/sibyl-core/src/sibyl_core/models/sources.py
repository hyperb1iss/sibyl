"""Source and document models for documentation crawling and imports."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from sibyl_core.models.entities import Entity, EntityType


class SourceType(StrEnum):
    """Types of knowledge sources."""

    WEBSITE = "website"  # Documentation website
    GITHUB = "github"  # GitHub repository
    LOCAL = "local"  # Local file path
    API_DOCS = "api_docs"  # API documentation (OpenAPI, etc.)


class SourcePrivacyClass(StrEnum):
    """Privacy class declared by a source adapter."""

    PERSONAL = "personal"
    PRIVATE = "private"
    SENSITIVE = "sensitive"
    PROJECT = "project"
    ORGANIZATION = "organization"
    PUBLIC = "public"


class SourceTransformBehavior(StrEnum):
    """Transformation expectation for an imported source record."""

    RAW = "raw"
    NORMALIZED = "normalized"
    METADATA_ONLY = "metadata_only"


class SourceAdapterCapability(StrEnum):
    """Capabilities advertised by source adapters."""

    ATTACHMENTS = "attachments"
    CHECKPOINTS = "checkpoints"
    INCREMENTAL = "incremental"
    SKIPPED_RECORDS = "skipped_records"


class SourceAdapterDescriptor(BaseModel):
    """Stable source adapter identity and behavior contract."""

    name: str = Field(..., min_length=1, max_length=80)
    version: str = Field(..., min_length=1, max_length=80)
    source_type: str = Field(..., min_length=1, max_length=80)
    display_name: str = Field(default="", max_length=160)
    capabilities: list[SourceAdapterCapability] = Field(default_factory=list)
    default_privacy_class: SourcePrivacyClass = SourcePrivacyClass.PERSONAL
    transform_behavior: SourceTransformBehavior = SourceTransformBehavior.RAW
    metadata_schema: dict[str, Any] = Field(default_factory=dict)
    supports_incremental: bool = False


class SourceDedupeKey(BaseModel):
    """Structured inputs and stable value for source-record dedupe."""

    adapter_name: str = Field(..., min_length=1)
    source_identity: str = Field(..., min_length=1)
    source_version: str = Field(..., min_length=1)
    adapter_record_id: str = Field(..., min_length=1)
    content_hash: str = Field(..., min_length=1)
    value: str = Field(..., min_length=1)


class SourceImportCheckpoint(BaseModel):
    """Resumable adapter checkpoint after a bounded batch."""

    cursor: str | None = None
    source_version: str | None = None
    records_seen: int = Field(default=0, ge=0)
    records_imported: int = Field(default=0, ge=0)
    records_skipped: int = Field(default=0, ge=0)
    done: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceImportManifest(BaseModel):
    """Import manifest produced by an adapter and checked by import services."""

    adapter_name: str = Field(..., min_length=1, max_length=80)
    adapter_version: str = Field(..., min_length=1, max_length=80)
    source_identity: str = Field(..., min_length=1, max_length=500)
    source_uri: str | None = Field(default=None, max_length=2000)
    source_version: str = Field(default="unknown", min_length=1, max_length=200)
    target_memory_scope: str = Field(default="private", min_length=1)
    target_scope_key: str | None = Field(default=None, max_length=500)
    privacy_class: SourcePrivacyClass = SourcePrivacyClass.PERSONAL
    transform_behavior: SourceTransformBehavior = SourceTransformBehavior.RAW
    metadata_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = Field(default=None, max_length=500)


class SourceAttachmentRecord(BaseModel):
    """Attachment metadata emitted by an adapter for a source record."""

    adapter_attachment_id: str = Field(..., min_length=1, max_length=500)
    filename: str = Field(..., min_length=1, max_length=500)
    media_type: str | None = Field(default=None, max_length=200)
    size_bytes: int | None = Field(default=None, ge=0)
    content_hash: str | None = Field(default=None, max_length=200)
    source_path: str | None = Field(default=None, max_length=2000)
    storage_pointer: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceRecord(BaseModel):
    """Source-preserving record emitted by a source adapter."""

    adapter_record_id: str = Field(..., min_length=1, max_length=500)
    source_id: str = Field(..., min_length=1, max_length=500)
    source_type: str = Field(..., min_length=1, max_length=80)
    source_uri: str | None = Field(default=None, max_length=2000)
    source_version: str = Field(default="unknown", min_length=1, max_length=200)
    title: str = Field(default="", max_length=1000)
    body: str = Field(default="")
    content_hash: str = Field(..., min_length=1, max_length=200)
    dedupe_key: str = Field(..., min_length=1, max_length=500)
    privacy_class: SourcePrivacyClass = SourcePrivacyClass.PERSONAL
    transform_behavior: SourceTransformBehavior = SourceTransformBehavior.RAW
    transform_version: str | None = Field(default=None, max_length=200)
    occurred_at: datetime | None = None
    participants: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachments: list[SourceAttachmentRecord] = Field(default_factory=list)


class SourceSkippedRecord(BaseModel):
    """Adapter-reported skipped source item."""

    adapter_record_id: str | None = Field(default=None, max_length=500)
    source_uri: str | None = Field(default=None, max_length=2000)
    reason: str = Field(..., min_length=1, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceRecordBatch(BaseModel):
    """Bounded adapter output with a resume checkpoint."""

    records: list[SourceRecord] = Field(default_factory=list)
    skipped: list[SourceSkippedRecord] = Field(default_factory=list)
    checkpoint: SourceImportCheckpoint


class CrawlStatus(StrEnum):
    """Status of a crawl operation."""

    PENDING = "pending"  # Not yet started
    IN_PROGRESS = "in_progress"  # Currently crawling
    COMPLETED = "completed"  # Successfully finished
    FAILED = "failed"  # Crawl failed
    PARTIAL = "partial"  # Some pages succeeded, some failed


class ChunkType(StrEnum):
    """Type of content chunk."""

    TEXT = "text"
    CODE = "code"
    HEADING = "heading"
    LIST = "list"
    TABLE = "table"


class Source(Entity):
    """A crawlable documentation source (URL, repo, local path)."""

    entity_type: EntityType = EntityType.SOURCE

    # Source identification
    url: str = Field(..., description="Base URL or path to crawl")
    source_type: SourceType = Field(default=SourceType.WEBSITE, description="Type of source")

    # Auto-detected metadata
    tags: list[str] = Field(default_factory=list, description="Auto-detected tags from content")
    categories: list[str] = Field(default_factory=list, description="Content categories")

    # Crawl configuration
    crawl_depth: int = Field(default=2, ge=0, le=10, description="Maximum depth to follow links")
    crawl_patterns: list[str] = Field(
        default_factory=list, description="URL patterns to include (regex)"
    )
    exclude_patterns: list[str] = Field(
        default_factory=list, description="URL patterns to exclude (regex)"
    )
    schedule: str | None = Field(default=None, description="Cron schedule for periodic crawls")
    respect_robots: bool = Field(default=True, description="Respect robots.txt")

    # Crawl status
    last_crawled: datetime | None = Field(
        default=None, description="Last successful crawl timestamp"
    )
    crawl_status: CrawlStatus = Field(
        default=CrawlStatus.PENDING, description="Current crawl status"
    )
    crawl_error: str | None = Field(default=None, description="Last crawl error message if failed")

    # Statistics
    document_count: int = Field(default=0, description="Number of documents crawled")
    total_tokens: int = Field(default=0, description="Total tokens across all documents")
    total_entities: int = Field(default=0, description="Total entities extracted")

    @model_validator(mode="before")
    @classmethod
    def set_entity_fields(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Set name and content from source-specific fields."""
        if isinstance(data, dict):
            if "name" not in data and "url" in data:
                url = data["url"].rstrip("/")
                if "://" in url:
                    url = url.split("://", 1)[1]
                data["name"] = url[:100]
            if "content" not in data:
                data["content"] = data.get("description") or data.get("url", "")
        return data


class Document(Entity):
    """A crawled document/page from a source."""

    entity_type: EntityType = EntityType.DOCUMENT

    # Source linkage
    source_id: str = Field(..., description="Parent Source entity UUID")

    # Document identification
    url: str = Field(..., description="Full URL of this document")
    title: str = Field(default="", description="Page title")
    content: str = Field(default="", description="Extracted markdown content")

    # Hierarchy
    parent_url: str | None = Field(default=None, description="Parent page URL for hierarchy")
    section_path: list[str] = Field(
        default_factory=list, description="Breadcrumb path (e.g., ['Docs', 'API', 'Auth'])"
    )
    depth: int = Field(default=0, description="Depth from source root")

    # Extracted content
    extracted_entities: list[str] = Field(
        default_factory=list, description="Entity IDs extracted from this document"
    )
    headings: list[str] = Field(default_factory=list, description="H1/H2/H3 headings in document")
    links: list[str] = Field(default_factory=list, description="Outgoing links found in document")

    # Crawl metadata
    crawled_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="When this document was crawled"
    )
    content_hash: str = Field(default="", description="Hash of content for change detection")
    word_count: int = Field(default=0, description="Number of words in content")
    token_count: int = Field(default=0, description="Estimated token count for LLM context")

    # Quality signals
    is_index: bool = Field(default=False, description="Is this an index/listing page?")
    has_code: bool = Field(default=False, description="Does this document contain code blocks?")
    language: str | None = Field(
        default=None, description="Primary programming language if code-focused"
    )

    @model_validator(mode="before")
    @classmethod
    def set_entity_fields(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Set name from document-specific fields."""
        if isinstance(data, dict) and "name" not in data:
            title = data.get("title", "")
            if title:
                data["name"] = title
            else:
                url = data.get("url", "")
                segment = url.rstrip("/").split("/")[-1] if url else ""
                data["name"] = segment or "Untitled"
        return data


class Community(Entity):
    """Entity cluster from community detection (GraphRAG)."""

    entity_type: EntityType = EntityType.COMMUNITY

    # Community structure
    member_ids: list[str] = Field(default_factory=list, description="Entity IDs in this community")
    member_count: int = Field(default=0, description="Number of members")

    # Hierarchy (one Louvain pass per configured resolution)
    level: int = Field(default=0, description="Hierarchy level (0 = leaf, higher = broader)")
    parent_community_id: str | None = Field(
        default=None, description="Parent community at higher level"
    )
    child_community_ids: list[str] = Field(
        default_factory=list, description="Child communities at lower level"
    )

    # Summarization (Microsoft GraphRAG approach)
    summary: str = Field(default="", description="LLM-generated summary of community")
    key_concepts: list[str] = Field(
        default_factory=list, description="Key concepts/themes in this community"
    )
    representative_entities: list[str] = Field(
        default_factory=list, description="Most central/representative entity IDs"
    )

    # Community metrics
    modularity: float | None = Field(default=None, description="Modularity score of this community")
    density: float | None = Field(default=None, description="Internal edge density")

    @model_validator(mode="before")
    @classmethod
    def set_entity_fields(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Set name and content from community-specific fields."""
        if isinstance(data, dict):
            if "name" not in data:
                key_concepts = data.get("key_concepts", [])
                if key_concepts:
                    data["name"] = ", ".join(key_concepts[:3])
                else:
                    level = data.get("level", 0)
                    member_count = data.get("member_count", 0)
                    data["name"] = f"Community L{level} ({member_count} members)"
            if "content" not in data:
                data["content"] = data.get("summary", "")
        return data
