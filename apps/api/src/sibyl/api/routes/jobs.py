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

from sibyl.auth.dependencies import get_current_organization, require_org_admin
from sibyl.persistence.content_runtime import (
    get_content_read_session,
    get_crawl_source_by_id,
)
from sibyl_core.auth import AuthOrganization

log = structlog.get_logger()


async def _source_visible_to_org(
    *,
    org_id: UUID,
    source_uuid: UUID,
    session: Any | None,
) -> bool:
    source = await get_crawl_source_by_id(session, source_id=source_uuid)
    return source is not None and source.organization_id == org_id


async def _job_visible_to_org(
    job: Any,
    *,
    org: AuthOrganization,
    session: Any | None = None,
    visible_source_ids: set[UUID] | None = None,
) -> bool:
    fn = getattr(job, "function", "") or ""
    args: list[Any] = list(getattr(job, "args", None) or ())
    kwargs = dict(getattr(job, "kwargs", None) or {})

    if fn == "create_entity" and len(args) >= 3:
        return str(args[2]) == str(org.id)
    if fn == "update_entity" and len(args) >= 4:
        return str(args[3]) == str(org.id)
    if fn in {"consolidate_org", "priority_decay"} and args:
        return str(args[0]) == str(org.id)

    if fn in {"crawl_source", "sync_source"} and args:
        metadata_org_id = kwargs.get("organization_id")
        if metadata_org_id is not None:
            return str(metadata_org_id) == str(org.id)

        try:
            source_uuid = UUID(str(args[0]))
        except (TypeError, ValueError):
            return False

        if visible_source_ids is not None:
            return source_uuid in visible_source_ids
        if session is not None:
            return await _source_visible_to_org(
                org_id=org.id,
                source_uuid=source_uuid,
                session=session,
            )

        async with get_content_read_session() as read_session:
            return await _source_visible_to_org(
                org_id=org.id,
                source_uuid=source_uuid,
                session=read_session,
            )

    return False


async def _resolve_visible_source_ids(
    jobs: list[Any],
    *,
    org: AuthOrganization,
    session: Any | None = None,
) -> set[UUID]:
    source_ids: set[UUID] = set()

    for job in jobs:
        fn = getattr(job, "function", "") or ""
        if fn not in {"crawl_source", "sync_source"}:
            continue

        args: list[Any] = list(getattr(job, "args", None) or ())
        kwargs = dict(getattr(job, "kwargs", None) or {})
        if kwargs.get("organization_id") is not None or not args:
            continue

        try:
            source_ids.add(UUID(str(args[0])))
        except (TypeError, ValueError):
            continue

    if not source_ids:
        return set()

    async def _resolve(read_session: Any | None) -> set[UUID]:
        visible: set[UUID] = set()
        for source_id in source_ids:
            if await _source_visible_to_org(
                org_id=org.id,
                source_uuid=source_id,
                session=read_session,
            ):
                visible.add(source_id)
        return visible

    if session is not None:
        return await _resolve(session)

    async with get_content_read_session() as read_session:
        return await _resolve(read_session)


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
    from sibyl.coordination import get_coordination_health

    try:
        return await get_coordination_health()
    except Exception as e:
        log.warning("Job queue health check failed", error=str(e))
        return {
            "status": "unhealthy",
            "backend": "unknown",
            "error": "Health check failed",
        }


@router.get("")
async def list_jobs(
    function: str | None = None,
    limit: int = 50,
    org: AuthOrganization = Depends(get_current_organization),
) -> dict[str, Any]:
    """List recent jobs."""
    from sibyl.jobs.queue import list_jobs as _list_jobs

    try:
        jobs = await _list_jobs(function=function, limit=limit)
        visible_source_ids = await _resolve_visible_source_ids(jobs, org=org)
        visible = [
            j
            for j in jobs
            if await _job_visible_to_org(
                j,
                org=org,
                visible_source_ids=visible_source_ids,
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
    org: AuthOrganization = Depends(get_current_organization),
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
    org: AuthOrganization = Depends(get_current_organization),
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
    org: AuthOrganization = Depends(get_current_organization),
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
    org: AuthOrganization = Depends(get_current_organization),
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
