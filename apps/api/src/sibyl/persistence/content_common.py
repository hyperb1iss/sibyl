"""Shared content runtime DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sibyl_core.models import ChunkType, CrawlStatus, SourceType

type ContentSession = object


class ContentConflictError(RuntimeError):
    """Raised when content writes collide with an existing unique record."""


def utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(slots=True)
class CrawlSourceRecord:
    organization_id: UUID
    name: str
    url: str
    id: UUID = field(default_factory=uuid4)
    source_type: SourceType = SourceType.WEBSITE
    description: str | None = None
    crawl_depth: int = 2
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    respect_robots: bool = True
    crawl_status: CrawlStatus = CrawlStatus.PENDING
    current_job_id: str | None = None
    last_crawled_at: datetime | None = None
    last_error: str | None = None
    document_count: int = 0
    chunk_count: int = 0
    total_tokens: int = 0
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    favicon_url: str | None = None
    created_at: datetime = field(default_factory=utcnow_naive)
    updated_at: datetime = field(default_factory=utcnow_naive)


@dataclass(slots=True)
class CrawledDocumentRecord:
    source_id: UUID
    url: str
    id: UUID = field(default_factory=uuid4)
    title: str = ""
    raw_content: str = ""
    content: str = ""
    content_hash: str = ""
    parent_url: str | None = None
    section_path: list[str] = field(default_factory=list)
    depth: int = 0
    language: str | None = None
    word_count: int = 0
    token_count: int = 0
    has_code: bool = False
    is_index: bool = False
    headings: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    code_languages: list[str] = field(default_factory=list)
    crawled_at: datetime = field(default_factory=utcnow_naive)
    http_status: int | None = None
    created_at: datetime = field(default_factory=utcnow_naive)
    updated_at: datetime = field(default_factory=utcnow_naive)


@dataclass(slots=True)
class DocumentChunkRecord:
    document_id: UUID
    chunk_index: int
    content: str
    id: UUID = field(default_factory=uuid4)
    chunk_type: ChunkType = ChunkType.TEXT
    context: str | None = None
    token_count: int = 0
    start_char: int = 0
    end_char: int = 0
    heading_path: list[str] = field(default_factory=list)
    embedding: object | None = None
    language: str | None = None
    is_complete: bool = True
    has_entities: bool = False
    entity_ids: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow_naive)
    updated_at: datetime = field(default_factory=utcnow_naive)


type RAGSearchRow = tuple[DocumentChunkRecord, CrawledDocumentRecord, str, UUID, float]
type CodeExampleSearchRow = tuple[DocumentChunkRecord, CrawledDocumentRecord, UUID, str, float]
type HybridSearchRow = tuple[DocumentChunkRecord, CrawledDocumentRecord, str, UUID, float, float]


@dataclass(frozen=True)
class CrawlStats:
    total_sources: int
    total_documents: int
    total_chunks: int
    chunks_with_embeddings: int
    sources_by_status: dict[str, int]


@dataclass(frozen=True)
class RawCaptureRecord:
    organization_id: UUID
    title: str
    raw_content: str
    entity_type: str
    entity_id: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
    capture_surface: str | None = None
    created_by_user_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utcnow_naive)


@dataclass(frozen=True)
class ApiIdempotencyRecord:
    organization_id: UUID
    principal_id: str
    idempotency_key: str
    method: str
    path: str
    request_hash: str
    response_status_code: int
    response_body: dict[str, object]
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utcnow_naive)


@dataclass(frozen=True)
class DocumentEntityRecord:
    """Resolved document-backed entity payload for entity routes."""

    chunk_id: UUID
    document_id: UUID
    source_id: UUID
    source_name: str
    source_url: str
    document_title: str
    document_url: str
    chunk_index: int
    chunk_type: ChunkType | None
    heading_path: tuple[str, ...]
    language: str | None
    content: str
    created_at: datetime
    updated_at: datetime


__all__ = [
    "ApiIdempotencyRecord",
    "CodeExampleSearchRow",
    "ContentConflictError",
    "ContentSession",
    "CrawledDocumentRecord",
    "CrawlSourceRecord",
    "CrawlStats",
    "DocumentChunkRecord",
    "DocumentEntityRecord",
    "HybridSearchRow",
    "RAGSearchRow",
    "RawCaptureRecord",
    "utcnow_naive",
]
