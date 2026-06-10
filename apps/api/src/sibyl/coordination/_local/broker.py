"""Local in-process queue broker."""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn
from uuid import UUID

import structlog
from arq.connections import RedisSettings

from sibyl.backup_ids import generate_backup_id
from sibyl.coordination.broker import (
    RECENT_JOB_INDEX_LIMIT,
    JobInfo,
    JobStatus,
    memory_extraction_job_id,
    memory_projection_job_id,
    raw_capture_changefeed_job_id,
    raw_promotion_job_id,
)
from sibyl.jobs.worker import WorkerSettings
from sibyl_core.observability import telemetry_registry

log = structlog.get_logger()

JobCallable = Callable[..., Awaitable[Any]]
LOCAL_BROKER_ERROR = "Local job broker is not running"


@dataclass
class EnqueueResult:
    job_id: str
    created: bool


@dataclass
class LocalJobRecord:
    job_id: str
    function: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    status: JobStatus = JobStatus.QUEUED
    enqueue_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    start_time: datetime | None = None
    finish_time: datetime | None = None
    result: Any = None
    error: str | None = None
    expires_at: datetime | None = None
    running_task: asyncio.Task[Any] | None = None

    def to_job_info(self) -> JobInfo:
        return JobInfo(
            job_id=self.job_id,
            function=self.function,
            status=self.status,
            enqueue_time=self.enqueue_time,
            start_time=self.start_time,
            finish_time=self.finish_time,
            result=self.result,
            error=self.error,
            args=self.args,
            kwargs=self.kwargs,
        )


class LocalQueueBroker:
    """Execute background jobs in-process with asyncio primitives."""

    def __init__(
        self,
        *,
        functions: dict[str, JobCallable] | None = None,
        max_concurrency: int | None = None,
        result_ttl_seconds: int | None = None,
        recent_job_limit: int = RECENT_JOB_INDEX_LIMIT,
    ) -> None:
        resolved_functions = functions or {
            function.__name__: function for function in WorkerSettings.functions
        }
        self._functions = resolved_functions
        self._max_concurrency = max_concurrency or WorkerSettings.max_jobs
        self._result_ttl = timedelta(seconds=result_ttl_seconds or WorkerSettings.keep_result)
        self._recent_job_limit = recent_job_limit
        self._queue: asyncio.Queue[str] | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._jobs: dict[str, LocalJobRecord] = {}
        self._recent_job_ids: deque[str] = deque(maxlen=recent_job_limit)
        self._ctx: dict[str, Any] = {}
        self._lifecycle_lock = asyncio.Lock()

    async def startup(self) -> None:
        """Start local worker tasks."""
        async with self._lifecycle_lock:
            if self._queue is not None:
                return

            self._queue = asyncio.Queue()
            self._ctx = {"start_time": datetime.now(UTC)}
            self._workers = [
                asyncio.create_task(
                    self._worker_loop(index),
                    name=f"sibyl-local-worker-{index}",
                )
                for index in range(self._max_concurrency)
            ]
            log.info("Local queue broker ready", workers=self._max_concurrency)

    async def shutdown(self) -> None:
        """Stop local worker tasks and clear ephemeral state."""
        async with self._lifecycle_lock:
            workers = self._workers
            running_tasks = [
                record.running_task
                for record in self._jobs.values()
                if record.running_task is not None and not record.running_task.done()
            ]
            self._workers = []
            self._queue = None
            self._ctx = {}
            self._jobs.clear()
            self._recent_job_ids.clear()

        for task in running_tasks:
            task.cancel()
        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)

        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def health(self) -> dict[str, Any]:
        """Report local broker health."""
        self._purge_expired_jobs()
        queue = self._queue
        worker_healthy = bool(self._workers) and all(not worker.done() for worker in self._workers)

        if queue is None:
            return {
                "status": "degraded",
                "error": LOCAL_BROKER_ERROR,
                "queue_healthy": False,
                "worker_healthy": False,
                "queue_depth": 0,
            }

        return {
            "status": "healthy" if worker_healthy else "degraded",
            "queue_healthy": True,
            "worker_healthy": worker_healthy,
            "queue_depth": queue.qsize(),
            "running_jobs": sum(
                1 for record in self._jobs.values() if record.status == JobStatus.IN_PROGRESS
            ),
        }

    def get_redis_settings(self) -> RedisSettings:
        """Redis settings are unavailable for local mode."""
        self._raise_unsupported()

    async def get_pool(self) -> Any:
        """Redis pools are unavailable for local mode."""
        self._raise_unsupported()

    async def close_pool(self) -> None:
        """No pool exists in local mode."""

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
        job_kwargs: dict[str, Any] = {
            "max_pages": max_pages,
            "max_depth": max_depth,
            "generate_embeddings": generate_embeddings,
        }
        if organization_id is not None:
            job_kwargs["organization_id"] = organization_id

        result = await self._enqueue_unique(
            "crawl_source",
            str(source_id),
            job_id=f"crawl:{source_id}",
            clear_result=force,
            **job_kwargs,
        )
        return result.job_id

    async def enqueue_sync(
        self,
        source_id: str | UUID,
        *,
        organization_id: str | None = None,
    ) -> str:
        job_kwargs: dict[str, Any] = {}
        if organization_id is not None:
            job_kwargs["organization_id"] = organization_id

        result = await self._enqueue_unique(
            "sync_source",
            str(source_id),
            job_id=f"sync:{source_id}",
            **job_kwargs,
        )
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

        if result.created:
            await mark_pending(entity_id, job_id, entity_type, group_id)
        return result.job_id

    async def enqueue_update_entity(
        self,
        entity_id: str,
        updates: dict[str, Any],
        entity_type: str,
        group_id: str,
    ) -> str:
        result = await self._enqueue_unique(
            "update_entity",
            entity_id,
            updates,
            entity_type,
            group_id,
            job_id=f"update_entity:{entity_id}",
        )
        return result.job_id

    async def enqueue_memory_projection(
        self,
        sources_data: list[dict[str, Any]],
        group_id: str,
        *,
        created_source_ids: list[str] | None = None,
    ) -> str:
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
        return result.job_id

    async def enqueue_create_learning_episode(
        self,
        task_data: dict[str, Any],
        group_id: str,
        *,
        policy_context: dict[str, Any] | None = None,
    ) -> str:
        task_id = task_data.get("id", "unknown")
        if policy_context is None:
            result = await self._enqueue_unique(
                "create_learning_episode",
                task_data,
                group_id,
                job_id=f"learning_episode:{task_id}",
            )
        else:
            result = await self._enqueue_unique(
                "create_learning_episode",
                task_data,
                group_id,
                job_id=f"learning_episode:{task_id}",
                policy_context=policy_context,
            )
        return result.job_id

    async def enqueue_create_learning_procedure(
        self,
        task_data: dict[str, Any],
        group_id: str,
        *,
        policy_context: dict[str, Any] | None = None,
    ) -> str:
        task_id = task_data.get("id", "unknown")
        if policy_context is None:
            result = await self._enqueue_unique(
                "create_learning_procedure",
                task_data,
                group_id,
                job_id=f"learning_procedure:{task_id}",
            )
        else:
            result = await self._enqueue_unique(
                "create_learning_procedure",
                task_data,
                group_id,
                job_id=f"learning_procedure:{task_id}",
                policy_context=policy_context,
            )
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
        import time

        epoch_ms = int(time.time() * 1000)
        result = await self._enqueue_unique(
            "update_task",
            task_id,
            updates,
            group_id,
            job_id=f"update_task:{task_id}:{epoch_ms}",
            epic_id=epic_id,
            new_status=new_status,
            add_depends_on=add_depends_on or [],
            remove_depends_on=remove_depends_on or [],
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
        result = await self._enqueue_unique(
            "drain_source_import",
            import_id,
            job_id=f"source_import_drain:{import_id}",
            clear_result=True,
            organization_id=organization_id,
            principal_id=principal_id,
            policy_context=policy_context,
            batch_size=batch_size,
            promotion_preview_approved=promotion_preview_approved,
        )
        return result.job_id

    async def enqueue_raw_promotion(
        self,
        organization_id: str,
        *,
        raw_memory_ids: list[str] | None = None,
        limit: int = 100,
        force: bool = False,
    ) -> str:
        result = await self._enqueue_unique(
            "promote_raw_captures",
            organization_id,
            job_id=raw_promotion_job_id(
                organization_id,
                raw_memory_ids=raw_memory_ids,
            ),
            clear_result=True,
            raw_memory_ids=raw_memory_ids,
            limit=limit,
            force=force,
        )
        return result.job_id

    async def enqueue_raw_capture_changefeed_poll(
        self,
        organization_id: str,
        *,
        limit: int = 100,
    ) -> str:
        result = await self._enqueue_unique(
            "poll_raw_capture_changefeed",
            organization_id,
            job_id=raw_capture_changefeed_job_id(organization_id),
            clear_result=True,
            limit=limit,
        )
        return result.job_id

    async def enqueue_scheduled_job(self, function: str) -> str:
        result = await self._enqueue_unique(
            function,
            job_id=f"scheduled:{function}",
            clear_result=True,
        )
        return result.job_id

    async def get_job_status(self, job_id: str) -> JobInfo:
        self._purge_expired_jobs()
        record = self._jobs.get(job_id)
        if record is None:
            return JobInfo(job_id=job_id, function="unknown", status=JobStatus.NOT_FOUND)
        return record.to_job_info()

    async def list_jobs(self, *, function: str | None = None, limit: int = 50) -> list[JobInfo]:
        self._purge_expired_jobs()

        jobs = [
            self._jobs[job_id].to_job_info()
            for job_id in self._recent_job_ids
            if job_id in self._jobs
        ]
        if function is not None:
            jobs = [job for job in jobs if job.function == function]
        jobs.sort(
            key=lambda info: info.enqueue_time.timestamp() if info.enqueue_time is not None else 0,
            reverse=True,
        )
        return jobs[:limit]

    async def cancel_job(self, job_id: str) -> bool:
        self._purge_expired_jobs()
        record = self._jobs.get(job_id)
        if record is None:
            return False

        if record.status == JobStatus.QUEUED:
            record.status = JobStatus.CANCELLED
            record.finish_time = datetime.now(UTC)
            record.result = None
            record.error = "cancelled"
            record.expires_at = record.finish_time + self._result_ttl
            self._record_recent_job(job_id)
            return True

        if record.status == JobStatus.IN_PROGRESS and record.running_task is not None:
            record.running_task.cancel()

        return False

    async def enqueue_backup(
        self,
        organization_id: str,
        *,
        include_database_dump: bool = True,
        include_graph: bool = True,
        backup_id: str | None = None,
    ) -> str:
        resolved_backup_id = backup_id or generate_backup_id(organization_id)
        result = await self._enqueue_unique(
            "run_backup",
            organization_id,
            job_id=f"backup:{resolved_backup_id}",
            include_database_dump=include_database_dump,
            include_graph=include_graph,
            backup_id=resolved_backup_id,
        )
        return result.job_id

    async def enqueue_backup_cleanup(
        self,
        *,
        retention_days: int | None = None,
    ) -> str:
        job_kwargs: dict[str, Any] = {}
        if retention_days is not None:
            job_kwargs["retention_days"] = retention_days

        result = await self._enqueue_unique(
            "cleanup_old_backups",
            job_id="backup_cleanup",
            clear_result=True,
            **job_kwargs,
        )
        return result.job_id

    async def enqueue_consolidation(
        self,
        group_id: str,
        *,
        similarity_threshold: float = 0.90,
        max_merges_per_run: int = 50,
    ) -> str:
        result = await self._enqueue_unique(
            "consolidate_org",
            group_id,
            job_id=f"consolidate:{group_id}",
            clear_result=True,
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
        result = await self._enqueue_unique(
            "priority_decay",
            group_id,
            job_id=f"priority_decay:{group_id}",
            clear_result=True,
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
        result = await self._enqueue_unique(
            "run_reflection_dream_cycle",
            group_id,
            job_id=f"reflection_dream:{group_id}",
            clear_result=True,
            dry_run=dry_run,
            source_limit=source_limit,
            candidate_limit=candidate_limit,
            archive_exceptions=archive_exceptions,
            confidence_threshold=confidence_threshold,
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
        self._purge_expired_jobs()
        queue = self._require_queue()
        record = self._jobs.get(job_id)

        if record is not None:
            if clear_result and record.status == JobStatus.COMPLETE:
                self._jobs.pop(job_id, None)
                record = None
            elif record.status in {JobStatus.QUEUED, JobStatus.IN_PROGRESS, JobStatus.COMPLETE}:
                self._record_recent_job(job_id)
                telemetry_registry().record_job_enqueued(function=function, created=False)
                return EnqueueResult(job_id=job_id, created=False)
            else:
                self._jobs.pop(job_id, None)
                record = None

        if function not in self._functions:
            raise RuntimeError(f"Unknown local job function: {function}")

        self._jobs[job_id] = LocalJobRecord(
            job_id=job_id,
            function=function,
            args=args,
            kwargs=kwargs,
        )
        self._record_recent_job(job_id)
        await queue.put(job_id)
        telemetry_registry().record_job_enqueued(function=function, created=True)
        return EnqueueResult(job_id=job_id, created=True)

    async def _worker_loop(self, worker_index: int) -> None:
        queue = self._require_queue()

        while True:
            job_id = await queue.get()
            record = self._jobs.get(job_id)
            try:
                if record is None or record.status != JobStatus.QUEUED:
                    continue

                record.status = JobStatus.IN_PROGRESS
                record.start_time = datetime.now(UTC)
                record.running_task = asyncio.create_task(
                    self._run_job(record),
                    name=f"sibyl-local-job-{worker_index}-{job_id}",
                )
                await record.running_task
            finally:
                if record is not None:
                    record.running_task = None
                queue.task_done()

    async def _run_job(self, record: LocalJobRecord) -> None:
        import time

        function = self._functions[record.function]
        started_at = time.perf_counter()

        try:
            result = await function(self._ctx, *record.args, **record.kwargs)
        except asyncio.CancelledError:
            record.status = JobStatus.CANCELLED
            record.finish_time = datetime.now(UTC)
            record.result = None
            record.error = "cancelled"
            record.expires_at = record.finish_time + self._result_ttl
            telemetry_registry().record_job_finished(
                function=record.function,
                status="cancelled",
                duration_ms=(time.perf_counter() - started_at) * 1000,
            )
            log.info("Local job cancelled", job_id=record.job_id, function=record.function)
            return
        except Exception as e:
            record.status = JobStatus.COMPLETE
            record.finish_time = datetime.now(UTC)
            record.error = str(e)
            record.result = None
            record.expires_at = record.finish_time + self._result_ttl
            telemetry_registry().record_job_finished(
                function=record.function,
                status="error",
                duration_ms=(time.perf_counter() - started_at) * 1000,
            )
            log.exception("Local job failed", job_id=record.job_id, function=record.function)
            return

        record.status = JobStatus.COMPLETE
        record.finish_time = datetime.now(UTC)
        record.result = result
        record.error = None
        record.expires_at = record.finish_time + self._result_ttl
        telemetry_registry().record_job_finished(
            function=record.function,
            status="ok",
            duration_ms=(time.perf_counter() - started_at) * 1000,
        )
        log.info("Local job complete", job_id=record.job_id, function=record.function)

    def _purge_expired_jobs(self) -> None:
        expired_job_ids = [
            job_id
            for job_id, record in self._jobs.items()
            if record.expires_at is not None and datetime.now(UTC) >= record.expires_at
        ]
        if not expired_job_ids:
            return

        expired = set(expired_job_ids)
        for job_id in expired:
            self._jobs.pop(job_id, None)

        self._recent_job_ids = deque(
            (job_id for job_id in self._recent_job_ids if job_id not in expired),
            maxlen=self._recent_job_limit,
        )

    def _record_recent_job(self, job_id: str) -> None:
        with contextlib.suppress(ValueError):
            self._recent_job_ids.remove(job_id)
        self._recent_job_ids.appendleft(job_id)

    def _require_queue(self) -> asyncio.Queue[str]:
        if self._queue is None:
            self._raise_unsupported()
        return self._queue

    def _raise_unsupported(self) -> NoReturn:
        raise RuntimeError(LOCAL_BROKER_ERROR)
