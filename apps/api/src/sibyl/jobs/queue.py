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


async def enqueue_crawl(
    source_id: str | UUID,
    *,
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

    job = await pool.enqueue_job(
        "crawl_source",
        str(source_id),
        max_pages=max_pages,
        max_depth=max_depth,
        generate_embeddings=generate_embeddings,
        _job_id=job_id,
    )

    if job is None:
        # Job already exists (queued/running) - return the existing job ID
        log.info("Crawl job already exists", job_id=job_id, source_id=str(source_id))
        return job_id

    log.info(
        "Enqueued crawl job",
        job_id=job.job_id,
        source_id=str(source_id),
        max_pages=max_pages,
    )

    return job.job_id


async def enqueue_sync(source_id: str | UUID) -> str:
    """Enqueue a source sync job.

    Uses a deterministic job ID based on source_id to prevent duplicate jobs.
    Recalculates document/chunk counts from actual data.

    Args:
        source_id: UUID of the source to sync

    Returns:
        Job ID for tracking
    """
    pool = await get_pool()

    # Deterministic job ID prevents duplicate jobs
    job_id = f"sync:{source_id}"

    job = await pool.enqueue_job("sync_source", str(source_id), _job_id=job_id)

    if job is None:
        # Job already exists
        log.info("Sync job already exists", job_id=job_id, source_id=str(source_id))
        return job_id

    log.info("Enqueued sync job", job_id=job.job_id, source_id=str(source_id))

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
        return job_id

    # Mark entity as pending so operations can queue against it
    await mark_pending(entity_id, job_id, entity_type, group_id)

    log.info(
        "Enqueued create_entity job",
        job_id=job.job_id,
        entity_id=entity_id,
        entity_type=entity_type,
    )

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
        return job_id

    log.info(
        "Enqueued update_entity job",
        job_id=job.job_id,
        entity_id=entity_id,
        entity_type=entity_type,
        fields=list(updates.keys()),
    )

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
        return job_id

    log.info(
        "Enqueued learning episode job",
        job_id=job.job_id,
        task_id=task_id,
    )

    return job.job_id


async def enqueue_update_task(
    task_id: str,
    updates: dict[str, Any],
    group_id: str,
) -> str:
    """Enqueue a task update job.

    Convenience wrapper around enqueue_update_entity for tasks.

    Args:
        task_id: The task entity ID to update
        updates: Dict of field names to new values
        group_id: Organization ID

    Returns:
        Job ID for tracking
    """
    return await enqueue_update_entity(task_id, updates, "task", group_id)


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

    # Get job IDs from the queue
    # Note: arq doesn't have a built-in list function, so we track manually
    # This is a simplified implementation
    job_ids = await pool.keys("arq:job:*")

    jobs = []
    for key in job_ids[:limit]:
        job_id = (
            key.decode().replace("arq:job:", "")
            if isinstance(key, bytes)
            else key.replace("arq:job:", "")
        )
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


async def enqueue_agent_execution(
    agent_id: str,
    org_id: str,
    project_id: str,
    prompt: str,
    agent_type: str = "general",
    task_id: str | None = None,
    created_by: str | None = None,
) -> str:
    """Enqueue an agent execution job.

    Runs a Claude agent in the worker process for long-running AI tasks.
    Uses deterministic job ID to prevent duplicate executions.

    Args:
        agent_id: Pre-generated agent ID
        org_id: Organization ID
        project_id: Project ID
        prompt: Initial prompt for the agent
        agent_type: Type of agent (general, planner, implementer, etc.)
        task_id: Optional task ID the agent is working on
        created_by: User ID who spawned the agent

    Returns:
        Job ID for tracking
    """
    pool = await get_pool()

    # Deterministic job ID prevents duplicate agent runs
    job_id = f"agent:{agent_id}"

    job = await pool.enqueue_job(
        "run_agent_execution",
        agent_id,
        org_id,
        project_id,
        prompt,
        agent_type=agent_type,
        task_id=task_id,
        created_by=created_by,
        _job_id=job_id,
    )

    if job is None:
        log.info("Agent execution job already exists", job_id=job_id, agent_id=agent_id)
        return job_id

    log.info(
        "Enqueued agent execution job",
        job_id=job.job_id,
        agent_id=agent_id,
        agent_type=agent_type,
    )

    return job.job_id


async def enqueue_agent_resume(
    agent_id: str,
    org_id: str,
    prompt: str = "Continue from where you left off.",
) -> str:
    """Enqueue an agent resume job.

    Clears any completed job result and enqueues a new execution.
    Uses the agent's stored session_id for Claude session resumption.

    Args:
        agent_id: Agent ID to resume
        org_id: Organization ID
        prompt: User message or continuation prompt

    Returns:
        Job ID for tracking
    """
    pool = await get_pool()

    job_id = f"agent:{agent_id}"

    # Clear old result to allow re-enqueue (like force crawl)
    result_key = f"arq:result:{job_id}"
    await pool.delete(result_key)
    log.debug("Cleared old result for agent resume", job_id=job_id)

    job = await pool.enqueue_job(
        "resume_agent_execution",
        agent_id,
        org_id,
        prompt,
        _job_id=job_id,
    )

    if job is None:
        # Job is currently running - that's fine, it will pick up the message
        log.info("Agent job already running", job_id=job_id, agent_id=agent_id)
        return job_id

    log.info(
        "Enqueued agent resume job",
        job_id=job.job_id,
        agent_id=agent_id,
    )

    return job.job_id


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
    from datetime import UTC, datetime

    pool = await get_pool()

    # Use timestamp in job ID to allow multiple backups
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    job_id = f"backup:{organization_id}:{timestamp}"

    job = await pool.enqueue_job(
        "run_backup",
        organization_id,
        include_postgres=include_postgres,
        include_graph=include_graph,
        backup_id=backup_id,
        _job_id=job_id,
    )

    if job is None:
        # Shouldn't happen with timestamped IDs, but handle gracefully
        log.info("Backup job already exists", job_id=job_id)
        return job_id

    log.info(
        "Enqueued backup job",
        job_id=job.job_id,
        organization_id=organization_id,
        include_postgres=include_postgres,
        include_graph=include_graph,
        backup_id=backup_id,
    )

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
        return job_id

    log.info("Enqueued backup cleanup job", job_id=job.job_id, retention_days=retention_days)

    return job.job_id
