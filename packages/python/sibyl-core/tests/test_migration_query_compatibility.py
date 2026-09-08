"""Content migration predicates match the target backend."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.backends.surreal import content_schema


@pytest.mark.parametrize("url", ["memory://", "ws://127.0.0.1:33333/rpc"])
async def test_content_bootstrap_renders_checkpoint_migration(url, monkeypatch):
    client = SimpleNamespace(_url=url, execute_query=AsyncMock())
    apply = AsyncMock()
    monkeypatch.setattr(content_schema, "_assert_content_migrations_safe", AsyncMock())
    monkeypatch.setattr(content_schema, "apply_schema_migrations", apply)

    await content_schema.bootstrap_content_schema(client)

    migrations = apply.await_args.args[1]
    assert migrations[0].version == 1
    checkpoint = next(migration for migration in migrations if migration.version == 27)
    definition, backfill = checkpoint.statements
    assert definition.startswith("-- A checkpoint")
    assert "DEFINE FIELD OVERWRITE legacy_content_checkpoint" in definition
    assert "LET $checkpoint_rows = (SELECT id, revision, metadata" in backfill
    assert "FOR $row IN $checkpoint_rows {" in backfill
    assert "LET $capture_id = $row.id;" in backfill
    assert "UPDATE $capture_id MERGE" in backfill
    if url == "memory://":
        assert definition == content_schema.CONTENT_LEGACY_CONTENT_CHECKPOINT_DEFINITIONS
        assert backfill == content_schema.CONTENT_LEGACY_CONTENT_CHECKPOINT_BACKFILL
    else:
        for predicate in ("array", "object", "int"):
            assert f"type::is_{predicate}(" in definition
        assert "type::is_object(" in backfill
        assert "type::is::" not in definition + backfill
