"""Legacy backup adapters backed by the relational runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from sibyl.db.connection import get_session
from sibyl.db.models import Backup, BackupSettings, BackupStatus


@dataclass(frozen=True, slots=True)
class BackupListResult:
    backups: list[Backup]
    total: int


LegacyBackupList = BackupListResult


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


async def _get_backup_by_record_id(session: AsyncSession, *, record_id: UUID) -> Backup | None:
    return await session.get(Backup, record_id)


async def _get_backup_by_backup_id(session: AsyncSession, *, backup_id: str) -> Backup | None:
    result = await session.execute(select(Backup).where(col(Backup.backup_id) == backup_id))
    return result.scalar_one_or_none()


async def get_or_create_backup_settings(session: AsyncSession, org_id: UUID) -> BackupSettings:
    """Get or create backup settings for an organization."""
    result = await session.execute(
        select(BackupSettings).where(col(BackupSettings.organization_id) == org_id)
    )
    settings = result.scalar_one_or_none()

    if settings is None:
        settings = BackupSettings(organization_id=org_id)
        session.add(settings)
        await session.flush()

    return settings


async def get_backup_settings(org_id: UUID) -> BackupSettings:
    """Return persisted backup settings for an organization."""
    async with get_session() as session:
        settings = await get_or_create_backup_settings(session, org_id)
        await session.commit()
        await session.refresh(settings)
        return settings


async def update_backup_settings(
    org_id: UUID,
    *,
    enabled: bool | None = None,
    schedule: str | None = None,
    retention_days: int | None = None,
    include_postgres: bool | None = None,
    include_graph: bool | None = None,
) -> BackupSettings:
    """Persist backup setting changes for an organization."""
    async with get_session() as session:
        settings = await get_or_create_backup_settings(session, org_id)

        if enabled is not None:
            settings.enabled = enabled
        if schedule is not None:
            settings.schedule = schedule
        if retention_days is not None:
            settings.retention_days = retention_days
        if include_postgres is not None:
            settings.include_postgres = include_postgres
        if include_graph is not None:
            settings.include_graph = include_graph

        settings.updated_at = _utcnow()
        await session.commit()
        await session.refresh(settings)
        return settings


async def create_backup_record(
    *,
    org_id: UUID,
    backup_id: str,
    include_postgres: bool,
    include_graph: bool,
    created_by_user_id: UUID | None,
    triggered_by: str = "manual",
) -> Backup:
    """Create a pending backup record for an organization."""
    async with get_session() as session:
        backup = Backup(
            organization_id=org_id,
            backup_id=backup_id,
            status=BackupStatus.PENDING.value,
            include_postgres=include_postgres,
            include_graph=include_graph,
            triggered_by=triggered_by,
            created_by_user_id=created_by_user_id,
        )
        session.add(backup)
        await session.commit()
        await session.refresh(backup)
        return backup


async def attach_backup_job(record_id: UUID, job_id: str) -> Backup:
    """Attach a queued job identifier to a backup record."""
    async with get_session() as session:
        backup = await _get_backup_by_record_id(session, record_id=record_id)
        if backup is None:
            raise HTTPException(status_code=404, detail="Backup record not found")

        backup.job_id = job_id
        await session.commit()
        await session.refresh(backup)
        return backup


async def list_backups(org_id: UUID, *, limit: int, offset: int) -> BackupListResult:
    """List persisted backup records for an organization."""
    async with get_session() as session:
        count_result = await session.execute(
            select(Backup).where(col(Backup.organization_id) == org_id)
        )
        all_backups = count_result.scalars().all()

        result = await session.execute(
            select(Backup)
            .where(col(Backup.organization_id) == org_id)
            .order_by(col(Backup.created_at).desc())
            .limit(limit)
            .offset(offset)
        )
        return BackupListResult(backups=list(result.scalars().all()), total=len(all_backups))


async def get_backup(org_id: UUID, backup_id: str) -> Backup:
    """Return a persisted backup record or raise 404."""
    async with get_session() as session:
        result = await session.execute(
            select(Backup).where(
                col(Backup.organization_id) == org_id,
                col(Backup.backup_id) == backup_id,
            )
        )
        backup = result.scalar_one_or_none()

    if backup is None:
        raise HTTPException(status_code=404, detail=f"Backup not found: {backup_id}")
    return backup


async def get_backup_retention(org_id: UUID, requested_retention: int | None) -> int:
    """Resolve backup retention days from the request or stored settings."""
    if requested_retention is not None:
        return requested_retention

    settings = await get_backup_settings(org_id)
    return settings.retention_days


async def delete_backup_record(org_id: UUID, backup_id: str) -> Backup:
    """Delete a persisted backup record and return it."""
    async with get_session() as session:
        result = await session.execute(
            select(Backup).where(
                col(Backup.organization_id) == org_id,
                col(Backup.backup_id) == backup_id,
            )
        )
        backup = result.scalar_one_or_none()

        if backup is None:
            raise HTTPException(status_code=404, detail=f"Backup not found: {backup_id}")

        await session.delete(backup)
        await session.commit()
        return backup


async def update_backup_record(
    backup_id: str,
    *,
    status: str | None = None,
    filename: str | None = None,
    file_path: str | None = None,
    size_bytes: int | None = None,
    entity_count: int | None = None,
    relationship_count: int | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    duration_seconds: float | None = None,
    error: str | None = None,
) -> Backup | None:
    """Update a backup record by backup_id and sync denormalized settings state."""
    async with get_session() as session:
        backup = await _get_backup_by_backup_id(session, backup_id=backup_id)
        if backup is None:
            return None

        if status is not None:
            backup.status = status
        if filename is not None:
            backup.filename = filename
        if file_path is not None:
            backup.file_path = file_path
        if size_bytes is not None:
            backup.size_bytes = size_bytes
        if entity_count is not None:
            backup.entity_count = entity_count
        if relationship_count is not None:
            backup.relationship_count = relationship_count
        if started_at is not None:
            backup.started_at = _normalize_datetime(started_at)
        if completed_at is not None:
            backup.completed_at = _normalize_datetime(completed_at)
        if duration_seconds is not None:
            backup.duration_seconds = duration_seconds
        if error is not None:
            backup.error = error

        backup.updated_at = _utcnow()

        if backup.status == BackupStatus.COMPLETED.value:
            settings = await get_or_create_backup_settings(session, backup.organization_id)
            settings.last_backup_at = backup.completed_at or _utcnow()
            settings.last_backup_id = backup.backup_id
            settings.updated_at = _utcnow()

        await session.commit()
        await session.refresh(backup)
        return backup


async def list_enabled_backup_settings() -> list[BackupSettings]:
    """List all organizations with scheduled backups enabled."""
    async with get_session() as session:
        result = await session.execute(
            select(BackupSettings).where(col(BackupSettings.enabled).is_(True))
        )
        return list(result.scalars().all())


async def get_legacy_backup_settings(org_id: UUID) -> BackupSettings:
    return await get_backup_settings(org_id)


async def update_legacy_backup_settings(
    org_id: UUID,
    *,
    enabled: bool | None = None,
    schedule: str | None = None,
    retention_days: int | None = None,
    include_postgres: bool | None = None,
    include_graph: bool | None = None,
) -> BackupSettings:
    return await update_backup_settings(
        org_id,
        enabled=enabled,
        schedule=schedule,
        retention_days=retention_days,
        include_postgres=include_postgres,
        include_graph=include_graph,
    )


async def create_legacy_backup_record(
    *,
    org_id: UUID,
    backup_id: str,
    include_postgres: bool,
    include_graph: bool,
    created_by_user_id: UUID | None,
) -> Backup:
    return await create_backup_record(
        org_id=org_id,
        backup_id=backup_id,
        include_postgres=include_postgres,
        include_graph=include_graph,
        created_by_user_id=created_by_user_id,
        triggered_by="manual",
    )


async def attach_legacy_backup_job(record_id: UUID, job_id: str) -> Backup:
    return await attach_backup_job(record_id, job_id)


async def list_legacy_backups(org_id: UUID, *, limit: int, offset: int) -> LegacyBackupList:
    return await list_backups(org_id, limit=limit, offset=offset)


async def get_legacy_backup(org_id: UUID, backup_id: str) -> Backup:
    return await get_backup(org_id, backup_id)


async def get_legacy_backup_retention(org_id: UUID, requested_retention: int | None) -> int:
    return await get_backup_retention(org_id, requested_retention)


async def delete_legacy_backup_record(org_id: UUID, backup_id: str) -> Backup:
    return await delete_backup_record(org_id, backup_id)


get_or_create_legacy_backup_settings = get_or_create_backup_settings
