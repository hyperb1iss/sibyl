"""arq worker - processes background jobs.

Run with: uv run arq sibyl.jobs.WorkerSettings

This worker processes:
- crawl_source: Full documentation crawling
- sync_source: Recalculate source stats
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from arq.connections import RedisSettings
from sqlalchemy import func, select
from sqlmodel import col

from sibyl.config import settings
from sibyl.db import (
    CrawledDocument,
    CrawlSource,
    CrawlStatus,
    DocumentChunk,
    get_session,
)

log = structlog.get_logger()


async def _safe_broadcast(event: str, data: dict[str, Any]) -> None:
    """Broadcast event, silently ignoring failures (WebSocket may not be available)."""
    try:
        from sibyl.api.websocket import broadcast_event

        await broadcast_event(event, data)
    except Exception:
        log.debug("Broadcast failed (WebSocket unavailable)", event=event)


def get_redis_settings() -> RedisSettings:
    """Get Redis connection settings."""
    return RedisSettings(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        password=settings.falkordb_password,
        database=settings.redis_jobs_db,
    )


async def startup(ctx: dict[str, Any]) -> None:
    """Worker startup - initialize resources."""
    log.info("Job worker starting up")
    # Could initialize shared resources here
    ctx["start_time"] = datetime.now(UTC)


async def shutdown(ctx: dict[str, Any]) -> None:  # noqa: ARG001
    """Worker shutdown - cleanup resources."""
    log.info("Job worker shutting down")


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

    # Get source
    async with get_session() as session:
        source = await session.get(CrawlSource, UUID(source_id))
        if not source:
            raise ValueError(f"Source not found: {source_id}")

        # Update status
        source.crawl_status = CrawlStatus.IN_PROGRESS
        source.last_error = None

        # Detach for background processing
        source_name = source.name
        session.expunge(source)

    # Broadcast start event
    await _safe_broadcast(
        "crawl_started",
        {
            "source_id": source_id,
            "source_name": source_name,
            "max_pages": max_pages,
        },
    )

    # Run ingestion
    try:
        async with IngestionPipeline(generate_embeddings=generate_embeddings) as pipeline:
            stats = await pipeline.ingest_source(
                source,
                max_pages=max_pages,
                max_depth=max_depth,
            )

        # Update source with results
        async with get_session() as session:
            db_source = await session.get(CrawlSource, UUID(source_id))
            if db_source:
                db_source.crawl_status = (
                    CrawlStatus.COMPLETED if stats.errors == 0 else CrawlStatus.PARTIAL
                )
                db_source.last_crawled_at = datetime.now(UTC)
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
        await _safe_broadcast("crawl_complete", result)

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

        await _safe_broadcast("crawl_complete", {"source_id": source_id, "error": str(e)})

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

        # Count actual documents
        doc_result = await session.execute(
            select(func.count(CrawledDocument.id)).where(
                col(CrawledDocument.source_id) == UUID(source_id)
            )
        )
        doc_count = doc_result.scalar() or 0

        # Count actual chunks
        chunk_result = await session.execute(
            select(func.count(DocumentChunk.id))
            .join(CrawledDocument)
            .where(col(CrawledDocument.source_id) == UUID(source_id))
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
                source.last_crawled_at = datetime.now(UTC)
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
    return result


# Optional: Scheduled job to sync all sources
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


# Worker configuration
class WorkerSettings:
    """arq worker settings."""

    redis_settings = get_redis_settings()

    # Job functions
    functions = [crawl_source, sync_source, sync_all_sources]

    # Lifecycle hooks
    on_startup = startup
    on_shutdown = shutdown

    # Worker settings
    max_jobs = 3  # Max concurrent jobs
    job_timeout = 3600  # 1 hour timeout for crawl jobs
    keep_result = 86400  # Keep results for 24 hours
    poll_delay = 0.5  # Check for jobs every 0.5s



async def run_worker_async() -> None:
    """Run the arq worker in-process.

    This allows running the worker as part of the main server process
    instead of as a separate process. Useful for development and
    simpler deployments.
    """
    from arq import Worker

    log.info("Starting in-process job worker")

    worker = Worker(
        functions=WorkerSettings.functions,
        redis_settings=WorkerSettings.redis_settings,
        on_startup=WorkerSettings.on_startup,
        on_shutdown=WorkerSettings.on_shutdown,
        max_jobs=WorkerSettings.max_jobs,
        job_timeout=WorkerSettings.job_timeout,
        keep_result=WorkerSettings.keep_result,
        poll_delay=WorkerSettings.poll_delay,
    )

    await worker.async_run()
