"""Legacy relational helpers for crawler route queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlmodel import col

from sibyl.db import CrawledDocument, CrawlSource, CrawlStatus, DocumentChunk
from sibyl.db.models import SourceType
from sibyl_core.services.link_graph_status import get_link_graph_status_data


@dataclass(frozen=True)
class LegacyCrawlStats:
    total_sources: int
    total_documents: int
    total_chunks: int
    chunks_with_embeddings: int
    sources_by_status: dict[str, int]


async def get_org_crawl_source(
    session: Any,
    *,
    source_id: UUID,
    organization_id: UUID,
) -> Any | None:
    """Fetch a crawl source scoped to an organization."""

    result = await session.execute(
        select(CrawlSource).where(
            col(CrawlSource.id) == source_id,
            col(CrawlSource.organization_id) == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def get_crawl_source_by_id(
    session: Any,
    *,
    source_id: UUID,
) -> CrawlSource | None:
    """Fetch a crawl source by primary key."""

    return await session.get(CrawlSource, source_id)


async def get_crawl_source_by_url(
    session: Any,
    *,
    url: str,
) -> CrawlSource | None:
    """Fetch a crawl source by normalized URL."""

    result = await session.execute(
        select(CrawlSource).where(col(CrawlSource.url) == url.rstrip("/"))
    )
    return result.scalar_one_or_none()


async def list_crawl_sources_for_org(
    session: Any,
    *,
    organization_id: UUID,
    status: Any | None,
    limit: int,
) -> tuple[list[Any], int]:
    """List crawl sources for an organization."""

    query = (
        select(CrawlSource)
        .where(col(CrawlSource.organization_id) == organization_id)
        .order_by(col(CrawlSource.created_at).desc())
        .limit(limit)
    )
    if status is not None:
        query = query.where(col(CrawlSource.crawl_status) == status)

    result = await session.execute(query)
    count_result = await session.execute(
        select(func.count(CrawlSource.id)).where(col(CrawlSource.organization_id) == organization_id)
    )
    return list(result.scalars().all()), count_result.scalar() or 0


async def list_crawl_sources(
    session: Any,
    *,
    status: CrawlStatus | None = None,
    limit: int | None = 50,
) -> list[CrawlSource]:
    """List crawl sources across all organizations."""

    query = select(CrawlSource).order_by(col(CrawlSource.created_at).desc())
    if status is not None:
        query = query.where(col(CrawlSource.crawl_status) == status)
    if limit is not None:
        query = query.limit(limit)
    result = await session.execute(query)
    return list(result.scalars().all())


async def create_crawl_source_record(
    session: Any,
    *,
    name: str,
    url: str,
    organization_id: UUID,
    source_type: SourceType,
    description: str | None,
    crawl_depth: int,
    include_patterns: list[str] | None,
    exclude_patterns: list[str] | None,
) -> CrawlSource:
    """Create an org-scoped crawl source with duplicate protection."""

    from sibyl.crawler.service import SourceAlreadyExistsError

    normalized_url = url.rstrip("/")
    existing = await session.execute(
        select(CrawlSource).where(
            col(CrawlSource.url) == normalized_url,
            col(CrawlSource.organization_id) == organization_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise SourceAlreadyExistsError(normalized_url)

    source = CrawlSource(
        organization_id=organization_id,
        name=name,
        url=normalized_url,
        source_type=source_type,
        description=description,
        crawl_depth=crawl_depth,
        include_patterns=include_patterns or [],
        exclude_patterns=exclude_patterns or [],
    )
    session.add(source)
    await session.flush()
    await session.refresh(source)
    return source


async def save_crawl_source_record(
    session: Any,
    *,
    source: CrawlSource,
) -> CrawlSource:
    """Persist a crawl source mutation."""

    session.add(source)
    await session.flush()
    await session.refresh(source)
    return source


async def get_crawl_stats_payload(
    session: Any,
    *,
    organization_id: UUID,
) -> LegacyCrawlStats:
    """Aggregate crawl stats for an organization."""

    sources_result = await session.execute(
        select(func.count(CrawlSource.id)).where(col(CrawlSource.organization_id) == organization_id)
    )
    total_sources = sources_result.scalar() or 0

    docs_result = await session.execute(
        select(func.count(CrawledDocument.id))
        .join(CrawlSource, col(CrawledDocument.source_id) == col(CrawlSource.id))
        .where(col(CrawlSource.organization_id) == organization_id)
    )
    total_documents = docs_result.scalar() or 0

    chunks_result = await session.execute(
        select(func.count(DocumentChunk.id))
        .join(CrawledDocument, col(DocumentChunk.document_id) == col(CrawledDocument.id))
        .join(CrawlSource, col(CrawledDocument.source_id) == col(CrawlSource.id))
        .where(col(CrawlSource.organization_id) == organization_id)
    )
    total_chunks = chunks_result.scalar() or 0

    embedded_result = await session.execute(
        select(func.count(DocumentChunk.id))
        .join(CrawledDocument, col(DocumentChunk.document_id) == col(CrawledDocument.id))
        .join(CrawlSource, col(CrawledDocument.source_id) == col(CrawlSource.id))
        .where(
            col(CrawlSource.organization_id) == organization_id,
            col(DocumentChunk.embedding).is_not(None),
        )
    )
    chunks_with_embeddings = embedded_result.scalar() or 0

    status_result = await session.execute(
        select(
            col(CrawlSource.crawl_status),
            func.count(col(CrawlSource.id)),
        )
        .where(col(CrawlSource.organization_id) == organization_id)
        .group_by(col(CrawlSource.crawl_status))
    )
    sources_by_status = {
        str(status.value) if hasattr(status, "value") else str(status): count
        for status, count in status_result.all()
    }

    return LegacyCrawlStats(
        total_sources=total_sources,
        total_documents=total_documents,
        total_chunks=total_chunks,
        chunks_with_embeddings=chunks_with_embeddings,
        sources_by_status=sources_by_status,
    )


async def get_link_graph_status_payload(
    session: Any,
    *,
    organization_id: UUID,
) -> Any:
    """Load link-graph status for the active organization."""

    return await get_link_graph_status_data(session, organization_id)


async def list_crawled_documents_for_org(
    session: Any,
    *,
    organization_id: UUID,
    limit: int,
    offset: int,
) -> tuple[list[Any], int]:
    """List paginated documents for an organization."""

    query = (
        select(CrawledDocument)
        .join(CrawlSource, col(CrawledDocument.source_id) == col(CrawlSource.id))
        .where(col(CrawlSource.organization_id) == organization_id)
        .order_by(col(CrawledDocument.crawled_at).desc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(query)
    documents = list(result.scalars().all())

    count_result = await session.execute(
        select(func.count(CrawledDocument.id))
        .join(CrawlSource, col(CrawledDocument.source_id) == col(CrawlSource.id))
        .where(col(CrawlSource.organization_id) == organization_id)
    )
    total = count_result.scalar() or 0
    return documents, total


async def get_crawled_document_for_org(
    session: Any,
    *,
    document_id: UUID,
    organization_id: UUID,
) -> Any | None:
    """Fetch a crawled document scoped to an organization."""

    result = await session.execute(
        select(CrawledDocument)
        .join(CrawlSource, col(CrawledDocument.source_id) == col(CrawlSource.id))
        .where(
            col(CrawledDocument.id) == document_id,
            col(CrawlSource.organization_id) == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def save_crawled_document_record(
    session: Any,
    *,
    document: CrawledDocument,
) -> CrawledDocument:
    """Persist a crawled document mutation."""

    session.add(document)
    await session.flush()
    await session.refresh(document)
    return document


async def save_document_chunks(
    session: Any,
    *,
    chunks: list[DocumentChunk],
) -> list[DocumentChunk]:
    """Persist document chunk mutations."""

    for chunk in chunks:
        session.add(chunk)
    await session.flush()
    for chunk in chunks:
        await session.refresh(chunk)
    return chunks


async def delete_crawled_document_record(
    session: Any,
    *,
    document_id: UUID,
    organization_id: UUID,
) -> tuple[CrawledDocument, int] | None:
    """Delete a document, its chunks, and update source counters."""

    document = await get_crawled_document_for_org(
        session,
        document_id=document_id,
        organization_id=organization_id,
    )
    if document is None:
        return None

    source = await session.get(CrawlSource, document.source_id)
    chunks = await list_document_chunks(session, document_id=document_id)
    chunks_deleted = len(chunks)

    for chunk in chunks:
        await session.delete(chunk)
    await session.delete(document)

    if source is not None:
        source.document_count = max(0, source.document_count - 1)
        source.chunk_count = max(0, source.chunk_count - chunks_deleted)

    return document, chunks_deleted


async def list_document_chunks(
    session: Any,
    *,
    document_id: UUID,
) -> list[Any]:
    """List ordered chunks for a document."""

    result = await session.execute(
        select(DocumentChunk)
        .where(col(DocumentChunk.document_id) == document_id)
        .order_by(col(DocumentChunk.chunk_index))
    )
    return list(result.scalars().all())


async def list_source_documents_page(
    session: Any,
    *,
    source_id: UUID,
    limit: int,
    offset: int,
) -> tuple[list[Any], int]:
    """List paginated documents for a source."""

    query = (
        select(CrawledDocument)
        .where(col(CrawledDocument.source_id) == source_id)
        .order_by(col(CrawledDocument.crawled_at).desc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(query)
    documents = list(result.scalars().all())

    count_result = await session.execute(
        select(func.count(CrawledDocument.id)).where(col(CrawledDocument.source_id) == source_id)
    )
    total = count_result.scalar() or 0
    return documents, total


async def list_source_chunks(
    session: Any,
    *,
    source_id: UUID,
) -> list[Any]:
    """List all chunks belonging to a source."""

    result = await session.execute(
        select(DocumentChunk)
        .join(CrawledDocument, col(DocumentChunk.document_id) == col(CrawledDocument.id))
        .where(col(CrawledDocument.source_id) == source_id)
    )
    return list(result.scalars().all())


async def list_source_documents(
    session: Any,
    *,
    source_id: UUID,
) -> list[Any]:
    """List all documents belonging to a source."""

    result = await session.execute(
        select(CrawledDocument).where(col(CrawledDocument.source_id) == source_id)
    )
    return list(result.scalars().all())


async def delete_crawl_source_record(
    session: Any,
    *,
    source_id: UUID,
    organization_id: UUID,
) -> CrawlSource | None:
    """Delete a source and all of its documents and chunks."""

    source = await get_org_crawl_source(
        session,
        source_id=source_id,
        organization_id=organization_id,
    )
    if source is None:
        return None

    chunks = await list_source_chunks(session, source_id=source_id)
    for chunk in chunks:
        await session.delete(chunk)

    documents = await list_source_documents(session, source_id=source_id)
    for document in documents:
        await session.delete(document)

    await session.delete(source)
    return source


async def count_source_documents(
    session: Any,
    *,
    source_id: UUID,
) -> int:
    """Count documents for a source."""

    result = await session.execute(
        select(func.count(CrawledDocument.id)).where(col(CrawledDocument.source_id) == source_id)
    )
    return result.scalar() or 0


async def count_source_chunks(
    session: Any,
    *,
    source_id: UUID,
) -> int:
    """Count chunks for a source."""

    result = await session.execute(
        select(func.count(DocumentChunk.id))
        .join(CrawledDocument, col(DocumentChunk.document_id) == col(CrawledDocument.id))
        .where(col(CrawledDocument.source_id) == source_id)
    )
    return result.scalar() or 0


async def get_source_sync_counts(
    session: Any,
    *,
    source_id: UUID,
) -> tuple[int, int]:
    """Return actual document and chunk totals for a source."""

    document_count = await count_source_documents(session, source_id=source_id)
    chunk_count = await count_source_chunks(session, source_id=source_id)
    return document_count, chunk_count


async def list_sources_for_graph_linking(
    session: Any,
    *,
    organization_id: UUID,
    source_id: UUID | None = None,
) -> list[Any]:
    """List sources to process for graph linking."""

    if source_id is not None:
        source = await get_org_crawl_source(
            session,
            source_id=source_id,
            organization_id=organization_id,
        )
        return [source] if source is not None else []

    result = await session.execute(
        select(CrawlSource).where(col(CrawlSource.organization_id) == organization_id)
    )
    return list(result.scalars().all())


async def list_unlinked_source_chunks(
    session: Any,
    *,
    source_id: UUID,
    limit: int,
) -> list[Any]:
    """List pending entity-linking chunks for a source."""

    result = await session.execute(
        select(DocumentChunk)
        .join(CrawledDocument, col(DocumentChunk.document_id) == col(CrawledDocument.id))
        .where(col(CrawledDocument.source_id) == source_id)
        .where(col(DocumentChunk.has_entities) == False)  # noqa: E712
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_remaining_unlinked_chunks(
    session: Any,
    *,
    organization_id: UUID,
    source_id: UUID | None = None,
) -> int:
    """Count remaining unlinked chunks for an organization or source."""

    query = (
        select(func.count(DocumentChunk.id))
        .join(CrawledDocument, col(CrawledDocument.id) == col(DocumentChunk.document_id))
        .join(CrawlSource, col(CrawlSource.id) == col(CrawledDocument.source_id))
        .where(col(CrawlSource.organization_id) == organization_id)
        .where(col(DocumentChunk.has_entities) == False)  # noqa: E712
    )
    if source_id is not None:
        query = query.where(col(CrawledDocument.source_id) == source_id)

    result = await session.execute(query)
    return result.scalar() or 0
