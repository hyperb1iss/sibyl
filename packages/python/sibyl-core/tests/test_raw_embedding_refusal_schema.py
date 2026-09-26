"""The raw capture refusal table exists on a fresh store and after the v48 upgrade."""

from __future__ import annotations

from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_SCHEMA_CURRENT_VERSION,
    CONTENT_SCHEMA_NAME,
    CONTENT_TABLES,
)
from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_embedding_states import (
    RAW_EMBEDDING_REFUSAL_SCHEMA_VERSION,
    RAW_EMBEDDING_REFUSALS_TABLE,
)
from sibyl_core.backends.surreal.schema_version import schema_version_record_id


async def _refusal_indexes(client: SurrealContentClient) -> dict[str, str]:
    info = normalize_records(
        await client.execute_query(f"INFO FOR TABLE {RAW_EMBEDDING_REFUSALS_TABLE};")
    )
    return dict(info[0].get("indexes") or {}) if info else {}


async def _version(client: SurrealContentClient) -> object:
    rows = normalize_records(
        await client.execute_query(
            f"SELECT version FROM [{schema_version_record_id(CONTENT_SCHEMA_NAME)}];"
        )
    )
    return rows[0].get("version") if rows else None


async def test_refusal_table_is_created_and_upgraded_into() -> None:
    assert RAW_EMBEDDING_REFUSALS_TABLE in CONTENT_TABLES
    assert CONTENT_SCHEMA_CURRENT_VERSION == RAW_EMBEDDING_REFUSAL_SCHEMA_VERSION == 48
    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client, reset=True)
        indexes = await _refusal_indexes(client)
        assert "UNIQUE" in indexes["idx_raw_embedding_refusals_capture"]
        assert "idx_raw_embedding_refusals_expiry" in indexes

        # A store at v47 (698's release) gains the table when it upgrades.
        await client.execute_query(f"REMOVE TABLE IF EXISTS {RAW_EMBEDDING_REFUSALS_TABLE};")
        await client.execute_query(
            "UPDATE type::record($record) SET version = 47;",
            record=schema_version_record_id(CONTENT_SCHEMA_NAME),
        )
        await bootstrap_content_schema(client)

        assert await _version(client) == 48
        assert "UNIQUE" in (await _refusal_indexes(client))["idx_raw_embedding_refusals_capture"]
    finally:
        await client.close()
