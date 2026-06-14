"""arq worker - processes background jobs.

Run with: uv run arq sibyl.jobs.WorkerSettings

This is the worker entrypoint. Job implementations are in:
- crawl.py: crawl_source, sync_source, sync_all_sources
- entities.py: create_entity, create_learning_episode, create_learning_procedure, update_entity
- memory_extraction.py: extract_memory_entities
- backup.py: run_backup, cleanup_old_backups
- source_imports.py: import_source_archive, drain_source_import
- raw_promotion.py: promote_raw_captures
- raw_changefeed.py: poll_raw_capture_changefeed, poll_all_raw_capture_changefeeds
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from arq.connections import RedisSettings
from arq.cron import cron
from arq.jobs import Job

from sibyl.config import settings

# Import job functions from their modules
from sibyl.jobs.backup import cleanup_old_backups, run_backup, run_scheduled_backups
from sibyl.jobs.consolidation import consolidate_all_orgs, consolidate_org, priority_decay
from sibyl.jobs.crawl import crawl_source, sync_all_sources, sync_source
from sibyl.jobs.entities import (
    backfill_entity_embeddings,
    create_entity,
    create_learning_episode,
    create_learning_procedure,
    project_memory_batch,
    update_entity,
    update_task,
)
from sibyl.jobs.memory_extraction import extract_memory_entities
from sibyl.jobs.privacy import purge_due_deleted_personal_memories
from sibyl.jobs.raw_changefeed import (
    poll_all_raw_capture_changefeeds,
    poll_raw_capture_changefeed,
)
from sibyl.jobs.raw_promotion import promote_raw_captures
from sibyl.jobs.reflection import run_reflection_dream_cycle, run_reflection_dream_cycle_all_orgs
from sibyl.jobs.source_imports import drain_source_import, import_source_archive
from sibyl_core.observability import elapsed_ms, telemetry_registry

log = structlog.get_logger()


@dataclass(frozen=True)
class ScheduleSpec:
    """Shared schedule intent for Redis cron and local scheduling."""

    name: str
    function: Any
    schedule_label: str
    minute: int | set[int] | None = None
    hour: int | set[int] | None = None
    day: int | set[int] | None = None
    month: int | set[int] | None = None
    weekday: int | set[int] | None = None


def get_redis_settings() -> RedisSettings:
    """Get Redis connection settings."""
    redis_host = settings.redis_host or "127.0.0.1"
    redis_port = settings.redis_port or 6381
    return RedisSettings(
        host=redis_host,
        port=redis_port,
        password=settings.redis_password_value or None,
        database=settings.redis_jobs_db,
    )


def build_cron_jobs() -> list:
    """Build cron job list based on settings."""
    return [
        cron(
            spec.function,
            minute=spec.minute,
            hour=spec.hour,
            day=spec.day,
            month=spec.month,
            weekday=spec.weekday,
            unique=True,
        )
        for spec in get_schedule_specs()
    ]


async def startup(ctx: dict[str, Any]) -> None:
    """Worker startup - initialize resources."""
    from sibyl.banner import log_banner
    from sibyl_core.logging import configure_logging

    # Reconfigure logging for worker (overrides API default)
    configure_logging(service_name="worker")

    log_banner(component="worker")
    log.info("Job worker online")
    ctx["start_time"] = datetime.now(UTC)

    # Load API keys from database into environment before graph jobs run.
    from sibyl.services.settings import load_api_keys_from_db

    await load_api_keys_from_db()

    from sibyl.ai.llm.service import install_db_config_source
    from sibyl.core_runtime_ports import install_core_runtime_ports

    install_db_config_source()
    install_core_runtime_ports()


async def shutdown(ctx: dict[str, Any]) -> None:  # noqa: ARG001
    """Worker shutdown - cleanup resources."""
    log.info("Job worker shutting down")


async def job_start(ctx: dict[str, Any]) -> None:
    ctx["telemetry_started_at"] = time.perf_counter()


async def job_end(ctx: dict[str, Any]) -> None:
    started_at = ctx.get("telemetry_started_at")
    if not isinstance(started_at, float):
        return
    result = await _job_result_info(ctx)
    telemetry_registry().record_job_finished(
        function=_job_function_name(ctx, result),
        status="ok" if getattr(result, "success", True) else "error",
        duration_ms=elapsed_ms(started_at),
    )


async def _job_result_info(ctx: dict[str, Any]) -> Any | None:
    job_id = ctx.get("job_id")
    redis = ctx.get("redis")
    if not isinstance(job_id, str) or redis is None:
        return None
    try:
        return await Job(job_id, redis).result_info()
    except Exception:
        return None


def _job_function_name(ctx: dict[str, Any], result: Any | None) -> str:
    function = getattr(result, "function", None)
    if isinstance(function, str) and function:
        return function
    job_id = ctx.get("job_id")
    if isinstance(job_id, str) and job_id:
        return job_id.split(":", 1)[0] or "unknown"
    return "unknown"


def _parse_cron_schedule(schedule: str) -> dict[str, int | set[int] | None]:
    """Parse a cron schedule string into arq cron kwargs.

    Args:
        schedule: Cron expression (e.g., "0 2 * * *")

    Returns:
        Dict suitable for arq cron() kwargs
    """
    parts = schedule.split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron schedule: {schedule}")

    minute, hour, day, month, weekday = parts

    def parse_field(field: str) -> int | set[int] | None:
        if field == "*":
            return None
        if "," in field:
            return {int(x) for x in field.split(",")}
        return int(field)

    return {
        "minute": parse_field(minute),
        "hour": parse_field(hour),
        "day": parse_field(day),
        "month": parse_field(month),
        "weekday": parse_field(weekday),
    }


def get_schedule_specs() -> list[ScheduleSpec]:
    """Build the schedule definitions shared by Redis and local runtimes."""
    schedule_specs: list[ScheduleSpec] = []

    if settings.backup_enabled:
        try:
            schedule_kwargs = _parse_cron_schedule(settings.backup_schedule)
            schedule_specs.append(
                ScheduleSpec(
                    name="run_scheduled_backups",
                    function=run_scheduled_backups,
                    schedule_label=settings.backup_schedule,
                    **schedule_kwargs,
                )
            )

            cleanup_hour = schedule_kwargs.get("hour")
            if cleanup_hour is not None and isinstance(cleanup_hour, int):
                cleanup_schedule = {**schedule_kwargs, "hour": (cleanup_hour + 1) % 24}
            else:
                cleanup_schedule = schedule_kwargs

            schedule_specs.append(
                ScheduleSpec(
                    name="cleanup_old_backups",
                    function=cleanup_old_backups,
                    schedule_label="1 hour after backup schedule",
                    **cleanup_schedule,
                )
            )
        except Exception as e:
            log.warning(
                "cron_schedule_parse_failed", schedule=settings.backup_schedule, error=str(e)
            )

    schedule_specs.append(
        ScheduleSpec(
            name="consolidate_all_orgs",
            function=consolidate_all_orgs,
            schedule_label="0 3 * * *",
            hour=3,
            minute=0,
        )
    )
    schedule_specs.append(
        ScheduleSpec(
            name="run_reflection_dream_cycle_all_orgs",
            function=run_reflection_dream_cycle_all_orgs,
            schedule_label="30 3 * * *",
            hour=3,
            minute=30,
        )
    )
    schedule_specs.append(
        ScheduleSpec(
            name="purge_due_deleted_personal_memories",
            function=purge_due_deleted_personal_memories,
            schedule_label="0 4 * * *",
            hour=4,
            minute=0,
        )
    )
    if settings.raw_capture_changefeed_poll_enabled:
        schedule_specs.append(
            ScheduleSpec(
                name="poll_all_raw_capture_changefeeds",
                function=poll_all_raw_capture_changefeeds,
                schedule_label="* * * * *",
            )
        )

    return schedule_specs


def log_schedule_specs(schedule_specs: list[ScheduleSpec]) -> None:
    """Log the schedule definitions registered by the active scheduler runtime."""
    for spec in schedule_specs:
        log.info("cron_job_registered", job=spec.name, schedule=spec.schedule_label)


class WorkerSettings:
    """arq worker settings."""

    redis_settings = get_redis_settings()

    # Job functions (imported from separate modules)
    functions = [
        # Crawl jobs
        crawl_source,
        sync_source,
        sync_all_sources,
        # Entity jobs
        create_entity,
        backfill_entity_embeddings,
        project_memory_batch,
        extract_memory_entities,
        create_learning_episode,
        create_learning_procedure,
        update_entity,
        update_task,
        # Backup jobs
        run_backup,
        cleanup_old_backups,
        run_scheduled_backups,
        # Source import jobs
        import_source_archive,
        drain_source_import,
        promote_raw_captures,
        poll_raw_capture_changefeed,
        poll_all_raw_capture_changefeeds,
        # Consolidation jobs
        consolidate_org,
        consolidate_all_orgs,
        priority_decay,
        purge_due_deleted_personal_memories,
        run_reflection_dream_cycle,
        run_reflection_dream_cycle_all_orgs,
    ]

    # Cron jobs for scheduled tasks
    get_cron_jobs = staticmethod(build_cron_jobs)
    cron_jobs = build_cron_jobs()

    # Lifecycle hooks
    on_startup = startup
    on_shutdown = shutdown
    on_job_start = job_start
    after_job_end = job_end

    # Worker settings
    max_jobs = settings.resolved_worker_max_jobs
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

    worker_settings = WorkerSettings.redis_settings
    log.info(
        "Starting in-process job worker",
        redis_host=worker_settings.host,
        redis_port=worker_settings.port,
        redis_db=worker_settings.database,
        max_jobs=WorkerSettings.max_jobs,
        cron_jobs=len(WorkerSettings.cron_jobs),
    )

    try:
        worker = Worker(
            functions=WorkerSettings.functions,
            cron_jobs=WorkerSettings.cron_jobs,
            redis_settings=worker_settings,
            on_startup=WorkerSettings.on_startup,
            on_shutdown=WorkerSettings.on_shutdown,
            on_job_start=WorkerSettings.on_job_start,
            after_job_end=WorkerSettings.after_job_end,
            max_jobs=WorkerSettings.max_jobs,
            job_timeout=WorkerSettings.job_timeout,
            keep_result=WorkerSettings.keep_result,
            poll_delay=WorkerSettings.poll_delay,
        )

        await worker.async_run()
    except Exception:
        log.exception("Job worker crashed")
        raise
