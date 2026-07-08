"""SurrealDB schema bootstrap for Sibyl content storage."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sibyl_core.backends.surreal.schema import render_fulltext_compatible_sql
from sibyl_core.backends.surreal.schema_helpers import is_missing_table_error, split_statements
from sibyl_core.backends.surreal.schema_version import (
    SCHEMA_VERSION_TABLE,
    SchemaMigration,
    apply_schema_migrations,
    ensure_schema_version_table,
    get_schema_version,
)
from sibyl_core.config import core_config
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.models.sources import CrawlStatus, SourceType

# Document chunks use the OpenAI embedder dimension (text-embedding-3-small = 1536),
# which differs from the graph node embedder dimension. Keep them as separate
# constants so a graph dim change can't silently break content search and vice versa.
EMBEDDING_DIM = core_config.embedding_dimensions

if TYPE_CHECKING:
    from sibyl_core.backends.surreal.content_client import SurrealContentClient


CONTENT_RELATION_TABLES = (
    "derived_from",
    "chunk_of",
    "supersedes",
    "extracted_into",
)
CONTENT_TABLES = (
    *CONTENT_RELATION_TABLES,
    "entity",
    "crawl_sources",
    "crawled_documents",
    "document_chunks",
    "raw_captures",
    "memory_usage_events",
    "api_idempotency_records",
    "source_imports",
    "content_changefeed_cursors",
    "system_settings",
    "telemetry_rollups",
    "backup_settings",
    "backups",
)
CONTENT_SCHEMA_CURRENT_VERSION = 17
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
UPDATE raw_captures SET
    source_id = source_id ?? '',
    principal_id = principal_id ?? '',
    title = title ?? '',
    raw_content = raw_content ?? '',
    entity_type = entity_type ?? '',
    tags = tags ?? [],
    metadata = metadata ?? {{}},
    provenance = provenance ?? {{}},
    captured_at = captured_at ?? created_at ?? time::now(),
    created_at = created_at ?? captured_at ?? time::now(),
    memory_scope = IF memory_scope = NONE OR memory_scope = '' THEN 'private' ELSE memory_scope END,
    review_state = IF review_state = NONE OR review_state = '' THEN 'pending' ELSE review_state END
WHERE source_id = NONE
    OR principal_id = NONE
    OR title = NONE
    OR raw_content = NONE
    OR entity_type = NONE
    OR tags = NONE
    OR metadata = NONE
    OR provenance = NONE
    OR captured_at = NONE
    OR created_at = NONE
    OR memory_scope = NONE
    OR memory_scope = ''
    OR review_state = NONE
    OR review_state = '';
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
ALTER TABLE IF EXISTS memory_usage_events PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS api_idempotency_records PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS source_imports PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS content_changefeed_cursors PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS system_settings PERMISSIONS NONE;
ALTER TABLE IF EXISTS telemetry_rollups PERMISSIONS NONE;
ALTER TABLE IF EXISTS backup_settings PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS backups PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS derived_from PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS chunk_of PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS supersedes PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS extracted_into PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
ALTER TABLE IF EXISTS entity PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
"""

CONTENT_RAW_CAPTURE_CHANGEFEED_MIGRATION_DEFINITIONS = """
ALTER TABLE IF EXISTS raw_captures CHANGEFEED 7d;

DEFINE TABLE IF NOT EXISTS content_changefeed_cursors SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON content_changefeed_cursors TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON content_changefeed_cursors TYPE string;
DEFINE FIELD IF NOT EXISTS table_name ON content_changefeed_cursors TYPE string;
DEFINE FIELD IF NOT EXISTS consumer_name ON content_changefeed_cursors TYPE string;
DEFINE FIELD IF NOT EXISTS versionstamp ON content_changefeed_cursors TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS metadata ON content_changefeed_cursors TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS created_at ON content_changefeed_cursors TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON content_changefeed_cursors TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_content_changefeed_cursors_uuid
    ON content_changefeed_cursors FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_content_changefeed_cursors_org_consumer
    ON content_changefeed_cursors FIELDS organization_id, table_name, consumer_name UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_content_changefeed_cursors_updated_at
    ON content_changefeed_cursors FIELDS updated_at;
ALTER TABLE IF EXISTS content_changefeed_cursors PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
"""

CONTENT_HIGHLIGHT_SNIPPET_MIGRATION_DEFINITIONS = f"""
{CONTENT_ANALYZER_DEFINITIONS}

DEFINE INDEX OVERWRITE idx_crawl_sources_name_ft
    ON crawl_sources FIELDS name FULLTEXT ANALYZER title_analyzer BM25 HIGHLIGHTS CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_crawled_documents_title_ft
    ON crawled_documents FIELDS title FULLTEXT ANALYZER title_analyzer BM25 HIGHLIGHTS CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_crawled_documents_content_ft
    ON crawled_documents FIELDS content FULLTEXT ANALYZER content_analyzer BM25 HIGHLIGHTS CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_document_chunks_content_ft
    ON document_chunks FIELDS content FULLTEXT ANALYZER content_analyzer BM25 HIGHLIGHTS CONCURRENTLY;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_code_ft
    ON document_chunks FIELDS content FULLTEXT ANALYZER code_analyzer BM25 HIGHLIGHTS CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_raw_captures_title_ft
    ON raw_captures FIELDS title FULLTEXT ANALYZER title_analyzer BM25 HIGHLIGHTS CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_raw_captures_content_ft
    ON raw_captures FIELDS raw_content FULLTEXT ANALYZER content_analyzer BM25 HIGHLIGHTS CONCURRENTLY;
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

CONTENT_LINEAGE_RELATION_MIGRATION_DEFINITIONS = """
DEFINE TABLE IF NOT EXISTS derived_from SCHEMAFULL TYPE RELATION IN raw_captures OUT source_imports ENFORCED;
DEFINE FIELD IF NOT EXISTS uuid ON derived_from TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON derived_from TYPE string;
DEFINE FIELD IF NOT EXISTS raw_memory_id ON derived_from TYPE string;
DEFINE FIELD IF NOT EXISTS source_import_id ON derived_from TYPE string;
DEFINE FIELD IF NOT EXISTS source_id ON derived_from TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON derived_from TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_derived_from_uuid ON derived_from FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_derived_from_org_raw ON derived_from FIELDS organization_id, raw_memory_id;
DEFINE INDEX IF NOT EXISTS idx_derived_from_org_import ON derived_from FIELDS organization_id, source_import_id;
ALTER TABLE IF EXISTS derived_from PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;

DEFINE TABLE IF NOT EXISTS chunk_of SCHEMAFULL TYPE RELATION IN document_chunks OUT crawled_documents ENFORCED;
DEFINE FIELD IF NOT EXISTS uuid ON chunk_of TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON chunk_of TYPE string;
DEFINE FIELD IF NOT EXISTS chunk_id ON chunk_of TYPE string;
DEFINE FIELD IF NOT EXISTS document_id ON chunk_of TYPE string;
DEFINE FIELD IF NOT EXISTS source_id ON chunk_of TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON chunk_of TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_chunk_of_uuid ON chunk_of FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_chunk_of_org_chunk ON chunk_of FIELDS organization_id, chunk_id;
DEFINE INDEX IF NOT EXISTS idx_chunk_of_org_document ON chunk_of FIELDS organization_id, document_id;
ALTER TABLE IF EXISTS chunk_of PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;

DEFINE TABLE IF NOT EXISTS supersedes SCHEMAFULL TYPE RELATION IN raw_captures OUT raw_captures ENFORCED;
DEFINE FIELD IF NOT EXISTS uuid ON supersedes TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON supersedes TYPE string;
DEFINE FIELD IF NOT EXISTS raw_memory_id ON supersedes TYPE string;
DEFINE FIELD IF NOT EXISTS superseded_raw_memory_id ON supersedes TYPE string;
DEFINE FIELD IF NOT EXISTS source_id ON supersedes TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON supersedes TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_supersedes_uuid ON supersedes FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_supersedes_org_raw ON supersedes FIELDS organization_id, raw_memory_id;
DEFINE INDEX IF NOT EXISTS idx_supersedes_org_superseded ON supersedes FIELDS organization_id, superseded_raw_memory_id;
ALTER TABLE IF EXISTS supersedes PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;

DEFINE TABLE IF NOT EXISTS extracted_into SCHEMAFULL TYPE RELATION IN entity OUT document_chunks;
DEFINE FIELD IF NOT EXISTS uuid ON extracted_into TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON extracted_into TYPE string;
DEFINE FIELD IF NOT EXISTS entity_id ON extracted_into TYPE string;
DEFINE FIELD IF NOT EXISTS chunk_id ON extracted_into TYPE string;
DEFINE FIELD IF NOT EXISTS document_id ON extracted_into TYPE string;
DEFINE FIELD IF NOT EXISTS source_id ON extracted_into TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON extracted_into TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_extracted_into_uuid ON extracted_into FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_extracted_into_org_entity ON extracted_into FIELDS organization_id, entity_id;
DEFINE INDEX IF NOT EXISTS idx_extracted_into_org_chunk ON extracted_into FIELDS organization_id, chunk_id;
ALTER TABLE IF EXISTS extracted_into PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
"""

CONTENT_EXTRACTED_INTO_RELATION_MIGRATION_DEFINITIONS = """
DEFINE TABLE IF NOT EXISTS extracted_into SCHEMAFULL TYPE RELATION IN entity OUT document_chunks;
DEFINE FIELD IF NOT EXISTS uuid ON extracted_into TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON extracted_into TYPE string;
DEFINE FIELD IF NOT EXISTS entity_id ON extracted_into TYPE string;
DEFINE FIELD IF NOT EXISTS chunk_id ON extracted_into TYPE string;
DEFINE FIELD IF NOT EXISTS document_id ON extracted_into TYPE string;
DEFINE FIELD IF NOT EXISTS source_id ON extracted_into TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON extracted_into TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_extracted_into_uuid ON extracted_into FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_extracted_into_org_entity ON extracted_into FIELDS organization_id, entity_id;
DEFINE INDEX IF NOT EXISTS idx_extracted_into_org_chunk ON extracted_into FIELDS organization_id, chunk_id;
ALTER TABLE IF EXISTS extracted_into PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
"""

CONTENT_ENTITY_ANCHOR_MIGRATION_DEFINITIONS = """
DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON entity TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON entity TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_content_entity_uuid ON entity FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_content_entity_org_uuid ON entity FIELDS organization_id, uuid UNIQUE;
ALTER TABLE IF EXISTS entity PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
"""

CONTENT_BACKUP_LEGACY_INCLUDE_CLEANUP_DEFINITIONS = """
REMOVE FIELD IF EXISTS include_postgres ON TABLE backup_settings;
REMOVE FIELD IF EXISTS include_postgres ON TABLE backups;
DEFINE FIELD OVERWRITE include_database_dump ON backup_settings TYPE option<bool>;
DEFINE FIELD OVERWRITE include_database_dump ON backups TYPE option<bool>;
DEFINE FIELD OVERWRITE include_graph ON backup_settings TYPE bool DEFAULT true;
DEFINE FIELD OVERWRITE include_graph ON backups TYPE bool DEFAULT true;
UPDATE backup_settings SET include_database_dump = false, include_graph = true;
UPDATE backups SET include_database_dump = false, include_graph = true;
"""

CONTENT_LOOKUP_INDEX_MIGRATION_DEFINITIONS = """
DEFINE INDEX IF NOT EXISTS idx_crawl_sources_org_uuid
    ON crawl_sources FIELDS organization_id, uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_crawl_sources_org_status_created
    ON crawl_sources FIELDS organization_id, crawl_status, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_crawled_documents_org_uuid
    ON crawled_documents FIELDS organization_id, uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_document_chunks_org_uuid
    ON document_chunks FIELDS organization_id, uuid UNIQUE;
"""

CONTENT_USAGE_SIGNAL_MIGRATION_DEFINITIONS = """
DEFINE FIELD IF NOT EXISTS last_recalled_at ON raw_captures TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS last_used_at ON raw_captures TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS retrieval_count ON raw_captures TYPE option<int> DEFAULT 0;
DEFINE FIELD IF NOT EXISTS citation_count ON raw_captures TYPE option<int> DEFAULT 0;
UPDATE raw_captures SET
    retrieval_count = retrieval_count ?? metadata.retrieval_count ?? 0,
    citation_count = citation_count ?? metadata.citation_count ?? 0
WHERE retrieval_count = NONE OR citation_count = NONE;
DEFINE FIELD OVERWRITE retrieval_count ON raw_captures TYPE int DEFAULT 0;
DEFINE FIELD OVERWRITE citation_count ON raw_captures TYPE int DEFAULT 0;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_last_recalled
    ON raw_captures FIELDS organization_id, last_recalled_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_raw_captures_last_used
    ON raw_captures FIELDS organization_id, last_used_at, uuid;

DEFINE TABLE IF NOT EXISTS memory_usage_events SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON memory_usage_events TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON memory_usage_events TYPE string;
DEFINE FIELD IF NOT EXISTS session_key ON memory_usage_events TYPE string;
DEFINE FIELD IF NOT EXISTS message_key ON memory_usage_events TYPE string;
DEFINE FIELD IF NOT EXISTS source_surface ON memory_usage_events TYPE string;
DEFINE FIELD IF NOT EXISTS item_kind ON memory_usage_events TYPE string;
DEFINE FIELD IF NOT EXISTS item_id ON memory_usage_events TYPE string;
DEFINE FIELD IF NOT EXISTS signal_type ON memory_usage_events TYPE string;
DEFINE FIELD IF NOT EXISTS principal_id ON memory_usage_events TYPE option<string>;
DEFINE FIELD IF NOT EXISTS project_id ON memory_usage_events TYPE option<string>;
DEFINE FIELD IF NOT EXISTS metadata ON memory_usage_events TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS event_at ON memory_usage_events TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS created_at ON memory_usage_events TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_memory_usage_events_uuid
    ON memory_usage_events FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_memory_usage_events_dedupe
    ON memory_usage_events
    FIELDS organization_id, session_key, message_key, source_surface, item_kind, item_id, signal_type UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_memory_usage_events_item
    ON memory_usage_events FIELDS organization_id, item_kind, item_id, signal_type, event_at;
DEFINE INDEX IF NOT EXISTS idx_memory_usage_events_session
    ON memory_usage_events FIELDS organization_id, session_key, message_key;
DEFINE INDEX IF NOT EXISTS idx_memory_usage_events_created_at
    ON memory_usage_events FIELDS created_at;
ALTER TABLE IF EXISTS memory_usage_events PERMISSIONS
    FOR select, create, update, delete WHERE organization_id = $token.org OR organization_id = $auth.organization_id;
UPDATE raw_captures SET
    last_recalled_at = metadata.last_recalled_at,
    last_used_at = metadata.last_used_at,
    retrieval_count = metadata.retrieval_count ?? 0,
    citation_count = metadata.citation_count ?? 0
WHERE last_recalled_at = NONE
    AND last_used_at = NONE
    AND metadata != NONE
    AND (
        metadata.last_recalled_at != NONE
        OR metadata.last_used_at != NONE
        OR metadata.retrieval_count != NONE
        OR metadata.citation_count != NONE
    );
"""

CONTENT_SOURCE_IMPORT_RECEIPT_MIGRATION_DEFINITIONS = """
DEFINE FIELD IF NOT EXISTS skipped_records.* ON source_imports TYPE object FLEXIBLE;
DEFINE FIELD IF NOT EXISTS errors.* ON source_imports TYPE object FLEXIBLE;
DEFINE FIELD OVERWRITE skipped_records.* ON source_imports TYPE object FLEXIBLE;
DEFINE FIELD OVERWRITE errors.* ON source_imports TYPE object FLEXIBLE;
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
        SchemaMigration(
            version=9,
            name="content_lineage_relation_tables",
            statements=tuple(split_statements(CONTENT_LINEAGE_RELATION_MIGRATION_DEFINITIONS)),
        ),
        SchemaMigration(
            version=10,
            name="raw_capture_changefeed_cursor",
            statements=tuple(
                split_statements(CONTENT_RAW_CAPTURE_CHANGEFEED_MIGRATION_DEFINITIONS)
            ),
        ),
        SchemaMigration(
            version=11,
            name="highlight_snippets_and_code_analyzer",
            statements=tuple(
                split_statements(
                    render_fulltext_compatible_sql(
                        CONTENT_HIGHLIGHT_SNIPPET_MIGRATION_DEFINITIONS,
                        url=url,
                    )
                )
            ),
        ),
        SchemaMigration(
            version=12,
            name="content_extracted_into_relation_table",
            statements=tuple(
                split_statements(CONTENT_EXTRACTED_INTO_RELATION_MIGRATION_DEFINITIONS)
            ),
        ),
        SchemaMigration(
            version=13,
            name="content_entity_anchors",
            statements=tuple(split_statements(CONTENT_ENTITY_ANCHOR_MIGRATION_DEFINITIONS)),
        ),
        SchemaMigration(
            version=14,
            name="content_backup_full_org_archives",
            statements=tuple(split_statements(CONTENT_BACKUP_LEGACY_INCLUDE_CLEANUP_DEFINITIONS)),
        ),
        SchemaMigration(
            version=15,
            name="content_lookup_indexes",
            statements=tuple(split_statements(CONTENT_LOOKUP_INDEX_MIGRATION_DEFINITIONS)),
        ),
        SchemaMigration(
            version=16,
            name="content_usage_signals",
            statements=tuple(split_statements(CONTENT_USAGE_SIGNAL_MIGRATION_DEFINITIONS)),
        ),
        SchemaMigration(
            version=17,
            name="content_source_import_receipt_arrays",
            statements=tuple(split_statements(CONTENT_SOURCE_IMPORT_RECEIPT_MIGRATION_DEFINITIONS)),
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
    if current_version == 0 and not await _content_tables_have_rows(client):
        return
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
        await _normalize_legacy_enum_values(client)
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


async def _normalize_legacy_enum_values(client: SurrealContentClient) -> None:
    for value in SourceType:
        await _execute_optional_table_update(
            client,
            "UPDATE crawl_sources SET source_type = $normalized WHERE source_type = $legacy;",
            legacy=value.value.upper(),
            normalized=value.value,
        )
    for value in CrawlStatus:
        await _execute_optional_table_update(
            client,
            "UPDATE crawl_sources SET crawl_status = $normalized WHERE crawl_status = $legacy;",
            legacy=value.value.upper(),
            normalized=value.value,
        )
    for value in MemoryScope:
        await _execute_optional_table_update(
            client,
            "UPDATE raw_captures SET memory_scope = $normalized WHERE memory_scope = $legacy;",
            legacy=value.value.upper(),
            normalized=value.value,
        )
    for value in _CONTENT_REVIEW_STATE_VALUES:
        await _execute_optional_table_update(
            client,
            "UPDATE raw_captures SET review_state = $normalized WHERE review_state = $legacy;",
            legacy=value.upper(),
            normalized=value,
        )
    for value in MemoryScope:
        await _execute_optional_table_update(
            client,
            "UPDATE source_imports SET target_memory_scope = $normalized "
            "WHERE target_memory_scope = $legacy;",
            legacy=value.value.upper(),
            normalized=value.value,
        )
    for value in _CONTENT_SOURCE_IMPORT_STATUS_VALUES:
        await _execute_optional_table_update(
            client,
            "UPDATE source_imports SET status = $normalized WHERE status = $legacy;",
            legacy=value.upper(),
            normalized=value,
        )
    for value in _CONTENT_BACKUP_STATUS_VALUES:
        await _execute_optional_table_update(
            client,
            "UPDATE backups SET status = $normalized WHERE status = $legacy;",
            legacy=value.upper(),
            normalized=value,
        )


async def _matching_rows(
    client: SurrealContentClient,
    statement: str,
    **params: object,
) -> list[dict[str, object]]:
    from sibyl_core.backends.surreal.records import normalize_records

    try:
        return normalize_records(await client.execute_query(statement, **params))
    except Exception as exc:
        if is_missing_table_error(exc):
            return []
        raise


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


async def _content_tables_have_rows(client: SurrealContentClient) -> bool:
    for table in CONTENT_TABLES:
        if await _table_has_rows(client, table=table):
            return True
    return False


async def _table_has_rows(client: SurrealContentClient, *, table: str) -> bool:
    rows = await _matching_rows(client, f"SELECT count() AS count FROM {table};")
    return any(_coerce_int(row.get("count")) > 0 for row in rows)


async def _execute_optional_table_update(
    client: SurrealContentClient,
    statement: str,
    **params: object,
) -> None:
    try:
        await client.execute_query(statement, **params)
    except Exception as exc:
        if is_missing_table_error(exc):
            return
        raise


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
    "CONTENT_BACKUP_LEGACY_INCLUDE_CLEANUP_DEFINITIONS",
    "CONTENT_CHILD_SCOPE_MIGRATION_DEFINITIONS",
    "CONTENT_DOCUMENT_URL_SCOPE_MIGRATION_DEFINITIONS",
    "CONTENT_ENTITY_ANCHOR_MIGRATION_DEFINITIONS",
    "CONTENT_ENUM_ASSERTION_MIGRATION_DEFINITIONS",
    "CONTENT_EXTRACTED_INTO_RELATION_MIGRATION_DEFINITIONS",
    "CONTENT_HIGHLIGHT_SNIPPET_MIGRATION_DEFINITIONS",
    "CONTENT_LINEAGE_RELATION_MIGRATION_DEFINITIONS",
    "CONTENT_LOOKUP_INDEX_MIGRATION_DEFINITIONS",
    "CONTENT_PERMISSION_MIGRATION_DEFINITIONS",
    "CONTENT_RELATION_TABLES",
    "CONTENT_REVIEW_STATE_DEFERRED_MIGRATION_DEFINITIONS",
    "CONTENT_SCHEMA_CURRENT_VERSION",
    "CONTENT_SCHEMA_DEFINITIONS",
    "CONTENT_SCHEMA_NAME",
    "CONTENT_SOURCE_URL_SCOPE_MIGRATION_DEFINITIONS",
    "CONTENT_SOURCE_IMPORT_RECEIPT_MIGRATION_DEFINITIONS",
    "CONTENT_TABLES",
    "CONTENT_USAGE_SIGNAL_MIGRATION_DEFINITIONS",
    "bootstrap_content_schema",
]
