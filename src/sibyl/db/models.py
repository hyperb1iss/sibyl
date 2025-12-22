"""SQLModel schemas for Sibyl PostgreSQL storage.

This module defines the PostgreSQL tables for:
- Auth/account primitives (users)
- Crawled documents, chunks, and embeddings (pgvector)

Architecture:
- User: Auth identity (GitHub-backed for now)
- CrawlSource: Track documentation sources (websites, repos)
- CrawledDocument: Store raw crawled documents
- DocumentChunk: Store chunked content with embeddings for hybrid search
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from pydantic import field_validator
from sqlalchemy import ARRAY, Column, Enum, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
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
# User - Authentication identity
# =============================================================================


class User(TimestampMixin, table=True):
    """A user identity record (GitHub OAuth or local email/password)."""

    __tablename__ = "users"  # type: ignore[assignment]

    id: UUID = Field(default_factory=uuid4, primary_key=True)

    # GitHub identity
    github_id: int | None = Field(
        default=None,
        index=True,
        unique=True,
        description="GitHub numeric user id (null for local users)",
    )

    # Profile info
    email: str | None = Field(
        default=None,
        max_length=255,
        unique=True,
        index=True,
        description="Primary email (may be null if unavailable)",
    )
    name: str = Field(default="", max_length=255, description="Display name")
    avatar_url: str | None = Field(
        default=None,
        max_length=2048,
        description="Profile avatar URL",
    )

    # Local auth (PBKDF2 hash)
    password_salt: str | None = Field(
        default=None,
        max_length=128,
        description="Hex salt for local password hash (null if no local password)",
    )
    password_hash: str | None = Field(
        default=None,
        max_length=128,
        description="Hex PBKDF2 hash for local password (null if no local password)",
    )
    password_iterations: int | None = Field(
        default=None,
        description="PBKDF2 iteration count used for password hash",
    )

    def __repr__(self) -> str:
        return f"<User github_id={self.github_id!r} email={self.email!r}>"


# =============================================================================
# Organization - Tenant boundary for auth
# =============================================================================


class Organization(TimestampMixin, table=True):
    """An organization/tenant."""

    __tablename__ = "organizations"  # type: ignore[assignment]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=255, description="Organization display name")
    slug: str = Field(max_length=64, unique=True, index=True, description="URL-safe unique slug")
    is_personal: bool = Field(default=False, description="Personal org owned by one user")

    settings: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Arbitrary org settings",
    )

    graph_name: str = Field(
        default="conventions",
        max_length=255,
        description="FalkorDB graph name for this org",
    )

    def __repr__(self) -> str:
        return f"<Organization slug={self.slug!r} personal={self.is_personal}>"


class OrganizationRole(StrEnum):
    """Role of a user within an organization."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class OrganizationMember(TimestampMixin, table=True):
    """Membership record linking a user to an organization."""

    __tablename__ = "organization_members"  # type: ignore[assignment]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(foreign_key="organizations.id", index=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    role: OrganizationRole = Field(
        default=OrganizationRole.MEMBER,
        sa_column=Column(
            Enum(
                OrganizationRole,
                name="organizationrole",
                values_callable=lambda enum: [e.value for e in enum],
            ),
            nullable=False,
            server_default=text("'member'"),
        ),
        description="Membership role",
    )

    organization: Organization = Relationship()
    user: User = Relationship()

    __table_args__ = (
        Index(
            "ix_organization_members_org_user_unique",
            "organization_id",
            "user_id",
            unique=True,
        ),
    )


# =============================================================================
# API Keys - Long-lived credentials for CLI + automation
# =============================================================================


class ApiKey(TimestampMixin, table=True):
    """Long-lived API key (store only hash + salt, never raw key)."""

    __tablename__ = "api_keys"  # type: ignore[assignment]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(foreign_key="organizations.id", index=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)

    name: str = Field(default="", max_length=255, description="Display name for this key")
    key_prefix: str = Field(
        max_length=32,
        index=True,
        description="Non-secret prefix for lookup (e.g. sk_live_abc123...)",
    )
    key_salt: str = Field(max_length=64, description="Hex-encoded salt")
    key_hash: str = Field(max_length=128, description="Hex-encoded derived key hash")

    revoked_at: datetime | None = Field(default=None, description="Revocation timestamp")
    last_used_at: datetime | None = Field(default=None, description="Last usage timestamp")


# =============================================================================
# Org Invitations - Invite a user (by email) to an org
# =============================================================================


class OrganizationInvitation(TimestampMixin, table=True):
    """Invitation to join an organization."""

    __tablename__ = "organization_invitations"  # type: ignore[assignment]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(foreign_key="organizations.id", index=True)
    invited_email: str = Field(max_length=255, index=True, description="Invitee email")
    invited_role: OrganizationRole = Field(
        default=OrganizationRole.MEMBER,
        sa_column=Column(
            Enum(
                OrganizationRole,
                name="organizationrole",
                values_callable=lambda enum: [e.value for e in enum],
            ),
            nullable=False,
            server_default=text("'member'"),
        ),
        description="Role to grant when accepted",
    )
    token: str = Field(max_length=96, unique=True, index=True, description="Opaque invite token")
    created_by_user_id: UUID = Field(foreign_key="users.id", index=True)

    expires_at: datetime | None = Field(default=None, description="Expiry timestamp")
    accepted_at: datetime | None = Field(default=None, description="Acceptance timestamp")
    accepted_by_user_id: UUID | None = Field(default=None, foreign_key="users.id", index=True)


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
    current_job_id: str | None = Field(
        default=None, max_length=64, description="Active crawl job ID"
    )
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

    @field_validator("last_crawled_at", "created_at", "updated_at", mode="before")
    @classmethod
    def strip_timezone(cls, v: datetime | None) -> datetime | None:
        """Ensure datetimes are naive (PostgreSQL TIMESTAMP WITHOUT TIME ZONE)."""
        if v is not None and v.tzinfo is not None:
            return v.replace(tzinfo=None)
        return v


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
    headings: list[str] = Field(
        default_factory=list, sa_type=ARRAY(String), description="Page headings"
    )
    links: list[str] = Field(
        default_factory=list, sa_type=ARRAY(String), description="Outgoing links"
    )
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
