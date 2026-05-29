"""Surreal-backed backup persistence helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException

from sibyl.config import settings as app_settings
from sibyl.persistence.backups_common import (
    BackupListResult,
    BackupRecord,
    BackupSettingsRecord,
    BackupStatus,
    resolve_backup_runtime_options,
    resolve_mapping_database_dump,
)
from sibyl.persistence.surreal.content import (
    _coerce_bool,
    _coerce_float,
    _coerce_int,
    _coerce_optional_str,
    _coerce_optional_uuid,
    _coerce_str,
    surreal_content_client,
)
from sibyl_core.backends.surreal.records import (
    coerce_datetime as _coerce_datetime,
    coerce_uuid as _coerce_uuid,
    normalize_records as _normalize_records,
    query_error as _query_error,
    utcnow as _utcnow,
)


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


async def _execute_query(query: str, **params: object) -> list[dict[str, object]]:
    async with surreal_content_client() as client:
        result = await client.execute_query(query, **params)

    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return _normalize_records(result)


async def _count_query(query: str, **params: object) -> int:
    rows = await _execute_query(query, **params)
    if not rows:
        return 0
    value = rows[0].get("total")
    return int(value) if isinstance(value, int | float) else 0


def _database_dump_supported() -> bool:
    return resolve_backup_runtime_options(
        store=app_settings.store,
        auth_store=app_settings.auth_store,
    ).database_dump_supported


def _effective_include_database_dump(requested: bool | None = None) -> bool:
    return resolve_backup_runtime_options(
        store=app_settings.store,
        auth_store=app_settings.auth_store,
        include_database_dump=requested,
    ).include_database_dump


def _record_include_database_dump(record: dict[str, object]) -> bool:
    requested_database_dump = resolve_mapping_database_dump(
        record,
        coerce=lambda value: (
            _coerce_bool(value, default=_database_dump_supported()) if value is not None else None
        ),
    )
    return _effective_include_database_dump(requested_database_dump)


def _backup_settings_from_record(record: dict[str, object]) -> BackupSettingsRecord:
    now = _utcnow()
    include_database_dump = _record_include_database_dump(record)
    return BackupSettingsRecord(
        id=_coerce_uuid(record.get("uuid"), field_name="backup_settings.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"),
            field_name="backup_settings.organization_id",
        ),
        enabled=_coerce_bool(record.get("enabled"), default=True),
        schedule=_coerce_str(record.get("schedule"), default="0 2 * * *"),
        retention_days=_coerce_int(record.get("retention_days"), default=30),
        include_database_dump=include_database_dump,
        include_graph=_coerce_bool(record.get("include_graph"), default=True),
        last_backup_at=_coerce_datetime(record.get("last_backup_at")),
        last_backup_id=_coerce_optional_str(record.get("last_backup_id")),
        created_at=_coerce_datetime(record.get("created_at")) or now,
        updated_at=_coerce_datetime(record.get("updated_at")) or now,
    )


def _backup_settings_record(settings: BackupSettingsRecord) -> dict[str, object]:
    include_database_dump = _effective_include_database_dump(settings.include_database_dump)
    return {
        "uuid": str(settings.id),
        "organization_id": str(settings.organization_id),
        "enabled": settings.enabled,
        "schedule": settings.schedule,
        "retention_days": settings.retention_days,
        "include_database_dump": include_database_dump,
        "include_graph": settings.include_graph,
        "last_backup_at": settings.last_backup_at,
        "last_backup_id": settings.last_backup_id,
        "created_at": settings.created_at,
        "updated_at": settings.updated_at,
    }


def _backup_from_record(record: dict[str, object]) -> BackupRecord:
    now = _utcnow()
    include_database_dump = _record_include_database_dump(record)
    return BackupRecord(
        id=_coerce_uuid(record.get("uuid"), field_name="backups.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"), field_name="backups.organization_id"
        ),
        backup_id=_coerce_str(record.get("backup_id")),
        status=_coerce_str(record.get("status"), default=BackupStatus.PENDING.value),
        job_id=_coerce_optional_str(record.get("job_id")),
        filename=_coerce_optional_str(record.get("filename")),
        file_path=_coerce_optional_str(record.get("file_path")),
        size_bytes=_coerce_int(record.get("size_bytes")),
        include_database_dump=include_database_dump,
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


def _backup_record(backup: BackupRecord) -> dict[str, object]:
    include_database_dump = _effective_include_database_dump(backup.include_database_dump)
    return {
        "uuid": str(backup.id),
        "organization_id": str(backup.organization_id),
        "backup_id": backup.backup_id,
        "status": backup.status,
        "job_id": backup.job_id,
        "filename": backup.filename,
        "file_path": backup.file_path,
        "size_bytes": backup.size_bytes,
        "include_database_dump": include_database_dump,
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


async def _get_backup_settings_for_org(org_id: UUID) -> BackupSettingsRecord | None:
    rows = await _execute_query(
        "SELECT * FROM backup_settings WHERE organization_id = $organization_id LIMIT 1;",
        organization_id=str(org_id),
    )
    return _backup_settings_from_record(rows[0]) if rows else None


async def _save_backup_settings(settings: BackupSettingsRecord) -> BackupSettingsRecord:
    existing = await _get_backup_settings_for_org(settings.organization_id)
    if existing is not None:
        settings.id = existing.id
        settings.created_at = existing.created_at
    settings.updated_at = _utcnow()
    rows = await _execute_query(
        "UPSERT backup_settings CONTENT $record WHERE uuid = $uuid;",
        uuid=str(settings.id),
        record=_backup_settings_record(settings),
    )
    if not rows:
        msg = f"Failed to write backup settings for {settings.organization_id}"
        raise RuntimeError(msg)
    persisted = await _get_backup_settings_for_org(settings.organization_id)
    if persisted is None:
        msg = f"Failed to reload backup settings for {settings.organization_id}"
        raise RuntimeError(msg)
    return persisted


async def _get_backup_by_record_id(record_id: UUID) -> BackupRecord | None:
    rows = await _execute_query(
        "SELECT * FROM backups WHERE uuid = $record_id LIMIT 1;",
        record_id=str(record_id),
    )
    return _backup_from_record(rows[0]) if rows else None


async def _get_backup_by_backup_id(backup_id: str) -> BackupRecord | None:
    rows = await _execute_query(
        "SELECT * FROM backups WHERE backup_id = $backup_id LIMIT 1;",
        backup_id=backup_id,
    )
    return _backup_from_record(rows[0]) if rows else None


async def _save_backup(backup: BackupRecord) -> BackupRecord:
    existing = await _get_backup_by_backup_id(backup.backup_id)
    if existing is not None:
        backup.id = existing.id
        backup.created_at = existing.created_at
    backup.updated_at = _utcnow()
    rows = await _execute_query(
        "UPSERT backups CONTENT $record WHERE uuid = $uuid;",
        uuid=str(backup.id),
        record=_backup_record(backup),
    )
    if not rows:
        msg = f"Failed to write backup record {backup.backup_id}"
        raise RuntimeError(msg)
    persisted = await _get_backup_by_backup_id(backup.backup_id)
    if persisted is None:
        msg = f"Failed to reload backup record {backup.backup_id}"
        raise RuntimeError(msg)
    return persisted


async def get_backup_settings(org_id: UUID) -> BackupSettingsRecord:
    settings = await _get_backup_settings_for_org(org_id)
    if settings is not None:
        return settings
    return await _save_backup_settings(
        BackupSettingsRecord(
            organization_id=org_id,
            include_database_dump=_database_dump_supported(),
        )
    )


async def update_backup_settings(
    org_id: UUID,
    *,
    enabled: bool | None = None,
    schedule: str | None = None,
    retention_days: int | None = None,
    include_database_dump: bool | None = None,
    include_graph: bool | None = None,
) -> BackupSettingsRecord:
    settings = await get_backup_settings(org_id)
    if enabled is not None:
        settings.enabled = enabled
    if schedule is not None:
        settings.schedule = schedule
    if retention_days is not None:
        settings.retention_days = retention_days
    if include_database_dump is not None:
        settings.include_database_dump = _effective_include_database_dump(include_database_dump)
    if include_graph is not None:
        settings.include_graph = include_graph
    return await _save_backup_settings(settings)


async def create_backup_record(
    *,
    org_id: UUID,
    backup_id: str,
    include_database_dump: bool = True,
    include_graph: bool,
    created_by_user_id: UUID | None,
    triggered_by: str = "manual",
) -> BackupRecord:
    return await _save_backup(
        BackupRecord(
            id=uuid4(),
            organization_id=org_id,
            backup_id=backup_id,
            status=BackupStatus.PENDING.value,
            include_database_dump=_effective_include_database_dump(include_database_dump),
            include_graph=include_graph,
            triggered_by=triggered_by,
            created_by_user_id=created_by_user_id,
        )
    )


async def attach_backup_job(record_id: UUID, job_id: str) -> BackupRecord:
    backup = await _get_backup_by_record_id(record_id)
    if backup is None:
        raise HTTPException(status_code=404, detail="Backup record not found")
    backup.job_id = job_id
    return await _save_backup(backup)


async def list_backups(org_id: UUID, *, limit: int, offset: int) -> BackupListResult:
    total = await _count_query(
        "SELECT count() AS total FROM backups WHERE organization_id = $organization_id GROUP ALL;",
        organization_id=str(org_id),
    )
    rows = await _execute_query(
        "SELECT * FROM backups WHERE organization_id = $organization_id "
        "ORDER BY created_at DESC, uuid DESC START $offset LIMIT $limit;",
        organization_id=str(org_id),
        offset=max(offset, 0),
        limit=max(limit, 0),
    )
    backups = [_backup_from_record(row) for row in rows]
    return BackupListResult(backups=backups, total=total)


async def get_backup(org_id: UUID, backup_id: str) -> BackupRecord:
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


async def delete_backup_record(org_id: UUID, backup_id: str) -> BackupRecord:
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
) -> BackupRecord | None:
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


async def list_enabled_backup_settings() -> list[BackupSettingsRecord]:
    rows = await _execute_query("SELECT * FROM backup_settings WHERE enabled = true;")
    settings = [_backup_settings_from_record(row) for row in rows]
    settings.sort(key=lambda item: str(item.organization_id))
    return settings
