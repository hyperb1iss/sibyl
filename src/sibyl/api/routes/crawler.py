"""Crawler API endpoints for documentation ingestion.

Provides REST API for:
- Managing crawl sources
- Triggering crawl jobs
- Listing crawled documents
- Crawler health and stats
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select
from sqlmodel import col

from sibyl.api.schemas import (
    CrawlDocumentListResponse,
    CrawlDocumentResponse,
    CrawlHealthResponse,
    CrawlIngestRequest,
    CrawlIngestResponse,
    CrawlSourceCreate,
    CrawlSourceListResponse,
    CrawlSourceResponse,
    CrawlStatsResponse,
)
from sibyl.api.websocket import broadcast_event
from sibyl.db import (
    CrawledDocument,
    CrawlSource,
    CrawlStatus,
    DocumentChunk,
    SourceType,
    check_postgres_health,
    get_session,
)

log = structlog.get_logger()
router = APIRouter(prefix="/sources", tags=["sources"])


def _source_to_response(source: CrawlSource) -> CrawlSourceResponse:
    """Convert DB model to response schema."""
    return CrawlSourceResponse(
        id=str(source.id),
        name=source.name,
        url=source.url,
        source_type=source.source_type.value if hasattr(source.source_type, "value") else str(source.source_type),
        description=source.description,
        crawl_depth=source.crawl_depth,
        crawl_status=source.crawl_status.value if hasattr(source.crawl_status, "value") else str(source.crawl_status),
        document_count=source.document_count,
        chunk_count=source.chunk_count,
        last_crawled_at=source.last_crawled_at,
        last_error=source.last_error,
        created_at=source.created_at,
        include_patterns=source.include_patterns or [],
        exclude_patterns=source.exclude_patterns or [],
    )


def _document_to_response(doc: CrawledDocument) -> CrawlDocumentResponse:
    """Convert DB model to response schema."""
    return CrawlDocumentResponse(
        id=str(doc.id),
        source_id=str(doc.source_id),
        url=doc.url,
        title=doc.title,
        word_count=doc.word_count,
        has_code=doc.has_code,
        is_index=doc.is_index,
        depth=doc.depth,
        crawled_at=doc.crawled_at,
        headings=doc.headings or [],
        code_languages=doc.code_languages or [],
    )


# =============================================================================
# Stats & Health (MUST come before /{source_id} routes)
# =============================================================================


@router.get("/stats", response_model=CrawlStatsResponse)
async def get_stats() -> CrawlStatsResponse:
    """Get crawler statistics."""
    async with get_session() as session:
        # Count sources
        sources_result = await session.execute(select(func.count(CrawlSource.id)))
        total_sources = sources_result.scalar() or 0

        # Count documents
        docs_result = await session.execute(select(func.count(CrawledDocument.id)))
        total_documents = docs_result.scalar() or 0

        # Count chunks
        chunks_result = await session.execute(select(func.count(DocumentChunk.id)))
        total_chunks = chunks_result.scalar() or 0

        # Count chunks with embeddings
        embedded_result = await session.execute(
            select(func.count(DocumentChunk.id)).where(col(DocumentChunk.embedding).is_not(None))
        )
        chunks_with_embeddings = embedded_result.scalar() or 0

        # Count sources by status
        status_result = await session.execute(
            select(CrawlSource.crawl_status, func.count(CrawlSource.id)).group_by(  # type: ignore[call-overload]
                CrawlSource.crawl_status
            )
        )
        sources_by_status = {
            str(status.value) if hasattr(status, "value") else str(status): count
            for status, count in status_result.all()
        }

    return CrawlStatsResponse(
        total_sources=total_sources,
        total_documents=total_documents,
        total_chunks=total_chunks,
        chunks_with_embeddings=chunks_with_embeddings,
        sources_by_status=sources_by_status,
    )


@router.get("/health", response_model=CrawlHealthResponse)
async def get_health() -> CrawlHealthResponse:
    """Check crawler system health."""
    pg_health = await check_postgres_health()

    # Check Crawl4AI availability
    crawl4ai_available = False
    try:
        from crawl4ai import AsyncWebCrawler  # noqa: F401

        crawl4ai_available = True
    except ImportError:
        pass

    return CrawlHealthResponse(
        postgres_healthy=pg_health["status"] == "healthy",
        postgres_version=pg_health.get("postgres_version"),
        pgvector_version=pg_health.get("pgvector_version"),
        crawl4ai_available=crawl4ai_available,
        error=pg_health.get("error"),
    )


# =============================================================================
# Documents (MUST come before /{source_id} routes)
# =============================================================================


@router.get("/documents", response_model=CrawlDocumentListResponse)
async def list_documents(
    limit: int = 50,
    offset: int = 0,
) -> CrawlDocumentListResponse:
    """List all crawled documents."""
    async with get_session() as session:
        query = (
            select(CrawledDocument)
            .order_by(col(CrawledDocument.crawled_at).desc())
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(query)
        documents = list(result.scalars().all())

        # Get total count
        count_result = await session.execute(select(func.count(CrawledDocument.id)))
        total = count_result.scalar() or 0

    return CrawlDocumentListResponse(
        documents=[_document_to_response(d) for d in documents],
        total=total,
    )


@router.get("/documents/{document_id}", response_model=CrawlDocumentResponse)
async def get_document(document_id: str) -> CrawlDocumentResponse:
    """Get a crawled document by ID."""
    async with get_session() as session:
        doc = await session.get(CrawledDocument, UUID(document_id))
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document not found: {document_id}")

        return _document_to_response(doc)


# =============================================================================
# Source CRUD
# =============================================================================


@router.post("", response_model=CrawlSourceResponse)
async def create_source(request: CrawlSourceCreate) -> CrawlSourceResponse:
    """Create a new crawl source."""
    async with get_session() as session:
        # Check for existing source with same URL
        existing = await session.execute(
            select(CrawlSource).where(col(CrawlSource.url) == request.url.rstrip("/"))
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"Source with URL {request.url} already exists")

        source = CrawlSource(
            name=request.name,
            url=request.url.rstrip("/"),
            source_type=SourceType(request.source_type),
            description=request.description,
            crawl_depth=request.crawl_depth,
            include_patterns=request.include_patterns,
            exclude_patterns=request.exclude_patterns,
        )
        session.add(source)
        await session.flush()
        await session.refresh(source)

        log.info("Created crawl source", name=source.name, url=source.url, id=str(source.id))

        response = _source_to_response(source)

    await broadcast_event("entity_created", {"type": "crawl_source", "id": str(source.id)})
    return response


@router.get("", response_model=CrawlSourceListResponse)
async def list_sources(
    status: str | None = None,
    limit: int = 50,
) -> CrawlSourceListResponse:
    """List all crawl sources."""
    async with get_session() as session:
        query = select(CrawlSource)
        if status:
            query = query.where(col(CrawlSource.crawl_status) == CrawlStatus(status))
        query = query.order_by(col(CrawlSource.created_at).desc()).limit(limit)

        result = await session.execute(query)
        sources = list(result.scalars().all())

        # Get total count
        count_result = await session.execute(select(func.count(CrawlSource.id)))
        total = count_result.scalar() or 0

    return CrawlSourceListResponse(
        sources=[_source_to_response(s) for s in sources],
        total=total,
    )


@router.get("/{source_id}", response_model=CrawlSourceResponse)
async def get_source(source_id: str) -> CrawlSourceResponse:
    """Get a crawl source by ID."""
    async with get_session() as session:
        source = await session.get(CrawlSource, UUID(source_id))
        if not source:
            raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")

        return _source_to_response(source)


@router.delete("/{source_id}")
async def delete_source(source_id: str) -> dict[str, Any]:
    """Delete a crawl source and all its documents."""
    async with get_session() as session:
        source = await session.get(CrawlSource, UUID(source_id))
        if not source:
            raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")

        # Delete chunks first (foreign key constraint)
        chunks_deleted = await session.execute(
            select(DocumentChunk).join(CrawledDocument).where(
                col(CrawledDocument.source_id) == UUID(source_id)
            )
        )
        for chunk in chunks_deleted.scalars():
            await session.delete(chunk)

        # Delete documents
        docs_deleted = await session.execute(
            select(CrawledDocument).where(col(CrawledDocument.source_id) == UUID(source_id))
        )
        for doc in docs_deleted.scalars():
            await session.delete(doc)

        # Delete source
        await session.delete(source)

        log.info("Deleted crawl source", id=source_id, name=source.name)

    await broadcast_event("entity_deleted", {"type": "crawl_source", "id": source_id})
    return {"deleted": True, "id": source_id}


# =============================================================================
# Ingestion (via arq job queue)
# =============================================================================


@router.post("/{source_id}/ingest", response_model=CrawlIngestResponse)
async def ingest_source(
    source_id: str,
    request: CrawlIngestRequest,
) -> CrawlIngestResponse:
    """Start crawling a source via job queue.

    Jobs are processed by the arq worker for reliability and persistence.
    Run the worker with: uv run arq sibyl.jobs.WorkerSettings
    """
    from sibyl.jobs import enqueue_crawl, get_job_status

    # Verify source exists and check current status
    async with get_session() as session:
        source = await session.get(CrawlSource, UUID(source_id))
        if not source:
            raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")

        # Check if already crawling
        if source.crawl_status == CrawlStatus.IN_PROGRESS:
            return CrawlIngestResponse(
                source_id=source_id,
                status="already_running",
                message="Crawl already in progress for this source",
            )

        source_name = source.name

    # Enqueue the crawl job
    try:
        job_id = await enqueue_crawl(
            source_id,
            max_pages=request.max_pages,
            max_depth=request.max_depth,
            generate_embeddings=request.generate_embeddings,
        )

        log.info(
            "Enqueued crawl job",
            source_id=source_id,
            job_id=job_id,
            max_pages=request.max_pages,
        )

        return CrawlIngestResponse(
            source_id=source_id,
            status="queued",
            message=f"Crawl job queued for {source_name} (job_id: {job_id})",
        )

    except Exception as e:
        log.error("Failed to enqueue crawl job", source_id=source_id, error=str(e))
        raise HTTPException(
            status_code=503,
            detail=f"Failed to enqueue job. Is the job queue available? Error: {e}",
        ) from e


@router.get("/{source_id}/status")
async def get_ingestion_status(source_id: str) -> dict[str, Any]:
    """Get crawl status for a source.

    Returns both the source's crawl_status and any active job status.
    """
    # Get source status from DB
    async with get_session() as session:
        source = await session.get(CrawlSource, UUID(source_id))
        if not source:
            raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")

        return {
            "source_id": source_id,
            "crawl_status": source.crawl_status.value,
            "document_count": source.document_count,
            "chunk_count": source.chunk_count,
            "last_crawled_at": source.last_crawled_at.isoformat() if source.last_crawled_at else None,
            "last_error": source.last_error,
        }


@router.post("/{source_id}/sync")
async def sync_source(source_id: str) -> dict[str, Any]:
    """Sync source stats from actual document/chunk counts.

    Useful for fixing stuck sources or after manual data changes.
    Recalculates document_count, chunk_count, and fixes status if stuck.
    """
    async with get_session() as session:
        source = await session.get(CrawlSource, UUID(source_id))
        if not source:
            raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")

        # Count actual documents
        doc_count_result = await session.execute(
            select(func.count(CrawledDocument.id)).where(
                col(CrawledDocument.source_id) == UUID(source_id)
            )
        )
        actual_doc_count = doc_count_result.scalar() or 0

        # Count actual chunks
        chunk_count_result = await session.execute(
            select(func.count(DocumentChunk.id))
            .join(CrawledDocument)
            .where(col(CrawledDocument.source_id) == UUID(source_id))
        )
        actual_chunk_count = chunk_count_result.scalar() or 0

        # Determine correct status
        old_status = source.crawl_status
        if actual_doc_count > 0:
            # Has documents - should be completed or partial
            if source.crawl_status == CrawlStatus.IN_PROGRESS:
                source.crawl_status = CrawlStatus.COMPLETED
                if source.last_crawled_at is None:
                    source.last_crawled_at = datetime.now(UTC)
        elif source.crawl_status == CrawlStatus.IN_PROGRESS:
            # No documents but stuck in progress - reset to pending
            source.crawl_status = CrawlStatus.PENDING

        # Update counts
        old_doc_count = source.document_count
        old_chunk_count = source.chunk_count
        source.document_count = actual_doc_count
        source.chunk_count = actual_chunk_count

        # Capture values before session closes
        new_status = source.crawl_status

        log.info(
            "Synced source stats",
            source_id=source_id,
            old_status=old_status.value,
            new_status=new_status.value,
            old_doc_count=old_doc_count,
            new_doc_count=actual_doc_count,
            old_chunk_count=old_chunk_count,
            new_chunk_count=actual_chunk_count,
        )

    await broadcast_event("entity_updated", {"type": "crawl_source", "id": source_id})

    return {
        "source_id": source_id,
        "synced": True,
        "document_count": actual_doc_count,
        "chunk_count": actual_chunk_count,
        "status": new_status.value,
        "changes": {
            "status": f"{old_status.value} -> {new_status.value}" if old_status != new_status else None,
            "document_count": f"{old_doc_count} -> {actual_doc_count}" if old_doc_count != actual_doc_count else None,
            "chunk_count": f"{old_chunk_count} -> {actual_chunk_count}" if old_chunk_count != actual_chunk_count else None,
        },
    }


@router.get("/{source_id}/documents", response_model=CrawlDocumentListResponse)
async def list_source_documents(
    source_id: str,
    limit: int = 50,
    offset: int = 0,
) -> CrawlDocumentListResponse:
    """List documents for a source."""
    async with get_session() as session:
        query = (
            select(CrawledDocument)
            .where(col(CrawledDocument.source_id) == UUID(source_id))
            .order_by(col(CrawledDocument.crawled_at).desc())
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(query)
        documents = list(result.scalars().all())

        # Get total count
        count_result = await session.execute(
            select(func.count(CrawledDocument.id)).where(
                col(CrawledDocument.source_id) == UUID(source_id)
            )
        )
        total = count_result.scalar() or 0

    return CrawlDocumentListResponse(
        documents=[_document_to_response(d) for d in documents],
        total=total,
    )
