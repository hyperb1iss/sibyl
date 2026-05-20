"""Compatibility shim for coordination broker backends."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from arq.connections import RedisSettings

from sibyl.coordination.broker import (
    RECENT_JOB_INDEX_KEY,
    RECENT_JOB_INDEX_LIMIT,
    JobInfo,
    JobStatus,
    QueueBroker,
    get_broker,
)

__all__ = [
    "JobInfo",
    "JobStatus",
    "RECENT_JOB_INDEX_KEY",
    "RECENT_JOB_INDEX_LIMIT",
    "cancel_job",
    "close_pool",
    "enqueue_backup",
    "enqueue_backup_cleanup",
    "enqueue_consolidation",
    "enqueue_crawl",
    "enqueue_create_entity",
    "enqueue_create_learning_episode",
    "enqueue_create_learning_procedure",
    "enqueue_memory_projection",
    "enqueue_priority_decay",
    "enqueue_reflection_dream_cycle",
    "enqueue_sync",
    "enqueue_update_entity",
    "enqueue_update_task",
    "get_job_status",
    "get_pool",
    "get_queue",
    "get_redis_settings",
    "list_jobs",
]


def get_queue() -> QueueBroker:
    """Return the active queue broker."""
    return get_broker()


def get_redis_settings() -> RedisSettings:
    """Get Redis connection settings for the active broker."""
    return get_queue().get_redis_settings()


async def get_pool() -> Any:
    """Return the active broker's low-level pool when available."""
    return await get_queue().get_pool()


async def close_pool() -> None:
    """Close the active broker's low-level pool when available."""
    await get_queue().close_pool()


async def enqueue_crawl(
    source_id: str | UUID,
    *,
    organization_id: str | None = None,
    max_pages: int = 100,
    max_depth: int = 3,
    generate_embeddings: bool = True,
    force: bool = False,
) -> str:
    """Enqueue a crawl job for a source."""
    return await get_queue().enqueue_crawl(
        source_id,
        organization_id=organization_id,
        max_pages=max_pages,
        max_depth=max_depth,
        generate_embeddings=generate_embeddings,
        force=force,
    )


async def enqueue_sync(source_id: str | UUID, *, organization_id: str | None = None) -> str:
    """Enqueue a source sync job."""
    return await get_queue().enqueue_sync(source_id, organization_id=organization_id)


async def enqueue_create_entity(
    entity_id: str,
    entity_data: dict[str, Any],
    entity_type: str,
    group_id: str,
    relationships: list[dict[str, Any]] | None = None,
    auto_link_params: dict[str, Any] | None = None,
) -> str:
    """Enqueue an entity creation job."""
    return await get_queue().enqueue_create_entity(
        entity_id,
        entity_data,
        entity_type,
        group_id,
        relationships=relationships,
        auto_link_params=auto_link_params,
    )


async def enqueue_update_entity(
    entity_id: str,
    updates: dict[str, Any],
    entity_type: str,
    group_id: str,
) -> str:
    """Enqueue an entity update job."""
    return await get_queue().enqueue_update_entity(entity_id, updates, entity_type, group_id)


async def enqueue_memory_projection(
    sources_data: list[dict[str, Any]],
    group_id: str,
    *,
    created_source_ids: list[str] | None = None,
) -> str:
    """Enqueue graph projection for prose-bearing memory sources."""
    return await get_queue().enqueue_memory_projection(
        sources_data,
        group_id,
        created_source_ids=created_source_ids,
    )


async def enqueue_create_learning_episode(
    task_data: dict[str, Any],
    group_id: str,
    *,
    policy_context: dict[str, Any] | None = None,
) -> str:
    """Enqueue a learning episode creation job."""
    return await get_queue().enqueue_create_learning_episode(
        task_data,
        group_id,
        policy_context=policy_context,
    )


async def enqueue_create_learning_procedure(
    task_data: dict[str, Any],
    group_id: str,
    *,
    policy_context: dict[str, Any] | None = None,
) -> str:
    """Enqueue a learning procedure creation job."""
    return await get_queue().enqueue_create_learning_procedure(
        task_data,
        group_id,
        policy_context=policy_context,
    )


async def enqueue_update_task(
    task_id: str,
    updates: dict[str, Any],
    group_id: str,
    epic_id: str | None = None,
    new_status: str | None = None,
    add_depends_on: list[str] | None = None,
    remove_depends_on: list[str] | None = None,
) -> str:
    """Enqueue a task update job."""
    return await get_queue().enqueue_update_task(
        task_id,
        updates,
        group_id,
        epic_id=epic_id,
        new_status=new_status,
        add_depends_on=add_depends_on,
        remove_depends_on=remove_depends_on,
    )


async def get_job_status(job_id: str) -> JobInfo:
    """Get the status of a job."""
    return await get_queue().get_job_status(job_id)


async def list_jobs(*, function: str | None = None, limit: int = 50) -> list[JobInfo]:
    """List recent jobs."""
    return await get_queue().list_jobs(function=function, limit=limit)


async def cancel_job(job_id: str) -> bool:
    """Cancel a queued job."""
    return await get_queue().cancel_job(job_id)


async def enqueue_backup(
    organization_id: str,
    *,
    include_database_dump: bool = True,
    include_graph: bool = True,
    backup_id: str | None = None,
) -> str:
    """Enqueue a backup job."""
    return await get_queue().enqueue_backup(
        organization_id,
        include_database_dump=include_database_dump,
        include_graph=include_graph,
        backup_id=backup_id,
    )


async def enqueue_backup_cleanup(*, retention_days: int | None = None) -> str:
    """Enqueue a backup cleanup job."""
    return await get_queue().enqueue_backup_cleanup(retention_days=retention_days)


async def enqueue_consolidation(
    group_id: str,
    *,
    similarity_threshold: float = 0.90,
    max_merges_per_run: int = 50,
) -> str:
    """Enqueue an org-scoped consolidation job."""
    return await get_queue().enqueue_consolidation(
        group_id,
        similarity_threshold=similarity_threshold,
        max_merges_per_run=max_merges_per_run,
    )


async def enqueue_priority_decay(
    group_id: str,
    *,
    min_age_days: int = 180,
    max_archives_per_run: int = 100,
) -> str:
    """Enqueue an org-scoped forgetting sweep."""
    return await get_queue().enqueue_priority_decay(
        group_id,
        min_age_days=min_age_days,
        max_archives_per_run=max_archives_per_run,
    )


async def enqueue_reflection_dream_cycle(
    group_id: str,
    *,
    dry_run: bool = False,
    source_limit: int = 20,
    candidate_limit: int = 50,
    archive_exceptions: bool = True,
    confidence_threshold: float | None = None,
) -> str:
    """Enqueue an org-scoped reflection dream-cycle run."""
    return await get_queue().enqueue_reflection_dream_cycle(
        group_id,
        dry_run=dry_run,
        source_limit=source_limit,
        candidate_limit=candidate_limit,
        archive_exceptions=archive_exceptions,
        confidence_threshold=confidence_threshold,
    )
