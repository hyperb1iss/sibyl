"""Legacy persistence helpers for entity routes backed by Postgres."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import String, cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from sibyl.db import CrawledDocument, CrawlSource, DocumentChunk
from sibyl.db.models import RawCapture
from sibyl.persistence.content_common import DocumentEntityRecord, RawCaptureRecord
from sibyl_core.models import ChunkType

LegacyDocumentEntityRecord = DocumentEntityRecord


def _raw_capture_record_from_model(capture: RawCapture) -> RawCaptureRecord:
    return RawCaptureRecord(
        id=capture.id,
        organization_id=capture.organization_id,
        entity_id=capture.entity_id,
        title=capture.title,
        raw_content=capture.raw_content,
        entity_type=capture.entity_type,
        tags=list(capture.tags or []),
        metadata=dict(capture.metadata_ or {}),
        capture_surface=capture.capture_surface,
        created_by_user_id=capture.created_by_user_id,
        created_at=capture.created_at,
    )


def _raw_capture_model_from_record(capture: RawCaptureRecord) -> RawCapture:
    return RawCapture(
        id=capture.id,
        organization_id=capture.organization_id,
        entity_id=capture.entity_id,
        title=capture.title,
        raw_content=capture.raw_content,
        entity_type=capture.entity_type,
        tags=list(capture.tags or []),
        metadata_=dict(capture.metadata or {}),
        capture_surface=capture.capture_surface,
        created_by_user_id=capture.created_by_user_id,
        created_at=capture.created_at,
    )


async def list_raw_captures(
    session: AsyncSession,
    *,
    organization_id: UUID,
    entity_type: str | None,
    capture_surface: str | None,
    review_state: str | None,
    limit: int,
    offset: int,
) -> tuple[list[RawCaptureRecord], bool]:
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
    captures = [_raw_capture_record_from_model(row) for row in rows[:limit]]
    return captures, len(rows) > limit


async def get_raw_capture(
    session: AsyncSession,
    *,
    organization_id: UUID,
    capture_id: UUID,
) -> RawCaptureRecord | None:
    """Fetch a single raw capture scoped to the organization."""

    result = await session.execute(
        select(RawCapture).where(
            col(RawCapture.id) == capture_id,
            col(RawCapture.organization_id) == organization_id,
        )
    )
    capture = result.scalar_one_or_none()
    return _raw_capture_record_from_model(capture) if capture is not None else None


async def save_raw_capture_record(
    session: AsyncSession,
    *,
    capture: RawCaptureRecord,
) -> RawCaptureRecord:
    """Persist a raw-capture mutation."""

    persisted = await session.merge(_raw_capture_model_from_record(capture))
    await session.flush()
    await session.refresh(persisted)
    return _raw_capture_record_from_model(persisted)


async def resolve_document_entity(
    session: AsyncSession,
    *,
    organization_id: UUID,
    entity_id: str,
) -> DocumentEntityRecord | None:
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

    return DocumentEntityRecord(
        chunk_id=chunk.id,
        document_id=document.id,
        source_id=source.id,
        source_name=source.name,
        source_url=source.url,
        document_title=document.title,
        document_url=document.url,
        chunk_index=chunk.chunk_index,
        chunk_type=chunk.chunk_type,
        heading_path=tuple(chunk.heading_path or ()),
        language=chunk.language,
        content=content,
        created_at=chunk.created_at,
        updated_at=chunk.updated_at,
    )


list_legacy_raw_captures = list_raw_captures
get_legacy_raw_capture = get_raw_capture
resolve_legacy_document_entity = resolve_document_entity
