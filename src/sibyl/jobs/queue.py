"""Job queue client - enqueue jobs and check status.

This module provides the client-side interface for enqueuing jobs
and checking their status. Jobs are processed by the worker.
"""

import contextlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

import structlog
from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from arq.jobs import Job, JobStatus as ArqJobStatus

from sibyl.config import settings

log = structlog.get_logger()


def get_redis_settings() -> RedisSettings:
    """Get Redis connection settings for arq.

    Uses FalkorDB's Redis instance with a separate database for jobs.
    """
    return RedisSettings(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        password=settings.falkordb_password,
        database=settings.redis_jobs_db,
    )


class JobStatus(str, Enum):
    """Job status enum matching arq statuses."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    NOT_FOUND = "not_found"
    DEFERRED = "deferred"


@dataclass
class JobInfo:
    """Information about a job."""

    job_id: str
    function: str
    status: JobStatus
    enqueue_time: datetime | None = None
    start_time: datetime | None = None
    finish_time: datetime | None = None
    result: Any = None
    error: str | None = None


# Singleton pool for reuse
_pool: ArqRedis | None = None


async def get_pool() -> ArqRedis:
    """Get or create the Redis connection pool."""
    global _pool  # noqa: PLW0603
    if _pool is None:
        _pool = await create_pool(get_redis_settings())
    return _pool


async def close_pool() -> None:
    """Close the Redis connection pool."""
    global _pool  # noqa: PLW0603
    if _pool is not None:
        await _pool.close()
        _pool = None


async def enqueue_crawl(
    source_id: str | UUID,
    *,
    max_pages: int = 100,
    max_depth: int = 3,
    generate_embeddings: bool = True,
) -> str:
    """Enqueue a crawl job for a source.

    Args:
        source_id: UUID of the source to crawl
        max_pages: Maximum pages to crawl
        max_depth: Maximum link depth
        generate_embeddings: Whether to generate embeddings

    Returns:
        Job ID for tracking
    """
    pool = await get_pool()

    job = await pool.enqueue_job(
        "crawl_source",
        str(source_id),
        max_pages=max_pages,
        max_depth=max_depth,
        generate_embeddings=generate_embeddings,
    )

    if job is None:
        raise RuntimeError("Failed to enqueue crawl job")

    log.info(
        "Enqueued crawl job",
        job_id=job.job_id,
        source_id=str(source_id),
        max_pages=max_pages,
    )

    return job.job_id


async def enqueue_sync(source_id: str | UUID) -> str:
    """Enqueue a source sync job.

    Recalculates document/chunk counts from actual data.

    Args:
        source_id: UUID of the source to sync

    Returns:
        Job ID for tracking
    """
    pool = await get_pool()

    job = await pool.enqueue_job("sync_source", str(source_id))

    if job is None:
        raise RuntimeError("Failed to enqueue sync job")

    log.info("Enqueued sync job", job_id=job.job_id, source_id=str(source_id))

    return job.job_id


async def get_job_status(job_id: str) -> JobInfo:
    """Get the status of a job.

    Args:
        job_id: The job ID to check

    Returns:
        JobInfo with current status
    """
    pool = await get_pool()
    job = Job(job_id, pool)

    status = await job.status()
    info = await job.info()

    # Map arq status to our enum
    status_map = {
        ArqJobStatus.queued: JobStatus.QUEUED,
        ArqJobStatus.in_progress: JobStatus.IN_PROGRESS,
        ArqJobStatus.complete: JobStatus.COMPLETE,
        ArqJobStatus.not_found: JobStatus.NOT_FOUND,
        ArqJobStatus.deferred: JobStatus.DEFERRED,
    }

    job_info = JobInfo(
        job_id=job_id,
        function=info.function if info else "unknown",
        status=status_map.get(status, JobStatus.NOT_FOUND),
    )

    if info:
        # JobDef has enqueue_time
        job_info.enqueue_time = getattr(info, "enqueue_time", None)

    # For completed jobs, try to get result (non-blocking)
    if status == ArqJobStatus.complete:
        with contextlib.suppress(Exception):
            result = await job.result_info()
            if result:
                job_info.result = result.result
                job_info.finish_time = result.finish_time
                job_info.start_time = result.start_time
                if not result.success:
                    job_info.error = str(result.result)
                    job_info.result = None

    return job_info


async def list_jobs(
    *,
    function: str | None = None,
    limit: int = 50,
) -> list[JobInfo]:
    """List recent jobs.

    Args:
        function: Filter by function name
        limit: Maximum jobs to return

    Returns:
        List of JobInfo
    """
    pool = await get_pool()

    # Get job IDs from the queue
    # Note: arq doesn't have a built-in list function, so we track manually
    # This is a simplified implementation
    job_ids = await pool.keys("arq:job:*")

    jobs = []
    for key in job_ids[:limit]:
        job_id = key.decode().replace("arq:job:", "") if isinstance(key, bytes) else key.replace("arq:job:", "")
        try:
            info = await get_job_status(job_id)
            if function is None or info.function == function:
                jobs.append(info)
        except Exception:
            continue

    return jobs


async def cancel_job(job_id: str) -> bool:
    """Cancel a queued job.

    Args:
        job_id: The job ID to cancel

    Returns:
        True if cancelled, False if not found or already running
    """
    pool = await get_pool()
    job = Job(job_id, pool)

    status = await job.status()
    if status == ArqJobStatus.queued:
        await job.abort()
        log.info("Cancelled job", job_id=job_id)
        return True

    return False
