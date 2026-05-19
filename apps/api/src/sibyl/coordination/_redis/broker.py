"""Redis-backed queue broker."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from arq.jobs import Job, JobStatus as ArqJobStatus

from sibyl.backup_ids import generate_backup_id
from sibyl.config import settings
from sibyl.coordination.broker import (
    RECENT_JOB_INDEX_KEY,
    RECENT_JOB_INDEX_LIMIT,
    JobInfo,
    JobStatus,
)
from sibyl_core.observability import telemetry_registry

log = structlog.get_logger()


@dataclass
class EnqueueResult:
    job_id: str
    created: bool


class RedisQueueBroker:
    """Preserve the current arq-backed queue semantics behind a broker."""

    def __init__(self) -> None:
        self._pool: ArqRedis | None = None

    async def startup(self) -> None:
        """Pre-warm the Redis connection pool."""
        await self.get_pool()

    async def shutdown(self) -> None:
        """Shutdown the Redis connection pool."""
        await self.close_pool()

    async def health(self) -> dict[str, Any]:
        """Report queue health for admin and jobs surfaces."""
        try:
            pool = await self.get_pool()
            redis_info = await pool.info()
            pool_info = await pool.pool.info()
        except Exception:
            return {
                "status": "unhealthy",
                "error": "Health check failed",
                "queue_healthy": False,
                "worker_healthy": False,
                "queue_depth": 0,
            }

        return {
            "status": "healthy",
            "queue_healthy": bool(redis_info),
            "worker_healthy": bool(pool_info.get("workers", 0)),
            "queue_depth": pool_info.get("pending_jobs", 0) if pool_info else 0,
            "redis_version": redis_info.get("redis_version", "unknown"),
            "connected_clients": redis_info.get("connected_clients", 0),
            "used_memory_human": redis_info.get("used_memory_human", "unknown"),
        }

    def get_redis_settings(self) -> RedisSettings:
        """Get Redis connection settings for arq."""
        redis_host = settings.redis_host or "127.0.0.1"
        redis_port = settings.redis_port or 6381
        return RedisSettings(
            host=redis_host,
            port=redis_port,
            password=settings.redis_password_value or None,
            database=settings.redis_jobs_db,
        )

    async def get_pool(self) -> ArqRedis:
        """Get or create the Redis connection pool."""
        if self._pool is None:
            self._pool = await create_pool(self.get_redis_settings())
        return self._pool

    async def close_pool(self) -> None:
        """Close the Redis connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def enqueue_crawl(
        self,
        source_id: str | UUID,
        *,
        organization_id: str | None = None,
        max_pages: int = 100,
        max_depth: int = 3,
        generate_embeddings: bool = True,
        force: bool = False,
    ) -> str:
        """Enqueue a crawl job for a source."""
        job_kwargs: dict[str, Any] = {
            "max_pages": max_pages,
            "max_depth": max_depth,
            "generate_embeddings": generate_embeddings,
        }
        if organization_id is not None:
            job_kwargs["organization_id"] = organization_id

        job_id = f"crawl:{source_id}"
        result = await self._enqueue_unique(
            "crawl_source",
            str(source_id),
            job_id=job_id,
            clear_result=force,
            **job_kwargs,
        )

        if not result.created:
            log.info("Crawl job already exists", job_id=job_id, source_id=str(source_id))
            return result.job_id

        log.info(
            "Enqueued crawl job",
            job_id=result.job_id,
            source_id=str(source_id),
            max_pages=max_pages,
        )
        return result.job_id

    async def enqueue_sync(
        self,
        source_id: str | UUID,
        *,
        organization_id: str | None = None,
    ) -> str:
        """Enqueue a source sync job."""
        job_kwargs: dict[str, Any] = {}
        if organization_id is not None:
            job_kwargs["organization_id"] = organization_id

        job_id = f"sync:{source_id}"
        result = await self._enqueue_unique(
            "sync_source",
            str(source_id),
            job_id=job_id,
            **job_kwargs,
        )

        if not result.created:
            log.info("Sync job already exists", job_id=job_id, source_id=str(source_id))
            return result.job_id

        log.info("Enqueued sync job", job_id=result.job_id, source_id=str(source_id))
        return result.job_id

    async def enqueue_create_entity(
        self,
        entity_id: str,
        entity_data: dict[str, Any],
        entity_type: str,
        group_id: str,
        relationships: list[dict[str, Any]] | None = None,
        auto_link_params: dict[str, Any] | None = None,
    ) -> str:
        """Enqueue an entity creation job."""
        from sibyl.jobs.pending import mark_pending

        job_id = f"create_entity:{entity_id}"
        result = await self._enqueue_unique(
            "create_entity",
            entity_data,
            entity_type,
            group_id,
            job_id=job_id,
            relationships=relationships,
            auto_link_params=auto_link_params,
        )

        if not result.created:
            log.info("Create entity job already exists", job_id=job_id, entity_id=entity_id)
            return result.job_id

        await mark_pending(entity_id, job_id, entity_type, group_id)
        log.info(
            "Enqueued create_entity job",
            job_id=result.job_id,
            entity_id=entity_id,
            entity_type=entity_type,
        )
        return result.job_id

    async def enqueue_update_entity(
        self,
        entity_id: str,
        updates: dict[str, Any],
        entity_type: str,
        group_id: str,
    ) -> str:
        """Enqueue an entity update job."""
        job_id = f"update_entity:{entity_id}"
        result = await self._enqueue_unique(
            "update_entity",
            entity_id,
            updates,
            entity_type,
            group_id,
            job_id=job_id,
        )

        if not result.created:
            log.info("Update entity job already exists", job_id=job_id, entity_id=entity_id)
            return result.job_id

        log.info(
            "Enqueued update_entity job",
            job_id=result.job_id,
            entity_id=entity_id,
            entity_type=entity_type,
            fields=list(updates.keys()),
        )
        return result.job_id

    async def enqueue_create_learning_episode(
        self,
        task_data: dict[str, Any],
        group_id: str,
        *,
        policy_context: dict[str, Any] | None = None,
    ) -> str:
        """Enqueue a learning episode creation job."""
        task_id = task_data.get("id", "unknown")
        job_id = f"learning_episode:{task_id}"
        if policy_context is None:
            result = await self._enqueue_unique(
                "create_learning_episode",
                task_data,
                group_id,
                job_id=job_id,
            )
        else:
            result = await self._enqueue_unique(
                "create_learning_episode",
                task_data,
                group_id,
                job_id=job_id,
                policy_context=policy_context,
            )

        if not result.created:
            log.info("Learning episode job already exists", job_id=job_id, task_id=task_id)
            return result.job_id

        log.info("Enqueued learning episode job", job_id=result.job_id, task_id=task_id)
        return result.job_id

    async def enqueue_create_learning_procedure(
        self,
        task_data: dict[str, Any],
        group_id: str,
        *,
        policy_context: dict[str, Any] | None = None,
    ) -> str:
        """Enqueue a learning procedure creation job."""
        task_id = task_data.get("id", "unknown")
        job_id = f"learning_procedure:{task_id}"
        if policy_context is None:
            result = await self._enqueue_unique(
                "create_learning_procedure",
                task_data,
                group_id,
                job_id=job_id,
            )
        else:
            result = await self._enqueue_unique(
                "create_learning_procedure",
                task_data,
                group_id,
                job_id=job_id,
                policy_context=policy_context,
            )

        if not result.created:
            log.info("Learning procedure job already exists", job_id=job_id, task_id=task_id)
            return result.job_id

        log.info("Enqueued learning procedure job", job_id=result.job_id, task_id=task_id)
        return result.job_id

    async def enqueue_update_task(
        self,
        task_id: str,
        updates: dict[str, Any],
        group_id: str,
        epic_id: str | None = None,
        new_status: str | None = None,
        add_depends_on: list[str] | None = None,
        remove_depends_on: list[str] | None = None,
    ) -> str:
        """Enqueue a task update job."""
        import time

        epoch_ms = int(time.time() * 1000)
        job_id = f"update_task:{task_id}:{epoch_ms}"

        result = await self._enqueue_unique(
            "update_task",
            task_id,
            updates,
            group_id,
            job_id=job_id,
            epic_id=epic_id,
            new_status=new_status,
            add_depends_on=add_depends_on or [],
            remove_depends_on=remove_depends_on or [],
        )

        if not result.created:
            log.info("Update task job already exists", job_id=job_id, task_id=task_id)
            return result.job_id

        log.info(
            "Enqueued update_task job",
            job_id=result.job_id,
            task_id=task_id,
            fields=list(updates.keys()),
        )
        return result.job_id

    async def get_job_status(self, job_id: str) -> JobInfo:
        """Get the status of a job."""
        pool = await self.get_pool()
        job = Job(job_id, pool)

        status = await job.status()
        info = await job.info()

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
            job_info.enqueue_time = getattr(info, "enqueue_time", None)
            job_info.args = getattr(info, "args", None)
            job_info.kwargs = getattr(info, "kwargs", None)

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
        self,
        *,
        function: str | None = None,
        limit: int = 50,
    ) -> list[JobInfo]:
        """List recent jobs."""
        pool = await self.get_pool()
        job_ids = await self._list_recent_job_ids(pool)

        if limit <= 0 or not job_ids:
            return []

        job_ids = job_ids[:limit]

        semaphore = asyncio.Semaphore(25)

        async def load_job(job_id: str) -> JobInfo | None:
            async with semaphore:
                try:
                    return await self.get_job_status(job_id)
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

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a queued job."""
        pool = await self.get_pool()
        job = Job(job_id, pool)

        status = await job.status()
        if status == ArqJobStatus.queued:
            await job.abort()
            log.info("Cancelled job", job_id=job_id)
            return True

        return False

    async def enqueue_backup(
        self,
        organization_id: str,
        *,
        include_database_dump: bool = True,
        include_graph: bool = True,
        backup_id: str | None = None,
    ) -> str:
        """Enqueue a backup job."""
        resolved_backup_id = backup_id or generate_backup_id(organization_id)
        job_id = f"backup:{resolved_backup_id}"
        result = await self._enqueue_unique(
            "run_backup",
            organization_id,
            job_id=job_id,
            include_database_dump=include_database_dump,
            include_graph=include_graph,
            backup_id=resolved_backup_id,
        )

        if not result.created:
            log.info("Backup job already exists", job_id=job_id)
            return result.job_id

        log.info(
            "Enqueued backup job",
            job_id=result.job_id,
            organization_id=organization_id,
            include_database_dump=include_database_dump,
            include_graph=include_graph,
            backup_id=backup_id,
        )
        return result.job_id

    async def enqueue_backup_cleanup(
        self,
        *,
        retention_days: int | None = None,
    ) -> str:
        """Enqueue a backup cleanup job."""
        job_kwargs: dict[str, Any] = {}
        if retention_days is not None:
            job_kwargs["retention_days"] = retention_days

        job_id = "backup_cleanup"
        result = await self._enqueue_unique(
            "cleanup_old_backups",
            job_id=job_id,
            clear_result=True,
            **job_kwargs,
        )

        if not result.created:
            log.info("Backup cleanup job already running", job_id=job_id)
            return result.job_id

        log.info("Enqueued backup cleanup job", job_id=result.job_id, retention_days=retention_days)
        return result.job_id

    async def enqueue_consolidation(
        self,
        group_id: str,
        *,
        similarity_threshold: float = 0.90,
        max_merges_per_run: int = 50,
    ) -> str:
        """Enqueue an org-scoped consolidation job."""
        job_id = f"consolidate:{group_id}"
        result = await self._enqueue_unique(
            "consolidate_org",
            group_id,
            job_id=job_id,
            clear_result=True,
            similarity_threshold=similarity_threshold,
            max_merges_per_run=max_merges_per_run,
        )

        if not result.created:
            log.info("Consolidation job already running", job_id=job_id, group_id=group_id)
            return result.job_id

        log.info(
            "Enqueued consolidation job",
            job_id=result.job_id,
            group_id=group_id,
            similarity_threshold=similarity_threshold,
            max_merges_per_run=max_merges_per_run,
        )
        return result.job_id

    async def enqueue_priority_decay(
        self,
        group_id: str,
        *,
        min_age_days: int = 180,
        max_archives_per_run: int = 100,
    ) -> str:
        """Enqueue an org-scoped forgetting sweep."""
        job_id = f"priority_decay:{group_id}"
        result = await self._enqueue_unique(
            "priority_decay",
            group_id,
            job_id=job_id,
            clear_result=True,
            min_age_days=min_age_days,
            max_archives_per_run=max_archives_per_run,
        )

        if not result.created:
            log.info("Priority decay job already running", job_id=job_id, group_id=group_id)
            return result.job_id

        log.info(
            "Enqueued priority decay job",
            job_id=result.job_id,
            group_id=group_id,
            min_age_days=min_age_days,
            max_archives_per_run=max_archives_per_run,
        )
        return result.job_id

    async def enqueue_reflection_dream_cycle(
        self,
        group_id: str,
        *,
        dry_run: bool = False,
        source_limit: int = 20,
        candidate_limit: int = 50,
        archive_exceptions: bool = True,
        confidence_threshold: float | None = None,
    ) -> str:
        """Enqueue an org-scoped reflection dream-cycle run."""
        job_id = f"reflection_dream:{group_id}"
        result = await self._enqueue_unique(
            "run_reflection_dream_cycle",
            group_id,
            job_id=job_id,
            clear_result=True,
            dry_run=dry_run,
            source_limit=source_limit,
            candidate_limit=candidate_limit,
            archive_exceptions=archive_exceptions,
            confidence_threshold=confidence_threshold,
        )

        if not result.created:
            log.info("Reflection dream cycle already running", job_id=job_id, group_id=group_id)
            return result.job_id

        log.info(
            "Enqueued reflection dream cycle",
            job_id=result.job_id,
            group_id=group_id,
            dry_run=dry_run,
            source_limit=source_limit,
            candidate_limit=candidate_limit,
        )
        return result.job_id

    async def _enqueue_unique(
        self,
        function: str,
        *args: Any,
        job_id: str,
        clear_result: bool = False,
        **kwargs: Any,
    ) -> EnqueueResult:
        pool = await self.get_pool()

        if clear_result:
            await pool.delete(f"arq:result:{job_id}")
            log.debug("Cleared old result before re-enqueue", job_id=job_id)

        job = await pool.enqueue_job(function, *args, _job_id=job_id, **kwargs)
        if job is None:
            await self._record_recent_job(pool, job_id)
            telemetry_registry().record_job_enqueued(function=function, created=False)
            return EnqueueResult(job_id=job_id, created=False)

        await self._record_recent_job(pool, job.job_id)
        telemetry_registry().record_job_enqueued(function=function, created=True)
        return EnqueueResult(job_id=job.job_id, created=True)

    async def _record_recent_job(self, pool: ArqRedis, job_id: str) -> None:
        score = datetime.now(UTC).timestamp()
        await pool.zadd(RECENT_JOB_INDEX_KEY, {job_id: score})
        await pool.zremrangebyrank(RECENT_JOB_INDEX_KEY, 0, -(RECENT_JOB_INDEX_LIMIT + 1))

    async def _list_recent_job_ids(self, pool: ArqRedis) -> list[str]:
        try:
            job_ids = await pool.zrevrange(RECENT_JOB_INDEX_KEY, 0, -1)
        except Exception:
            job_ids = []

        if job_ids:
            return [self._decode_job_id(job_id) for job_id in job_ids]

        return [
            self._decode_job_id(key).removeprefix("arq:job:")
            async for key in pool.scan_iter(match="arq:job:*")
        ]

    def _decode_job_id(self, job_id: bytes | str) -> str:
        return job_id.decode() if isinstance(job_id, bytes) else job_id
