"""Active backup adapters for the current persistence runtime."""

from __future__ import annotations

from collections.abc import Awaitable
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Protocol, cast

from sibyl.config import settings


class RuntimeExport(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> Awaitable[object]: ...


if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sibyl.persistence.backups_common import (
        BackupListResult,
        BackupRecord,
        BackupSettingsRecord,
    )

    class AttachBackupJob(Protocol):
        def __call__(self, record_id: UUID, job_id: str) -> Awaitable[BackupRecord]: ...

    class CreateBackupRecord(Protocol):
        def __call__(
            self,
            *,
            org_id: UUID,
            backup_id: str,
            include_database_dump: bool = True,
            include_graph: bool,
            created_by_user_id: UUID | None,
            triggered_by: str = "manual",
        ) -> Awaitable[BackupRecord]: ...

    class DeleteBackupRecord(Protocol):
        def __call__(self, org_id: UUID, backup_id: str) -> Awaitable[BackupRecord]: ...

    class GetBackup(Protocol):
        def __call__(self, org_id: UUID, backup_id: str) -> Awaitable[BackupRecord]: ...

    class GetBackupRetention(Protocol):
        def __call__(
            self, org_id: UUID, requested_retention: int | None
        ) -> Awaitable[int]: ...

    class GetBackupSettings(Protocol):
        def __call__(self, org_id: UUID) -> Awaitable[BackupSettingsRecord]: ...

    class ListBackups(Protocol):
        def __call__(
            self, org_id: UUID, *, limit: int, offset: int
        ) -> Awaitable[BackupListResult]: ...

    class ListEnabledBackupSettings(Protocol):
        def __call__(self) -> Awaitable[list[BackupSettingsRecord]]: ...

    class UpdateBackupRecord(Protocol):
        def __call__(
            self,
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
        ) -> Awaitable[BackupRecord | None]: ...

    class UpdateBackupSettings(Protocol):
        def __call__(
            self,
            org_id: UUID,
            *,
            enabled: bool | None = None,
            schedule: str | None = None,
            retention_days: int | None = None,
            include_database_dump: bool | None = None,
            include_graph: bool | None = None,
        ) -> Awaitable[BackupSettingsRecord]: ...

    attach_backup_job: AttachBackupJob
    create_backup_record: CreateBackupRecord
    delete_backup_record: DeleteBackupRecord
    get_backup: GetBackup
    get_backup_retention: GetBackupRetention
    get_backup_settings: GetBackupSettings
    list_backups: ListBackups
    list_enabled_backup_settings: ListEnabledBackupSettings
    update_backup_record: UpdateBackupRecord
    update_backup_settings: UpdateBackupSettings

_BACKEND_MODULES = {
    "legacy": "sibyl.persistence.legacy.backups",
    "surreal": "sibyl.persistence.surreal.backups",
}

_RUNTIME_EXPORTS = [
    "attach_backup_job",
    "create_backup_record",
    "delete_backup_record",
    "get_backup",
    "get_backup_retention",
    "get_backup_settings",
    "list_backups",
    "list_enabled_backup_settings",
    "update_backup_record",
    "update_backup_settings",
]

__all__ = list(_RUNTIME_EXPORTS)


def _backend_module() -> ModuleType:
    return import_module(_BACKEND_MODULES[settings.store])


def _make_runtime_proxy(name: str) -> RuntimeExport:
    async def _proxy(*args: object, **kwargs: object) -> object:
        export = cast("RuntimeExport", getattr(_backend_module(), name))
        return await export(*args, **kwargs)

    _proxy.__name__ = name
    return cast("RuntimeExport", _proxy)


for _export_name in _RUNTIME_EXPORTS:
    if _export_name not in globals():
        globals()[_export_name] = _make_runtime_proxy(_export_name)
