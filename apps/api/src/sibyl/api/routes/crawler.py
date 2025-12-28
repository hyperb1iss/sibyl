"""Crawler API endpoints for documentation ingestion.

Provides REST API for:
- Managing crawl sources
- Triggering crawl jobs
- Listing crawled documents
- Crawler health and stats
"""

import re
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
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
    CrawlSourceUpdate,
    CrawlStatsResponse,
    LinkGraphRequest,
    LinkGraphResponse,
    LinkGraphStatusResponse,
)
from sibyl.api.websocket import broadcast_event
from sibyl.auth.dependencies import get_current_organization, require_org_role
from sibyl.db import (
    CrawledDocument,
    CrawlSource,
    CrawlStatus,
    DocumentChunk,
    SourceType,
    check_postgres_health,
    get_session,
)
from sibyl.db.models import Organization, OrganizationRole, utcnow_naive

log = structlog.get_logger()


async def _get_org_source(session: Any, source_id: str, org: Organization) -> CrawlSource:
    """Get a source and verify it belongs to the organization.

    Args:
        session: Database session
        source_id: Source UUID string
        org: Current organization

    Returns:
        The CrawlSource if found and owned by org

    Raises:
        HTTPException: 404 if not found or not owned by org
    """
    result = await session.execute(
        select(CrawlSource).where(
            col(CrawlSource.id) == UUID(source_id),
            col(CrawlSource.organization_id) == org.id,
        )
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")
    return source


router = APIRouter(
    prefix="/sources",
    tags=["sources"],
    dependencies=[
        Depends(
            require_org_role(
                OrganizationRole.OWNER,
                OrganizationRole.ADMIN,
                OrganizationRole.MEMBER,
            )
        ),
    ],
)


def _source_to_response(source: CrawlSource) -> CrawlSourceResponse:
    """Convert DB model to response schema."""
    return CrawlSourceResponse(
        id=str(source.id),
        name=source.name,
        url=source.url,
        source_type=source.source_type.value
        if hasattr(source.source_type, "value")
        else str(source.source_type),
        description=source.description,
        crawl_depth=source.crawl_depth,
        crawl_status=source.crawl_status.value
        if hasattr(source.crawl_status, "value")
        else str(source.crawl_status),
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
async def get_stats(
    org: Organization = Depends(get_current_organization),
) -> CrawlStatsResponse:
    """Get crawler statistics for the current organization."""
    async with get_session() as session:
        # Count sources (org-scoped)
        sources_result = await session.execute(
            select(func.count(CrawlSource.id)).where(col(CrawlSource.organization_id) == org.id)
        )
        total_sources = sources_result.scalar() or 0

        # Count documents (via org-scoped sources)
        docs_result = await session.execute(
            select(func.count(CrawledDocument.id))
            .join(CrawlSource)
            .where(col(CrawlSource.organization_id) == org.id)
        )
        total_documents = docs_result.scalar() or 0

        # Count chunks (via org-scoped sources)
        chunks_result = await session.execute(
            select(func.count(DocumentChunk.id))
            .join(CrawledDocument)
            .join(CrawlSource)
            .where(col(CrawlSource.organization_id) == org.id)
        )
        total_chunks = chunks_result.scalar() or 0

        # Count chunks with embeddings (via org-scoped sources)
        embedded_result = await session.execute(
            select(func.count(DocumentChunk.id))
            .join(CrawledDocument)
            .join(CrawlSource)
            .where(
                col(CrawlSource.organization_id) == org.id,
                col(DocumentChunk.embedding).is_not(None),
            )
        )
        chunks_with_embeddings = embedded_result.scalar() or 0

        # Count sources by status (org-scoped)
        status_result = await session.execute(
            select(CrawlSource.crawl_status, func.count(CrawlSource.id))  # type: ignore[call-overload]
            .where(col(CrawlSource.organization_id) == org.id)
            .group_by(CrawlSource.crawl_status)
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
    org: Organization = Depends(get_current_organization),
) -> CrawlDocumentListResponse:
    """List crawled documents for the current organization."""
    async with get_session() as session:
        # Filter documents by org-owned sources
        query = (
            select(CrawledDocument)
            .join(CrawlSource)
            .where(col(CrawlSource.organization_id) == org.id)
            .order_by(col(CrawledDocument.crawled_at).desc())
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(query)
        documents = list(result.scalars().all())

        # Get total count (org-scoped)
        count_result = await session.execute(
            select(func.count(CrawledDocument.id))
            .join(CrawlSource)
            .where(col(CrawlSource.organization_id) == org.id)
        )
        total = count_result.scalar() or 0

    return CrawlDocumentListResponse(
        documents=[_document_to_response(d) for d in documents],
        total=total,
    )


@router.get("/documents/{document_id}", response_model=CrawlDocumentResponse)
async def get_document(
    document_id: str,
    org: Organization = Depends(get_current_organization),
) -> CrawlDocumentResponse:
    """Get a crawled document by ID (org-scoped)."""
    async with get_session() as session:
        # Query with org check via source join
        result = await session.execute(
            select(CrawledDocument)
            .join(CrawlSource)
            .where(
                col(CrawledDocument.id) == UUID(document_id),
                col(CrawlSource.organization_id) == org.id,
            )
        )
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document not found: {document_id}")

        return _document_to_response(doc)


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: str,
    org: Organization = Depends(get_current_organization),
) -> dict[str, Any]:
    """Delete a crawled document and its chunks (org-scoped)."""
    async with get_session() as session:
        # Query with org check via source join
        result = await session.execute(
            select(CrawledDocument)
            .join(CrawlSource)
            .where(
                col(CrawledDocument.id) == UUID(document_id),
                col(CrawlSource.organization_id) == org.id,
            )
        )
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document not found: {document_id}")

        source_id = str(doc.source_id)

        # Delete chunks first
        chunks = await session.execute(
            select(DocumentChunk).where(col(DocumentChunk.document_id) == UUID(document_id))
        )
        chunks_deleted = 0
        for chunk in chunks.scalars():
            await session.delete(chunk)
            chunks_deleted += 1

        # Delete document
        await session.delete(doc)

        # Update source stats
        source = await session.get(CrawlSource, UUID(source_id))
        if source:
            source.document_count = max(0, source.document_count - 1)
            source.chunk_count = max(0, source.chunk_count - chunks_deleted)

        log.info(
            "Deleted document",
            document_id=document_id,
            chunks_deleted=chunks_deleted,
        )

    await broadcast_event(
        "entity_deleted",
        {"type": "crawled_document", "id": document_id},
        org_id=str(org.id),
    )
    return {"deleted": document_id, "chunks_deleted": chunks_deleted}


# =============================================================================
# URL Preview
# =============================================================================


@router.get("/preview")
async def preview_url(url: str) -> dict[str, str | None]:
    """Fetch metadata from a URL to help with source naming.

    Returns the page title and suggested name for use when creating a source.
    """
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise HTTPException(status_code=400, detail="Invalid URL")

        # Fetch the page
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "Sibyl/1.0"})
            response.raise_for_status()

        html = response.text[:50000]  # Only check first 50KB

        # Extract title
        title_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
        title = title_match.group(1).strip() if title_match else None

        # Clean up title for use as name
        suggested_name = None
        if title:
            # Remove common suffixes like "| Company" or "- Docs"
            suggested_name = re.sub(
                r"\s*[\|\-\u2013\u2014]\s*[^|\-\u2013\u2014]+$", "", title
            ).strip()
            # If still too generic, use domain + title
            if len(suggested_name) < 3:
                suggested_name = f"{parsed.netloc} - {title}"

        return {
            "url": url,
            "title": title,
            "suggested_name": suggested_name or parsed.netloc,
            "domain": parsed.netloc,
        }

    except httpx.HTTPStatusError as e:
        log.warning("URL preview failed", url=url, status=e.response.status_code)
        return {
            "url": url,
            "title": None,
            "suggested_name": urlparse(url).netloc,
            "domain": urlparse(url).netloc,
            "error": f"HTTP {e.response.status_code}",
        }
    except Exception as e:
        log.warning("URL preview failed", url=url, error=str(e))
        return {
            "url": url,
            "title": None,
            "suggested_name": urlparse(url).netloc,
            "domain": urlparse(url).netloc,
            "error": "Failed to preview URL",
        }


# =============================================================================
# Source CRUD
# =============================================================================


@router.post("", response_model=CrawlSourceResponse)
async def create_source(
    request: CrawlSourceCreate,
    org: Organization = Depends(get_current_organization),
) -> CrawlSourceResponse:
    """Create a new crawl source."""
    async with get_session() as session:
        # Check for existing source with same URL (within org)
        existing = await session.execute(
            select(CrawlSource).where(
                col(CrawlSource.url) == request.url.rstrip("/"),
                col(CrawlSource.organization_id) == org.id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=409, detail=f"Source with URL {request.url} already exists"
            )

        source = CrawlSource(
            name=request.name,
            url=request.url.rstrip("/"),
            source_type=SourceType(request.source_type),
            description=request.description,
            crawl_depth=request.crawl_depth,
            include_patterns=request.include_patterns,
            exclude_patterns=request.exclude_patterns,
            organization_id=org.id,
        )
        session.add(source)
        await session.flush()
        await session.refresh(source)

        log.info("Created crawl source", name=source.name, url=source.url, id=str(source.id))

        response = _source_to_response(source)

    await broadcast_event(
        "entity_created",
        {"type": "crawl_source", "id": str(source.id)},
        org_id=str(org.id),
    )
    return response


@router.get("", response_model=CrawlSourceListResponse)
async def list_sources(
    status: str | None = None,
    limit: int = 50,
    org: Organization = Depends(get_current_organization),
) -> CrawlSourceListResponse:
    """List crawl sources for the current organization."""
    async with get_session() as session:
        query = select(CrawlSource).where(col(CrawlSource.organization_id) == org.id)
        if status:
            query = query.where(col(CrawlSource.crawl_status) == CrawlStatus(status))
        query = query.order_by(col(CrawlSource.created_at).desc()).limit(limit)

        result = await session.execute(query)
        sources = list(result.scalars().all())

        # Get total count (org-scoped)
        count_result = await session.execute(
            select(func.count(CrawlSource.id)).where(col(CrawlSource.organization_id) == org.id)
        )
        total = count_result.scalar() or 0

    return CrawlSourceListResponse(
        sources=[_source_to_response(s) for s in sources],
        total=total,
    )


@router.get("/{source_id}", response_model=CrawlSourceResponse)
async def get_source(
    source_id: str,
    org: Organization = Depends(get_current_organization),
) -> CrawlSourceResponse:
    """Get a crawl source by ID (org-scoped)."""
    async with get_session() as session:
        source = await _get_org_source(session, source_id, org)
        return _source_to_response(source)


@router.patch("/{source_id}", response_model=CrawlSourceResponse)
async def update_source(
    source_id: str,
    request: CrawlSourceUpdate,
    org: Organization = Depends(get_current_organization),
) -> CrawlSourceResponse:
    """Update a crawl source (org-scoped)."""
    async with get_session() as session:
        source = await _get_org_source(session, source_id, org)

        # Update fields if provided
        update_data = request.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(source, field, value)

        await session.flush()
        await session.refresh(source)

        log.info("Updated crawl source", id=str(source.id), fields=list(update_data.keys()))

        response = _source_to_response(source)

    await broadcast_event(
        "entity_updated",
        {"type": "crawl_source", "id": str(source.id)},
        org_id=str(org.id),
    )
    return response


@router.delete("/{source_id}")
async def delete_source(
    source_id: str,
    org: Organization = Depends(get_current_organization),
) -> dict[str, Any]:
    """Delete a crawl source and all its documents (org-scoped)."""
    async with get_session() as session:
        source = await _get_org_source(session, source_id, org)

        # Delete chunks first (foreign key constraint)
        chunks_deleted = await session.execute(
            select(DocumentChunk)
            .join(CrawledDocument)
            .where(col(CrawledDocument.source_id) == UUID(source_id))
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

    await broadcast_event(
        "entity_deleted",
        {"type": "crawl_source", "id": source_id},
        org_id=str(org.id),
    )
    return {"deleted": True, "id": source_id}


# =============================================================================
# Ingestion (via arq job queue)
# =============================================================================


@router.post("/{source_id}/ingest", response_model=CrawlIngestResponse)
async def ingest_source(
    source_id: str,
    request: CrawlIngestRequest,
    org: Organization = Depends(get_current_organization),
) -> CrawlIngestResponse:
    """Start crawling a source via job queue (org-scoped).

    Jobs are processed by the arq worker for reliability and persistence.
    Run the worker with: uv run arq sibyl.jobs.WorkerSettings
    """
    from sibyl.jobs import enqueue_crawl

    # Verify source exists and belongs to org
    async with get_session() as session:
        source = await _get_org_source(session, source_id, org)

        # Check if already crawling
        if source.crawl_status == CrawlStatus.IN_PROGRESS:
            return CrawlIngestResponse(
                source_id=source_id,
                status="already_running",
                message="Crawl already in progress for this source",
            )

        source_name = source.name

    # Enqueue the crawl job (force=True clears old results for re-crawl)
    try:
        job_id = await enqueue_crawl(
            source_id,
            max_pages=request.max_pages,
            max_depth=request.max_depth,
            generate_embeddings=request.generate_embeddings,
            force=True,
        )

        # Save job_id to source for cancellation support
        async with get_session() as session:
            source = await session.get(CrawlSource, UUID(source_id))
            if source:
                source.current_job_id = job_id
                session.add(source)

        log.info(
            "Enqueued crawl job",
            source_id=source_id,
            job_id=job_id,
            max_pages=request.max_pages,
        )

        return CrawlIngestResponse(
            source_id=source_id,
            job_id=job_id,
            status="queued",
            message=f"Crawl job queued for {source_name}",
        )

    except Exception as e:
        log.exception("Failed to enqueue crawl job", source_id=source_id, error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Failed to enqueue job. Is the job queue available?",
        ) from e


@router.get("/{source_id}/status")
async def get_ingestion_status(
    source_id: str,
    org: Organization = Depends(get_current_organization),
) -> dict[str, Any]:
    """Get crawl status for a source (org-scoped).

    Returns both the source's crawl_status and any active job status.
    """
    # Get source status from DB (org-scoped)
    async with get_session() as session:
        source = await _get_org_source(session, source_id, org)

        return {
            "source_id": source_id,
            "crawl_status": source.crawl_status.value,
            "current_job_id": source.current_job_id,
            "document_count": source.document_count,
            "chunk_count": source.chunk_count,
            "last_crawled_at": source.last_crawled_at.isoformat()
            if source.last_crawled_at
            else None,
            "last_error": source.last_error,
        }


@router.post("/{source_id}/cancel", response_model=CrawlIngestResponse)
async def cancel_crawl(
    source_id: str,
    org: Organization = Depends(get_current_organization),
) -> CrawlIngestResponse:
    """Cancel an in-progress crawl for a source (org-scoped).

    Cancels the job if running and resets the source status.
    """
    from sibyl.jobs.queue import cancel_job

    async with get_session() as session:
        source = await _get_org_source(session, source_id, org)

        # Check if there's a job to cancel
        job_id = source.current_job_id
        if not job_id:
            return CrawlIngestResponse(
                source_id=source_id,
                status="no_job",
                message="No active crawl job to cancel",
            )

        # Try to cancel the job
        try:
            cancelled = await cancel_job(job_id)
        except Exception as e:
            log.warning("Failed to cancel job", job_id=job_id, error=str(e))
            cancelled = False

        # Reset source status regardless of cancel result
        source.crawl_status = CrawlStatus.PENDING
        source.current_job_id = None
        session.add(source)

        log.info(
            "Cancelled crawl",
            source_id=source_id,
            job_id=job_id,
            job_cancelled=cancelled,
        )

        await broadcast_event(
            "entity_updated",
            {"type": "crawl_source", "id": source_id},
            org_id=str(org.id),
        )

        return CrawlIngestResponse(
            source_id=source_id,
            job_id=job_id,
            status="cancelled",
            message=f"Crawl cancelled for {source.name}",
        )


@router.post("/{source_id}/sync")
async def sync_source(
    source_id: str,
    org: Organization = Depends(get_current_organization),
) -> dict[str, Any]:
    """Sync source stats from actual document/chunk counts (org-scoped).

    Useful for fixing stuck sources or after manual data changes.
    Recalculates document_count, chunk_count, and fixes status if stuck.
    """
    async with get_session() as session:
        source = await _get_org_source(session, source_id, org)

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
                    source.last_crawled_at = utcnow_naive()
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

    await broadcast_event(
        "entity_updated",
        {"type": "crawl_source", "id": source_id},
        org_id=str(org.id),
    )

    return {
        "source_id": source_id,
        "synced": True,
        "document_count": actual_doc_count,
        "chunk_count": actual_chunk_count,
        "status": new_status.value,
        "changes": {
            "status": f"{old_status.value} -> {new_status.value}"
            if old_status != new_status
            else None,
            "document_count": f"{old_doc_count} -> {actual_doc_count}"
            if old_doc_count != actual_doc_count
            else None,
            "chunk_count": f"{old_chunk_count} -> {actual_chunk_count}"
            if old_chunk_count != actual_chunk_count
            else None,
        },
    }


# =============================================================================
# Graph Integration
# =============================================================================


@router.get("/link-graph/status", response_model=LinkGraphStatusResponse)
async def get_link_graph_status(
    org: Organization = Depends(get_current_organization),
) -> LinkGraphStatusResponse:
    """Get status of pending graph linking work (org-scoped).

    Shows how many chunks still need entity extraction per source.
    """
    async with get_session() as session:
        # Total chunks
        total_result = await session.execute(
            select(func.count(DocumentChunk.id))
            .join(CrawledDocument, col(CrawledDocument.id) == col(DocumentChunk.document_id))
            .join(CrawlSource, col(CrawlSource.id) == col(CrawledDocument.source_id))
            .where(col(CrawlSource.organization_id) == org.id)
        )
        total_chunks = total_result.scalar() or 0

        # Chunks with entities
        linked_result = await session.execute(
            select(func.count(DocumentChunk.id))
            .join(CrawledDocument, col(CrawledDocument.id) == col(DocumentChunk.document_id))
            .join(CrawlSource, col(CrawlSource.id) == col(CrawledDocument.source_id))
            .where(col(CrawlSource.organization_id) == org.id)
            .where(col(DocumentChunk.has_entities) == True)  # noqa: E712
        )
        chunks_with_entities = linked_result.scalar() or 0

        # Pending per source
        pending_query = (
            select(  # type: ignore[call-overload]
                CrawlSource.name,
                func.count(DocumentChunk.id).label("pending"),
            )
            .join(CrawledDocument, CrawledDocument.source_id == CrawlSource.id)
            .join(DocumentChunk, DocumentChunk.document_id == CrawledDocument.id)
            .where(col(CrawlSource.organization_id) == org.id)
            .where(col(DocumentChunk.has_entities) == False)  # noqa: E712
            .group_by(CrawlSource.name)
        )
        pending_result = await session.execute(pending_query)
        sources = [{"name": row.name, "pending": row.pending} for row in pending_result.all()]

    return LinkGraphStatusResponse(
        total_chunks=total_chunks,
        chunks_with_entities=chunks_with_entities,
        chunks_pending=total_chunks - chunks_with_entities,
        sources=sources,
    )


@router.post("/link-graph", response_model=LinkGraphResponse)
async def link_all_sources_to_graph(
    request: LinkGraphRequest,
    org: Organization = Depends(get_current_organization),
) -> LinkGraphResponse:
    """Extract entities from all source chunks and link to knowledge graph.

    Processes chunks that haven't been entity-linked yet (has_entities=False).
    Uses LLM to extract entities and matches them to existing graph entities.
    """
    return await _process_graph_linking(
        source_id=None, request=request, organization_id=str(org.id)
    )


@router.post("/{source_id}/link-graph", response_model=LinkGraphResponse)
async def link_source_to_graph(
    source_id: str,
    request: LinkGraphRequest,
    org: Organization = Depends(get_current_organization),
) -> LinkGraphResponse:
    """Extract entities from source chunks and link to knowledge graph.

    Processes chunks that haven't been entity-linked yet (has_entities=False).
    Uses LLM to extract entities and matches them to existing graph entities.
    """
    return await _process_graph_linking(
        source_id=source_id, request=request, organization_id=str(org.id)
    )


async def _process_graph_linking(
    source_id: str | None,
    request: LinkGraphRequest,
    organization_id: str,
) -> LinkGraphResponse:
    """Internal function to process graph linking for one or all sources."""
    from sibyl.crawler.graph_integration import GraphIntegrationService
    from sibyl.graph.client import get_graph_client

    # Connect to graph
    try:
        graph_client = await get_graph_client()
    except Exception as e:
        log.warning("Failed to connect to graph", error=str(e))
        raise HTTPException(status_code=503, detail="Graph service unavailable") from e

    # Initialize integration service
    try:
        integration = GraphIntegrationService(
            graph_client,
            organization_id,
            extract_entities=True,
            create_new_entities=False,
        )
    except ValueError as e:
        log.warning("Entity extraction configuration error", error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Entity extraction not configured",
        ) from e

    org_uuid = UUID(organization_id)

    # Get sources to process (org-scoped)
    async with get_session() as session:
        if source_id:
            result = await session.execute(
                select(CrawlSource).where(
                    col(CrawlSource.id) == UUID(source_id),
                    col(CrawlSource.organization_id) == org_uuid,
                )
            )
            source = result.scalar_one_or_none()
            if not source:
                raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")
            sources = [source]
        else:
            result = await session.execute(
                select(CrawlSource).where(col(CrawlSource.organization_id) == org_uuid)
            )
            sources = list(result.scalars().all())

        if not sources:
            return LinkGraphResponse(
                source_id=source_id,
                status="no_sources",
                message="No sources found to process",
            )

        total_chunks = 0
        total_extracted = 0
        total_linked = 0
        sources_processed = []

        for source in sources:
            # Get unprocessed chunks for this source
            chunk_query = (
                select(DocumentChunk)
                .join(CrawledDocument)
                .where(col(CrawledDocument.source_id) == source.id)
                .where(col(DocumentChunk.has_entities) == False)  # noqa: E712
                .limit(request.batch_size * 10)
            )
            result = await session.execute(chunk_query)
            chunks = list(result.scalars().all())

            if not chunks:
                continue

            sources_processed.append(source.name)

            if request.dry_run:
                total_chunks += len(chunks)
                continue

            # Process in batches
            for i in range(0, len(chunks), request.batch_size):
                batch = chunks[i : i + request.batch_size]
                stats = await integration.process_chunks(batch, source.name)
                total_chunks += len(batch)
                total_extracted += stats.entities_extracted
                total_linked += stats.entities_linked

    # Count remaining unprocessed chunks
    async with get_session() as session:
        remaining_query = (
            select(func.count(DocumentChunk.id))
            .join(CrawledDocument, col(CrawledDocument.id) == col(DocumentChunk.document_id))
            .join(CrawlSource, col(CrawlSource.id) == col(CrawledDocument.source_id))
            .where(col(CrawlSource.organization_id) == org_uuid)
            .where(col(DocumentChunk.has_entities) == False)  # noqa: E712
        )
        if source_id:
            remaining_query = remaining_query.where(col(CrawledDocument.source_id) == UUID(source_id))
        remaining_result = await session.execute(remaining_query)
        chunks_remaining = remaining_result.scalar() or 0

    if request.dry_run:
        return LinkGraphResponse(
            source_id=source_id,
            status="dry_run",
            chunks_processed=total_chunks,
            chunks_remaining=chunks_remaining,
            sources_processed=sources_processed,
            message=f"Would process {total_chunks} chunks from {len(sources_processed)} source(s)",
        )

    if total_chunks == 0:
        return LinkGraphResponse(
            source_id=source_id,
            status="no_chunks",
            chunks_remaining=chunks_remaining,
            message="No unprocessed chunks found",
        )

    await broadcast_event(
        "graph_updated",
        {"chunks_processed": total_chunks},
        org_id=str(org_uuid),
    )

    return LinkGraphResponse(
        source_id=source_id,
        status="completed",
        chunks_processed=total_chunks,
        chunks_remaining=chunks_remaining,
        entities_extracted=total_extracted,
        entities_linked=total_linked,
        sources_processed=sources_processed,
        message=f"Processed {total_chunks} chunks, extracted {total_extracted} entities",
    )


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
