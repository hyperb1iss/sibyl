"""Backup management API endpoints.

Provides REST API for:
- Configuring backup settings (schedule, retention)
- Triggering backups
- Listing and managing backup archives
- Downloading backup archives
"""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from sibyl.auth.dependencies import get_current_organization, get_current_user, require_org_admin
from sibyl.backup_ids import generate_backup_id
from sibyl.db.models import BackupStatus, Organization, User
from sibyl.persistence.backups_runtime import (
    attach_backup_job as attach_backup_job_record,
    create_backup_record,
    delete_backup_record,
    get_backup as get_backup_record,
    get_backup_retention as resolve_backup_retention,
    get_backup_settings as load_backup_settings,
    list_backups as list_backup_records,
    update_backup_settings as save_backup_settings,
)

log = structlog.get_logger()

router = APIRouter(
    prefix="/backups",
    tags=["backups"],
    dependencies=[Depends(require_org_admin())],
)


# =============================================================================
# Request/Response Models
# =============================================================================


class BackupSettingsUpdate(BaseModel):
    """Request to update backup settings."""

    enabled: bool | None = None
    schedule: str | None = Field(None, max_length=64)
    retention_days: int | None = Field(None, ge=1, le=365)
    include_postgres: bool | None = None
    include_graph: bool | None = None


class BackupSettingsResponse(BaseModel):
    """Backup configuration settings response."""

    enabled: bool
    schedule: str
    retention_days: int
    include_postgres: bool
    include_graph: bool
    last_backup_at: str | None
    last_backup_id: str | None


class CreateBackupRequest(BaseModel):
    """Request to create a new backup."""

    include_postgres: bool = Field(default=True, description="Include PostgreSQL dump")
    include_graph: bool = Field(default=True, description="Include graph export")


class CreateBackupResponse(BaseModel):
    """Response after creating a backup job."""

    id: str
    backup_id: str
    job_id: str
    status: str
    message: str


class BackupInfo(BaseModel):
    """Information about a backup archive."""

    id: str
    backup_id: str
    status: str
    filename: str | None
    size_bytes: int
    entity_count: int
    relationship_count: int
    duration_seconds: float
    triggered_by: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    error: str | None = None


class BackupListResponse(BaseModel):
    """Response containing list of backups."""

    backups: list[BackupInfo]
    total: int


class CleanupRequest(BaseModel):
    """Request to run backup cleanup."""

    retention_days: int | None = Field(
        default=None, ge=1, le=365, description="Override retention period"
    )


class CleanupResponse(BaseModel):
    """Response after queueing cleanup job."""

    job_id: str
    message: str


# =============================================================================
# Settings Endpoints
# =============================================================================


@router.get("/settings")
async def get_backup_settings(
    org: Organization = Depends(get_current_organization),
) -> BackupSettingsResponse:
    """Get backup configuration settings for the organization."""
    settings = await load_backup_settings(org.id)

    return BackupSettingsResponse(
        enabled=settings.enabled,
        schedule=settings.schedule,
        retention_days=settings.retention_days,
        include_postgres=settings.include_postgres,
        include_graph=settings.include_graph,
        last_backup_at=settings.last_backup_at.isoformat() if settings.last_backup_at else None,
        last_backup_id=settings.last_backup_id,
    )


@router.patch("/settings")
async def update_backup_settings(
    request: BackupSettingsUpdate,
    org: Organization = Depends(get_current_organization),
) -> BackupSettingsResponse:
    """Update backup configuration settings."""
    settings = await save_backup_settings(
        org.id,
        enabled=request.enabled,
        schedule=request.schedule,
        retention_days=request.retention_days,
        include_postgres=request.include_postgres,
        include_graph=request.include_graph,
    )

    log.info(
        "backup_settings_updated",
        organization_id=str(org.id),
        enabled=settings.enabled,
        schedule=settings.schedule,
        retention_days=settings.retention_days,
    )

    return BackupSettingsResponse(
        enabled=settings.enabled,
        schedule=settings.schedule,
        retention_days=settings.retention_days,
        include_postgres=settings.include_postgres,
        include_graph=settings.include_graph,
        last_backup_at=settings.last_backup_at.isoformat() if settings.last_backup_at else None,
        last_backup_id=settings.last_backup_id,
    )


# =============================================================================
# Backup CRUD Endpoints
# =============================================================================


@router.post("")
async def create_backup(
    request: CreateBackupRequest,
    org: Organization = Depends(get_current_organization),
    user: User = Depends(get_current_user),
) -> CreateBackupResponse:
    """Trigger a new backup job.

    Creates a compressed archive containing:
    - PostgreSQL database dump
    - FalkorDB graph export
    - Metadata with checksums

    Returns a job ID that can be used to track progress.
    """
    backup_id = generate_backup_id(str(org.id))
    backup = await create_backup_record(
        org_id=org.id,
        backup_id=backup_id,
        include_postgres=request.include_postgres,
        include_graph=request.include_graph,
        created_by_user_id=user.id if user else None,
    )

    log.info(
        "backup_requested",
        backup_id=backup_id,
        organization_id=str(org.id),
        include_postgres=request.include_postgres,
        include_graph=request.include_graph,
    )

    from sibyl.jobs.queue import enqueue_backup

    job_id = await enqueue_backup(
        str(org.id),
        include_postgres=request.include_postgres,
        include_graph=request.include_graph,
        backup_id=backup_id,
    )
    backup = await attach_backup_job_record(backup.id, job_id)

    return CreateBackupResponse(
        id=str(backup.id),
        backup_id=backup_id,
        job_id=job_id,
        status=backup.status,
        message="Backup job queued successfully",
    )


@router.get("")
async def list_backups(
    org: Organization = Depends(get_current_organization),
    limit: int = 50,
    offset: int = 0,
) -> BackupListResponse:
    """List all backup archives for the organization.

    Returns backups sorted by creation time (newest first).
    """
    results = await list_backup_records(org.id, limit=limit, offset=offset)

    return BackupListResponse(
        backups=[
            BackupInfo(
                id=str(backup.id),
                backup_id=backup.backup_id,
                status=backup.status,
                filename=backup.filename,
                size_bytes=backup.size_bytes,
                entity_count=backup.entity_count,
                relationship_count=backup.relationship_count,
                duration_seconds=backup.duration_seconds,
                triggered_by=backup.triggered_by,
                created_at=backup.created_at.isoformat() if backup.created_at else "",
                started_at=backup.started_at.isoformat() if backup.started_at else None,
                completed_at=backup.completed_at.isoformat() if backup.completed_at else None,
                error=backup.error,
            )
            for backup in results.backups
        ],
        total=results.total,
    )


@router.post("/cleanup")
async def run_cleanup(
    request: CleanupRequest,
    org: Organization = Depends(get_current_organization),
) -> CleanupResponse:
    """Trigger a backup cleanup job.

    Removes backup archives older than the retention period.
    """
    retention = await resolve_backup_retention(org.id, request.retention_days)

    log.info(
        "backup_cleanup_requested",
        organization_id=str(org.id),
        retention_days=retention,
    )

    from sibyl.jobs.queue import enqueue_backup_cleanup

    job_id = await enqueue_backup_cleanup(retention_days=retention)

    return CleanupResponse(
        job_id=job_id,
        message="Cleanup job queued successfully",
    )


@router.get("/{backup_id}")
async def get_backup_details(
    backup_id: str,
    org: Organization = Depends(get_current_organization),
) -> BackupInfo:
    """Get detailed information about a specific backup."""
    backup = await get_backup_record(org.id, backup_id)

    return BackupInfo(
        id=str(backup.id),
        backup_id=backup.backup_id,
        status=backup.status,
        filename=backup.filename,
        size_bytes=backup.size_bytes,
        entity_count=backup.entity_count,
        relationship_count=backup.relationship_count,
        duration_seconds=backup.duration_seconds,
        triggered_by=backup.triggered_by,
        created_at=backup.created_at.isoformat() if backup.created_at else "",
        started_at=backup.started_at.isoformat() if backup.started_at else None,
        completed_at=backup.completed_at.isoformat() if backup.completed_at else None,
        error=backup.error,
    )


@router.get("/{backup_id}/download")
async def download_backup(
    backup_id: str,
    org: Organization = Depends(get_current_organization),
) -> FileResponse:
    """Download a backup archive.

    Returns the .tar.gz file directly.
    """
    backup = await get_backup_record(org.id, backup_id)

    if backup.status != BackupStatus.COMPLETED.value:
        raise HTTPException(
            status_code=400,
            detail=f"Backup not ready for download (status: {backup.status})",
        )

    if not backup.file_path:
        raise HTTPException(status_code=404, detail="Backup file path not recorded")

    from sibyl.jobs.backup import get_backup as get_backup_file

    file_info = get_backup_file(backup_id)
    if file_info is None:
        raise HTTPException(status_code=404, detail="Backup file not found on disk")

    from pathlib import Path

    archive_path = Path(file_info["path"])
    if not archive_path.exists():
        raise HTTPException(status_code=404, detail="Backup file not found on disk")

    return FileResponse(
        path=archive_path,
        filename=backup.filename or f"sibyl_{backup_id}.tar.gz",
        media_type="application/gzip",
    )


@router.delete("/{backup_id}")
async def delete_backup(
    backup_id: str,
    org: Organization = Depends(get_current_organization),
) -> dict[str, Any]:
    """Delete a specific backup archive.

    This action cannot be undone.
    """
    backup = await get_backup_record(org.id, backup_id)

    log.info("backup_delete_requested", backup_id=backup_id, organization_id=str(org.id))

    from sibyl.jobs.backup import delete_backup as delete_backup_file

    delete_backup_file(backup_id)
    await delete_backup_record(org.id, backup.backup_id)

    return {"deleted": True, "backup_id": backup_id}


@router.get("/jobs/{job_id}")
async def get_backup_job_status(job_id: str) -> dict[str, Any]:
    """Get status of a backup job.

    Returns job status, result (if complete), or error (if failed).
    """
    from sibyl.jobs.queue import get_job_status

    info = await get_job_status(job_id)

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
