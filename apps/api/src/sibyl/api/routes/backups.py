"""Backup management API endpoints.

Provides REST API for:
- Configuring backup settings (schedule, retention)
- Triggering backups
- Listing and managing backup archives
- Downloading backup archives
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sibyl.auth.dependencies import get_current_organization, get_current_user, require_org_admin
from sibyl.db.connection import get_session
from sibyl.db.models import Backup, BackupSettings, BackupStatus, Organization, User

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


async def get_or_create_settings(session: AsyncSession, org_id: UUID) -> BackupSettings:
    """Get or create backup settings for an organization."""
    result = await session.execute(
        select(BackupSettings).where(BackupSettings.organization_id == org_id)
    )
    settings = result.scalar_one_or_none()

    if settings is None:
        settings = BackupSettings(organization_id=org_id)
        session.add(settings)
        await session.flush()

    return settings


@router.get("/settings")
async def get_backup_settings(
    org: Organization = Depends(get_current_organization),
) -> BackupSettingsResponse:
    """Get backup configuration settings for the organization."""
    async with get_session() as session:
        settings = await get_or_create_settings(session, org.id)
        await session.commit()

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
    async with get_session() as session:
        settings = await get_or_create_settings(session, org.id)

        # Update only provided fields
        if request.enabled is not None:
            settings.enabled = request.enabled
        if request.schedule is not None:
            settings.schedule = request.schedule
        if request.retention_days is not None:
            settings.retention_days = request.retention_days
        if request.include_postgres is not None:
            settings.include_postgres = request.include_postgres
        if request.include_graph is not None:
            settings.include_graph = request.include_graph

        settings.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()

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
    # Generate backup ID
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_id = f"backup_{timestamp}"

    async with get_session() as session:
        # Create backup record
        backup = Backup(
            organization_id=org.id,
            backup_id=backup_id,
            status=BackupStatus.PENDING.value,
            include_postgres=request.include_postgres,
            include_graph=request.include_graph,
            triggered_by="manual",
            created_by_user_id=user.id if user else None,
        )
        session.add(backup)
        await session.commit()
        await session.refresh(backup)

        log.info(
            "backup_requested",
            backup_id=backup_id,
            organization_id=str(org.id),
            include_postgres=request.include_postgres,
            include_graph=request.include_graph,
        )

        # Enqueue the job
        from sibyl.jobs.queue import enqueue_backup

        job_id = await enqueue_backup(
            str(org.id),
            include_postgres=request.include_postgres,
            include_graph=request.include_graph,
            backup_id=backup_id,
        )

        # Update backup with job ID
        backup.job_id = job_id
        await session.commit()

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
    async with get_session() as session:
        # Get total count
        count_result = await session.execute(select(Backup).where(Backup.organization_id == org.id))
        all_backups = count_result.scalars().all()
        total = len(all_backups)

        # Get paginated results
        result = await session.execute(
            select(Backup)
            .where(Backup.organization_id == org.id)
            .order_by(Backup.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        backups = result.scalars().all()

        return BackupListResponse(
            backups=[
                BackupInfo(
                    id=str(b.id),
                    backup_id=b.backup_id,
                    status=b.status,
                    filename=b.filename,
                    size_bytes=b.size_bytes,
                    entity_count=b.entity_count,
                    relationship_count=b.relationship_count,
                    duration_seconds=b.duration_seconds,
                    triggered_by=b.triggered_by,
                    created_at=b.created_at.isoformat() if b.created_at else "",
                    started_at=b.started_at.isoformat() if b.started_at else None,
                    completed_at=b.completed_at.isoformat() if b.completed_at else None,
                    error=b.error,
                )
                for b in backups
            ],
            total=total,
        )


@router.get("/{backup_id}")
async def get_backup_details(
    backup_id: str,
    org: Organization = Depends(get_current_organization),
) -> BackupInfo:
    """Get detailed information about a specific backup."""
    async with get_session() as session:
        result = await session.execute(
            select(Backup).where(
                Backup.organization_id == org.id,
                Backup.backup_id == backup_id,
            )
        )
        backup = result.scalar_one_or_none()

        if backup is None:
            raise HTTPException(status_code=404, detail=f"Backup not found: {backup_id}")

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
    async with get_session() as session:
        result = await session.execute(
            select(Backup).where(
                Backup.organization_id == org.id,
                Backup.backup_id == backup_id,
            )
        )
        backup = result.scalar_one_or_none()

        if backup is None:
            raise HTTPException(status_code=404, detail=f"Backup not found: {backup_id}")

        if backup.status != BackupStatus.COMPLETED.value:
            raise HTTPException(
                status_code=400,
                detail=f"Backup not ready for download (status: {backup.status})",
            )

        if not backup.file_path:
            raise HTTPException(status_code=404, detail="Backup file path not recorded")

        # Use the file-based function to verify and get file details
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
    async with get_session() as session:
        result = await session.execute(
            select(Backup).where(
                Backup.organization_id == org.id,
                Backup.backup_id == backup_id,
            )
        )
        backup = result.scalar_one_or_none()

        if backup is None:
            raise HTTPException(status_code=404, detail=f"Backup not found: {backup_id}")

        log.info("backup_delete_requested", backup_id=backup_id, organization_id=str(org.id))

        # Delete the file
        from sibyl.jobs.backup import delete_backup as delete_backup_file

        delete_backup_file(backup_id)

        # Delete the DB record
        await session.delete(backup)
        await session.commit()

        return {"deleted": True, "backup_id": backup_id}


@router.post("/cleanup")
async def run_cleanup(
    request: CleanupRequest,
    org: Organization = Depends(get_current_organization),
) -> CleanupResponse:
    """Trigger a backup cleanup job.

    Removes backup archives older than the retention period.
    """
    async with get_session() as session:
        settings = await get_or_create_settings(session, org.id)
        retention = request.retention_days or settings.retention_days

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
