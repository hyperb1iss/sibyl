"""raw_captures sheds the indexes no statement reads and regains the lineage index.

Every capture write and every recall exposure stamp maintained 22 indexes,
two of them FULLTEXT postings over title and content that nothing queries
(the lexical lane reads the raw_lexical_* mirrors). Migration 56 removes
those and the single-column duplicates of the lookup composites, keeps
purge_after for the purge job, and re-asserts the source lineage element
index that a live namespace at the current version was found without.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_SCHEMA_CURRENT_VERSION,
    _content_schema_migrations,
    bootstrap_content_schema,
)
from sibyl_core.backends.surreal.schema_invariants import fetch_table_indexes

REMOVED = (
    "idx_raw_captures_title_ft",
    "idx_raw_captures_content_ft",
    "idx_raw_captures_source",
    "idx_raw_captures_org_dedupe",
    "idx_raw_captures_captured_at",
    "idx_raw_captures_created_at",
    "idx_raw_captures_entity_id",
)
KEPT = (
    "idx_raw_captures_uuid",
    "idx_raw_captures_org_uuid",
    "idx_raw_captures_purge_after",
    "idx_raw_captures_source_lookup",
    "idx_raw_captures_dedupe_lookup",
    "idx_raw_captures_source_lineage",
    "idx_raw_captures_private_recent",
    "idx_raw_captures_scoped_recent",
    "idx_raw_captures_embedding",
)


def test_migration_56_prunes_dead_indexes_and_reasserts_lineage() -> None:
    assert CONTENT_SCHEMA_CURRENT_VERSION >= 56
    migration = {item.version: item for item in _content_schema_migrations(url="")}[56]
    assert migration.name == "content_raw_capture_index_prune"
    removed = []
    for statement in migration.statements[:-1]:
        match = re.fullmatch(
            r"REMOVE INDEX IF EXISTS (\w+) ON TABLE raw_captures;", statement.strip()
        )
        assert match is not None, statement
        removed.append(match.group(1))
    assert tuple(removed) == REMOVED
    assert migration.statements[-1].strip() == (
        "DEFINE INDEX IF NOT EXISTS idx_raw_captures_source_lineage "
        "ON raw_captures FIELDS metadata.raw_source_ids.*;"
    )


@pytest.fixture
async def content_client() -> AsyncIterator[SurrealContentClient]:
    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client, reset=True)
        yield client
    finally:
        await client.close()


async def test_bootstrapped_raw_captures_carry_only_live_indexes(
    content_client: SurrealContentClient,
) -> None:
    indexes = await fetch_table_indexes(content_client.execute_query, "raw_captures")
    assert not set(REMOVED) & set(indexes), sorted(set(REMOVED) & set(indexes))
    assert set(KEPT) <= set(indexes), sorted(set(KEPT) - set(indexes))
    lexical = await fetch_table_indexes(content_client.execute_query, "raw_lexical_originals")
    assert {"idx_raw_lexical_originals_title_ft", "idx_raw_lexical_originals_content_ft"} <= set(
        lexical
    )
