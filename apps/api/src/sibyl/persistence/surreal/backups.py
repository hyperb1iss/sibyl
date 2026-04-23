"""Surreal-backed backup persistence helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException

from sibyl.config import settings as app_settings
from sibyl.db.models import Backup, BackupSettings, BackupStatus
from sibyl.persistence.backups_common import BackupListResult, resolve_backup_runtime_options
from sibyl.persistence.surreal.content import (
    _coerce_bool,
    _coerce_datetime,
    _coerce_float,
    _coerce_int,
    _coerce_optional_str,
    _coerce_optional_uuid,
    _coerce_str,
    _coerce_uuid,
    _normalize_records,
    _query_error,
    build_surreal_content_client,
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


async def _execute_query(query: str, **params: object) -> list[dict[str, object]]:
    client = build_surreal_content_client()
    try:
        result = await client.execute_query(query, **params)
    finally:
        await client.close()

    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return _normalize_records(result)


def _sort_key(value: datetime | None) -> datetime:
    return value or datetime.min.replace(tzinfo=None)


def _postgres_backups_supported() -> bool:
    return resolve_backup_runtime_options(
        store=app_settings.store,
        auth_store=app_settings.auth_store,
    ).postgres_dump_supported


def _effective_include_postgres(requested: bool | None) -> bool:
    return resolve_backup_runtime_options(
        store=app_settings.store,
        auth_store=app_settings.auth_store,
        include_postgres=requested,
    ).include_postgres


def _backup_settings_from_record(record: dict[str, object]) -> BackupSettings:
    now = _utcnow()
    return BackupSettings(
        id=_coerce_uuid(record.get("uuid"), field_name="backup_settings.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"),
            field_name="backup_settings.organization_id",
        ),
        enabled=_coerce_bool(record.get("enabled"), default=True),
        schedule=_coerce_str(record.get("schedule"), default="0 2 * * *"),
        retention_days=_coerce_int(record.get("retention_days"), default=30),
        include_postgres=_effective_include_postgres(
            _coerce_bool(record.get("include_postgres"), default=_postgres_backups_supported())
        ),
        include_graph=_coerce_bool(record.get("include_graph"), default=True),
        last_backup_at=_coerce_datetime(record.get("last_backup_at")),
        last_backup_id=_coerce_optional_str(record.get("last_backup_id")),
        created_at=_coerce_datetime(record.get("created_at")) or now,
        updated_at=_coerce_datetime(record.get("updated_at")) or now,
    )


def _backup_settings_record(settings: BackupSettings) -> dict[str, object]:
    return {
        "uuid": str(settings.id),
        "organization_id": str(settings.organization_id),
        "enabled": settings.enabled,
        "schedule": settings.schedule,
        "retention_days": settings.retention_days,
        "include_postgres": _effective_include_postgres(settings.include_postgres),
        "include_graph": settings.include_graph,
        "last_backup_at": settings.last_backup_at,
        "last_backup_id": settings.last_backup_id,
        "created_at": settings.created_at,
        "updated_at": settings.updated_at,
    }


def _backup_from_record(record: dict[str, object]) -> Backup:
    now = _utcnow()
    return Backup(
        id=_coerce_uuid(record.get("uuid"), field_name="backups.uuid"),
        organization_id=_coerce_uuid(record.get("organization_id"), field_name="backups.organization_id"),
        backup_id=_coerce_str(record.get("backup_id")),
        status=_coerce_str(record.get("status"), default=BackupStatus.PENDING.value),
        job_id=_coerce_optional_str(record.get("job_id")),
        filename=_coerce_optional_str(record.get("filename")),
        file_path=_coerce_optional_str(record.get("file_path")),
        size_bytes=_coerce_int(record.get("size_bytes")),
        include_postgres=_effective_include_postgres(
            _coerce_bool(record.get("include_postgres"), default=_postgres_backups_supported())
        ),
        include_graph=_coerce_bool(record.get("include_graph"), default=True),
        entity_count=_coerce_int(record.get("entity_count")),
        relationship_count=_coerce_int(record.get("relationship_count")),
        started_at=_coerce_datetime(record.get("started_at")),
        completed_at=_coerce_datetime(record.get("completed_at")),
        duration_seconds=_coerce_float(record.get("duration_seconds")),
        error=_coerce_optional_str(record.get("error")),
        triggered_by=_coerce_optional_str(record.get("triggered_by")),
        created_by_user_id=_coerce_optional_uuid(record.get("created_by_user_id")),
        created_at=_coerce_datetime(record.get("created_at")) or now,
        updated_at=_coerce_datetime(record.get("updated_at")) or now,
    )


def _backup_record(backup: Backup) -> dict[str, object]:
    return {
        "uuid": str(backup.id),
        "organization_id": str(backup.organization_id),
        "backup_id": backup.backup_id,
        "status": backup.status,
        "job_id": backup.job_id,
        "filename": backup.filename,
        "file_path": backup.file_path,
        "size_bytes": backup.size_bytes,
        "include_postgres": _effective_include_postgres(backup.include_postgres),
        "include_graph": backup.include_graph,
        "entity_count": backup.entity_count,
        "relationship_count": backup.relationship_count,
        "started_at": backup.started_at,
        "completed_at": backup.completed_at,
        "duration_seconds": backup.duration_seconds,
        "error": backup.error,
        "triggered_by": backup.triggered_by,
        "created_by_user_id": str(backup.created_by_user_id) if backup.created_by_user_id else None,
        "created_at": backup.created_at,
        "updated_at": backup.updated_at,
    }


async def _get_backup_settings_for_org(org_id: UUID) -> BackupSettings | None:
    rows = await _execute_query(
        "SELECT * FROM backup_settings WHERE organization_id = $organization_id LIMIT 1;",
        organization_id=str(org_id),
    )
    return _backup_settings_from_record(rows[0]) if rows else None


async def _save_backup_settings(settings: BackupSettings) -> BackupSettings:
    existing = await _get_backup_settings_for_org(settings.organization_id)
    if existing is not None:
        await _execute_query("DELETE FROM backup_settings WHERE uuid = $uuid;", uuid=str(existing.id))
        settings.id = existing.id
        settings.created_at = existing.created_at
    settings.updated_at = _utcnow()
    rows = await _execute_query(
        "CREATE backup_settings CONTENT $record;",
        record=_backup_settings_record(settings),
    )
    if not rows:
        msg = f"Failed to write backup settings for {settings.organization_id}"
        raise RuntimeError(msg)
    return _backup_settings_from_record(rows[0])


async def _get_backup_by_record_id(record_id: UUID) -> Backup | None:
    rows = await _execute_query(
        "SELECT * FROM backups WHERE uuid = $record_id LIMIT 1;",
        record_id=str(record_id),
    )
    return _backup_from_record(rows[0]) if rows else None


async def _get_backup_by_backup_id(backup_id: str) -> Backup | None:
    rows = await _execute_query(
        "SELECT * FROM backups WHERE backup_id = $backup_id LIMIT 1;",
        backup_id=backup_id,
    )
    return _backup_from_record(rows[0]) if rows else None


async def _save_backup(backup: Backup) -> Backup:
    existing = await _get_backup_by_backup_id(backup.backup_id)
    if existing is not None:
        await _execute_query("DELETE FROM backups WHERE uuid = $uuid;", uuid=str(existing.id))
        backup.id = existing.id
        backup.created_at = existing.created_at
    backup.updated_at = _utcnow()
    rows = await _execute_query("CREATE backups CONTENT $record;", record=_backup_record(backup))
    if not rows:
        msg = f"Failed to write backup record {backup.backup_id}"
        raise RuntimeError(msg)
    return _backup_from_record(rows[0])


async def get_backup_settings(org_id: UUID) -> BackupSettings:
    settings = await _get_backup_settings_for_org(org_id)
    if settings is not None:
        return settings
    return await _save_backup_settings(
        BackupSettings(
            organization_id=org_id,
            include_postgres=_postgres_backups_supported(),
        )
    )


async def update_backup_settings(
    org_id: UUID,
    *,
    enabled: bool | None = None,
    schedule: str | None = None,
    retention_days: int | None = None,
    include_postgres: bool | None = None,
    include_graph: bool | None = None,
) -> BackupSettings:
    settings = await get_backup_settings(org_id)
    if enabled is not None:
        settings.enabled = enabled
    if schedule is not None:
        settings.schedule = schedule
    if retention_days is not None:
        settings.retention_days = retention_days
    if include_postgres is not None:
        settings.include_postgres = _effective_include_postgres(include_postgres)
    if include_graph is not None:
        settings.include_graph = include_graph
    return await _save_backup_settings(settings)


async def create_backup_record(
    *,
    org_id: UUID,
    backup_id: str,
    include_postgres: bool,
    include_graph: bool,
    created_by_user_id: UUID | None,
    triggered_by: str = "manual",
) -> Backup:
    return await _save_backup(
        Backup(
            id=uuid4(),
            organization_id=org_id,
            backup_id=backup_id,
            status=BackupStatus.PENDING.value,
            include_postgres=_effective_include_postgres(include_postgres),
            include_graph=include_graph,
            triggered_by=triggered_by,
            created_by_user_id=created_by_user_id,
        )
    )


async def attach_backup_job(record_id: UUID, job_id: str) -> Backup:
    backup = await _get_backup_by_record_id(record_id)
    if backup is None:
        raise HTTPException(status_code=404, detail="Backup record not found")
    backup.job_id = job_id
    return await _save_backup(backup)


async def list_backups(org_id: UUID, *, limit: int, offset: int) -> BackupListResult:
    rows = await _execute_query(
        "SELECT * FROM backups WHERE organization_id = $organization_id;",
        organization_id=str(org_id),
    )
    backups = [_backup_from_record(row) for row in rows]
    backups.sort(key=lambda backup: _sort_key(backup.created_at), reverse=True)
    return BackupListResult(backups=backups[offset : offset + limit], total=len(backups))


async def get_backup(org_id: UUID, backup_id: str) -> Backup:
    rows = await _execute_query(
        "SELECT * FROM backups WHERE organization_id = $organization_id AND backup_id = $backup_id LIMIT 1;",
        organization_id=str(org_id),
        backup_id=backup_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Backup not found: {backup_id}")
    return _backup_from_record(rows[0])


async def get_backup_retention(org_id: UUID, requested_retention: int | None) -> int:
    if requested_retention is not None:
        return requested_retention
    settings = await get_backup_settings(org_id)
    return settings.retention_days


async def delete_backup_record(org_id: UUID, backup_id: str) -> Backup:
    backup = await get_backup(org_id, backup_id)
    await _execute_query("DELETE FROM backups WHERE uuid = $uuid;", uuid=str(backup.id))
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
    backup = await _get_backup_by_backup_id(backup_id)
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

    backup = await _save_backup(backup)

    if backup.status == BackupStatus.COMPLETED.value:
        settings = await get_backup_settings(backup.organization_id)
        settings.last_backup_at = backup.completed_at or _utcnow()
        settings.last_backup_id = backup.backup_id
        await _save_backup_settings(settings)

    return backup


async def list_enabled_backup_settings() -> list[BackupSettings]:
    rows = await _execute_query("SELECT * FROM backup_settings WHERE enabled = true;")
    settings = [_backup_settings_from_record(row) for row in rows]
    settings.sort(key=lambda item: str(item.organization_id))
    return settings
