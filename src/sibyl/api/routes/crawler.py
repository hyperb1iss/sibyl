"""Crawler API endpoints for documentation ingestion.

Provides REST API for:
- Managing crawl sources
- Triggering crawl jobs
- Listing crawled documents
- Crawler health and stats
"""

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException
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

# Track running ingestion jobs
_ingestion_jobs: dict[str, dict[str, Any]] = {}


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
# Ingestion
# =============================================================================


async def _run_ingestion(
    source_id: str,
    max_pages: int,
    max_depth: int,
    generate_embeddings: bool,
) -> None:
    """Run ingestion job in background."""
    from sibyl.crawler import IngestionPipeline
    from sibyl.db import get_session

    job_status = _ingestion_jobs.get(source_id, {})
    job_status["running"] = True
    job_status["documents_crawled"] = 0
    job_status["documents_stored"] = 0
    job_status["chunks_created"] = 0
    job_status["errors"] = 0
    _ingestion_jobs[source_id] = job_status

    try:
        async with get_session() as session:
            source = await session.get(CrawlSource, UUID(source_id))
            if not source:
                raise ValueError(f"Source not found: {source_id}")

            # Detach from session for background processing
            session.expunge(source)

        await broadcast_event("crawl_started", {
            "source_id": source_id,
            "source_name": source.name,
            "max_pages": max_pages,
        })

        async with IngestionPipeline(generate_embeddings=generate_embeddings) as pipeline:
            stats = await pipeline.ingest_source(
                source,
                max_pages=max_pages,
                max_depth=max_depth,
            )

        job_status["running"] = False
        job_status["documents_crawled"] = stats.documents_crawled
        job_status["documents_stored"] = stats.documents_stored
        job_status["chunks_created"] = stats.chunks_created
        job_status["embeddings_generated"] = stats.embeddings_generated
        job_status["errors"] = stats.errors
        job_status["duration_seconds"] = stats.duration_seconds

        await broadcast_event("crawl_complete", {
            "source_id": source_id,
            "source_name": source.name,
            "stats": {
                "documents_crawled": stats.documents_crawled,
                "documents_stored": stats.documents_stored,
                "chunks_created": stats.chunks_created,
                "embeddings_generated": stats.embeddings_generated,
                "errors": stats.errors,
                "duration_seconds": stats.duration_seconds,
            },
        })

        log.info("Ingestion complete", source_id=source_id, stats=str(stats))

    except Exception as e:
        job_status["running"] = False
        job_status["error"] = str(e)
        log.exception("Ingestion failed", source_id=source_id, error=str(e))

        await broadcast_event("crawl_complete", {
            "source_id": source_id,
            "error": str(e),
        })


@router.post("/{source_id}/ingest", response_model=CrawlIngestResponse)
async def ingest_source(
    source_id: str,
    request: CrawlIngestRequest,
    background_tasks: BackgroundTasks,
) -> CrawlIngestResponse:
    """Start crawling a source (runs in background)."""
    # Check if already running
    if source_id in _ingestion_jobs and _ingestion_jobs[source_id].get("running"):
        return CrawlIngestResponse(
            source_id=source_id,
            status="already_running",
            message="Ingestion already in progress for this source",
        )

    # Verify source exists
    async with get_session() as session:
        source = await session.get(CrawlSource, UUID(source_id))
        if not source:
            raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")

    # Start background task
    background_tasks.add_task(
        _run_ingestion,
        source_id,
        request.max_pages,
        request.max_depth,
        request.generate_embeddings,
    )

    return CrawlIngestResponse(
        source_id=source_id,
        status="started",
        message=f"Started crawling {source.name} (max {request.max_pages} pages)",
    )


@router.get("/{source_id}/status")
async def get_ingestion_status(source_id: str) -> dict[str, Any]:
    """Get status of an ingestion job."""
    if source_id not in _ingestion_jobs:
        return {"source_id": source_id, "running": False, "message": "No ingestion job found"}

    return {"source_id": source_id, **_ingestion_jobs[source_id]}


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
