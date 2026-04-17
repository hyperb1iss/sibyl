"""Job queue API endpoints.

Provides REST API for:
- Listing jobs
- Checking job status
- Cancelling jobs
"""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException

from sibyl.auth.dependencies import get_current_organization, require_org_admin
from sibyl.db.models import Organization
from sibyl.persistence.legacy.jobs import (
    _job_visible_to_org,
    _resolve_visible_legacy_source_ids,
)

log = structlog.get_logger()
router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[
        Depends(require_org_admin()),
    ],
)

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
) -> dict[str, Any]:
    """List recent jobs."""
    from sibyl.jobs.queue import list_jobs as _list_jobs

    try:
        jobs = await _list_jobs(function=function, limit=limit)
        legacy_source_ids = await _resolve_visible_legacy_source_ids(jobs, org=org)
        visible = [
            j
            for j in jobs
            if await _job_visible_to_org(
                j,
                org=org,
                legacy_source_ids=legacy_source_ids,
            )
        ]
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


@router.post("/consolidation")
async def trigger_consolidation(
    org: Organization = Depends(get_current_organization),
) -> dict[str, Any]:
    """Trigger an org-scoped consolidation run."""
    from sibyl.jobs.queue import enqueue_consolidation

    try:
        job_id = await enqueue_consolidation(str(org.id))
    except Exception as e:
        log.warning("Failed to enqueue consolidation job", org_id=str(org.id), error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Failed to enqueue consolidation job",
        ) from e

    return {
        "job_id": job_id,
        "function": "consolidate_org",
        "status": "queued",
        "message": "Consolidation run queued",
    }


@router.post("/forgetting")
async def trigger_priority_decay(
    org: Organization = Depends(get_current_organization),
) -> dict[str, Any]:
    """Trigger an org-scoped forgetting sweep."""
    from sibyl.jobs.queue import enqueue_priority_decay

    try:
        job_id = await enqueue_priority_decay(str(org.id))
    except Exception as e:
        log.warning("Failed to enqueue priority decay job", org_id=str(org.id), error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Failed to enqueue priority decay job",
        ) from e

    return {
        "job_id": job_id,
        "function": "priority_decay",
        "status": "queued",
        "message": "Forgetting sweep queued",
    }


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    org: Organization = Depends(get_current_organization),
) -> dict[str, Any]:
    """Get status of a specific job."""
    from sibyl.jobs import JobStatus, get_job_status

    try:
        info = await get_job_status(job_id)

        if info.status == JobStatus.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        if not await _job_visible_to_org(info, org=org):
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
) -> dict[str, Any]:
    """Cancel a queued job."""
    from sibyl.jobs.queue import cancel_job as _cancel_job, get_job_status

    try:
        info = await get_job_status(job_id)
        if not await _job_visible_to_org(info, org=org):
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

        cancelled = await _cancel_job(job_id)
        if cancelled:
            return {"job_id": job_id, "cancelled": True}
        return {
            "job_id": job_id,
            "cancelled": False,
            "message": "Job not found or already running",
        }
    except HTTPException:
        raise
    except Exception as e:
        log.warning("Failed to cancel job", job_id=job_id, error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Failed to cancel job",
        ) from e
