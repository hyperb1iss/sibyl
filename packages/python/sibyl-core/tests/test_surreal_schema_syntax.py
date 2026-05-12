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
    EDGE_DEFINITIONS,
    NODE_DEFINITIONS,
    render_fulltext_compatible_sql,
)


class _RecordingSchemaClient:
    def __init__(self, duplicate_index_name: str) -> None:
        self.duplicate_index_name = duplicate_index_name
        self.calls: list[str] = []
        self._url = ""

    async def execute_query(self, statement: str) -> None:
        self.calls.append(statement)
        if self.duplicate_index_name in statement:
            raise RuntimeError(
                f"Database index `{self.duplicate_index_name}` already contains 'dirty-row'"
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


def test_runtime_schemafull_tables_are_altered_after_define() -> None:
    schema = "\n".join((AUTH_SCHEMA_DEFINITIONS, CONTENT_SCHEMA_DEFINITIONS))
    tables = (*AUTH_TABLES, *CONTENT_TABLES)

    for table in tables:
        assert (
            f"DEFINE TABLE IF NOT EXISTS {table} SCHEMAFULL;\n"
            f"ALTER TABLE IF EXISTS {table} SCHEMAFULL;"
        ) in schema


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
    assert "FIELDS description FULLTEXT" in NODE_DEFINITIONS
    assert "FIELDS content FULLTEXT" in NODE_DEFINITIONS
    assert "FIELDS attributes.description FULLTEXT" not in NODE_DEFINITIONS
    assert "FIELDS attributes.content FULLTEXT" not in NODE_DEFINITIONS
    assert "description = description ?? attributes.description" in NODE_DEFINITIONS
    assert "content = content ?? attributes.content" in NODE_DEFINITIONS


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
