"""Content archive export/import helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from sqlalchemy import text

from sibyl import config as config_module
from sibyl.db.connection import get_session
from sibyl.persistence.surreal.content import _normalize_records
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema

CONTENT_ARCHIVE_VERSION = "1.0"


@dataclass(frozen=True)
class ContentArchiveTableSpec:
    name: str
    source_identity_field: str
    target_identity_field: str
    select_sql: str
    delete_by_identity_sql: str
    create_sql: str


_CONTENT_ARCHIVE_TABLE_SPECS = (
    ContentArchiveTableSpec(
        name="crawl_sources",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM crawl_sources ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM crawl_sources WHERE uuid = $identity;",
        create_sql="CREATE crawl_sources CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="crawled_documents",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM crawled_documents ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM crawled_documents WHERE uuid = $identity;",
        create_sql="CREATE crawled_documents CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="document_chunks",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM document_chunks ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM document_chunks WHERE uuid = $identity;",
        create_sql="CREATE document_chunks CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="raw_captures",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM raw_captures ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM raw_captures WHERE uuid = $identity;",
        create_sql="CREATE raw_captures CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="system_settings",
        source_identity_field="key",
        target_identity_field="key",
        select_sql="SELECT * FROM system_settings ORDER BY key ASC;",
        delete_by_identity_sql="DELETE FROM system_settings WHERE key = $identity;",
        create_sql="CREATE system_settings CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="backup_settings",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM backup_settings ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM backup_settings WHERE uuid = $identity;",
        create_sql="CREATE backup_settings CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="backups",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM backups ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM backups WHERE uuid = $identity;",
        create_sql="CREATE backups CONTENT $record;",
    ),
)

CONTENT_ARCHIVE_TABLES = tuple(spec.name for spec in _CONTENT_ARCHIVE_TABLE_SPECS)
_CONTENT_ARCHIVE_TABLES_BY_NAME = {spec.name: spec for spec in _CONTENT_ARCHIVE_TABLE_SPECS}
_SELECT_SURREAL_TABLE_ROWS = {
    spec.name: f"SELECT * FROM {spec.name};"  # noqa: S608 - table names are fixed constants
    for spec in _CONTENT_ARCHIVE_TABLE_SPECS
}
_BACKUP_ARCHIVE_TABLES = frozenset({"backup_settings", "backups"})


@dataclass(frozen=True)
class ContentArchiveRestoreResult:
    """Summary of one content archive restore."""

    success: bool
    tables_restored: int
    rows_restored: int
    errors: list[str] = field(default_factory=list)


def build_surreal_content_client() -> SurrealContentClient:
    """Build a Surreal content client from application settings."""

    return SurrealContentClient(
        url=config_module.settings.resolved_surreal_url,
        username=config_module.settings.surreal_username,
        password=config_module.settings.surreal_password.get_secret_value(),
        token=config_module.settings.surreal_token.get_secret_value(),
    )


def _serialize_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return _serialize_value(value.value)
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_serialize_value(item) for item in value]
    return value


def _deserialize_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _deserialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deserialize_value(item) for item in value]
    if isinstance(value, str):
        if "T" not in value and not value.endswith("Z"):
            return value
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return value
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    return value


def _deserialize_vector(value: object) -> list[float] | object:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text.startswith("[") or not text.endswith("]"):
        return value
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return value
    if not isinstance(parsed, list):
        return value
    out: list[float] = []
    for item in parsed:
        if isinstance(item, bool):
            return value
        if isinstance(item, int | float):
            out.append(float(item))
            continue
        return value
    return out


def _query_error(result: object) -> str | None:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        status = payload.get("status")
        if isinstance(status, str) and status.upper() == "ERR":
            detail = payload.get("detail") or payload.get("result") or payload
            return str(detail)
        return None
    if not isinstance(result, list):
        return None
    for item in result:
        error = _query_error(item)
        if error is not None:
            return error
    return None


def _sort_content_rows(
    spec: ContentArchiveTableSpec,
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    def _key(row: dict[str, object]) -> str:
        value = row.get(spec.target_identity_field) or row.get(spec.source_identity_field)
        if value is not None:
            return str(value)
        return json.dumps(row, sort_keys=True, default=str)

    return sorted(rows, key=_key)


def _coerce_archive_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    return None


def _resolve_archive_database_dump_value(row: dict[str, object]) -> bool | None:
    return _coerce_archive_bool(row.get("include_database_dump"))


def _normalize_content_archive_export_row(
    spec: ContentArchiveTableSpec,
    row: dict[str, object],
) -> dict[str, object]:
    normalized = dict(row)
    if spec.name not in _BACKUP_ARCHIVE_TABLES:
        return normalized

    include_database_dump = _resolve_archive_database_dump_value(normalized)
    if include_database_dump is None:
        return normalized

    normalized["include_database_dump"] = include_database_dump
    return normalized


def _normalize_content_archive_restore_row(
    spec: ContentArchiveTableSpec,
    row: dict[str, object],
) -> dict[str, object]:
    normalized = dict(row)
    if spec.name not in _BACKUP_ARCHIVE_TABLES:
        return normalized

    include_database_dump = _resolve_archive_database_dump_value(normalized)
    if include_database_dump is None:
        normalized.pop("include_database_dump", None)
        return normalized

    normalized["include_database_dump"] = include_database_dump
    return normalized


async def _export_relational_content_archive_payload() -> dict[str, object]:
    tables: dict[str, list[dict[str, object]]] = {}
    row_counts: dict[str, int] = {}

    async with get_session() as session:
        for spec in _CONTENT_ARCHIVE_TABLE_SPECS:
            result = await session.execute(text(spec.select_sql))
            rows = [
                _normalize_content_archive_export_row(
                    spec,
                    {str(key): _serialize_value(value) for key, value in dict(row).items()},
                )
                for row in result.mappings().all()
            ]
            tables[spec.name] = rows
            row_counts[spec.name] = len(rows)

    return {
        "version": CONTENT_ARCHIVE_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "tables": tables,
        "row_counts": row_counts,
        "total_rows": sum(row_counts.values()),
    }


async def _export_surreal_content_archive_payload() -> dict[str, object]:
    tables: dict[str, list[dict[str, object]]] = {}
    row_counts: dict[str, int] = {}
    client = build_surreal_content_client()

    try:
        for spec in _CONTENT_ARCHIVE_TABLE_SPECS:
            result = await client.execute_query(_SELECT_SURREAL_TABLE_ROWS[spec.name])
            error = _query_error(result)
            if error is not None:
                raise RuntimeError(error)
            rows = _sort_content_rows(
                spec,
                [
                    _normalize_content_archive_export_row(
                        spec,
                        {str(key): _serialize_value(value) for key, value in row.items()},
                    )
                    for row in _normalize_records(result)
                ],
            )
            tables[spec.name] = rows
            row_counts[spec.name] = len(rows)
    finally:
        await client.close()

    return {
        "version": CONTENT_ARCHIVE_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "tables": tables,
        "row_counts": row_counts,
        "total_rows": sum(row_counts.values()),
    }


async def export_content_archive_payload() -> dict[str, object]:
    """Export content/operations tables from the active content backend."""

    if config_module.settings.store == "surreal":
        return await _export_surreal_content_archive_payload()
    return await _export_relational_content_archive_payload()


async def restore_content_archive_payload(
    payload: dict[str, object],
    *,
    clean: bool = False,
) -> ContentArchiveRestoreResult:
    """Restore a content payload into the Surreal content namespace."""

    raw_tables = payload.get("tables")
    if not isinstance(raw_tables, dict):
        raise TypeError("content archive payload is missing a tables object")
    tables = {str(key): value for key, value in raw_tables.items()}

    client = build_surreal_content_client()
    tables_restored = 0
    rows_restored = 0
    errors: list[str] = []

    try:
        await bootstrap_content_schema(client, reset=clean)

        for spec in _CONTENT_ARCHIVE_TABLE_SPECS:
            rows = tables.get(spec.name)
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                errors.append(f"{spec.name} payload must be a list")
                continue
            if not rows:
                continue

            tables_restored += 1
            for row in rows:
                if not isinstance(row, dict):
                    errors.append(f"{spec.name} row payload must be an object")
                    continue
                normalized_row = _normalize_content_archive_restore_row(
                    spec,
                    {str(key): value for key, value in row.items()},
                )
                identity = str(
                    normalized_row.get(spec.source_identity_field)
                    or normalized_row.get(spec.target_identity_field)
                    or ""
                ).strip()
                if not identity:
                    errors.append(f"{spec.name} row is missing {spec.source_identity_field}")
                    continue

                record = {
                    key: _deserialize_value(value)
                    for key, value in normalized_row.items()
                    if not (
                        spec.source_identity_field != spec.target_identity_field
                        and key == spec.source_identity_field
                    )
                }
                if spec.name == "document_chunks":
                    record["embedding"] = _deserialize_vector(record.get("embedding"))
                record[spec.target_identity_field] = identity

                try:
                    delete_result = await client.execute_query(
                        spec.delete_by_identity_sql,
                        identity=identity,
                    )
                    delete_error = _query_error(delete_result)
                    if delete_error is not None:
                        raise RuntimeError(delete_error)
                    create_result = await client.execute_query(spec.create_sql, record=record)
                    create_error = _query_error(create_result)
                    if create_error is not None:
                        raise RuntimeError(create_error)
                    rows_restored += 1
                except Exception as exc:
                    errors.append(f"{spec.name}:{identity}: {exc}")

        return ContentArchiveRestoreResult(
            success=not errors,
            tables_restored=tables_restored,
            rows_restored=rows_restored,
            errors=errors[:50],
        )
    finally:
        await client.close()


__all__ = [
    "CONTENT_ARCHIVE_TABLES",
    "CONTENT_ARCHIVE_VERSION",
    "ContentArchiveRestoreResult",
    "build_surreal_content_client",
    "export_content_archive_payload",
    "restore_content_archive_payload",
]
