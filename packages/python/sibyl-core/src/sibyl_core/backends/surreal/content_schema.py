"""SurrealDB schema bootstrap for Sibyl content storage."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sibyl_core.backends.surreal.schema import render_fulltext_compatible_sql
from sibyl_core.backends.surreal.schema_helpers import split_statements
from sibyl_core.backends.surreal.schema_version import (
    SCHEMA_VERSION_TABLE,
    SchemaMigration,
    apply_schema_migrations,
    ensure_schema_version_table,
    get_schema_version,
)
from sibyl_core.config import core_config
from sibyl_core.models.sources import CrawlStatus, SourceType
from sibyl_core.services.surreal_content import MemoryScope

# Document chunks use the OpenAI embedder dimension (text-embedding-3-small = 1536),
# which differs from the graph node embedder dimension. Keep them as separate
# constants so a graph dim change can't silently break content search and vice versa.
EMBEDDING_DIM = core_config.embedding_dimensions

if TYPE_CHECKING:
    from sibyl_core.backends.surreal.content_client import SurrealContentClient


CONTENT_TABLES = (
    "crawl_sources",
    "crawled_documents",
    "document_chunks",
    "raw_captures",
    "api_idempotency_records",
    "source_imports",
    "system_settings",
    "telemetry_rollups",
    "backup_settings",
    "backups",
)
CONTENT_SCHEMA_CURRENT_VERSION = 8
CONTENT_SCHEMA_NAME = "content"
_SCHEMA_CHECK_BATCH_SIZE = 128
_CONTENT_MEMORY_SCOPE_VALUES = tuple(scope.value for scope in MemoryScope)
_CONTENT_REVIEW_STATE_VALUES = (
    "pending",
    "deferred",
    "promoted",
    "archived",
    "deleted",
    "duplicate",
    "hidden",
    "redacted",
    "sensitive",
    "stale",
    "superseded",
    "wrong",
)
_CONTENT_SOURCE_IMPORT_STATUS_VALUES = (
    "pending",
    "running",
    "paused",
    "completed",
    "failed",
    "canceled",
)
_CONTENT_BACKUP_STATUS_VALUES = ("pending", "in_progress", "completed", "failed")

_SCHEMA_DIR = Path(__file__).with_name("schemas") / "content"


def _surql_string_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(f"'{value}'" for value in values) + "]"


def _load_schema_file(filename: str) -> str:
    return (_SCHEMA_DIR / filename).read_text(encoding="utf-8").format(EMBEDDING_DIM=EMBEDDING_DIM)


CONTENT_ANALYZER_DEFINITIONS = _load_schema_file("01_analyzers.surql")
CONTENT_SCHEMA_DEFINITIONS = _load_schema_file("10_tables.surql")

CONTENT_SOURCE_URL_SCOPE_MIGRATION_DEFINITIONS = """
REMOVE INDEX IF EXISTS idx_crawl_sources_url ON TABLE crawl_sources;
DEFINE INDEX IF NOT EXISTS idx_crawl_sources_org_url
    ON crawl_sources FIELDS organization_id, url UNIQUE;
"""

CONTENT_DOCUMENT_URL_SCOPE_MIGRATION_DEFINITIONS = """
REMOVE INDEX IF EXISTS idx_crawled_documents_url ON TABLE crawled_documents;
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_source_url
    ON crawled_documents FIELDS source_id, url UNIQUE;
"""

CONTENT_CHILD_SCOPE_MIGRATION_DEFINITIONS = """
DEFINE FIELD IF NOT EXISTS organization_id ON crawled_documents TYPE option<string> DEFAULT '';
DEFINE FIELD IF NOT EXISTS organization_id ON document_chunks TYPE option<string> DEFAULT '';
DEFINE FIELD IF NOT EXISTS source_id ON document_chunks TYPE option<string> DEFAULT '';
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_org_source
    ON crawled_documents FIELDS organization_id, source_id;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_org_source
    ON document_chunks FIELDS organization_id, source_id;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_org_source_entities
    ON document_chunks FIELDS organization_id, source_id, has_entities;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_org_document
    ON document_chunks FIELDS organization_id, document_id;
"""

CONTENT_ENUM_ASSERTION_MIGRATION_DEFINITIONS = f"""
UPDATE crawl_sources SET source_type = 'website' WHERE source_type = NONE OR source_type = '';
UPDATE crawl_sources SET crawl_status = 'pending' WHERE crawl_status = NONE OR crawl_status = '';
UPDATE raw_captures SET memory_scope = 'private' WHERE memory_scope = NONE OR memory_scope = '';
UPDATE raw_captures SET review_state = 'pending' WHERE review_state = NONE OR review_state = '';
UPDATE source_imports SET status = 'pending' WHERE status = NONE OR status = '';
UPDATE backups SET status = 'pending' WHERE status = NONE OR status = '';

DEFINE FIELD OVERWRITE source_type ON crawl_sources TYPE string DEFAULT 'website'
    ASSERT $value IN {_surql_string_array(tuple(source_type.value for source_type in SourceType))};
DEFINE FIELD OVERWRITE crawl_status ON crawl_sources TYPE string DEFAULT 'pending'
    ASSERT $value IN {_surql_string_array(tuple(status.value for status in CrawlStatus))};
DEFINE FIELD OVERWRITE memory_scope ON raw_captures TYPE string DEFAULT 'private'
    ASSERT $value IN {_surql_string_array(_CONTENT_MEMORY_SCOPE_VALUES)};
DEFINE FIELD OVERWRITE review_state ON raw_captures TYPE string DEFAULT 'pending'
    ASSERT $value IN {_surql_string_array(_CONTENT_REVIEW_STATE_VALUES)};
DEFINE FIELD OVERWRITE target_memory_scope ON source_imports TYPE option<string>
    ASSERT $value = NONE OR $value IN {_surql_string_array(_CONTENT_MEMORY_SCOPE_VALUES)};
DEFINE FIELD OVERWRITE status ON source_imports TYPE string DEFAULT 'pending'
    ASSERT $value IN {_surql_string_array(_CONTENT_SOURCE_IMPORT_STATUS_VALUES)};
DEFINE FIELD OVERWRITE status ON backups TYPE string DEFAULT 'pending'
    ASSERT $value IN {_surql_string_array(_CONTENT_BACKUP_STATUS_VALUES)};
"""

CONTENT_PERMISSION_MIGRATION_DEFINITIONS = """
ALTER TABLE IF EXISTS crawl_sources PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS crawled_documents PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS document_chunks PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS raw_captures PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS api_idempotency_records PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS source_imports PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS system_settings PERMISSIONS NONE;
ALTER TABLE IF EXISTS telemetry_rollups PERMISSIONS NONE;
ALTER TABLE IF EXISTS backup_settings PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS backups PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
"""

CONTENT_REVIEW_STATE_DEFERRED_MIGRATION_DEFINITIONS = f"""
DEFINE FIELD OVERWRITE review_state ON raw_captures TYPE string DEFAULT 'pending'
    ASSERT $value IN {_surql_string_array(_CONTENT_REVIEW_STATE_VALUES)};
"""

CONTENT_RAW_CAPTURE_INGESTION_MIGRATION_DEFINITIONS = f"""
DEFINE FIELD IF NOT EXISTS embedding ON raw_captures TYPE option<array<float, {EMBEDDING_DIM}>>;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_org_dedupe
    ON raw_captures FIELDS organization_id, metadata.dedupe_key;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_embedding ON raw_captures FIELDS embedding
    HNSW DIMENSION {EMBEDDING_DIM} DIST COSINE TYPE F32 EFC 150 M 12;
"""


def _content_schema_migrations(*, url: str) -> tuple[SchemaMigration, ...]:
    compatible_schema = render_fulltext_compatible_sql(
        CONTENT_SCHEMA_DEFINITIONS,
        url=url,
    )
    return (
        SchemaMigration(
            version=1,
            name="content_schema_bootstrap",
            statements=tuple(
                split_statements(CONTENT_ANALYZER_DEFINITIONS) + split_statements(compatible_schema)
            ),
        ),
        SchemaMigration(
            version=2,
            name="content_source_url_org_scope",
            statements=tuple(split_statements(CONTENT_SOURCE_URL_SCOPE_MIGRATION_DEFINITIONS)),
        ),
        SchemaMigration(
            version=3,
            name="content_document_url_source_scope",
            statements=tuple(split_statements(CONTENT_DOCUMENT_URL_SCOPE_MIGRATION_DEFINITIONS)),
        ),
        SchemaMigration(
            version=4,
            name="content_child_scope_fields",
            statements=tuple(split_statements(CONTENT_CHILD_SCOPE_MIGRATION_DEFINITIONS)),
        ),
        SchemaMigration(
            version=5,
            name="content_enum_assertions",
            statements=tuple(split_statements(CONTENT_ENUM_ASSERTION_MIGRATION_DEFINITIONS)),
        ),
        SchemaMigration(
            version=6,
            name="content_table_permissions",
            statements=tuple(split_statements(CONTENT_PERMISSION_MIGRATION_DEFINITIONS)),
        ),
        SchemaMigration(
            version=7,
            name="content_review_state_deferred",
            statements=tuple(split_statements(CONTENT_REVIEW_STATE_DEFERRED_MIGRATION_DEFINITIONS)),
        ),
        SchemaMigration(
            version=8,
            name="content_raw_capture_ingestion_indexes",
            statements=tuple(split_statements(CONTENT_RAW_CAPTURE_INGESTION_MIGRATION_DEFINITIONS)),
        ),
    )


async def bootstrap_content_schema(client: SurrealContentClient, *, reset: bool = False) -> None:
    if reset:
        for table in (*CONTENT_TABLES, SCHEMA_VERSION_TABLE):
            await client.execute_query(f"REMOVE TABLE IF EXISTS {table};")

    await _assert_content_migrations_safe(client)
    await apply_schema_migrations(
        client.execute_query,
        _content_schema_migrations(url=getattr(client, "_url", "")),
        name=CONTENT_SCHEMA_NAME,
        scope="content_schema_migration",
    )


async def _assert_content_migrations_safe(client: SurrealContentClient) -> None:
    await ensure_schema_version_table(
        client.execute_query,
        scope="content_schema_migration_version",
    )
    current_version = await get_schema_version(client.execute_query, name=CONTENT_SCHEMA_NAME)
    if current_version < 2:
        duplicates = await _duplicate_count_rows(
            client,
            """
            SELECT organization_id, url, count() AS total
            FROM crawl_sources
            WHERE organization_id != NONE AND url != ''
            GROUP BY organization_id, url;
            """,
        )
        if duplicates:
            raise RuntimeError(
                "Cannot migrate crawl_sources URL uniqueness: duplicate "
                "organization_id/url rows exist"
            )
    if current_version < 3:
        duplicates = await _duplicate_count_rows(
            client,
            """
            SELECT source_id, url, count() AS total
            FROM crawled_documents
            WHERE source_id != NONE AND url != ''
            GROUP BY source_id, url;
            """,
        )
        if duplicates:
            raise RuntimeError(
                "Cannot migrate crawled_documents URL uniqueness: duplicate "
                "source_id/url rows exist"
            )
    if current_version < 4:
        missing_source_id = await _matching_rows(
            client,
            """
            SELECT uuid FROM crawled_documents
            WHERE source_id = NONE OR source_id = ''
            LIMIT 1;
            """,
        )
        if missing_source_id:
            raise RuntimeError(
                "Cannot migrate crawled_documents organization scope: source_id is missing"
            )
        orphan_source_id = await _missing_parent_reference(
            client,
            child_table="crawled_documents",
            child_field="source_id",
            parent_table="crawl_sources",
        )
        if orphan_source_id is not None:
            raise RuntimeError(
                "Cannot migrate crawled_documents organization scope: "
                "parent crawl_sources rows are missing"
            )
        missing_document_id = await _matching_rows(
            client,
            """
            SELECT uuid FROM document_chunks
            WHERE document_id = NONE OR document_id = ''
            LIMIT 1;
            """,
        )
        if missing_document_id:
            raise RuntimeError(
                "Cannot migrate document_chunks content scope: document_id is missing"
            )
        orphan_document_id = await _missing_parent_reference(
            client,
            child_table="document_chunks",
            child_field="document_id",
            parent_table="crawled_documents",
        )
        if orphan_document_id is not None:
            raise RuntimeError(
                "Cannot migrate document_chunks content scope: "
                "parent crawled_documents rows are missing"
            )
    if current_version < 5:
        enum_checks = (
            (
                "crawl_sources",
                "source_type",
                tuple(source_type.value for source_type in SourceType),
                True,
            ),
            ("crawl_sources", "crawl_status", tuple(status.value for status in CrawlStatus), True),
            ("raw_captures", "memory_scope", _CONTENT_MEMORY_SCOPE_VALUES, True),
            ("raw_captures", "review_state", _CONTENT_REVIEW_STATE_VALUES, True),
            ("source_imports", "target_memory_scope", _CONTENT_MEMORY_SCOPE_VALUES, True),
            ("source_imports", "status", _CONTENT_SOURCE_IMPORT_STATUS_VALUES, True),
            ("backups", "status", _CONTENT_BACKUP_STATUS_VALUES, True),
        )
        for table, field, allowed, optional in enum_checks:
            invalid_value = await _first_invalid_enum_value(
                client,
                table=table,
                field=field,
                allowed=allowed,
                optional=optional,
            )
            if invalid_value is not None:
                raise RuntimeError(
                    f"Cannot migrate {table}.{field} enum assertion: "
                    f"invalid existing value {invalid_value!r}"
                )


async def _matching_rows(
    client: SurrealContentClient,
    statement: str,
    **params: object,
) -> list[dict[str, object]]:
    from sibyl_core.backends.surreal.records import normalize_records

    return normalize_records(await client.execute_query(statement, **params))


async def _missing_parent_reference(
    client: SurrealContentClient,
    *,
    child_table: str,
    child_field: str,
    parent_table: str,
) -> str | None:
    child_values = await _distinct_field_values(client, table=child_table, field=child_field)
    for batch in _batches(child_values):
        rows = await _matching_rows(
            client,
            f"SELECT uuid FROM {parent_table} WHERE uuid INSIDE $values;",
            values=batch,
        )
        existing = {str(row["uuid"]) for row in rows if row.get("uuid") is not None}
        for value in batch:
            if value not in existing:
                return value
    return None


async def _distinct_field_values(
    client: SurrealContentClient,
    *,
    table: str,
    field: str,
) -> list[str]:
    rows = await _matching_rows(
        client,
        f"""
        SELECT {field}
        FROM {table}
        WHERE {field} != NONE AND {field} != ''
        GROUP BY {field};
        """,
    )
    values: set[str] = set()
    for row in rows:
        value = row.get(field)
        if value not in {None, ""}:
            values.add(str(value))
    return sorted(values)


async def _first_invalid_enum_value(
    client: SurrealContentClient,
    *,
    table: str,
    field: str,
    allowed: tuple[str, ...],
    optional: bool,
) -> str | None:
    rows = await _matching_rows(
        client,
        f"""
        SELECT {field}
        FROM {table}
        GROUP BY {field};
        """,
    )
    allowed_values = set(allowed)
    for row in rows:
        value = row.get(field)
        if value in {None, ""}:
            if optional:
                continue
            return "" if value == "" else "NONE"
        normalized = str(value)
        if normalized not in allowed_values:
            return normalized
    return None


def _batches(values: list[str]) -> list[list[str]]:
    return [
        values[index : index + _SCHEMA_CHECK_BATCH_SIZE]
        for index in range(0, len(values), _SCHEMA_CHECK_BATCH_SIZE)
    ]


async def _duplicate_count_rows(
    client: SurrealContentClient,
    statement: str,
) -> list[dict[str, object]]:
    rows = await _matching_rows(client, statement)
    return [row for row in rows if _coerce_int(row.get("total")) > 1]


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value:
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


__all__ = [
    "CONTENT_ANALYZER_DEFINITIONS",
    "CONTENT_CHILD_SCOPE_MIGRATION_DEFINITIONS",
    "CONTENT_DOCUMENT_URL_SCOPE_MIGRATION_DEFINITIONS",
    "CONTENT_ENUM_ASSERTION_MIGRATION_DEFINITIONS",
    "CONTENT_PERMISSION_MIGRATION_DEFINITIONS",
    "CONTENT_REVIEW_STATE_DEFERRED_MIGRATION_DEFINITIONS",
    "CONTENT_SCHEMA_CURRENT_VERSION",
    "CONTENT_SCHEMA_DEFINITIONS",
    "CONTENT_SCHEMA_NAME",
    "CONTENT_SOURCE_URL_SCOPE_MIGRATION_DEFINITIONS",
    "CONTENT_TABLES",
    "bootstrap_content_schema",
]
