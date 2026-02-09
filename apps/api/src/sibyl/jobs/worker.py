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
from sibyl.jobs.entities import create_entity, create_learning_episode, update_entity, update_task

log = structlog.get_logger()


def get_redis_settings() -> RedisSettings:
    """Get Redis connection settings."""
    return RedisSettings(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        password=settings.falkordb_password,
        database=settings.redis_jobs_db,
    )


async def _cleanup_stale_working_agents() -> int:
    """Mark stale 'working' agents as failed.

    Called at worker startup to clean up agents that were working when
    the worker crashed or was killed. An agent is considered stale if:
    - Status is 'working' or 'running'
    - Last heartbeat is older than 5 minutes
    - No corresponding job exists in Redis

    Returns:
        Number of agents marked as failed
    """
    from datetime import timedelta

    from sqlmodel import col, select

    from sibyl.db import AgentState, get_session
    from sibyl.jobs.queue import get_pool

    try:
        pool = await get_pool()
        # Use naive datetime to match DB column (TIMESTAMP WITHOUT TIME ZONE)
        stale_threshold = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
        marked_failed = 0

        async with get_session() as session:
            # Find working agents with stale heartbeats
            result = await session.execute(
                select(AgentState).where(
                    col(AgentState.status).in_(["working", "running", "initializing"]),
                    (col(AgentState.last_heartbeat) < stale_threshold)
                    | (col(AgentState.last_heartbeat).is_(None)),
                )
            )
            stale_agents = result.scalars().all()

            for agent in stale_agents:
                # Check if there's still a job running in Redis
                job_key = f"arq:job:agent:{agent.graph_agent_id}"
                job_exists = await pool.exists(job_key)

                if not job_exists:
                    # No job running - mark as failed
                    agent.status = "failed"
                    agent.error_message = "worker_crashed"
                    agent.completed_at = datetime.now(UTC).replace(tzinfo=None)
                    marked_failed += 1
                    log.info(
                        "Marked stale agent as failed",
                        agent_id=agent.graph_agent_id,
                        last_heartbeat=agent.last_heartbeat,
                    )

            if marked_failed:
                await session.commit()

        return marked_failed

    except Exception as e:
        log.warning("Stale agent cleanup failed", error=str(e))
        return 0


async def _cleanup_orphaned_agent_jobs() -> int:
    """Clean up agent jobs for agents in terminal states.

    Called at worker startup to prevent stale jobs from auto-running.
    Checks each agent job against the database and removes jobs for
    agents that have been terminated/completed/failed.

    Returns:
        Number of jobs cleaned up
    """
    from sibyl.jobs.queue import get_pool

    try:
        pool = await get_pool()

        # Find all agent jobs in Redis
        job_keys = await pool.keys("arq:job:agent:*")
        if not job_keys:
            return 0

        cleaned = 0
        for key in job_keys:
            key_str = key.decode() if isinstance(key, bytes) else key
            # Extract agent_id from "arq:job:agent:agent_xyz123"
            agent_id = key_str.replace("arq:job:agent:", "")

            # Check agent status from database
            try:
                from sqlmodel import col, select

                from sibyl.db import AgentState, get_session

                async with get_session() as session:
                    result = await session.execute(
                        select(AgentState).where(col(AgentState.graph_agent_id) == agent_id)
                    )
                    state = result.scalar_one_or_none()

                    if state and state.status in ("terminated", "completed", "failed"):
                        # Agent is in terminal state - delete the job
                        result_key = f"arq:result:agent:{agent_id}"
                        await pool.delete(key_str, result_key)
                        log.info(
                            "Cleaned up orphaned agent job",
                            agent_id=agent_id,
                            status=state.status,
                        )
                        cleaned += 1

            except Exception as e:
                log.warning(
                    "Failed to check agent status during cleanup",
                    agent_id=agent_id,
                    error=str(e),
                )

        return cleaned

    except Exception as e:
        log.warning("Orphaned agent job cleanup failed", error=str(e))
        return 0


async def startup(ctx: dict[str, Any]) -> None:
    """Worker startup - initialize resources."""
    from sibyl.banner import log_banner
    from sibyl_core.logging import configure_logging

    # Reconfigure logging for worker (overrides API default)
    configure_logging(service_name="worker")

    log_banner(component="worker")
    log.info("Job worker online")
    ctx["start_time"] = datetime.now(UTC)

    # Clean up stale working agents (from worker crashes)
    stale_marked = await _cleanup_stale_working_agents()
    if stale_marked:
        log.info("Marked stale agents as failed", count=stale_marked)

    # Clean up orphaned agent jobs from previous runs
    cleaned = await _cleanup_orphaned_agent_jobs()
    if cleaned:
        log.info("Cleaned up orphaned agent jobs", count=cleaned)


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
        update_task,
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
