"""Redis-backed queue broker."""

from __future__ import annotations

import asyncio
import contextlib
import json
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
    entity_embedding_job_id,
    job_organization_id,
    memory_extraction_job_id,
    memory_projection_job_id,
    raw_capture_changefeed_job_id,
    raw_promotion_job_id,
)
from sibyl_core.observability import telemetry_registry

log = structlog.get_logger()

# Upper bound on get_job_status round-trips for a function-filtered
# list_jobs scan. The filter is applied after loading, so the scan
# window must exceed the requested limit to surface matches, but it
# stays capped so a request cannot fan out across the whole recent
# index. Never larger than the index itself.
LIST_JOBS_FILTER_SCAN_CAP = min(200, RECENT_JOB_INDEX_LIMIT)
JOB_METADATA_KEY_PREFIX = "sibyl:jobs:metadata:"
JOB_METADATA_TTL_SECONDS = 90_000


@dataclass
class EnqueueResult:
    job_id: str
    created: bool


def _job_metadata_key(job_id: str) -> str:
    return f"{JOB_METADATA_KEY_PREFIX}{job_id}"


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
        generate_embeddings: bool = True,
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
            generate_embeddings=generate_embeddings,
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

    async def enqueue_memory_projection(
        self,
        sources_data: list[dict[str, Any]],
        group_id: str,
        *,
        created_source_ids: list[str] | None = None,
    ) -> str:
        """Enqueue native memory projection for created source entities."""
        job_id = memory_projection_job_id(
            sources_data,
            group_id,
            created_source_ids=created_source_ids,
        )
        result = await self._enqueue_unique(
            "project_memory_batch",
            sources_data,
            group_id,
            job_id=job_id,
            created_source_ids=created_source_ids,
        )

        if not result.created:
            log.info("Memory projection job already exists", job_id=job_id)
            return result.job_id

        log.info(
            "Enqueued memory projection job",
            job_id=result.job_id,
            sources=len(sources_data),
        )
        return result.job_id

    async def enqueue_memory_extraction(
        self,
        sources_data: list[dict[str, Any]],
        group_id: str,
        *,
        created_source_ids: list[str] | None = None,
        max_entities_per_source: int = 4,
        max_source_chars: int = 12_000,
        max_concurrent: int = 2,
        max_tokens: int = 8192,
    ) -> str:
        """Enqueue LLM entity extraction for created source entities."""
        job_id = memory_extraction_job_id(
            sources_data,
            group_id,
            created_source_ids=created_source_ids,
        )
        result = await self._enqueue_unique(
            "extract_memory_entities",
            sources_data,
            group_id,
            job_id=job_id,
            created_source_ids=created_source_ids,
            max_entities_per_source=max_entities_per_source,
            max_source_chars=max_source_chars,
            max_concurrent=max_concurrent,
            max_tokens=max_tokens,
        )

        if not result.created:
            log.info("Memory extraction job already exists", job_id=job_id)
            return result.job_id

        log.info(
            "Enqueued memory extraction job",
            job_id=result.job_id,
            sources=len(sources_data),
        )
        return result.job_id

    async def enqueue_entity_embedding_backfill(
        self,
        entities_data: list[dict[str, Any]],
        group_id: str,
        *,
        relationships: list[dict[str, Any]] | None = None,
        completion_manifest: dict[str, Any] | None = None,
    ) -> str:
        """Enqueue embedding backfill for lexically-created graph records."""
        job_id = entity_embedding_job_id(
            entities_data,
            group_id,
            relationships=relationships,
            completion_manifest=completion_manifest,
        )
        job_kwargs: dict[str, Any] = {"relationships": relationships}
        if completion_manifest is not None:
            job_kwargs["completion_manifest"] = completion_manifest
        result = await self._enqueue_unique(
            "backfill_entity_embeddings",
            entities_data,
            group_id,
            job_id=job_id,
            clear_result=True,
            **job_kwargs,
        )

        if not result.created:
            log.info("Entity embedding backfill job already exists", job_id=job_id)
            return result.job_id

        log.info(
            "Enqueued entity embedding backfill",
            job_id=result.job_id,
            entities=len(entities_data),
            relationships=len(relationships or ()),
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
        expected_revision: int | None = None,
    ) -> str:
        """Enqueue a task update job."""
        import time

        epoch_ms = int(time.time() * 1000)
        job_id = f"update_task:{task_id}:{epoch_ms}"

        update_kwargs: dict[str, Any] = {
            "epic_id": epic_id,
            "new_status": new_status,
            "add_depends_on": add_depends_on or [],
            "remove_depends_on": remove_depends_on or [],
        }
        if expected_revision is not None:
            update_kwargs["expected_revision"] = expected_revision
        result = await self._enqueue_unique(
            "update_task",
            task_id,
            updates,
            group_id,
            job_id=job_id,
            **update_kwargs,
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

    async def enqueue_source_import_drain(
        self,
        import_id: str,
        *,
        organization_id: str,
        principal_id: str,
        policy_context: dict[str, Any],
        batch_size: int | None = None,
        promotion_preview_approved: bool | None = None,
    ) -> str:
        """Enqueue a resumable source import drain job."""
        job_id = f"source_import_drain:{import_id}"
        result = await self._enqueue_unique(
            "drain_source_import",
            import_id,
            job_id=job_id,
            clear_result=True,
            organization_id=organization_id,
            principal_id=principal_id,
            policy_context=policy_context,
            batch_size=batch_size,
            promotion_preview_approved=promotion_preview_approved,
        )

        if not result.created:
            log.info("Source import drain already running", job_id=job_id, import_id=import_id)
            return result.job_id

        log.info("Enqueued source import drain", job_id=result.job_id, import_id=import_id)
        return result.job_id

    async def enqueue_raw_promotion(
        self,
        organization_id: str,
        *,
        raw_memory_ids: list[str] | None = None,
        limit: int = 100,
        force: bool = False,
    ) -> str:
        """Enqueue raw capture promotion into searchable document chunks."""
        job_id = raw_promotion_job_id(
            organization_id,
            raw_memory_ids=raw_memory_ids,
        )
        result = await self._enqueue_unique(
            "promote_raw_captures",
            organization_id,
            job_id=job_id,
            clear_result=True,
            raw_memory_ids=raw_memory_ids,
            limit=limit,
            force=force,
        )

        if not result.created:
            log.info(
                "Raw promotion already running", job_id=job_id, organization_id=organization_id
            )
            return result.job_id

        log.info("Enqueued raw promotion", job_id=result.job_id, organization_id=organization_id)
        return result.job_id

    async def enqueue_raw_capture_changefeed_poll(
        self,
        organization_id: str,
        *,
        limit: int = 100,
    ) -> str:
        """Enqueue one raw capture changefeed poll for an organization."""
        job_id = raw_capture_changefeed_job_id(organization_id)
        result = await self._enqueue_unique(
            "poll_raw_capture_changefeed",
            organization_id,
            job_id=job_id,
            clear_result=True,
            limit=limit,
        )

        if not result.created:
            log.info(
                "Raw capture changefeed poll already running",
                job_id=job_id,
                organization_id=organization_id,
            )
            return result.job_id

        log.info(
            "Enqueued raw capture changefeed poll",
            job_id=result.job_id,
            organization_id=organization_id,
        )
        return result.job_id

    async def enqueue_scheduled_job(self, function: str) -> str:
        """Enqueue a scheduled worker function through the broker."""
        result = await self._enqueue_unique(
            function,
            job_id=f"scheduled:{function}",
            clear_result=True,
        )
        return result.job_id

    async def get_job_status(self, job_id: str) -> JobInfo:
        """Get the status of a job."""
        pool = await self.get_pool()
        job = Job(job_id, pool)

        status = await job.status()
        metadata = await self._load_job_metadata(pool, job_id)
        function = str(metadata.get("function") or "unknown") if metadata else "unknown"
        organization_id = (
            str(metadata["organization_id"])
            if metadata and metadata.get("organization_id") is not None
            else None
        )
        needs_info = (
            metadata is None
            or function == "unknown"
            or (function in {"crawl_source", "sync_source"} and organization_id is None)
        )
        info = await job.info() if needs_info else None

        status_map = {
            ArqJobStatus.queued: JobStatus.QUEUED,
            ArqJobStatus.in_progress: JobStatus.IN_PROGRESS,
            ArqJobStatus.complete: JobStatus.COMPLETE,
            ArqJobStatus.not_found: JobStatus.NOT_FOUND,
            ArqJobStatus.deferred: JobStatus.DEFERRED,
        }

        job_info = JobInfo(
            job_id=job_id,
            function=info.function if info else function,
            status=status_map.get(status, JobStatus.NOT_FOUND),
            organization_id=organization_id,
        )

        if metadata:
            enqueue_time = metadata.get("enqueue_time")
            if isinstance(enqueue_time, str):
                with contextlib.suppress(ValueError):
                    job_info.enqueue_time = datetime.fromisoformat(enqueue_time)
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

    async def _load_job_metadata(
        self,
        pool: ArqRedis,
        job_id: str,
    ) -> dict[str, Any] | None:
        try:
            raw = await pool.get(_job_metadata_key(job_id))
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode()
            metadata = json.loads(raw)
            return metadata if isinstance(metadata, dict) else None
        except Exception as exc:
            log.warning("Failed to load job metadata", job_id=job_id, error=str(exc))
            return None

    async def list_jobs(
        self,
        *,
        function: str | None = None,
        limit: int = 50,
    ) -> list[JobInfo]:
        """List recent jobs newest-first, bounding backend work by ``limit``.

        ``_list_recent_job_ids`` already returns ids newest-first, so an
        unfiltered listing only needs to load the first ``limit`` ids. A
        ``function`` filter is applied after loading, so the scan window is
        widened to ``LIST_JOBS_FILTER_SCAN_CAP`` to still surface matches
        beyond the newest ``limit`` jobs while keeping the number of
        ``get_job_status`` round-trips bounded regardless of how many jobs
        the recent index holds.
        """
        pool = await self.get_pool()
        job_ids = await self._list_recent_job_ids(pool)

        if limit <= 0 or not job_ids:
            return []

        scan_window = limit if function is None else max(limit, LIST_JOBS_FILTER_SCAN_CAP)
        job_ids = job_ids[:scan_window]

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

        await self._store_job_metadata(
            pool,
            job.job_id,
            function=function,
            args=args,
            kwargs=kwargs,
        )
        await self._record_recent_job(pool, job.job_id)
        telemetry_registry().record_job_enqueued(function=function, created=True)
        return EnqueueResult(job_id=job.job_id, created=True)

    async def _store_job_metadata(
        self,
        pool: ArqRedis,
        job_id: str,
        *,
        function: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        metadata = json.dumps(
            {
                "function": function,
                "organization_id": job_organization_id(function, args, kwargs),
                "enqueue_time": datetime.now(UTC).isoformat(),
            }
        )
        try:
            await pool.set(
                _job_metadata_key(job_id),
                metadata,
                ex=JOB_METADATA_TTL_SECONDS,
            )
        except Exception as exc:
            log.warning("Failed to store job metadata", job_id=job_id, error=str(exc))

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
