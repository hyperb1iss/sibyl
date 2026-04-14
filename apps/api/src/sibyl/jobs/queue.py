"""Job queue client - enqueue jobs and check status.

This module provides the client-side interface for enqueuing jobs
and checking their status. Jobs are processed by the worker.
"""

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID

import structlog
from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from arq.jobs import Job, JobStatus as ArqJobStatus

from sibyl.backup_ids import generate_backup_id
from sibyl.config import settings

log = structlog.get_logger()

RECENT_JOB_INDEX_KEY = "sibyl:jobs:recent"
RECENT_JOB_INDEX_LIMIT = 1000


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
    # Internal fields for filtering/debugging (not intended for API exposure).
    args: tuple[Any, ...] | None = None
    kwargs: dict[str, Any] | None = None


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


def _decode_job_id(job_id: bytes | str) -> str:
    return job_id.decode() if isinstance(job_id, bytes) else job_id


async def _record_recent_job(pool: ArqRedis, job_id: str) -> None:
    """Record a job in the recent-job index."""
    score = datetime.now(UTC).timestamp()
    await pool.zadd(RECENT_JOB_INDEX_KEY, {job_id: score})
    await pool.zremrangebyrank(RECENT_JOB_INDEX_KEY, 0, -(RECENT_JOB_INDEX_LIMIT + 1))


async def _list_recent_job_ids(pool: ArqRedis) -> list[str]:
    """Get recent job IDs from the index, with a legacy scan fallback."""
    try:
        job_ids = await pool.zrevrange(RECENT_JOB_INDEX_KEY, 0, -1)
    except Exception:
        job_ids = []

    if job_ids:
        return [_decode_job_id(job_id) for job_id in job_ids]

    return [
        _decode_job_id(key).removeprefix("arq:job:")
        async for key in pool.scan_iter(match="arq:job:*")
    ]


async def enqueue_crawl(
    source_id: str | UUID,
    *,
    organization_id: str | None = None,
    max_pages: int = 100,
    max_depth: int = 3,
    generate_embeddings: bool = True,
    force: bool = False,
) -> str:
    """Enqueue a crawl job for a source.

    Uses a deterministic job ID based on source_id to prevent duplicate jobs.
    If a job for this source is already queued/running, returns the existing job ID.

    Args:
        source_id: UUID of the source to crawl
        organization_id: Optional org ID metadata for route-side visibility checks
        max_pages: Maximum pages to crawl
        max_depth: Maximum link depth
        generate_embeddings: Whether to generate embeddings
        force: Clear old result and re-enqueue even if previously completed

    Returns:
        Job ID for tracking
    """
    pool = await get_pool()

    # Deterministic job ID prevents duplicate jobs for the same source
    job_id = f"crawl:{source_id}"

    # If force=True, clear any old result to allow re-enqueue
    if force:
        result_key = f"arq:result:{job_id}"
        await pool.delete(result_key)
        log.debug("Cleared old result for re-crawl", job_id=job_id)

    job_kwargs: dict[str, Any] = {
        "max_pages": max_pages,
        "max_depth": max_depth,
        "generate_embeddings": generate_embeddings,
        "_job_id": job_id,
    }
    if organization_id is not None:
        job_kwargs["organization_id"] = organization_id

    job = await pool.enqueue_job("crawl_source", str(source_id), **job_kwargs)

    if job is None:
        # Job already exists (queued/running) - return the existing job ID
        log.info("Crawl job already exists", job_id=job_id, source_id=str(source_id))
        await _record_recent_job(pool, job_id)
        return job_id

    log.info(
        "Enqueued crawl job",
        job_id=job.job_id,
        source_id=str(source_id),
        max_pages=max_pages,
    )

    await _record_recent_job(pool, job.job_id)
    return job.job_id


async def enqueue_sync(source_id: str | UUID, *, organization_id: str | None = None) -> str:
    """Enqueue a source sync job.

    Uses a deterministic job ID based on source_id to prevent duplicate jobs.
    Recalculates document/chunk counts from actual data.

    Args:
        source_id: UUID of the source to sync
        organization_id: Optional org ID metadata for route-side visibility checks

    Returns:
        Job ID for tracking
    """
    pool = await get_pool()

    # Deterministic job ID prevents duplicate jobs
    job_id = f"sync:{source_id}"

    job_kwargs: dict[str, Any] = {"_job_id": job_id}
    if organization_id is not None:
        job_kwargs["organization_id"] = organization_id

    job = await pool.enqueue_job("sync_source", str(source_id), **job_kwargs)

    if job is None:
        # Job already exists
        log.info("Sync job already exists", job_id=job_id, source_id=str(source_id))
        await _record_recent_job(pool, job_id)
        return job_id

    log.info("Enqueued sync job", job_id=job.job_id, source_id=str(source_id))

    await _record_recent_job(pool, job.job_id)
    return job.job_id


async def enqueue_create_entity(
    entity_id: str,
    entity_data: dict[str, Any],
    entity_type: str,
    group_id: str,
    relationships: list[dict[str, Any]] | None = None,
    auto_link_params: dict[str, Any] | None = None,
) -> str:
    """Enqueue an entity creation job.

    Creates entity asynchronously via Graphiti for LLM-powered
    relationship discovery. Marks entity as pending so operations
    targeting it can queue until it materializes.

    Args:
        entity_id: Pre-generated entity ID
        entity_data: Serialized entity dict
        entity_type: Type string (episode, pattern, task, project)
        group_id: Organization ID
        relationships: Optional explicit relationships to create
        auto_link_params: Parameters for auto-link discovery (always runs if provided)

    Returns:
        Job ID for tracking
    """
    from sibyl.jobs.pending import mark_pending

    pool = await get_pool()

    # Deterministic job ID based on entity ID
    job_id = f"create_entity:{entity_id}"

    job = await pool.enqueue_job(
        "create_entity",
        entity_data,
        entity_type,
        group_id,
        relationships=relationships,
        auto_link_params=auto_link_params,
        _job_id=job_id,
    )

    if job is None:
        log.info("Create entity job already exists", job_id=job_id, entity_id=entity_id)
        await _record_recent_job(pool, job_id)
        return job_id

    # Mark entity as pending so operations can queue against it
    await mark_pending(entity_id, job_id, entity_type, group_id)

    log.info(
        "Enqueued create_entity job",
        job_id=job.job_id,
        entity_id=entity_id,
        entity_type=entity_type,
    )

    await _record_recent_job(pool, job.job_id)
    return job.job_id


async def enqueue_update_entity(
    entity_id: str,
    updates: dict[str, Any],
    entity_type: str,
    group_id: str,
) -> str:
    """Enqueue an entity update job.

    Updates entity fields asynchronously. Useful for bulk updates or
    when caller doesn't need to wait for completion.

    Args:
        entity_id: The entity ID to update
        updates: Dict of field names to new values
        entity_type: Type string (episode, pattern, task, project, etc.)
        group_id: Organization ID

    Returns:
        Job ID for tracking
    """
    pool = await get_pool()

    # Deterministic job ID based on entity ID
    job_id = f"update_entity:{entity_id}"

    job = await pool.enqueue_job(
        "update_entity",
        entity_id,
        updates,
        entity_type,
        group_id,
        _job_id=job_id,
    )

    if job is None:
        log.info("Update entity job already exists", job_id=job_id, entity_id=entity_id)
        await _record_recent_job(pool, job_id)
        return job_id

    log.info(
        "Enqueued update_entity job",
        job_id=job.job_id,
        entity_id=entity_id,
        entity_type=entity_type,
        fields=list(updates.keys()),
    )

    await _record_recent_job(pool, job.job_id)
    return job.job_id


async def enqueue_create_learning_episode(
    task_data: dict[str, Any],
    group_id: str,
) -> str:
    """Enqueue a learning episode creation job.

    Creates a learning episode from a completed task asynchronously.
    The episode captures learnings and links back to the task.

    Args:
        task_data: Serialized task dict (from task.model_dump())
        group_id: Organization ID

    Returns:
        Job ID for tracking
    """
    pool = await get_pool()

    task_id = task_data.get("id", "unknown")
    job_id = f"learning_episode:{task_id}"

    job = await pool.enqueue_job(
        "create_learning_episode",
        task_data,
        group_id,
        _job_id=job_id,
    )

    if job is None:
        log.info("Learning episode job already exists", job_id=job_id, task_id=task_id)
        await _record_recent_job(pool, job_id)
        return job_id

    log.info(
        "Enqueued learning episode job",
        job_id=job.job_id,
        task_id=task_id,
    )

    await _record_recent_job(pool, job.job_id)
    return job.job_id


async def enqueue_update_task(
    task_id: str,
    updates: dict[str, Any],
    group_id: str,
    epic_id: str | None = None,
    new_status: str | None = None,
    add_depends_on: list[str] | None = None,
    remove_depends_on: list[str] | None = None,
) -> str:
    """Enqueue a task update job.

    Uses the task-aware ``update_task`` job which handles epic relationships,
    epic auto-start, and dependency mutations — concerns the generic
    ``update_entity`` doesn't cover.

    Job IDs are timestamp-suffixed to prevent arq deduplication from silently
    dropping rapid-fire updates to the same task.

    Args:
        task_id: The task entity ID to update
        updates: Dict of field names to new values
        group_id: Organization ID
        epic_id: Epic ID if being set/changed (triggers BELONGS_TO creation)
        new_status: New task status string (triggers epic auto-start check)
        add_depends_on: Task IDs to add as dependencies
        remove_depends_on: Task IDs to remove as dependencies

    Returns:
        Job ID for tracking
    """
    import time

    pool = await get_pool()

    # Timestamp-suffixed ID: allows multiple queued updates for the same task
    epoch_ms = int(time.time() * 1000)
    job_id = f"update_task:{task_id}:{epoch_ms}"

    job = await pool.enqueue_job(
        "update_task",
        task_id,
        updates,
        group_id,
        epic_id=epic_id,
        new_status=new_status,
        add_depends_on=add_depends_on or [],
        remove_depends_on=remove_depends_on or [],
        _job_id=job_id,
    )

    if job is None:
        log.info("Update task job already exists", job_id=job_id, task_id=task_id)
        await _record_recent_job(pool, job_id)
        return job_id

    log.info(
        "Enqueued update_task job",
        job_id=job.job_id,
        task_id=task_id,
        fields=list(updates.keys()),
    )

    await _record_recent_job(pool, job.job_id)
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
        job_info.args = getattr(info, "args", None)
        job_info.kwargs = getattr(info, "kwargs", None)

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

    job_ids = await _list_recent_job_ids(pool)

    if not job_ids:
        return []

    semaphore = asyncio.Semaphore(25)

    async def load_job(job_id: str) -> JobInfo | None:
        async with semaphore:
            try:
                return await get_job_status(job_id)
            except Exception:
                return None

    jobs = [
        info
        for info in await asyncio.gather(*(load_job(job_id) for job_id in job_ids))
        if info is not None and (function is None or info.function == function)
    ]
    jobs.sort(
        key=lambda info: info.enqueue_time.timestamp() if info.enqueue_time is not None else 0,
        reverse=True,
    )

    return jobs[:limit]


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


async def enqueue_backup(
    organization_id: str,
    *,
    include_postgres: bool = True,
    include_graph: bool = True,
    backup_id: str | None = None,
) -> str:
    """Enqueue a backup job.

    Creates a complete backup archive containing PostgreSQL dump
    and graph data export.

    Args:
        organization_id: Organization UUID to backup
        include_postgres: Include PostgreSQL dump
        include_graph: Include graph export
        backup_id: Pre-generated backup ID (optional, for API-triggered backups)

    Returns:
        Job ID for tracking
    """
    pool = await get_pool()

    resolved_backup_id = backup_id or generate_backup_id(organization_id)
    job_id = f"backup:{resolved_backup_id}"

    job = await pool.enqueue_job(
        "run_backup",
        organization_id,
        include_postgres=include_postgres,
        include_graph=include_graph,
        backup_id=resolved_backup_id,
        _job_id=job_id,
    )

    if job is None:
        # Shouldn't happen with unique backup IDs, but handle gracefully
        log.info("Backup job already exists", job_id=job_id)
        await _record_recent_job(pool, job_id)
        return job_id

    log.info(
        "Enqueued backup job",
        job_id=job.job_id,
        organization_id=organization_id,
        include_postgres=include_postgres,
        include_graph=include_graph,
        backup_id=backup_id,
    )

    await _record_recent_job(pool, job.job_id)
    return job.job_id


async def enqueue_backup_cleanup(
    *,
    retention_days: int | None = None,
) -> str:
    """Enqueue a backup cleanup job.

    Removes backup archives older than the retention period.

    Args:
        retention_days: Override retention period (default: from settings)

    Returns:
        Job ID for tracking
    """
    pool = await get_pool()

    job_id = "backup_cleanup"

    # Clear old result to allow re-run
    result_key = f"arq:result:{job_id}"
    await pool.delete(result_key)

    kwargs = {}
    if retention_days is not None:
        kwargs["retention_days"] = retention_days

    job = await pool.enqueue_job(
        "cleanup_old_backups",
        **kwargs,
        _job_id=job_id,
    )

    if job is None:
        log.info("Backup cleanup job already running", job_id=job_id)
        await _record_recent_job(pool, job_id)
        return job_id

    log.info("Enqueued backup cleanup job", job_id=job.job_id, retention_days=retention_days)

    await _record_recent_job(pool, job.job_id)
    return job.job_id


async def enqueue_consolidation(
    group_id: str,
    *,
    similarity_threshold: float = 0.90,
    max_merges_per_run: int = 50,
) -> str:
    """Enqueue an org-scoped consolidation job."""
    pool = await get_pool()

    job_id = f"consolidate:{group_id}"
    result_key = f"arq:result:{job_id}"
    await pool.delete(result_key)

    job = await pool.enqueue_job(
        "consolidate_org",
        group_id,
        similarity_threshold=similarity_threshold,
        max_merges_per_run=max_merges_per_run,
        _job_id=job_id,
    )

    if job is None:
        log.info("Consolidation job already running", job_id=job_id, group_id=group_id)
        await _record_recent_job(pool, job_id)
        return job_id

    log.info(
        "Enqueued consolidation job",
        job_id=job.job_id,
        group_id=group_id,
        similarity_threshold=similarity_threshold,
        max_merges_per_run=max_merges_per_run,
    )

    await _record_recent_job(pool, job.job_id)
    return job.job_id


async def enqueue_priority_decay(
    group_id: str,
    *,
    min_age_days: int = 180,
    max_archives_per_run: int = 100,
) -> str:
    """Enqueue an org-scoped forgetting sweep."""
    pool = await get_pool()

    job_id = f"priority_decay:{group_id}"
    result_key = f"arq:result:{job_id}"
    await pool.delete(result_key)

    job = await pool.enqueue_job(
        "priority_decay",
        group_id,
        min_age_days=min_age_days,
        max_archives_per_run=max_archives_per_run,
        _job_id=job_id,
    )

    if job is None:
        log.info("Priority decay job already running", job_id=job_id, group_id=group_id)
        await _record_recent_job(pool, job_id)
        return job_id

    log.info(
        "Enqueued priority decay job",
        job_id=job.job_id,
        group_id=group_id,
        min_age_days=min_age_days,
        max_archives_per_run=max_archives_per_run,
    )

    await _record_recent_job(pool, job.job_id)
    return job.job_id
