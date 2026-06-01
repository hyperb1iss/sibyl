"""Content archive export/import helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from sibyl import config as config_module
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.records import (
    normalize_records as _normalize_records,
    query_error as _query_error,
    raise_on_error as _raise_on_error,
)

CONTENT_ARCHIVE_VERSION = "1.0"


@dataclass(frozen=True)
class ContentArchiveTableSpec:
    name: str
    source_identity_field: str
    target_identity_field: str
    select_sql: str
    delete_by_identity_sql: str
    delete_all_sql: str
    create_sql: str


_CONTENT_ARCHIVE_TABLE_SPECS = (
    ContentArchiveTableSpec(
        name="crawl_sources",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM crawl_sources ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM crawl_sources WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM crawl_sources;",
        create_sql="CREATE crawl_sources CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="crawled_documents",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM crawled_documents ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM crawled_documents WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM crawled_documents;",
        create_sql="CREATE crawled_documents CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="document_chunks",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM document_chunks ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM document_chunks WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM document_chunks;",
        create_sql="CREATE document_chunks CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="entity",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM entity ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM entity WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM entity;",
        create_sql="CREATE entity CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="raw_captures",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM raw_captures ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM raw_captures WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM raw_captures;",
        create_sql="CREATE raw_captures CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="api_idempotency_records",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM api_idempotency_records ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM api_idempotency_records WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM api_idempotency_records;",
        create_sql="CREATE api_idempotency_records CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="source_imports",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM source_imports ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM source_imports WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM source_imports;",
        create_sql="CREATE source_imports CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="content_changefeed_cursors",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM content_changefeed_cursors ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM content_changefeed_cursors WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM content_changefeed_cursors;",
        create_sql="CREATE content_changefeed_cursors CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="derived_from",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM derived_from ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM derived_from WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM derived_from;",
        create_sql="CREATE derived_from CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="chunk_of",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM chunk_of ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM chunk_of WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM chunk_of;",
        create_sql="CREATE chunk_of CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="supersedes",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM supersedes ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM supersedes WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM supersedes;",
        create_sql="CREATE supersedes CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="extracted_into",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM extracted_into ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM extracted_into WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM extracted_into;",
        create_sql="CREATE extracted_into CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="system_settings",
        source_identity_field="key",
        target_identity_field="key",
        select_sql="SELECT * FROM system_settings ORDER BY key ASC;",
        delete_by_identity_sql="DELETE FROM system_settings WHERE key = $identity;",
        delete_all_sql="DELETE FROM system_settings;",
        create_sql="CREATE system_settings CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="telemetry_rollups",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM telemetry_rollups ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM telemetry_rollups WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM telemetry_rollups;",
        create_sql="CREATE telemetry_rollups CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="backup_settings",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM backup_settings ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM backup_settings WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM backup_settings;",
        create_sql="CREATE backup_settings CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="backups",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM backups ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM backups WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM backups;",
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
_GLOBAL_CONTENT_ARCHIVE_TABLES = frozenset({"system_settings", "telemetry_rollups"})
_CONTENT_RELATION_ARCHIVE_TABLES = frozenset(
    {"derived_from", "chunk_of", "supersedes", "extracted_into"}
)


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


def _normalize_backup_archive_flags(row: dict[str, object]) -> dict[str, object]:
    normalized = dict(row)
    normalized.pop("include_postgres", None)
    normalized["include_database_dump"] = False
    normalized["include_graph"] = True
    return normalized


def _normalize_content_archive_export_row(
    spec: ContentArchiveTableSpec,
    row: dict[str, object],
) -> dict[str, object]:
    normalized = dict(row)
    if spec.name not in _BACKUP_ARCHIVE_TABLES:
        return normalized

    return _normalize_backup_archive_flags(normalized)


def _normalize_content_archive_restore_row(
    spec: ContentArchiveTableSpec,
    row: dict[str, object],
) -> dict[str, object]:
    normalized = dict(row)
    if spec.name not in _BACKUP_ARCHIVE_TABLES:
        return normalized

    return _normalize_backup_archive_flags(normalized)


def _content_archive_export_query(
    spec: ContentArchiveTableSpec,
    organization_id: str | None,
) -> tuple[str, dict[str, object]] | None:
    if organization_id is None:
        return _SELECT_SURREAL_TABLE_ROWS[spec.name], {}
    if spec.name in _GLOBAL_CONTENT_ARCHIVE_TABLES:
        return None
    return (
        f"SELECT * FROM {spec.name} WHERE organization_id = $organization_id;",  # noqa: S608
        {"organization_id": organization_id},
    )


async def _clean_content_archive_rows(
    client: SurrealContentClient,
    organization_id: str | None,
) -> None:
    wipe_specs = sorted(
        _CONTENT_ARCHIVE_TABLE_SPECS,
        key=lambda spec: spec.name not in _CONTENT_RELATION_ARCHIVE_TABLES,
    )
    if organization_id is None:
        wipe_sql = "\n".join(spec.delete_all_sql for spec in wipe_specs)
        wipe_result = await client.execute_query_raw(
            f"BEGIN TRANSACTION;\n{wipe_sql}\nCOMMIT TRANSACTION;",
        )
        _raise_on_error(wipe_result, query="restore_content_archive_payload:clean")
        return

    wipe_sql = "\n".join(
        f"DELETE FROM {spec.name} WHERE organization_id = $organization_id;"  # noqa: S608
        for spec in wipe_specs
        if spec.name not in _GLOBAL_CONTENT_ARCHIVE_TABLES
    )
    wipe_result = await client.execute_query_raw(
        f"BEGIN TRANSACTION;\n{wipe_sql}\nCOMMIT TRANSACTION;",
        organization_id=organization_id,
    )
    _raise_on_error(wipe_result, query="restore_content_archive_payload:clean_org")


async def _export_surreal_content_archive_payload(
    organization_id: str | UUID | None = None,
) -> dict[str, object]:
    tables: dict[str, list[dict[str, object]]] = {}
    row_counts: dict[str, int] = {}
    client = build_surreal_content_client()

    try:
        org_id = str(organization_id) if organization_id is not None else None
        for spec in _CONTENT_ARCHIVE_TABLE_SPECS:
            query = _content_archive_export_query(spec, org_id)
            if query is None:
                tables[spec.name] = []
                row_counts[spec.name] = 0
                continue
            statement, params = query
            result = await client.execute_query(statement, **params)
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
        "organization_id": str(organization_id) if organization_id is not None else None,
        "tables": tables,
        "row_counts": row_counts,
        "total_rows": sum(row_counts.values()),
    }


async def export_content_archive_payload(
    organization_id: str | UUID | None = None,
) -> dict[str, object]:
    """Export Surreal content/operations tables into a JSON-safe payload."""

    return await _export_surreal_content_archive_payload(organization_id)


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
    payload_org_id = payload.get("organization_id")
    organization_id = str(payload_org_id).strip() if payload_org_id is not None else None
    if organization_id == "":
        organization_id = None

    client = build_surreal_content_client()
    tables_restored = 0
    rows_restored = 0
    errors: list[str] = []

    try:
        await bootstrap_content_schema(client, reset=False)
        if clean:
            await _clean_content_archive_rows(client, organization_id)

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

                # Delete+create must be atomic: a create that fails after the
                # delete would otherwise drop the existing row on a restore, the
                # worst outcome on the DR path. The transaction rolls the delete
                # back so the prior row survives a failed replace.
                restore_row_sql = (
                    f"BEGIN TRANSACTION;\n"
                    f"{spec.delete_by_identity_sql}\n"
                    f"{spec.create_sql}\n"
                    f"COMMIT TRANSACTION;"
                )
                try:
                    result = await client.execute_query_raw(
                        restore_row_sql,
                        identity=identity,
                        record=record,
                    )
                    _raise_on_error(result, query=restore_row_sql)
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
