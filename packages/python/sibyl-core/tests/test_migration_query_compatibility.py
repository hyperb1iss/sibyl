"""Migration transactions render the canonical upsert for the target backend."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.backends.surreal import content_schema
from sibyl_core.migrate import collapse_epics, scope_backfill
from sibyl_core.models.entities import Entity, EntityType


@pytest.mark.parametrize("module", [collapse_epics, scope_backfill])
@pytest.mark.parametrize(
    ("url", "expected", "absent"),
    [
        ("memory://", "type::is::object", "type::is_object"),
        ("ws://127.0.0.1:33333/rpc", "type::is_object", "type::is::object"),
    ],
)
async def test_migration_transaction_renders_backend_predicates(
    module, url, expected, absent, monkeypatch
):
    client = SimpleNamespace(_url=url, execute_query_raw=AsyncMock(return_value=[]))
    monkeypatch.setattr(module, "heal_entity_metadata_snapshots", AsyncMock())
    if module is collapse_epics:
        records = [{"uuid": "migration-row"}]
        await module._apply_records(client, records, group_id="test-org", reverse=False)
    else:
        entity = Entity(id="migration-row", name="row", entity_type=EntityType.EPISODE)
        await module._apply(client, [entity], group_id="test-org", operation="forward")
    query = client.execute_query_raw.await_args.args[0]
    assert expected in query
    assert absent not in query
    assert query.startswith("BEGIN TRANSACTION;")
    assert query.endswith("COMMIT TRANSACTION;")
    assert client.execute_query_raw.await_args.kwargs["rows"][0]["uuid"] == "migration-row"


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
