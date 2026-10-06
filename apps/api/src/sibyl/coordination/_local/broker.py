"""Local in-process queue broker."""

from __future__ import annotations

import asyncio
import contextlib
import heapq
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any, NoReturn
from uuid import UUID

import structlog
from arq.connections import RedisSettings

from sibyl.backup_ids import generate_backup_id
from sibyl.config import settings
from sibyl.coordination.broker import (
    RECENT_JOB_INDEX_LIMIT,
    JobInfo,
    JobStatus,
    entity_embedding_job_id,
    memory_extraction_job_id,
    memory_projection_job_id,
    operational_note_distillation_job_id,
    raw_capture_changefeed_job_id,
    raw_promotion_job_id,
)
from sibyl.jobs.worker import WorkerSettings
from sibyl_core.observability import telemetry_registry

log = structlog.get_logger()

JobCallable = Callable[..., Awaitable[Any]]
QueueEntry = tuple[int, int, str]
LOCAL_BROKER_ERROR = "Local job broker is not running"
_DEFAULT_QUEUE_PRIORITY = 0
_DERIVED_QUEUE_PRIORITY = 1
_FINISHED_STATUSES = frozenset({JobStatus.COMPLETE, JobStatus.FAILED, JobStatus.CANCELLED})
# Payload fields that hold vectors. A finished record keeps its arguments
# for the job listing, but a vector there is dead weight: a day of entity
# writes with 1,024-float embeddings would hold hundreds of megabytes.
_VECTOR_KEYS = frozenset(
    {"embedding", "embeddings", "name_embedding", "fact_embedding", "vector", "vectors"}
)
_VECTOR_SUFFIXES = ("_embedding", "_embeddings", "_vector", "_vectors")
_VECTOR_MIN_LENGTH = 32


def _is_vector_key(key: object) -> bool:
    name = str(key).lower()
    return name in _VECTOR_KEYS or name.endswith(_VECTOR_SUFFIXES)


def _slim_payload(value: Any) -> Any:
    """A copy of a job payload with its vectors dropped, for a finished record.

    Named vector fields go, and so does any long list of numbers whatever
    its name; everything else, ids included, is kept as it was.
    """
    if isinstance(value, dict):
        return {key: _slim_payload(item) for key, item in value.items() if not _is_vector_key(key)}
    if isinstance(value, list | tuple):
        if len(value) >= _VECTOR_MIN_LENGTH and all(
            isinstance(item, int | float) and not isinstance(item, bool) for item in value
        ):
            return f"<{len(value)} numbers dropped>"
        slimmed = [_slim_payload(item) for item in value]
        return tuple(slimmed) if isinstance(value, tuple) else slimmed
    return value


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
        shutdown_grace_seconds: float | None = None,
    ) -> None:
        resolved_functions = functions or {
            function.__name__: function for function in WorkerSettings.functions
        }
        self._functions = resolved_functions
        self._max_concurrency = max_concurrency or WorkerSettings.max_jobs
        self._result_ttl = timedelta(seconds=result_ttl_seconds or WorkerSettings.keep_result)
        self._recent_job_limit = recent_job_limit
        self._shutdown_grace_seconds = (
            settings.local_queue_shutdown_grace_seconds
            if shutdown_grace_seconds is None
            else shutdown_grace_seconds
        )
        self._queue: asyncio.PriorityQueue[QueueEntry] | None = None
        self._queue_sequence = count()
        self._workers: list[asyncio.Task[None]] = []
        self._jobs: dict[str, LocalJobRecord] = {}
        self._recent_job_ids: deque[str] = deque(maxlen=recent_job_limit)
        # Finished records by expiry, so a purge pops only what has expired
        # instead of reading every record on every enqueue and status call.
        self._expiring: list[tuple[datetime, int, str]] = []
        self._expiry_sequence = count()
        # Finished records in finish order; the oldest beyond the recent
        # index limit are retired, so a day's jobs never pile up in memory.
        self._finished: deque[tuple[str, datetime]] = deque()
        self._ctx: dict[str, Any] = {}
        self._lifecycle_lock = asyncio.Lock()

    async def startup(self) -> None:
        """Start local worker tasks."""
        async with self._lifecycle_lock:
            if self._queue is not None:
                return

            self._queue = asyncio.PriorityQueue()
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
        """Stop accepting jobs, drain queued work, then stop local worker tasks."""
        async with self._lifecycle_lock:
            queue = self._queue
            workers = self._workers
            self._workers = []
            self._queue = None

        drained = True
        if queue is not None:
            try:
                await asyncio.wait_for(queue.join(), timeout=self._shutdown_grace_seconds)
            except TimeoutError:
                drained = False
                log.warning(
                    "Local queue shutdown grace expired",
                    grace_seconds=self._shutdown_grace_seconds,
                    queue_depth=queue.qsize(),
                    running_jobs=sum(
                        1
                        for record in self._jobs.values()
                        if record.status == JobStatus.IN_PROGRESS
                    ),
                )

        workers_cancelled = False
        if not drained:
            for worker in workers:
                worker.cancel()
            workers_cancelled = True

            running_tasks = [
                record.running_task
                for record in self._jobs.values()
                if record.running_task is not None and not record.running_task.done()
            ]
            for task in running_tasks:
                task.cancel()
            if running_tasks:
                await asyncio.gather(*running_tasks, return_exceptions=True)
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            self._mark_queued_jobs_cancelled(reason="shutdown")

        if not workers_cancelled:
            for worker in workers:
                worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)

        self._ctx = {}

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
        generate_embeddings: bool = True,
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
            generate_embeddings=generate_embeddings,
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
            queue_priority=_DERIVED_QUEUE_PRIORITY,
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
            queue_priority=_DERIVED_QUEUE_PRIORITY,
            created_source_ids=created_source_ids,
            max_entities_per_source=max_entities_per_source,
            max_source_chars=max_source_chars,
            max_concurrent=max_concurrent,
            max_tokens=max_tokens,
        )
        return result.job_id

    async def enqueue_operational_note_distillation(
        self,
        experience_data: dict[str, Any],
        group_id: str,
        *,
        content_hash: str,
        created_by: str | None,
        max_tokens: int = 2_048,
        operational_source: dict[str, Any] | None = None,
    ) -> str:
        job_id = operational_note_distillation_job_id(
            experience_data,
            group_id,
            content_hash=content_hash,
            **(
                {"operational_source": operational_source} if operational_source is not None else {}
            ),
        )
        result = await self._enqueue_unique(
            "distill_operational_experience_notes",
            experience_data,
            group_id,
            job_id=job_id,
            queue_priority=_DERIVED_QUEUE_PRIORITY,
            content_hash=content_hash,
            **(
                {"operational_source": operational_source} if operational_source is not None else {}
            ),
            created_by=created_by,
            max_tokens=max_tokens,
        )
        return result.job_id

    async def enqueue_entity_embedding_backfill(
        self,
        entities_data: list[dict[str, Any]],
        group_id: str,
        *,
        relationships: list[dict[str, Any]] | None = None,
        completion_manifest: dict[str, Any] | None = None,
        operational_source: dict[str, Any] | None = None,
    ) -> str:
        job_id = entity_embedding_job_id(
            entities_data,
            group_id,
            relationships=relationships,
            completion_manifest=completion_manifest,
            **(
                {"operational_source": operational_source} if operational_source is not None else {}
            ),
        )
        job_kwargs: dict[str, Any] = {"relationships": relationships}
        if operational_source is not None:
            job_kwargs["operational_source"] = operational_source
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
        expected_revision: int | None = None,
    ) -> str:
        import time

        epoch_ms = int(time.time() * 1000)
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
            job_id=f"update_task:{task_id}:{epoch_ms}",
            **update_kwargs,
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
            self._finish(record, status=JobStatus.CANCELLED, result=None, error="cancelled")
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

    async def enqueue_probe_replay(
        self,
        group_id: str,
        *,
        window_hours: int = 168,
        max_memories: int = 200,
    ) -> str:
        result = await self._enqueue_unique(
            "replay_memory_probes",
            group_id,
            job_id=f"replay_memory_probes:{group_id}",
            clear_result=True,
            window_hours=window_hours,
            max_memories=max_memories,
        )
        return result.job_id

    async def enqueue_reflection_dream_cycle(
        self,
        group_id: str,
        *,
        dry_run: bool = False,
        source_limit: int = 20,
        candidate_limit: int = 50,
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
            confidence_threshold=confidence_threshold,
        )
        return result.job_id

    async def _enqueue_unique(
        self,
        function: str,
        *args: Any,
        job_id: str,
        clear_result: bool = False,
        queue_priority: int = _DEFAULT_QUEUE_PRIORITY,
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
        await queue.put((queue_priority, next(self._queue_sequence), job_id))
        telemetry_registry().record_job_enqueued(function=function, created=True)
        return EnqueueResult(job_id=job_id, created=True)

    async def _worker_loop(self, worker_index: int) -> None:
        queue = self._require_queue()
        worker_task = asyncio.current_task()

        while True:
            _, _, job_id = await queue.get()
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
                if worker_task is not None and worker_task.cancelling():
                    raise asyncio.CancelledError
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
            self._finish(record, status=JobStatus.CANCELLED, result=None, error="cancelled")
            telemetry_registry().record_job_finished(
                function=record.function,
                status="cancelled",
                duration_ms=(time.perf_counter() - started_at) * 1000,
            )
            log.info("Local job cancelled", job_id=record.job_id, function=record.function)
            return
        except Exception as e:
            self._finish(record, status=JobStatus.FAILED, result=None, error=str(e))
            await self._clear_failed_create_pending(record)
            telemetry_registry().record_job_finished(
                function=record.function,
                status="error",
                duration_ms=(time.perf_counter() - started_at) * 1000,
            )
            log.exception("Local job failed", job_id=record.job_id, function=record.function)
            return

        self._finish(record, status=JobStatus.COMPLETE, result=result, error=None)
        telemetry_registry().record_job_finished(
            function=record.function,
            status="ok",
            duration_ms=(time.perf_counter() - started_at) * 1000,
        )
        log.info("Local job complete", job_id=record.job_id, function=record.function)

    async def _clear_failed_create_pending(self, record: LocalJobRecord) -> None:
        if record.function != "create_entity" or not record.args:
            return

        entity_data = record.args[0]
        if not isinstance(entity_data, dict) or not entity_data.get("id"):
            return

        from sibyl.jobs.pending import clear_pending

        entity_id = str(entity_data["id"])
        try:
            await clear_pending(entity_id)
        except Exception as exc:
            log.warning(
                "Failed to clear pending marker after local create failure",
                entity_id=entity_id,
                job_id=record.job_id,
                error=str(exc),
            )

    def _finish(
        self, record: LocalJobRecord, *, status: JobStatus, result: Any, error: str | None
    ) -> None:
        """Close a record: outcome, expiry, and a payload that no longer carries vectors."""
        record.status = status
        record.finish_time = datetime.now(UTC)
        record.result = _slim_payload(result)
        record.error = error
        record.expires_at = record.finish_time + self._result_ttl
        record.args = _slim_payload(record.args)
        record.kwargs = _slim_payload(record.kwargs)
        heapq.heappush(
            self._expiring, (record.expires_at, next(self._expiry_sequence), record.job_id)
        )
        self._finished.append((record.job_id, record.finish_time))
        self._retire_finished_beyond_limit()

    def _retire_finished_beyond_limit(self) -> None:
        """Drop the oldest finished records once more than the recent index can list."""
        while len(self._finished) > self._recent_job_limit:
            job_id, finish_time = self._finished.popleft()
            record = self._jobs.get(job_id)
            # The same id re-enqueued since is a newer record with its own
            # place in the order; only the record that finished then goes.
            if (
                record is not None
                and record.status in _FINISHED_STATUSES
                and record.finish_time == finish_time
            ):
                self._jobs.pop(job_id, None)

    def _purge_expired_jobs(self) -> None:
        now = datetime.now(UTC)
        expired: set[str] = set()
        while self._expiring and self._expiring[0][0] <= now:
            expires_at, _sequence, job_id = heapq.heappop(self._expiring)
            record = self._jobs.get(job_id)
            # A record re-enqueued under the same id carries a newer expiry
            # and its own heap entry; this stale entry says nothing about it.
            if record is not None and record.expires_at == expires_at:
                self._jobs.pop(job_id, None)
                expired.add(job_id)
        if not expired:
            return

        self._recent_job_ids = deque(
            (job_id for job_id in self._recent_job_ids if job_id not in expired),
            maxlen=self._recent_job_limit,
        )

    def _mark_queued_jobs_cancelled(self, *, reason: str) -> None:
        for record in list(self._jobs.values()):
            if record.status != JobStatus.QUEUED:
                continue

            self._finish(record, status=JobStatus.CANCELLED, result=None, error=reason)
            self._record_recent_job(record.job_id)
            telemetry_registry().record_job_finished(
                function=record.function,
                status="cancelled",
                duration_ms=0,
            )

    def _record_recent_job(self, job_id: str) -> None:
        with contextlib.suppress(ValueError):
            self._recent_job_ids.remove(job_id)
        self._recent_job_ids.appendleft(job_id)

    def _require_queue(self) -> asyncio.PriorityQueue[QueueEntry]:
        if self._queue is None:
            self._raise_unsupported()
        return self._queue

    def _raise_unsupported(self) -> NoReturn:
        raise RuntimeError(LOCAL_BROKER_ERROR)
