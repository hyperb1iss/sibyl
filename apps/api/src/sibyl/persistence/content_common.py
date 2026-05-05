"""Shared content runtime DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sibyl_core.models import ChunkType


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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
    created_at: datetime = field(default_factory=_utcnow_naive)


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


__all__ = ["CrawlStats", "DocumentEntityRecord", "RawCaptureRecord"]
