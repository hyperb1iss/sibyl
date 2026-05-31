"""Regression checks for SurrealDB schema syntax accepted by the server parser."""

from __future__ import annotations

import time

import jwt
import pytest
from surrealdb import AsyncSurreal

from sibyl_core.backends.surreal.auth_schema import (
    AUTH_ENUM_ASSERTION_MIGRATION_DEFINITIONS,
    AUTH_INVITATION_TOKEN_MIGRATION_DEFINITIONS,
    AUTH_PERMISSION_MIGRATION_DEFINITIONS,
    AUTH_PROJECT_SLUG_MIGRATION_DEFINITIONS,
    AUTH_SCHEMA_CURRENT_VERSION,
    AUTH_SCHEMA_DEFINITIONS,
    AUTH_SCHEMA_MIGRATIONS,
    AUTH_TABLES,
    bootstrap_auth_schema,
)
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_PERMISSION_MIGRATION_DEFINITIONS,
    CONTENT_RAW_CAPTURE_INGESTION_MIGRATION_DEFINITIONS,
    CONTENT_REVIEW_STATE_DEFERRED_MIGRATION_DEFINITIONS,
    CONTENT_SCHEMA_CURRENT_VERSION,
    CONTENT_SCHEMA_DEFINITIONS,
    CONTENT_TABLES,
    _content_schema_migrations,
    bootstrap_content_schema,
)
from sibyl_core.backends.surreal.schema import (
    CURRENT_SCHEMA_MAINTENANCE_DEFINITIONS,
    DEAD_GRAPH_OBJECT_REMOVAL_DEFINITIONS,
    EDGE_DEFINITIONS,
    ENTITY_UPDATED_AT_DATETIME_MIGRATION_DEFINITIONS,
    GRAPH_ENUM_ASSERTION_DEFINITIONS,
    GRAPH_INDEX_PRUNE_DEFINITIONS,
    GRAPH_SCHEMA_MIGRATIONS,
    NODE_DEFINITIONS,
    PARENT_TASK_CANONICALIZATION_DEFINITIONS,
    RELATION_EDGE_CLEANUP_DEFINITIONS,
    RELATION_ENDPOINT_BACKFILL_DEFINITIONS,
    RELATION_ENDPOINT_SCHEMA_DEFINITIONS,
    REMOVED_GRAPH_EDGES,
    REMOVED_GRAPH_TABLES,
    bootstrap_schema,
    render_fulltext_compatible_sql,
)
from sibyl_core.backends.surreal.schema_helpers import split_statements
from sibyl_core.backends.surreal.schema_version import (
    GRAPH_SCHEMA_CURRENT_VERSION,
    SCHEMA_VERSION_DEFINITIONS,
)


class _RecordingSchemaClient:
    def __init__(
        self,
        duplicate_index_name: str = "",
        schema_version: int = 0,
        missing_tables: set[str] | None = None,
        table_counts: dict[str, int] | None = None,
    ) -> None:
        self.duplicate_index_name = duplicate_index_name
        self.schema_version = schema_version
        self.missing_tables = missing_tables or set()
        self.table_counts = table_counts or {}
        self.calls: list[str] = []
        self._url = ""
        self.group_id = "org_123"

    async def execute_query(self, statement: str, **params: object) -> object:
        self.calls.append(statement)
        stripped = statement.strip()
        if stripped.startswith("SELECT version FROM schema_version"):
            return [{"version": self.schema_version}]
        if stripped.startswith("SELECT count() AS count FROM "):
            table = stripped.removeprefix("SELECT count() AS count FROM ").split()[0]
            if table in self.missing_tables:
                raise RuntimeError(f"The table '{table}' does not exist")
            return [{"count": self.table_counts.get(table, 0)}]
        if stripped.startswith("UPSERT schema_version:"):
            version = params.get("version")
            self.schema_version = int(version) if isinstance(version, int | str | float) else 0
        for table in tuple(self.missing_tables):
            if stripped.startswith(f"DELETE FROM {table}") or stripped.startswith(
                f"UPDATE {table}"
            ):
                raise RuntimeError(f"The table '{table}' does not exist")
            if stripped.startswith(f"DEFINE TABLE OVERWRITE {table}"):
                self.missing_tables.discard(table)
        if self.duplicate_index_name and self.duplicate_index_name in statement:
            raise RuntimeError(
                f"Database index `{self.duplicate_index_name}` already contains 'dirty-row'"
            )
        return None


_ACCESS_SECRET = "s" * 64


def _permission_statement(definitions: str, table: str) -> str:
    prefix = f"ALTER TABLE IF EXISTS {table} PERMISSIONS"
    return next(
        statement for statement in split_statements(definitions) if statement.startswith(prefix)
    )


def _record_access_token(*, namespace: str, database: str, organization_id: str) -> str:
    return jwt.encode(
        {
            "exp": int(time.time()) + 3600,
            "ns": namespace,
            "db": database,
            "ac": "record_user",
            "id": "access_user:org_scoped_user",
            "org": organization_id,
        },
        _ACCESS_SECRET,
        algorithm="HS512",
    )


async def _define_record_access(db: AsyncSurreal) -> None:
    await db.query(
        f"""
        DEFINE ACCESS record_user ON DATABASE TYPE RECORD
            WITH JWT ALGORITHM HS512 KEY '{_ACCESS_SECRET}';
        """
    )


def test_flexible_object_fields_keep_server_accepted_token_order() -> None:
    schema = "\n".join(
        (
            AUTH_SCHEMA_DEFINITIONS,
            CONTENT_SCHEMA_DEFINITIONS,
            NODE_DEFINITIONS,
            EDGE_DEFINITIONS,
        )
    )

    assert "FLEXIBLE TYPE object" not in schema
    assert "TYPE object FLEXIBLE" in schema


def test_runtime_schemafull_tables_define_schemafull_without_redundant_alter() -> None:
    schema = "\n".join((AUTH_SCHEMA_DEFINITIONS, CONTENT_SCHEMA_DEFINITIONS))
    tables = (*AUTH_TABLES, *CONTENT_TABLES)

    for table in tables:
        assert f"DEFINE TABLE IF NOT EXISTS {table} SCHEMAFULL;" in schema
        assert f"ALTER TABLE IF EXISTS {table} SCHEMAFULL;" not in schema


def test_auth_invitation_schema_supports_hashed_tokens() -> None:
    assert "DEFINE FIELD IF NOT EXISTS token ON organization_invitations" in AUTH_SCHEMA_DEFINITIONS
    assert (
        "DEFINE FIELD OVERWRITE token ON organization_invitations TYPE option<string>"
        in AUTH_INVITATION_TOKEN_MIGRATION_DEFINITIONS
    )
    assert "DEFINE FIELD IF NOT EXISTS token_hash ON organization_invitations" in (
        AUTH_SCHEMA_DEFINITIONS
    )
    assert "REMOVE INDEX IF EXISTS idx_organization_invitations_token" not in (
        AUTH_SCHEMA_DEFINITIONS
    )
    assert "REMOVE INDEX IF EXISTS idx_organization_invitations_token" in (
        AUTH_INVITATION_TOKEN_MIGRATION_DEFINITIONS
    )
    assert "idx_organization_invitations_token_hash" in AUTH_SCHEMA_DEFINITIONS


def test_auth_project_slug_cleanup_is_versioned() -> None:
    assert "REMOVE INDEX IF EXISTS idx_projects_org_slug" not in AUTH_SCHEMA_DEFINITIONS
    assert "idx_projects_org_slug_lookup" in AUTH_SCHEMA_DEFINITIONS
    assert "REMOVE INDEX IF EXISTS idx_projects_org_slug" in (
        AUTH_PROJECT_SLUG_MIGRATION_DEFINITIONS
    )


def test_auth_enum_assertions_are_versioned() -> None:
    migration_sql = "\n".join(
        statement for migration in AUTH_SCHEMA_MIGRATIONS for statement in migration.statements
    )

    assert "DEFINE FIELD OVERWRITE role ON organization_members" in (
        AUTH_ENUM_ASSERTION_MIGRATION_DEFINITIONS
    )
    assert "ASSERT $value IN ['owner', 'admin', 'member', 'viewer']" in (
        AUTH_ENUM_ASSERTION_MIGRATION_DEFINITIONS
    )
    assert "DEFINE FIELD OVERWRITE status ON device_authorization_requests" in (
        AUTH_ENUM_ASSERTION_MIGRATION_DEFINITIONS
    )
    assert "auth_enum_assertions" in [migration.name for migration in AUTH_SCHEMA_MIGRATIONS]
    assert AUTH_ENUM_ASSERTION_MIGRATION_DEFINITIONS.strip().splitlines()[0] in migration_sql


def test_auth_table_permissions_are_versioned() -> None:
    migration_sql = "\n".join(
        statement for migration in AUTH_SCHEMA_MIGRATIONS for statement in migration.statements
    )

    assert "auth_table_permissions" in [migration.name for migration in AUTH_SCHEMA_MIGRATIONS]
    for table in AUTH_TABLES:
        assert f"ALTER TABLE IF EXISTS {table} PERMISSIONS" in (
            AUTH_PERMISSION_MIGRATION_DEFINITIONS
        )
    assert "WHERE organization_id = $token.org" in AUTH_PERMISSION_MIGRATION_DEFINITIONS
    assert "OR organization_id = $auth.organization_id" in (AUTH_PERMISSION_MIGRATION_DEFINITIONS)
    assert "WHERE uuid = $token.org" in AUTH_PERMISSION_MIGRATION_DEFINITIONS
    assert "OR uuid = $auth.organization_id" in AUTH_PERMISSION_MIGRATION_DEFINITIONS
    for table in (
        "users",
        "api_keys",
        "user_sessions",
        "password_reset_tokens",
        "oauth_connections",
    ):
        assert f"ALTER TABLE IF EXISTS {table} PERMISSIONS NONE" in (
            AUTH_PERMISSION_MIGRATION_DEFINITIONS
        )
    assert AUTH_PERMISSION_MIGRATION_DEFINITIONS.strip().splitlines()[0] in migration_sql


def test_auth_schema_includes_oidc_identity_tables() -> None:
    assert "identity_provider" in AUTH_TABLES
    assert "user_identity" in AUTH_TABLES
    assert "DEFINE FIELD IF NOT EXISTS subject_key ON user_identity TYPE string" in (
        AUTH_SCHEMA_DEFINITIONS
    )
    assert "idx_user_identity_provider_subject" in AUTH_SCHEMA_DEFINITIONS


def test_auth_schema_includes_llm_usage_buckets() -> None:
    assert "llm_usage_buckets" in AUTH_TABLES
    assert "DEFINE FIELD IF NOT EXISTS bucket_key ON llm_usage_buckets TYPE string" in (
        AUTH_SCHEMA_DEFINITIONS
    )
    assert "idx_llm_usage_buckets_key" in AUTH_SCHEMA_DEFINITIONS
    assert "idx_llm_usage_buckets_subject" in AUTH_SCHEMA_DEFINITIONS


def test_auth_and_content_schema_include_deletion_lifecycle_fields() -> None:
    assert "DEFINE FIELD IF NOT EXISTS deleted_at ON users TYPE option<datetime>" in (
        AUTH_SCHEMA_DEFINITIONS
    )
    assert "DEFINE FIELD IF NOT EXISTS purge_after ON users TYPE option<datetime>" in (
        AUTH_SCHEMA_DEFINITIONS
    )
    assert "DEFINE FIELD IF NOT EXISTS deleted_at ON raw_captures TYPE option<datetime>" in (
        CONTENT_SCHEMA_DEFINITIONS
    )
    assert "DEFINE FIELD IF NOT EXISTS purge_after ON raw_captures TYPE option<datetime>" in (
        CONTENT_SCHEMA_DEFINITIONS
    )
    assert "idx_raw_captures_purge_after" in CONTENT_SCHEMA_DEFINITIONS


def test_content_table_permissions_are_versioned() -> None:
    migration_sql = "\n".join(
        statement
        for migration in _content_schema_migrations(url="memory://")
        for statement in migration.statements
    )

    assert "content_table_permissions" in [
        migration.name for migration in _content_schema_migrations(url="memory://")
    ]
    for table in CONTENT_TABLES:
        assert f"ALTER TABLE IF EXISTS {table} PERMISSIONS" in (
            CONTENT_PERMISSION_MIGRATION_DEFINITIONS
        )
    assert "WHERE organization_id = $token.org" in CONTENT_PERMISSION_MIGRATION_DEFINITIONS
    assert "OR organization_id = $auth.organization_id" in (
        CONTENT_PERMISSION_MIGRATION_DEFINITIONS
    )
    for table in ("system_settings", "telemetry_rollups"):
        assert f"ALTER TABLE IF EXISTS {table} PERMISSIONS NONE" in (
            CONTENT_PERMISSION_MIGRATION_DEFINITIONS
        )
    assert CONTENT_PERMISSION_MIGRATION_DEFINITIONS.strip().splitlines()[0] in migration_sql


def test_content_review_state_deferred_is_versioned() -> None:
    migrations = _content_schema_migrations(url="memory://")
    migration_sql = "\n".join(
        statement for migration in migrations for statement in migration.statements
    )

    assert "deferred" in CONTENT_REVIEW_STATE_DEFERRED_MIGRATION_DEFINITIONS
    assert "content_review_state_deferred" in [migration.name for migration in migrations]
    assert (
        CONTENT_REVIEW_STATE_DEFERRED_MIGRATION_DEFINITIONS.strip().splitlines()[0] in migration_sql
    )


def test_content_raw_capture_ingestion_fields_are_versioned() -> None:
    migrations = _content_schema_migrations(url="memory://")
    migration_sql = "\n".join(
        statement for migration in migrations for statement in migration.statements
    )

    assert CONTENT_SCHEMA_CURRENT_VERSION == 8
    assert "DEFINE FIELD IF NOT EXISTS embedding ON raw_captures" in (
        CONTENT_RAW_CAPTURE_INGESTION_MIGRATION_DEFINITIONS
    )
    assert "idx_raw_captures_org_dedupe" in (CONTENT_RAW_CAPTURE_INGESTION_MIGRATION_DEFINITIONS)
    assert "idx_raw_captures_embedding" in (CONTENT_RAW_CAPTURE_INGESTION_MIGRATION_DEFINITIONS)
    assert "content_raw_capture_ingestion_indexes" in [migration.name for migration in migrations]
    assert (
        CONTENT_RAW_CAPTURE_INGESTION_MIGRATION_DEFINITIONS.strip().splitlines()[0] in migration_sql
    )


@pytest.mark.asyncio
async def test_content_permissions_filter_record_users_by_org() -> None:
    namespace = "permissions_content"
    database = "content"
    db = AsyncSurreal("memory://")
    try:
        await db.use(namespace, database)
        await _define_record_access(db)
        await db.query(
            """
            DEFINE TABLE crawl_sources SCHEMAFULL;
            DEFINE FIELD uuid ON crawl_sources TYPE string;
            DEFINE FIELD organization_id ON crawl_sources TYPE string;
            DEFINE FIELD name ON crawl_sources TYPE string;
            """
        )
        await db.query(
            _permission_statement(CONTENT_PERMISSION_MIGRATION_DEFINITIONS, "crawl_sources")
        )
        await db.query(
            """
            CREATE crawl_sources CONTENT { uuid: 'source-a', organization_id: 'org-a', name: 'A' };
            CREATE crawl_sources CONTENT { uuid: 'source-b', organization_id: 'org-b', name: 'B' };
            """
        )

        await db.authenticate(
            _record_access_token(
                namespace=namespace,
                database=database,
                organization_id="org-a",
            )
        )
        await db.use(namespace, database)
        visible = await db.query("SELECT name, organization_id FROM crawl_sources ORDER BY name;")
        denied_create = await db.query(
            """
            CREATE crawl_sources CONTENT {
                uuid: 'source-denied',
                organization_id: 'org-b',
                name: 'denied'
            };
            """
        )
    finally:
        await db.close()

    assert visible == [{"name": "A", "organization_id": "org-a"}]
    assert denied_create == []


@pytest.mark.asyncio
async def test_auth_permissions_filter_record_users_by_org() -> None:
    namespace = "permissions_auth"
    database = "auth"
    db = AsyncSurreal("memory://")
    try:
        await db.use(namespace, database)
        await _define_record_access(db)
        await db.query(
            """
            DEFINE TABLE projects SCHEMAFULL;
            DEFINE FIELD uuid ON projects TYPE string;
            DEFINE FIELD organization_id ON projects TYPE string;
            DEFINE FIELD name ON projects TYPE string;
            DEFINE TABLE api_keys SCHEMAFULL;
            DEFINE FIELD uuid ON api_keys TYPE string;
            DEFINE FIELD organization_id ON api_keys TYPE string;
            """
        )
        await db.query(_permission_statement(AUTH_PERMISSION_MIGRATION_DEFINITIONS, "projects"))
        await db.query(_permission_statement(AUTH_PERMISSION_MIGRATION_DEFINITIONS, "api_keys"))
        await db.query(
            """
            CREATE projects CONTENT { uuid: 'project-a', organization_id: 'org-a', name: 'A' };
            CREATE projects CONTENT { uuid: 'project-b', organization_id: 'org-b', name: 'B' };
            CREATE api_keys CONTENT { uuid: 'api-key-a', organization_id: 'org-a' };
            """
        )

        await db.authenticate(
            _record_access_token(
                namespace=namespace,
                database=database,
                organization_id="org-a",
            )
        )
        await db.use(namespace, database)
        visible_projects = await db.query(
            "SELECT name, organization_id FROM projects ORDER BY name;"
        )
        denied_project_create = await db.query(
            """
            CREATE projects CONTENT {
                uuid: 'project-denied',
                organization_id: 'org-b',
                name: 'denied'
            };
            """
        )
        hidden_keys = await db.query("SELECT uuid FROM api_keys;")
    finally:
        await db.close()

    assert visible_projects == [{"name": "A", "organization_id": "org-a"}]
    assert denied_project_create == []
    assert hidden_keys == []


def test_fulltext_indexes_render_with_embedded_search_syntax() -> None:
    rendered = render_fulltext_compatible_sql(CONTENT_SCHEMA_DEFINITIONS, url="memory://")

    assert "SEARCH ANALYZER" in rendered
    assert "FULLTEXT ANALYZER" not in rendered


def test_fulltext_indexes_keep_remote_server_syntax() -> None:
    rendered = render_fulltext_compatible_sql(
        CONTENT_SCHEMA_DEFINITIONS, url="ws://surrealdb:8000/rpc"
    )

    assert "FULLTEXT ANALYZER" in rendered
    assert "SEARCH ANALYZER" not in rendered


def test_entity_fulltext_uses_top_level_description_and_content_indexes() -> None:
    assert "idx_entity_description_text_ft" in NODE_DEFINITIONS
    assert "idx_entity_content_text_ft" in NODE_DEFINITIONS
    assert "REMOVE INDEX IF EXISTS idx_entity_description_ft ON TABLE entity" in NODE_DEFINITIONS
    assert "REMOVE INDEX IF EXISTS idx_entity_content_ft ON TABLE entity" in NODE_DEFINITIONS
    assert "FIELDS description FULLTEXT" in NODE_DEFINITIONS
    assert "FIELDS content FULLTEXT" in NODE_DEFINITIONS
    assert "FIELDS attributes.description FULLTEXT" not in NODE_DEFINITIONS
    assert "FIELDS attributes.content FULLTEXT" not in NODE_DEFINITIONS
    assert "description = description ?? attributes.description" in NODE_DEFINITIONS
    assert "content = content ?? attributes.content" in NODE_DEFINITIONS
    assert NODE_DEFINITIONS.index("REMOVE INDEX IF EXISTS idx_entity_description_ft") < (
        NODE_DEFINITIONS.index("DEFINE INDEX IF NOT EXISTS idx_entity_description_text_ft")
    )
    assert NODE_DEFINITIONS.index("REMOVE INDEX IF EXISTS idx_entity_content_ft") < (
        NODE_DEFINITIONS.index("DEFINE INDEX IF NOT EXISTS idx_entity_content_text_ft")
    )


def test_graph_relation_tables_are_enforced() -> None:
    for relation in ("relates_to", "mentions"):
        assert f"DEFINE TABLE OVERWRITE {relation} SCHEMAFULL TYPE RELATION" in EDGE_DEFINITIONS

    assert "relates_to SCHEMAFULL TYPE RELATION IN entity OUT entity ENFORCED" in EDGE_DEFINITIONS
    assert "mentions SCHEMAFULL TYPE RELATION IN episode OUT entity ENFORCED" in EDGE_DEFINITIONS
    for relation in REMOVED_GRAPH_EDGES:
        assert f"DEFINE TABLE OVERWRITE {relation}" not in EDGE_DEFINITIONS


def test_graph_hnsw_indexes_use_configurable_defaults() -> None:
    assert "idx_entity_embedding" in NODE_DEFINITIONS
    assert "idx_community_embedding" not in NODE_DEFINITIONS
    assert "idx_relates_fact_embedding" in EDGE_DEFINITIONS
    assert "HNSW DIMENSION 1024 DIST COSINE TYPE F32 EFC 150 M 12" in NODE_DEFINITIONS
    assert "HNSW DIMENSION 1024 DIST COSINE TYPE F32 EFC 150 M 12" in EDGE_DEFINITIONS


def test_graph_relation_endpoint_indexes_match_hot_lookups() -> None:
    assert "DEFINE FIELD IF NOT EXISTS source_id ON mentions" in EDGE_DEFINITIONS
    assert "DEFINE FIELD IF NOT EXISTS target_id ON mentions" in EDGE_DEFINITIONS
    assert "idx_relates_source_created" in EDGE_DEFINITIONS
    assert "idx_relates_target_created" in EDGE_DEFINITIONS
    assert "idx_mentions_source_created" in EDGE_DEFINITIONS
    assert "idx_mentions_target_created" in EDGE_DEFINITIONS


def test_graph_relation_endpoint_backfill_is_versioned() -> None:
    assert "idx_relates_source_created" in RELATION_ENDPOINT_SCHEMA_DEFINITIONS
    assert "idx_mentions_source_created" in RELATION_ENDPOINT_SCHEMA_DEFINITIONS
    assert "UPDATE relates_to SET" in RELATION_ENDPOINT_BACKFILL_DEFINITIONS
    assert "source_id = in.uuid" in RELATION_ENDPOINT_BACKFILL_DEFINITIONS
    assert "target_id = out.uuid" in RELATION_ENDPOINT_BACKFILL_DEFINITIONS
    assert "source_id != in.uuid" in RELATION_ENDPOINT_BACKFILL_DEFINITIONS
    assert "target_id != out.uuid" in RELATION_ENDPOINT_BACKFILL_DEFINITIONS
    assert "UPDATE mentions SET" in RELATION_ENDPOINT_BACKFILL_DEFINITIONS
    migration_sql = "\n".join(
        statement for migration in GRAPH_SCHEMA_MIGRATIONS for statement in migration.statements
    )
    assert "UPDATE relates_to SET" in migration_sql
    assert "UPDATE mentions SET" in migration_sql


def test_dead_graph_object_removal_is_versioned() -> None:
    migration_sql = "\n".join(
        statement for migration in GRAPH_SCHEMA_MIGRATIONS for statement in migration.statements
    )

    for table in (*REMOVED_GRAPH_EDGES, *REMOVED_GRAPH_TABLES):
        assert f"REMOVE TABLE IF EXISTS {table}" in DEAD_GRAPH_OBJECT_REMOVAL_DEFINITIONS
        assert f"REMOVE TABLE IF EXISTS {table}" in migration_sql


def test_entity_updated_at_datetime_migration_is_versioned() -> None:
    migration_sql = "\n".join(
        statement for migration in GRAPH_SCHEMA_MIGRATIONS for statement in migration.statements
    )

    assert "DEFINE FIELD IF NOT EXISTS updated_at ON entity TYPE option<datetime>" in (
        NODE_DEFINITIONS
    )
    assert "DEFINE FIELD OVERWRITE updated_at ON entity TYPE option<datetime>" in (
        ENTITY_UPDATED_AT_DATETIME_MIGRATION_DEFINITIONS
    )
    assert "DEFINE FIELD IF NOT EXISTS parent_task_id ON entity TYPE option<string>" in (
        ENTITY_UPDATED_AT_DATETIME_MIGRATION_DEFINITIONS
    )
    assert "type::datetime(updated_at)" in ENTITY_UPDATED_AT_DATETIME_MIGRATION_DEFINITIONS
    assert "string::is::datetime(updated_at)" in ENTITY_UPDATED_AT_DATETIME_MIGRATION_DEFINITIONS
    assert "!type::is::datetime(updated_at)" in ENTITY_UPDATED_AT_DATETIME_MIGRATION_DEFINITIONS
    assert "DEFINE INDEX OVERWRITE idx_entity_group_updated" in (
        ENTITY_UPDATED_AT_DATETIME_MIGRATION_DEFINITIONS
    )
    assert "CONCURRENTLY" in ENTITY_UPDATED_AT_DATETIME_MIGRATION_DEFINITIONS
    assert "DEFINE FIELD OVERWRITE updated_at ON entity TYPE option<datetime>" in migration_sql


def test_graph_index_prune_removes_constant_namespace_prefixes() -> None:
    current_schema = "\n".join((NODE_DEFINITIONS, EDGE_DEFINITIONS))
    migration_sql = "\n".join(
        statement for migration in GRAPH_SCHEMA_MIGRATIONS for statement in migration.statements
    )

    for old_index in (
        "idx_entity_group",
        "idx_entity_epic",
        "idx_entity_group_updated",
        "idx_entity_group_type_updated",
        "idx_entity_group_type_project_updated",
        "idx_entity_group_type_epic_updated",
        "idx_entity_group_type_parent_task_updated",
        "idx_entity_group_type_status_updated",
        "idx_entity_group_type_epic_status",
        "idx_entity_group_type_project_status",
        "idx_episode_group",
        "idx_relates_group",
        "idx_relates_group_source",
        "idx_relates_group_target",
        "idx_relates_group_name_source",
        "idx_relates_group_name_target",
        "idx_relates_group_source_target_name",
        "idx_relates_group_source_created",
        "idx_relates_group_target_created",
        "idx_relates_group_created",
        "idx_mentions_group",
        "idx_mentions_group_source",
        "idx_mentions_group_target",
        "idx_mentions_group_source_created",
        "idx_mentions_group_target_created",
    ):
        assert f"DEFINE INDEX IF NOT EXISTS {old_index}" not in current_schema
        assert f"REMOVE INDEX IF EXISTS {old_index}" in GRAPH_INDEX_PRUNE_DEFINITIONS

    for new_index in (
        "idx_entity_updated",
        "idx_entity_type_updated",
        "idx_entity_type_project_updated",
        "idx_entity_type_parent_task_updated",
        "idx_entity_type_status_updated",
        "idx_entity_type_project_status",
        "idx_relates_source_created",
        "idx_relates_target_created",
        "idx_mentions_source_created",
        "idx_mentions_target_created",
    ):
        assert f"DEFINE INDEX IF NOT EXISTS {new_index}" in current_schema
        assert f"DEFINE INDEX OVERWRITE {new_index}" in GRAPH_INDEX_PRUNE_DEFINITIONS

    assert "SET parent_task_id = epic_id" in PARENT_TASK_CANONICALIZATION_DEFINITIONS
    assert "DEFINE FIELD IF NOT EXISTS parent_task_id ON entity TYPE option<string>" in (
        PARENT_TASK_CANONICALIZATION_DEFINITIONS
    )
    assert "SET parent_task_id = attributes.parent_task_id" in (
        PARENT_TASK_CANONICALIZATION_DEFINITIONS
    )
    assert "SET parent_task_id = attributes.epic_id" in (PARENT_TASK_CANONICALIZATION_DEFINITIONS)
    assert "DEFINE INDEX OVERWRITE idx_entity_type_parent_task_updated" in migration_sql


def test_graph_enum_assertions_are_versioned() -> None:
    migration_sql = "\n".join(
        statement for migration in GRAPH_SCHEMA_MIGRATIONS for statement in migration.statements
    )

    assert "DEFINE FIELD OVERWRITE entity_type ON entity TYPE string" in (
        GRAPH_ENUM_ASSERTION_DEFINITIONS
    )
    assert "ASSERT $value IN ['pattern'" in GRAPH_ENUM_ASSERTION_DEFINITIONS
    assert "graph_enum_assertions" in [migration.name for migration in GRAPH_SCHEMA_MIGRATIONS]
    assert GRAPH_ENUM_ASSERTION_DEFINITIONS.strip().splitlines()[0] in migration_sql


def test_graph_relation_cleanup_covers_all_relation_tables() -> None:
    for relation in ("relates_to", "mentions"):
        assert f"DELETE FROM {relation}" in RELATION_EDGE_CLEANUP_DEFINITIONS
    for relation in REMOVED_GRAPH_EDGES:
        assert f"DELETE FROM {relation}" not in RELATION_EDGE_CLEANUP_DEFINITIONS

    assert "SELECT VALUE id FROM entity" in RELATION_EDGE_CLEANUP_DEFINITIONS
    assert "SELECT VALUE id FROM episode" in RELATION_EDGE_CLEANUP_DEFINITIONS
    assert "SELECT VALUE id FROM saga" not in RELATION_EDGE_CLEANUP_DEFINITIONS
    assert "SELECT VALUE id FROM community" not in RELATION_EDGE_CLEANUP_DEFINITIONS


def test_current_graph_maintenance_skips_orphan_cleanup() -> None:
    for relation in ("relates_to", "mentions", *REMOVED_GRAPH_EDGES):
        assert f"DELETE FROM {relation}" not in CURRENT_SCHEMA_MAINTENANCE_DEFINITIONS

    assert "SELECT VALUE id FROM entity" not in CURRENT_SCHEMA_MAINTENANCE_DEFINITIONS
    assert "UPDATE relates_to SET" not in CURRENT_SCHEMA_MAINTENANCE_DEFINITIONS
    assert "UPDATE mentions SET" not in CURRENT_SCHEMA_MAINTENANCE_DEFINITIONS


@pytest.mark.asyncio
async def test_graph_bootstrap_cleans_relations_before_enforcement() -> None:
    client = _RecordingSchemaClient()

    await bootstrap_schema(client)  # type: ignore[arg-type]

    relation_define_index = next(
        index
        for index, statement in enumerate(client.calls)
        if "DEFINE TABLE OVERWRITE relates_to" in statement
    )
    cleanup_index = next(
        index
        for index, statement in enumerate(client.calls)
        if "DELETE FROM relates_to" in statement
    )
    assert cleanup_index < relation_define_index


@pytest.mark.asyncio
async def test_graph_bootstrap_skips_missing_relation_cleanup_on_new_schema() -> None:
    client = _RecordingSchemaClient(missing_tables={"relates_to"})

    await bootstrap_schema(client)  # type: ignore[arg-type]

    assert any("DEFINE TABLE OVERWRITE relates_to" in statement for statement in client.calls)


@pytest.mark.asyncio
async def test_graph_bootstrap_skips_maintenance_when_version_is_current() -> None:
    client = _RecordingSchemaClient(schema_version=GRAPH_SCHEMA_CURRENT_VERSION)

    await bootstrap_schema(client)  # type: ignore[arg-type]

    assert not any("REMOVE INDEX" in statement for statement in client.calls)
    assert not any("DEFINE TABLE IF NOT EXISTS entity" in statement for statement in client.calls)
    assert not any("UPDATE entity SET" in statement for statement in client.calls)
    assert not any("UPDATE relates_to SET" in statement for statement in client.calls)
    assert not any("UPDATE mentions SET" in statement for statement in client.calls)
    assert not any("DELETE FROM relates_to" in statement for statement in client.calls)
    assert not any("DELETE FROM mentions" in statement for statement in client.calls)


@pytest.mark.asyncio
async def test_graph_bootstrap_applies_migrations_without_full_rebuild() -> None:
    client = _RecordingSchemaClient(schema_version=2)

    await bootstrap_schema(client)  # type: ignore[arg-type]

    assert not any("DEFINE TABLE IF NOT EXISTS entity" in statement for statement in client.calls)
    assert not any("DEFINE TABLE OVERWRITE relates_to" in statement for statement in client.calls)
    assert any("idx_relates_source_created" in statement for statement in client.calls)
    assert any("idx_mentions_source_created" in statement for statement in client.calls)
    assert sum("UPDATE relates_to SET" in statement for statement in client.calls) == 2
    assert sum("UPDATE mentions SET" in statement for statement in client.calls) == 2
    for table in (*REMOVED_GRAPH_EDGES, *REMOVED_GRAPH_TABLES):
        assert any(f"REMOVE TABLE IF EXISTS {table}" in statement for statement in client.calls)
    assert not any("DELETE FROM relates_to" in statement for statement in client.calls)
    assert client.schema_version == GRAPH_SCHEMA_CURRENT_VERSION


@pytest.mark.asyncio
async def test_graph_bootstrap_applies_dead_graph_drop_without_full_rebuild() -> None:
    client = _RecordingSchemaClient(
        schema_version=3,
        missing_tables=set(REMOVED_GRAPH_EDGES + REMOVED_GRAPH_TABLES),
    )

    await bootstrap_schema(client)  # type: ignore[arg-type]

    assert not any("DEFINE TABLE IF NOT EXISTS entity" in statement for statement in client.calls)
    assert not any("DEFINE TABLE OVERWRITE relates_to" in statement for statement in client.calls)
    assert sum("UPDATE relates_to SET" in statement for statement in client.calls) == 1
    assert sum("UPDATE mentions SET" in statement for statement in client.calls) == 1
    for table in (*REMOVED_GRAPH_EDGES, *REMOVED_GRAPH_TABLES):
        assert any(f"REMOVE TABLE IF EXISTS {table}" in statement for statement in client.calls)
    assert client.schema_version == GRAPH_SCHEMA_CURRENT_VERSION


@pytest.mark.asyncio
async def test_graph_bootstrap_refuses_dead_graph_drop_with_rows() -> None:
    client = _RecordingSchemaClient(
        schema_version=3,
        table_counts={"community": 2, "has_member": 1},
    )

    with pytest.raises(RuntimeError, match="community=2"):
        await bootstrap_schema(client)  # type: ignore[arg-type]

    assert not any("REMOVE TABLE IF EXISTS community" in statement for statement in client.calls)
    assert client.schema_version == 3


@pytest.mark.asyncio
async def test_graph_bootstrap_force_rebuilds_when_current_schema_is_missing_relation() -> None:
    client = _RecordingSchemaClient(
        schema_version=GRAPH_SCHEMA_CURRENT_VERSION,
        missing_tables={"relates_to"},
    )

    await bootstrap_schema(client, force=True)  # type: ignore[arg-type]

    assert any("DEFINE TABLE IF NOT EXISTS entity" in statement for statement in client.calls)
    assert any("DEFINE TABLE OVERWRITE relates_to" in statement for statement in client.calls)


def test_graph_schema_version_table_is_schemafull() -> None:
    assert "DEFINE TABLE IF NOT EXISTS schema_version SCHEMAFULL;" in SCHEMA_VERSION_DEFINITIONS
    assert "DEFINE FIELD IF NOT EXISTS version ON schema_version TYPE int;" in (
        SCHEMA_VERSION_DEFINITIONS
    )
    assert "DEFINE FIELD IF NOT EXISTS migrations ON schema_version TYPE array<object>" in (
        SCHEMA_VERSION_DEFINITIONS
    )
    assert "DEFINE FIELD IF NOT EXISTS migrations.*.version ON schema_version TYPE int;" in (
        SCHEMA_VERSION_DEFINITIONS
    )
    assert "DEFINE FIELD IF NOT EXISTS migrations.*.name ON schema_version TYPE string;" in (
        SCHEMA_VERSION_DEFINITIONS
    )


@pytest.mark.asyncio
async def test_auth_bootstrap_continues_after_duplicate_unique_index() -> None:
    client = _RecordingSchemaClient("idx_users_uuid")

    await bootstrap_auth_schema(client)  # type: ignore[arg-type]

    assert any("idx_users_email" in statement for statement in client.calls)
    assert any(
        "DEFINE TABLE IF NOT EXISTS organizations" in statement for statement in client.calls
    )


@pytest.mark.asyncio
async def test_content_bootstrap_continues_after_duplicate_unique_index() -> None:
    client = _RecordingSchemaClient("idx_raw_captures_uuid")
    client._url = "ws://127.0.0.1:8000/rpc"

    await bootstrap_content_schema(client)  # type: ignore[arg-type]

    assert any("idx_raw_captures_org" in statement for statement in client.calls)
    assert any(
        "DEFINE TABLE IF NOT EXISTS system_settings" in statement for statement in client.calls
    )


@pytest.mark.asyncio
async def test_auth_bootstrap_skips_schema_when_version_is_current() -> None:
    client = _RecordingSchemaClient(schema_version=AUTH_SCHEMA_CURRENT_VERSION)

    await bootstrap_auth_schema(client)  # type: ignore[arg-type]

    assert not any("DEFINE TABLE IF NOT EXISTS users" in statement for statement in client.calls)
    assert any("SELECT version FROM schema_version" in statement for statement in client.calls)


@pytest.mark.asyncio
async def test_auth_bootstrap_applies_legacy_index_cleanup_without_full_rebuild() -> None:
    client = _RecordingSchemaClient(schema_version=1)

    await bootstrap_auth_schema(client)  # type: ignore[arg-type]

    assert not any("DEFINE TABLE IF NOT EXISTS users" in statement for statement in client.calls)
    assert any(
        "DEFINE FIELD OVERWRITE token ON organization_invitations" in statement
        for statement in client.calls
    )
    assert any(
        "REMOVE INDEX IF EXISTS idx_organization_invitations_token" in statement
        for statement in client.calls
    )
    assert any(
        "REMOVE INDEX IF EXISTS idx_projects_org_slug" in statement for statement in client.calls
    )
    assert client.schema_version == AUTH_SCHEMA_CURRENT_VERSION


@pytest.mark.asyncio
async def test_auth_bootstrap_does_not_repeat_migrations_after_recording_version() -> None:
    client = _RecordingSchemaClient()

    await bootstrap_auth_schema(client)  # type: ignore[arg-type]
    first_call_count = len(client.calls)
    await bootstrap_auth_schema(client)  # type: ignore[arg-type]
    second_calls = client.calls[first_call_count:]

    assert client.schema_version == AUTH_SCHEMA_CURRENT_VERSION
    assert not any("DEFINE TABLE IF NOT EXISTS users" in statement for statement in second_calls)
    assert not any("REMOVE INDEX" in statement for statement in second_calls)


@pytest.mark.asyncio
async def test_content_bootstrap_skips_schema_when_version_is_current() -> None:
    client = _RecordingSchemaClient(schema_version=CONTENT_SCHEMA_CURRENT_VERSION)

    await bootstrap_content_schema(client)  # type: ignore[arg-type]

    assert not any(
        "DEFINE TABLE IF NOT EXISTS crawl_sources" in statement for statement in client.calls
    )
    assert any("SELECT version FROM schema_version" in statement for statement in client.calls)


@pytest.mark.asyncio
async def test_content_bootstrap_applies_ingestion_migration_from_v7() -> None:
    client = _RecordingSchemaClient(schema_version=7)

    await bootstrap_content_schema(client)  # type: ignore[arg-type]

    assert client.schema_version == CONTENT_SCHEMA_CURRENT_VERSION
    assert not any(
        "DEFINE TABLE IF NOT EXISTS crawl_sources" in statement for statement in client.calls
    )
    assert any(
        "DEFINE FIELD IF NOT EXISTS embedding ON raw_captures" in statement
        for statement in client.calls
    )
    assert any("idx_raw_captures_org_dedupe" in statement for statement in client.calls)
    assert any("idx_raw_captures_embedding" in statement for statement in client.calls)


@pytest.mark.asyncio
async def test_content_bootstrap_does_not_repeat_migrations_after_recording_version() -> None:
    client = _RecordingSchemaClient()

    await bootstrap_content_schema(client)  # type: ignore[arg-type]
    first_call_count = len(client.calls)
    await bootstrap_content_schema(client)  # type: ignore[arg-type]
    second_calls = client.calls[first_call_count:]

    assert client.schema_version == CONTENT_SCHEMA_CURRENT_VERSION
    assert not any(
        "DEFINE TABLE IF NOT EXISTS crawl_sources" in statement for statement in second_calls
    )
