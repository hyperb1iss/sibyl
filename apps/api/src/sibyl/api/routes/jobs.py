"""Job queue API endpoints.

Provides REST API for:
- Listing jobs
- Checking job status
- Cancelling jobs
"""

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from sibyl.auth.dependencies import get_current_organization, require_org_admin
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import CrawlSource, Organization

log = structlog.get_logger()
router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[
        Depends(require_org_admin()),
    ],
)


async def _job_visible_to_org(
    job: Any,
    *,
    org: Organization,
    session: AsyncSession,
) -> bool:
    """Return True if job's target belongs to this org.

    Prevents leaking job metadata across organizations. Jobs are best-effort
    classified by function name + args.
    """
    fn = getattr(job, "function", "") or ""
    args: list[Any] = list(getattr(job, "args", None) or ())

    # Graph jobs include group_id explicitly.
    if fn == "create_entity" and len(args) >= 3:
        return str(args[2]) == str(org.id)
    if fn == "update_entity" and len(args) >= 4:
        return str(args[3]) == str(org.id)

    # Source jobs are keyed by source_id; resolve org ownership from DB.
    if fn in {"crawl_source", "sync_source"} and len(args) >= 1:
        try:
            source_uuid = UUID(str(args[0]))
        except ValueError:
            return False
        result = await session.execute(
            select(CrawlSource).where(
                col(CrawlSource.id) == source_uuid,
                col(CrawlSource.organization_id) == org.id,
            )
        )
        return result.scalar_one_or_none() is not None

    # Unknown job type: hide by default.
    return False


# IMPORTANT: Health endpoint must come before /{job_id} to avoid route matching issues
@router.get("/health")
async def jobs_health() -> dict[str, Any]:
    """Check job queue health."""
    from sibyl.jobs.queue import get_pool

    try:
        pool = await get_pool()
        info = await pool.info()
        return {
            "status": "healthy",
            "redis_version": info.get("redis_version", "unknown"),
            "connected_clients": info.get("connected_clients", 0),
            "used_memory_human": info.get("used_memory_human", "unknown"),
        }
    except Exception as e:
        log.warning("Job queue health check failed", error=str(e))
        return {
            "status": "unhealthy",
            "error": "Health check failed",
        }


@router.get("")
async def list_jobs(
    function: str | None = None,
    limit: int = 50,
    org: Organization = Depends(get_current_organization),
    session: AsyncSession = Depends(get_session_dependency),
) -> dict[str, Any]:
    """List recent jobs."""
    from sibyl.jobs.queue import list_jobs as _list_jobs

    try:
        jobs = await _list_jobs(function=function, limit=limit)
        visible = [j for j in jobs if await _job_visible_to_org(j, org=org, session=session)]
        return {
            "jobs": [
                {
                    "job_id": j.job_id,
                    "function": j.function,
                    "status": j.status.value,
                    "enqueue_time": j.enqueue_time.isoformat() if j.enqueue_time else None,
                    "start_time": j.start_time.isoformat() if j.start_time else None,
                    "finish_time": j.finish_time.isoformat() if j.finish_time else None,
                    "error": j.error,
                }
                for j in visible
            ],
            "total": len(visible),
        }
    except Exception as e:
        log.warning("Failed to list jobs", error=str(e))
        return {"jobs": [], "total": 0, "error": "Failed to list jobs"}


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    org: Organization = Depends(get_current_organization),
    session: AsyncSession = Depends(get_session_dependency),
) -> dict[str, Any]:
    """Get status of a specific job."""
    from sibyl.jobs import JobStatus, get_job_status

    try:
        info = await get_job_status(job_id)

        if info.status == JobStatus.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        if not await _job_visible_to_org(info, org=org, session=session):
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

        return {
            "job_id": info.job_id,
            "function": info.function,
            "status": info.status.value,
            "enqueue_time": info.enqueue_time.isoformat() if info.enqueue_time else None,
            "start_time": info.start_time.isoformat() if info.start_time else None,
            "finish_time": info.finish_time.isoformat() if info.finish_time else None,
            "result": info.result,
            "error": info.error,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.warning("Failed to get job status", job_id=job_id, error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Failed to get job status. Is Redis available?",
        ) from e


@router.delete("/{job_id}")
async def cancel_job(
    job_id: str,
    org: Organization = Depends(get_current_organization),
    session: AsyncSession = Depends(get_session_dependency),
) -> dict[str, Any]:
    """Cancel a queued job."""
    from sibyl.jobs.queue import cancel_job as _cancel_job, get_job_status

    try:
        info = await get_job_status(job_id)
        if not await _job_visible_to_org(info, org=org, session=session):
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

        cancelled = await _cancel_job(job_id)
        if cancelled:
            return {"job_id": job_id, "cancelled": True}
        return {
            "job_id": job_id,
            "cancelled": False,
            "message": "Job not found or already running",
        }
    except Exception as e:
        log.warning("Failed to cancel job", job_id=job_id, error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Failed to cancel job",
        ) from e
