"""Legacy persistence helpers for entity routes backed by Postgres."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import String, cast
from sqlmodel import col, select

from sibyl.db import CrawledDocument, CrawlSource, DocumentChunk
from sibyl.db.models import ChunkType, RawCapture


@dataclass(frozen=True)
class LegacyDocumentEntityRecord:
    """Resolved document-backed entity payload for entity routes."""

    chunk: Any
    document: Any
    source: Any
    content: str


async def list_legacy_raw_captures(
    session: Any,
    *,
    organization_id: UUID,
    entity_type: str | None,
    capture_surface: str | None,
    review_state: str | None,
    limit: int,
    offset: int,
) -> tuple[list[RawCapture], bool]:
    """List raw captures for an organization with route-compatible filters."""

    stmt = (
        select(RawCapture)
        .where(col(RawCapture.organization_id) == organization_id)
        .order_by(col(RawCapture.created_at).desc())
        .offset(offset)
        .limit(limit + 1)
    )
    if entity_type:
        stmt = stmt.where(col(RawCapture.entity_type) == entity_type)
    if capture_surface:
        stmt = stmt.where(col(RawCapture.capture_surface) == capture_surface)
    if review_state:
        if review_state == "pending":
            stmt = stmt.where(
                col(RawCapture.metadata_).op("->>")("review_state").is_(None)
                | (col(RawCapture.metadata_).op("->>")("review_state") == "pending")
            )
        else:
            stmt = stmt.where(col(RawCapture.metadata_).op("->>")("review_state") == review_state)

    result = await session.execute(stmt)
    rows = result.scalars().all()
    return rows[:limit], len(rows) > limit


async def get_legacy_raw_capture(
    session: Any,
    *,
    organization_id: UUID,
    capture_id: UUID,
) -> RawCapture | None:
    """Fetch a single raw capture scoped to the organization."""

    result = await session.execute(
        select(RawCapture).where(
            col(RawCapture.id) == capture_id,
            col(RawCapture.organization_id) == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def resolve_legacy_document_entity(
    session: Any,
    *,
    organization_id: UUID,
    entity_id: str,
) -> LegacyDocumentEntityRecord | None:
    """Resolve a document chunk entity by exact UUID or UUID prefix."""

    row = None

    try:
        chunk_uuid = UUID(entity_id)
        result = await session.execute(
            select(DocumentChunk, CrawledDocument, CrawlSource)
            .join(CrawledDocument, col(DocumentChunk.document_id) == col(CrawledDocument.id))
            .join(CrawlSource, col(CrawledDocument.source_id) == col(CrawlSource.id))
            .where(col(DocumentChunk.id) == chunk_uuid)
            .where(col(CrawlSource.organization_id) == organization_id)
        )
        row = result.first()
    except ValueError:
        row = None

    if (
        not row
        and len(entity_id) >= 4
        and all(char in "0123456789abcdef-" for char in entity_id.lower())
    ):
        prefix = entity_id.lower().replace("-", "")
        result = await session.execute(
            select(DocumentChunk, CrawledDocument, CrawlSource)
            .join(CrawledDocument, col(DocumentChunk.document_id) == col(CrawledDocument.id))
            .join(CrawlSource, col(CrawledDocument.source_id) == col(CrawlSource.id))
            .where(cast(DocumentChunk.id, String).like(f"{prefix[:8]}%"))
            .where(col(CrawlSource.organization_id) == organization_id)
            .limit(1)
        )
        row = result.first()

    if not row:
        return None

    chunk, document, source = row
    content = chunk.content or ""

    if chunk.chunk_type == ChunkType.HEADING:
        following_result = await session.execute(
            select(DocumentChunk)
            .where(col(DocumentChunk.document_id) == chunk.document_id)
            .where(col(DocumentChunk.chunk_index) > chunk.chunk_index)
            .order_by(col(DocumentChunk.chunk_index))
            .limit(10)
        )
        following_chunks = following_result.scalars().all()
        section_parts = [content]
        for following_chunk in following_chunks:
            if following_chunk.chunk_type == ChunkType.HEADING:
                break
            section_parts.append(following_chunk.content or "")
        content = "\n\n".join(section_parts)

    return LegacyDocumentEntityRecord(
        chunk=chunk,
        document=document,
        source=source,
        content=content,
    )
