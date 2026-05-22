"""Regression checks for SurrealDB schema syntax accepted by the server parser."""

from __future__ import annotations

import pytest

from sibyl_core.backends.surreal.auth_schema import (
    AUTH_SCHEMA_DEFINITIONS,
    AUTH_TABLES,
    bootstrap_auth_schema,
)
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_SCHEMA_DEFINITIONS,
    CONTENT_TABLES,
    bootstrap_content_schema,
)
from sibyl_core.backends.surreal.schema import (
    CURRENT_SCHEMA_MAINTENANCE_DEFINITIONS,
    EDGE_DEFINITIONS,
    NODE_DEFINITIONS,
    RELATION_EDGE_CLEANUP_DEFINITIONS,
    RELATION_ENDPOINT_BACKFILL_DEFINITIONS,
    RELATION_ENDPOINT_SCHEMA_DEFINITIONS,
    bootstrap_schema,
    render_fulltext_compatible_sql,
)
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
    ) -> None:
        self.duplicate_index_name = duplicate_index_name
        self.schema_version = schema_version
        self.missing_tables = missing_tables or set()
        self.calls: list[str] = []
        self._url = ""
        self.group_id = "org_123"

    async def execute_query(self, statement: str, **_params: object) -> object:
        self.calls.append(statement)
        stripped = statement.strip()
        if statement.strip().startswith("SELECT version FROM schema_version"):
            return [{"version": self.schema_version}]
        if statement.strip().startswith("UPSERT schema_version:graph"):
            self.schema_version = GRAPH_SCHEMA_CURRENT_VERSION
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


def test_runtime_schemafull_tables_are_altered_after_define() -> None:
    schema = "\n".join((AUTH_SCHEMA_DEFINITIONS, CONTENT_SCHEMA_DEFINITIONS))
    tables = (*AUTH_TABLES, *CONTENT_TABLES)

    for table in tables:
        assert (
            f"DEFINE TABLE IF NOT EXISTS {table} SCHEMAFULL;\n"
            f"ALTER TABLE IF EXISTS {table} SCHEMAFULL;"
        ) in schema


def test_auth_invitation_schema_supports_hashed_tokens() -> None:
    assert "DEFINE FIELD OVERWRITE token ON organization_invitations TYPE option<string>" in (
        AUTH_SCHEMA_DEFINITIONS
    )
    assert "DEFINE FIELD IF NOT EXISTS token_hash ON organization_invitations" in (
        AUTH_SCHEMA_DEFINITIONS
    )
    assert "REMOVE INDEX IF EXISTS idx_organization_invitations_token" in AUTH_SCHEMA_DEFINITIONS
    assert "idx_organization_invitations_token_hash" in AUTH_SCHEMA_DEFINITIONS


def test_auth_schema_includes_oidc_identity_tables() -> None:
    assert "identity_provider" in AUTH_TABLES
    assert "user_identity" in AUTH_TABLES
    assert "DEFINE FIELD IF NOT EXISTS subject_key ON user_identity TYPE string" in (
        AUTH_SCHEMA_DEFINITIONS
    )
    assert "idx_user_identity_provider_subject" in AUTH_SCHEMA_DEFINITIONS


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
    for relation in ("relates_to", "mentions", "has_episode", "next_episode", "has_member"):
        assert f"DEFINE TABLE OVERWRITE {relation} SCHEMAFULL TYPE RELATION" in EDGE_DEFINITIONS

    assert "relates_to SCHEMAFULL TYPE RELATION IN entity OUT entity ENFORCED" in EDGE_DEFINITIONS
    assert "mentions SCHEMAFULL TYPE RELATION IN episode OUT entity ENFORCED" in EDGE_DEFINITIONS
    assert "has_episode SCHEMAFULL TYPE RELATION IN saga OUT episode ENFORCED" in EDGE_DEFINITIONS
    assert "next_episode SCHEMAFULL TYPE RELATION IN episode OUT episode ENFORCED" in EDGE_DEFINITIONS
    assert (
        "has_member SCHEMAFULL TYPE RELATION IN community OUT entity | community ENFORCED"
        in EDGE_DEFINITIONS
    )


def test_graph_hnsw_indexes_use_configurable_defaults() -> None:
    assert "idx_entity_embedding" in NODE_DEFINITIONS
    assert "idx_community_embedding" in NODE_DEFINITIONS
    assert "idx_relates_fact_embedding" in EDGE_DEFINITIONS
    assert "HNSW DIMENSION 1024 DIST COSINE TYPE F32 EFC 150 M 12" in NODE_DEFINITIONS
    assert "HNSW DIMENSION 1024 DIST COSINE TYPE F32 EFC 150 M 12" in EDGE_DEFINITIONS


def test_graph_relation_endpoint_indexes_match_hot_lookups() -> None:
    assert "DEFINE FIELD IF NOT EXISTS source_id ON mentions" in EDGE_DEFINITIONS
    assert "DEFINE FIELD IF NOT EXISTS target_id ON mentions" in EDGE_DEFINITIONS
    assert "idx_relates_group_source_created" in EDGE_DEFINITIONS
    assert "idx_relates_group_target_created" in EDGE_DEFINITIONS
    assert "idx_mentions_group_source_created" in EDGE_DEFINITIONS
    assert "idx_mentions_group_target_created" in EDGE_DEFINITIONS


def test_graph_relation_endpoint_backfill_is_versioned() -> None:
    assert "idx_relates_group_source_created" in RELATION_ENDPOINT_SCHEMA_DEFINITIONS
    assert "idx_mentions_group_source_created" in RELATION_ENDPOINT_SCHEMA_DEFINITIONS
    assert "UPDATE relates_to SET" in RELATION_ENDPOINT_BACKFILL_DEFINITIONS
    assert "source_id = source_id ?? in.uuid" in RELATION_ENDPOINT_BACKFILL_DEFINITIONS
    assert "target_id = target_id ?? out.uuid" in RELATION_ENDPOINT_BACKFILL_DEFINITIONS
    assert "UPDATE mentions SET" in RELATION_ENDPOINT_BACKFILL_DEFINITIONS
    assert RELATION_ENDPOINT_BACKFILL_DEFINITIONS in CURRENT_SCHEMA_MAINTENANCE_DEFINITIONS


def test_graph_relation_cleanup_covers_all_relation_tables() -> None:
    for relation in ("relates_to", "mentions", "has_episode", "next_episode", "has_member"):
        assert f"DELETE FROM {relation}" in RELATION_EDGE_CLEANUP_DEFINITIONS

    assert "SELECT VALUE id FROM entity" in RELATION_EDGE_CLEANUP_DEFINITIONS
    assert "SELECT VALUE id FROM episode" in RELATION_EDGE_CLEANUP_DEFINITIONS
    assert "SELECT VALUE id FROM saga" in RELATION_EDGE_CLEANUP_DEFINITIONS
    assert "SELECT VALUE id FROM community" in RELATION_EDGE_CLEANUP_DEFINITIONS


def test_current_graph_maintenance_skips_orphan_cleanup() -> None:
    for relation in ("relates_to", "mentions", "has_episode", "next_episode", "has_member"):
        assert f"DELETE FROM {relation}" not in CURRENT_SCHEMA_MAINTENANCE_DEFINITIONS

    assert "SELECT VALUE id FROM entity" not in CURRENT_SCHEMA_MAINTENANCE_DEFINITIONS


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
        index for index, statement in enumerate(client.calls) if "DELETE FROM relates_to" in statement
    )
    assert cleanup_index < relation_define_index


@pytest.mark.asyncio
async def test_graph_bootstrap_skips_missing_relation_cleanup_on_new_schema() -> None:
    client = _RecordingSchemaClient(missing_tables={"relates_to"})

    await bootstrap_schema(client)  # type: ignore[arg-type]

    assert any("DEFINE TABLE OVERWRITE relates_to" in statement for statement in client.calls)


@pytest.mark.asyncio
async def test_graph_bootstrap_runs_light_maintenance_when_version_is_current() -> None:
    client = _RecordingSchemaClient(schema_version=GRAPH_SCHEMA_CURRENT_VERSION)

    await bootstrap_schema(client)  # type: ignore[arg-type]

    assert not any("REMOVE INDEX" in statement for statement in client.calls)
    assert any("UPDATE entity SET" in statement for statement in client.calls)
    assert not any("DEFINE TABLE IF NOT EXISTS entity" in statement for statement in client.calls)
    assert "description = description ?? attributes.description" in (
        CURRENT_SCHEMA_MAINTENANCE_DEFINITIONS
    )
    assert any("UPDATE relates_to SET" in statement for statement in client.calls)
    assert any("UPDATE mentions SET" in statement for statement in client.calls)
    assert not any("DELETE FROM relates_to" in statement for statement in client.calls)
    assert not any("DELETE FROM mentions" in statement for statement in client.calls)


@pytest.mark.asyncio
async def test_graph_bootstrap_applies_migrations_without_full_rebuild() -> None:
    client = _RecordingSchemaClient(schema_version=GRAPH_SCHEMA_CURRENT_VERSION - 1)

    await bootstrap_schema(client)  # type: ignore[arg-type]

    assert not any("DEFINE TABLE IF NOT EXISTS entity" in statement for statement in client.calls)
    assert not any("DEFINE TABLE OVERWRITE relates_to" in statement for statement in client.calls)
    assert any("idx_relates_group_source_created" in statement for statement in client.calls)
    assert any("idx_mentions_group_source_created" in statement for statement in client.calls)
    assert any("UPDATE relates_to SET" in statement for statement in client.calls)
    assert not any("DELETE FROM relates_to" in statement for statement in client.calls)
    assert client.schema_version == GRAPH_SCHEMA_CURRENT_VERSION


@pytest.mark.asyncio
async def test_graph_bootstrap_rebuilds_when_current_schema_is_missing_relation() -> None:
    client = _RecordingSchemaClient(
        schema_version=GRAPH_SCHEMA_CURRENT_VERSION,
        missing_tables={"relates_to"},
    )

    await bootstrap_schema(client)  # type: ignore[arg-type]

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
