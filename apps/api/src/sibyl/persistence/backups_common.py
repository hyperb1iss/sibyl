"""Shared backup runtime DTOs."""

from __future__ import annotations

from dataclasses import dataclass

from sibyl.db.models import Backup


@dataclass(frozen=True, slots=True)
class BackupListResult:
    backups: list[Backup]
    total: int


@dataclass(frozen=True, slots=True)
class BackupRuntimeOptions:
    include_postgres: bool
    include_graph: bool
    include_auth_snapshot: bool
    include_content_snapshot: bool
    postgres_dump_supported: bool
    archive_contents: tuple[str, ...]

    @property
    def include_database_dump(self) -> bool:
        return self.include_postgres

    @property
    def database_dump_supported(self) -> bool:
        return self.postgres_dump_supported


def resolve_backup_runtime_options(
    *,
    store: str,
    auth_store: str,
    include_postgres: bool | None = None,
    include_graph: bool = True,
) -> BackupRuntimeOptions:
    postgres_dump_supported = not (store == "surreal" and auth_store == "surreal")
    include_postgres = (True if include_postgres is None else include_postgres) and (
        postgres_dump_supported
    )
    include_auth_snapshot = auth_store == "surreal"
    include_content_snapshot = store == "surreal"

    contents: list[str] = []
    if include_postgres:
        contents.append("postgres.sql")
    if include_auth_snapshot:
        contents.append("auth.json")
    if include_content_snapshot:
        contents.append("content.json")
    if include_graph:
        contents.append("graph.json")
    contents.append("metadata.json")

    return BackupRuntimeOptions(
        include_postgres=include_postgres,
        include_graph=include_graph,
        include_auth_snapshot=include_auth_snapshot,
        include_content_snapshot=include_content_snapshot,
        postgres_dump_supported=postgres_dump_supported,
        archive_contents=tuple(contents),
    )


LegacyBackupList = BackupListResult
