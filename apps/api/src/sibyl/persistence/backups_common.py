"""Shared backup runtime DTOs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field as dataclass_field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(slots=True)
class BackupSettingsRecord:
    organization_id: UUID
    id: UUID = dataclass_field(default_factory=uuid4)
    enabled: bool = True
    schedule: str = "0 2 * * *"
    retention_days: int = 30
    include_database_dump: bool = True
    include_graph: bool = True
    last_backup_at: datetime | None = None
    last_backup_id: str | None = None
    created_at: datetime = dataclass_field(default_factory=_utcnow_naive)
    updated_at: datetime = dataclass_field(default_factory=_utcnow_naive)


class BackupStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class BackupRecord:
    organization_id: UUID
    backup_id: str
    id: UUID = dataclass_field(default_factory=uuid4)
    status: str = BackupStatus.PENDING.value
    job_id: str | None = None
    filename: str | None = None
    file_path: str | None = None
    size_bytes: int = 0
    include_database_dump: bool = True
    include_graph: bool = True
    entity_count: int = 0
    relationship_count: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float = 0.0
    error: str | None = None
    triggered_by: str | None = None
    created_by_user_id: UUID | None = None
    created_at: datetime = dataclass_field(default_factory=_utcnow_naive)
    updated_at: datetime = dataclass_field(default_factory=_utcnow_naive)


@dataclass(frozen=True, slots=True)
class BackupListResult:
    backups: list[BackupRecord]
    total: int


@dataclass(frozen=True, slots=True)
class BackupRuntimeOptions:
    include_database_dump: bool
    include_graph: bool
    include_auth_snapshot: bool
    include_content_snapshot: bool
    database_dump_supported: bool
    archive_contents: tuple[str, ...]


def resolve_object_database_dump(value: object) -> bool | None:
    return getattr(value, "include_database_dump", None)


def resolve_mapping_database_dump(
    value: Mapping[str, object],
    *,
    coerce: Callable[[object], bool | None],
) -> bool | None:
    return coerce(value.get("include_database_dump"))


def resolve_backup_runtime_options(
    *,
    store: str,
    auth_store: str,
    include_database_dump: bool | None = None,
    include_graph: bool = True,
) -> BackupRuntimeOptions:
    database_dump_supported = not (store == "surreal" and auth_store == "surreal")
    include_database_dump = (True if include_database_dump is None else include_database_dump) and (
        database_dump_supported
    )
    include_auth_snapshot = auth_store == "surreal"
    include_content_snapshot = store == "surreal"

    contents: list[str] = []
    if include_database_dump:
        contents.append("postgres.sql")
    if include_auth_snapshot:
        contents.append("auth.json")
    if include_content_snapshot:
        contents.append("content.json")
    if include_graph:
        contents.append("graph.json")
    contents.append("metadata.json")

    return BackupRuntimeOptions(
        include_database_dump=include_database_dump,
        include_graph=include_graph,
        include_auth_snapshot=include_auth_snapshot,
        include_content_snapshot=include_content_snapshot,
        database_dump_supported=database_dump_supported,
        archive_contents=tuple(contents),
    )


__all__ = [
    "BackupListResult",
    "BackupRecord",
    "BackupRuntimeOptions",
    "BackupSettingsRecord",
    "BackupStatus",
    "resolve_backup_runtime_options",
    "resolve_mapping_database_dump",
    "resolve_object_database_dump",
]
