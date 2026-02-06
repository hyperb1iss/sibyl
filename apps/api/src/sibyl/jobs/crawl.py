"""Crawl and sync jobs for documentation sources.

These jobs handle web crawling and source synchronization in the background.
"""

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlmodel import col

from sibyl.api.event_types import WSEvent
from sibyl.db import CrawledDocument, CrawlSource, CrawlStatus, DocumentChunk, get_session
from sibyl.db.models import utcnow_naive

log = structlog.get_logger()


async def _safe_broadcast(event: str, data: dict[str, Any], *, org_id: str | None) -> None:
    """Broadcast event via Redis pub/sub (worker runs in separate process)."""
    try:
        from sibyl.api.pubsub import publish_event

        await publish_event(event, data, org_id=org_id)
    except Exception:
        log.debug("Broadcast failed (Redis unavailable)", event=event)


async def crawl_source(
    ctx: dict[str, Any],  # noqa: ARG001
    source_id: str,
    *,
    max_pages: int = 100,
    max_depth: int = 3,
    generate_embeddings: bool = True,
) -> dict[str, Any]:
    """Crawl a documentation source.

    This is the main crawl job that:
    1. Fetches source from DB
    2. Runs the ingestion pipeline
    3. Updates source status
    4. Returns stats

    Args:
        ctx: arq context
        source_id: UUID of source to crawl
        max_pages: Maximum pages to crawl
        max_depth: Maximum link depth
        generate_embeddings: Whether to generate embeddings

    Returns:
        Dict with crawl stats
    """
    from sibyl.crawler import IngestionPipeline

    log.info(
        "Starting crawl job",
        source_id=source_id,
        max_pages=max_pages,
        max_depth=max_depth,
    )

    # Get source and update status
    async with get_session() as session:
        source = await session.get(CrawlSource, UUID(source_id))
        if not source:
            raise ValueError(f"Source not found: {source_id}")

        # Update status to IN_PROGRESS
        source.crawl_status = CrawlStatus.IN_PROGRESS
        source.last_error = None
        await session.flush()  # Persist status change before expunge

        # Detach for background processing
        source_name = source.name
        organization_id = str(source.organization_id)
        session.expunge(source)

    # Broadcast start event
    await _safe_broadcast(
        WSEvent.CRAWL_STARTED,
        {
            "source_id": source_id,
            "source_name": source_name,
            "max_pages": max_pages,
        },
        org_id=organization_id,
    )

    # Progress callback: update DB + broadcast after each document
    async def on_progress(stats: Any, chunks_added: int) -> None:
        """Update source stats and broadcast progress after each document."""
        async with get_session() as session:
            db_source = await session.get(CrawlSource, UUID(source_id))
            if db_source:
                db_source.document_count = stats.documents_stored
                db_source.chunk_count = stats.chunks_created

        await _safe_broadcast(
            WSEvent.CRAWL_PROGRESS,
            {
                "source_id": source_id,
                "source_name": source_name,
                "documents_crawled": stats.documents_crawled,
                "documents_stored": stats.documents_stored,
                "chunks_created": stats.chunks_created,
                "chunks_added": chunks_added,
                "errors": stats.errors,
            },
            org_id=organization_id,
        )

    # Run ingestion
    try:
        async with IngestionPipeline(
            organization_id, generate_embeddings=generate_embeddings
        ) as pipeline:
            stats = await pipeline.ingest_source(
                source,
                max_pages=max_pages,
                max_depth=max_depth,
                on_progress=on_progress,
            )

        # Update source with results
        async with get_session() as session:
            db_source = await session.get(CrawlSource, UUID(source_id))
            if db_source:
                db_source.crawl_status = (
                    CrawlStatus.COMPLETED if stats.errors == 0 else CrawlStatus.PARTIAL
                )
                db_source.current_job_id = None  # Clear job ID on completion
                db_source.last_crawled_at = utcnow_naive()
                db_source.document_count = stats.documents_stored
                db_source.chunk_count = stats.chunks_created

        result = {
            "source_id": source_id,
            "source_name": source_name,
            "documents_crawled": stats.documents_crawled,
            "documents_stored": stats.documents_stored,
            "chunks_created": stats.chunks_created,
            "embeddings_generated": stats.embeddings_generated,
            "errors": stats.errors,
            "duration_seconds": stats.duration_seconds,
        }

        # Broadcast completion
        await _safe_broadcast(WSEvent.CRAWL_COMPLETE, result, org_id=organization_id)

        log.info("Crawl job complete", **result)
        return result

    except Exception as e:
        # Update source with error
        async with get_session() as session:
            db_source = await session.get(CrawlSource, UUID(source_id))
            if db_source:
                db_source.crawl_status = CrawlStatus.FAILED
                db_source.current_job_id = None  # Clear job on failure
                db_source.last_error = str(e)[:1000]

        await _safe_broadcast(
            WSEvent.CRAWL_COMPLETE,
            {"source_id": source_id, "error": str(e)},
            org_id=organization_id,
        )

        log.exception("Crawl job failed", source_id=source_id)
        raise


async def sync_source(ctx: dict[str, Any], source_id: str) -> dict[str, Any]:  # noqa: ARG001
    """Sync source stats from actual data.

    Recalculates document_count, chunk_count, and fixes status.

    Args:
        ctx: arq context
        source_id: UUID of source to sync

    Returns:
        Dict with sync results
    """
    log.info("Starting sync job", source_id=source_id)

    async with get_session() as session:
        source = await session.get(CrawlSource, UUID(source_id))
        if not source:
            raise ValueError(f"Source not found: {source_id}")
        organization_id = str(source.organization_id)

        # Count actual documents
        source_uuid = UUID(source_id)
        source_filter = col(CrawledDocument.source_id) == source_uuid
        doc_result = await session.execute(
            select(func.count(CrawledDocument.id)).where(source_filter)  # pyright: ignore[reportArgumentType]
        )
        doc_count = doc_result.scalar() or 0

        # Count actual chunks
        chunk_result = await session.execute(
            select(func.count(DocumentChunk.id))  # pyright: ignore[reportArgumentType]
            .join(CrawledDocument)
            .where(source_filter)
        )
        chunk_count = chunk_result.scalar() or 0

        # Update source
        old_status = source.crawl_status
        old_doc_count = source.document_count
        old_chunk_count = source.chunk_count

        source.document_count = doc_count
        source.chunk_count = chunk_count

        if doc_count > 0 and source.crawl_status == CrawlStatus.IN_PROGRESS:
            source.crawl_status = CrawlStatus.COMPLETED
            source.current_job_id = None  # Clear job on sync completion
            if source.last_crawled_at is None:
                source.last_crawled_at = utcnow_naive()
        elif doc_count == 0 and source.crawl_status == CrawlStatus.IN_PROGRESS:
            source.crawl_status = CrawlStatus.PENDING
            source.current_job_id = None  # Clear job on sync reset

        result = {
            "source_id": source_id,
            "document_count": doc_count,
            "chunk_count": chunk_count,
            "status": source.crawl_status.value,
            "changes": {
                "status": f"{old_status.value} -> {source.crawl_status.value}"
                if old_status != source.crawl_status
                else None,
                "document_count": f"{old_doc_count} -> {doc_count}"
                if old_doc_count != doc_count
                else None,
                "chunk_count": f"{old_chunk_count} -> {chunk_count}"
                if old_chunk_count != chunk_count
                else None,
            },
        }

    log.info("Sync job complete", **result)
    await _safe_broadcast(WSEvent.CRAWL_SYNC_COMPLETE, result, org_id=organization_id)
    return result


async def sync_all_sources(ctx: dict[str, Any]) -> dict[str, Any]:
    """Sync all sources - can be run as a cron job."""
    from sibyl.crawler.service import list_sources

    sources = await list_sources()
    synced = 0

    for source in sources:
        try:
            await sync_source(ctx, str(source.id))
            synced += 1
        except Exception as e:
            log.warning("Failed to sync source", source_id=str(source.id), error=str(e))

    return {"synced": synced, "total": len(sources)}
