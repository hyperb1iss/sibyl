"""Job queue API endpoints.

Provides REST API for:
- Listing jobs
- Checking job status
- Cancelling jobs
"""

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException

log = structlog.get_logger()
router = APIRouter(prefix="/jobs", tags=["jobs"])


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
        return {
            "status": "unhealthy",
            "error": str(e),
        }


@router.get("")
async def list_jobs(
    function: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List recent jobs."""
    from sibyl.jobs import get_job_status
    from sibyl.jobs.queue import list_jobs as _list_jobs

    try:
        jobs = await _list_jobs(function=function, limit=limit)
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
                for j in jobs
            ],
            "total": len(jobs),
        }
    except Exception as e:
        log.warning("Failed to list jobs", error=str(e))
        return {"jobs": [], "total": 0, "error": str(e)}


@router.get("/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    """Get status of a specific job."""
    from sibyl.jobs import JobStatus, get_job_status

    try:
        info = await get_job_status(job_id)

        if info.status == JobStatus.NOT_FOUND:
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
        raise HTTPException(
            status_code=503,
            detail=f"Failed to get job status. Is Redis available? Error: {e}",
        ) from e


@router.delete("/{job_id}")
async def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel a queued job."""
    from sibyl.jobs.queue import cancel_job as _cancel_job

    try:
        cancelled = await _cancel_job(job_id)
        if cancelled:
            return {"job_id": job_id, "cancelled": True}
        return {
            "job_id": job_id,
            "cancelled": False,
            "message": "Job not found or already running",
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to cancel job: {e}",
        ) from e
