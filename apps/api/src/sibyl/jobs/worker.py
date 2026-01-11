"""arq worker - processes background jobs.

Run with: uv run arq sibyl.jobs.WorkerSettings

This is the worker entrypoint. Job implementations are in:
- crawl.py: crawl_source, sync_source, sync_all_sources
- entities.py: create_entity, create_learning_episode, update_entity
- agents.py: run_agent_execution, resume_agent_execution, generate_status_hint
- backup.py: run_backup, cleanup_old_backups
"""

from datetime import UTC, datetime
from typing import Any

import structlog
from arq.connections import RedisSettings
from arq.cron import cron

from sibyl.config import settings

# Import job functions from their modules
from sibyl.jobs.agents import (
    generate_status_hint,
    resume_agent_execution,
    run_agent_execution,
)
from sibyl.jobs.backup import cleanup_old_backups, run_backup, run_scheduled_backups
from sibyl.jobs.crawl import crawl_source, sync_all_sources, sync_source
from sibyl.jobs.entities import create_entity, create_learning_episode, update_entity

log = structlog.get_logger()


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
    from sibyl.banner import log_banner
    from sibyl_core.logging import configure_logging

    # Reconfigure logging for worker (overrides API default)
    configure_logging(service_name="worker")

    log_banner(component="worker")
    log.info("Job worker online")
    ctx["start_time"] = datetime.now(UTC)


async def shutdown(ctx: dict[str, Any]) -> None:  # noqa: ARG001
    """Worker shutdown - cleanup resources."""
    log.info("Job worker shutting down")


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
        create_learning_episode,
        update_entity,
        # Agent jobs
        run_agent_execution,
        resume_agent_execution,
        generate_status_hint,
        # Backup jobs
        run_backup,
        cleanup_old_backups,
        run_scheduled_backups,
    ]

    # Cron jobs for scheduled tasks
    @staticmethod
    def get_cron_jobs() -> list:
        """Build cron job list based on settings."""
        cron_jobs = []

        if settings.backup_enabled:
            try:
                schedule_kwargs = _parse_cron_schedule(settings.backup_schedule)

                # Scheduled backups - queries all orgs with enabled backup settings
                cron_jobs.append(
                    cron(
                        run_scheduled_backups,
                        **schedule_kwargs,
                        unique=True,
                    )
                )
                log.info(
                    "cron_job_registered",
                    job="run_scheduled_backups",
                    schedule=settings.backup_schedule,
                )

                # Cleanup old backups - runs 1 hour after backups (offset by 1 hour)
                cleanup_hour = schedule_kwargs.get("hour")
                if cleanup_hour is not None and isinstance(cleanup_hour, int):
                    cleanup_schedule = {**schedule_kwargs, "hour": (cleanup_hour + 1) % 24}
                else:
                    cleanup_schedule = schedule_kwargs

                cron_jobs.append(
                    cron(
                        cleanup_old_backups,
                        **cleanup_schedule,
                        unique=True,
                    )
                )
                log.info(
                    "cron_job_registered",
                    job="cleanup_old_backups",
                    schedule="1 hour after backup schedule",
                )
            except Exception as e:
                log.warning(
                    "cron_schedule_parse_failed", schedule=settings.backup_schedule, error=str(e)
                )

        return cron_jobs

    cron_jobs = get_cron_jobs.__func__()  # type: ignore[attr-defined]

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
            on_startup=WorkerSettings.on_startup,  # pyright: ignore[reportAttributeAccessIssue]
            on_shutdown=WorkerSettings.on_shutdown,  # pyright: ignore[reportAttributeAccessIssue]
            max_jobs=WorkerSettings.max_jobs,
            job_timeout=WorkerSettings.job_timeout,
            keep_result=WorkerSettings.keep_result,
            poll_delay=WorkerSettings.poll_delay,
        )

        await worker.async_run()
    except Exception:
        log.exception("Job worker crashed")
        raise
