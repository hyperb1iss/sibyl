"""SQLModel schemas for document storage with pgvector support.

This module defines the PostgreSQL tables for storing crawled documents,
chunks, and embeddings. Uses pgvector for efficient vector similarity search.

Architecture:
- CrawlSource: Track documentation sources (websites, repos)
- CrawledDocument: Store raw crawled documents
- DocumentChunk: Store chunked content with embeddings for hybrid search
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, Column, Index, String, Text, text
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    pass


def utcnow_naive() -> datetime:
    """Get current UTC time as naive datetime (for TIMESTAMP WITHOUT TIME ZONE)."""
    return datetime.now(UTC).replace(tzinfo=None)


# =============================================================================
# Enums
# =============================================================================


class SourceType(StrEnum):
    """Types of documentation sources."""

    WEBSITE = "website"
    GITHUB = "github"
    LOCAL = "local"
    API_DOCS = "api_docs"


class CrawlStatus(StrEnum):
    """Status of a crawl operation."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


class ChunkType(StrEnum):
    """Type of content chunk."""

    TEXT = "text"
    CODE = "code"
    HEADING = "heading"
    LIST = "list"
    TABLE = "table"


# =============================================================================
# Base Model
# =============================================================================


class TimestampMixin(SQLModel):
    """Mixin for created/updated timestamps."""

    created_at: datetime = Field(
        default_factory=utcnow_naive,
        description="When this record was created",
    )
    updated_at: datetime = Field(
        default_factory=utcnow_naive,
        description="When this record was last updated",
        sa_column_kwargs={"onupdate": utcnow_naive},
    )


# =============================================================================
# CrawlSource - Documentation sources to crawl
# =============================================================================


class CrawlSource(TimestampMixin, table=True):
    """A documentation source to be crawled.

    Tracks configuration and status for each documentation source.
    One source can have many documents.
    """

    __tablename__ = "crawl_sources"  # type: ignore[assignment]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=255, index=True, description="Human-readable source name")
    url: str = Field(max_length=2048, unique=True, description="Base URL or path")
    source_type: SourceType = Field(default=SourceType.WEBSITE, description="Type of source")
    description: str | None = Field(default=None, sa_type=Text, description="Source description")

    # Crawl configuration
    crawl_depth: int = Field(default=2, ge=0, le=10, description="Max link follow depth")
    include_patterns: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),
        description="URL patterns to include (regex)",
    )
    exclude_patterns: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),
        description="URL patterns to exclude (regex)",
    )
    respect_robots: bool = Field(default=True, description="Respect robots.txt")

    # Crawl status
    crawl_status: CrawlStatus = Field(default=CrawlStatus.PENDING, description="Current status")
    last_crawled_at: datetime | None = Field(default=None, description="Last successful crawl")
    last_error: str | None = Field(default=None, sa_type=Text, description="Last error message")

    # Statistics
    document_count: int = Field(default=0, ge=0, description="Number of documents crawled")
    chunk_count: int = Field(default=0, ge=0, description="Total chunks across documents")
    total_tokens: int = Field(default=0, ge=0, description="Total tokens processed")

    # Relationships
    documents: list["CrawledDocument"] = Relationship(
        back_populates="source",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    def __repr__(self) -> str:
        return f"<CrawlSource {self.name} ({self.url})>"


# =============================================================================
# CrawledDocument - Raw crawled pages
# =============================================================================


class CrawledDocument(TimestampMixin, table=True):
    """A crawled document/page from a source.

    Stores the raw content and metadata for each crawled page.
    One document has many chunks.
    """

    __tablename__ = "crawled_documents"  # type: ignore[assignment]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    source_id: UUID = Field(foreign_key="crawl_sources.id", index=True)
    url: str = Field(max_length=2048, unique=True, description="Full page URL")
    title: str = Field(max_length=512, default="", description="Page title")

    # Content
    raw_content: str = Field(default="", sa_type=Text, description="Raw HTML/markdown")
    content: str = Field(default="", sa_type=Text, description="Extracted clean text")
    content_hash: str = Field(max_length=64, default="", description="SHA256 of content")

    # Hierarchy
    parent_url: str | None = Field(default=None, max_length=2048, description="Parent page URL")
    section_path: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),
        description="Breadcrumb path",
    )
    depth: int = Field(default=0, ge=0, description="Depth from source root")

    # Metadata
    language: str | None = Field(default=None, max_length=10, description="Primary language code")
    word_count: int = Field(default=0, ge=0, description="Word count")
    token_count: int = Field(default=0, ge=0, description="Estimated token count")
    has_code: bool = Field(default=False, description="Contains code blocks")
    is_index: bool = Field(default=False, description="Is an index/listing page")

    # Extracted data
    headings: list[str] = Field(default_factory=list, sa_type=ARRAY(String), description="Page headings")
    links: list[str] = Field(default_factory=list, sa_type=ARRAY(String), description="Outgoing links")
    code_languages: list[str] = Field(
        default_factory=list, sa_type=ARRAY(String), description="Languages in code blocks"
    )

    # Crawl metadata
    crawled_at: datetime = Field(
        default_factory=utcnow_naive,
        description="When this page was crawled",
    )
    http_status: int | None = Field(default=None, description="HTTP response status")

    # Relationships
    source: CrawlSource = Relationship(back_populates="documents")
    chunks: list["DocumentChunk"] = Relationship(
        back_populates="document",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    def __repr__(self) -> str:
        return f"<CrawledDocument {self.title or self.url[:50]}>"


# =============================================================================
# DocumentChunk - Chunked content with embeddings
# =============================================================================


class DocumentChunk(TimestampMixin, table=True):
    """A chunk of document content with embedding.

    Stores chunked content for hybrid retrieval:
    - Dense vector for semantic search (pgvector)
    - Full text for BM25 search (tsvector)
    - Sparse vector for learned sparse retrieval
    """

    __tablename__ = "document_chunks"  # type: ignore[assignment]
    __table_args__ = (
        # Full-text search index
        Index(
            "ix_chunks_content_fts",
            text("to_tsvector('english', content)"),
            postgresql_using="gin",
        ),
        # Vector similarity index (IVFFlat for speed, HNSW for accuracy)
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    document_id: UUID = Field(foreign_key="crawled_documents.id", index=True)

    # Chunk identification
    chunk_index: int = Field(ge=0, description="Position in document")
    chunk_type: ChunkType = Field(default=ChunkType.TEXT, description="Type of chunk")

    # Content
    content: str = Field(sa_type=Text, description="Chunk text content")
    context: str | None = Field(
        default=None,
        sa_type=Text,
        description="Contextual prefix (Anthropic technique)",
    )
    token_count: int = Field(default=0, ge=0, description="Token count for this chunk")

    # Location in document
    start_char: int = Field(default=0, ge=0, description="Start character offset")
    end_char: int = Field(default=0, ge=0, description="End character offset")
    heading_path: list[str] = Field(
        default_factory=list, sa_type=ARRAY(String), description="Heading hierarchy to this chunk"
    )

    # Embeddings - using 1536 dims for OpenAI ada-002
    # Will add support for other models via config
    embedding: Any = Field(
        default=None,
        sa_column=Column(Vector(1536), nullable=True),
        description="Dense embedding vector",
    )

    # Code-specific metadata
    language: str | None = Field(default=None, max_length=50, description="Code language if code")
    is_complete: bool = Field(default=True, description="Is this a complete code block")

    # Quality signals
    has_entities: bool = Field(default=False, description="Contains named entities")
    entity_ids: list[str] = Field(
        default_factory=list, sa_type=ARRAY(String), description="Extracted entity UUIDs"
    )

    # Relationships
    document: CrawledDocument = Relationship(back_populates="chunks")

    def __repr__(self) -> str:
        return f"<DocumentChunk {self.id} [{self.chunk_type}]>"


# =============================================================================
# Utility functions
# =============================================================================


def create_tables_sql() -> str:
    """Generate SQL for creating tables and extensions.

    Returns raw SQL for manual execution or Alembic migrations.
    """
    return """
    -- Enable pgvector extension
    CREATE EXTENSION IF NOT EXISTS vector;

    -- Create enum types
    CREATE TYPE source_type AS ENUM ('website', 'github', 'local', 'api_docs');
    CREATE TYPE crawl_status AS ENUM ('pending', 'in_progress', 'completed', 'failed', 'partial');
    CREATE TYPE chunk_type AS ENUM ('text', 'code', 'heading', 'list', 'table');
    """
