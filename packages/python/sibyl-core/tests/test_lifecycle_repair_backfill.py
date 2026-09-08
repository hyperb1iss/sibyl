"""Populated repair backfills retain progress across interrupted execution."""

import pytest

from sibyl_core.backends.surreal import schema_lifecycle_repair as migration
from sibyl_core.backends.surreal.schema import bootstrap_schema
from sibyl_core.backends.surreal.schema_helpers import split_statements
from sibyl_core.backends.surreal.schema_ownership import (
    SchemaOwnershipLost,
    try_acquire_schema_ownership,
)
from sibyl_core.models.entities import Entity, EntityType
from tests.test_reflection_identity import runtime as runtime

_PENDING_ROWS_QUERY = (
    "SELECT uuid, lifecycle_repair_key FROM entity WITH INDEX idx_entity_lifecycle_repair_key "
    "WHERE lifecycle_repair_key > $cursor "
    "AND group_id = $group_id ORDER BY lifecycle_repair_key LIMIT $limit"
)


async def test_backfill_resumes_committed_pages_and_derives_new_writes(runtime, monkeypatch):
    monkeypatch.setattr(migration, "_BATCH_SIZE", 2)
    execute = runtime.client.execute_query
    await execute("REMOVE INDEX idx_entity_lifecycle_repair_key ON entity;")
    await execute("REMOVE FIELD lifecycle_repair_key ON entity;")
    for i in range(7):
        await runtime.entity_manager.create_direct(
            Entity(
                id=f"old-{i}",
                name=f"Old {i}",
                entity_type=EntityType.EPISODE,
                metadata={"source_validation_pending": {"parent:absent": True}},
            ),
            generate_embedding=False,
        )
    for statement in split_statements(migration.LIFECYCLE_REPAIR_FIELDS):
        await execute(statement)

    pages = []

    async def interrupted(statement, **params):
        if statement.startswith("SELECT id, uuid"):
            pages.append(params["cursor"])
            if len(pages) == 2:
                raise RuntimeError("injected interruption after committed page")
        return await execute(statement, **params)

    with pytest.raises(RuntimeError, match="injected interruption"):
        await migration.migrate_lifecycle_repair(interrupted)
    checkpoint = await execute("SELECT lifecycle_repair_cursor FROM schema_version:graph;")
    cursor = checkpoint[0]["lifecycle_repair_cursor"]
    assert cursor == pages[1]
    assert cursor

    # A concurrent write behind the durable cursor derives its key immediately.
    await runtime.entity_manager.create_direct(
        Entity(
            id="a-new",
            name="New pending source",
            entity_type=EntityType.EPISODE,
            metadata={"source_validation_pending": True},
        ),
        generate_embedding=False,
    )
    resumed_pages = []
    batch_sizes = []

    async def resumed(statement, **params):
        if statement.startswith("SELECT id, uuid"):
            resumed_pages.append(params["cursor"])
        if statement.startswith("BEGIN TRANSACTION"):
            batch_sizes.append(len(params["rows"]))
        return await execute(statement, **params)

    await migration.migrate_lifecycle_repair(resumed)
    assert resumed_pages[0] == cursor
    assert all(size <= 2 for size in batch_sizes)
    rows = await execute(
        _PENDING_ROWS_QUERY + ";", cursor="", limit=100, group_id=runtime.client.group_id
    )
    assert [row["uuid"] for row in rows] == ["a-new", *[f"old-{i}" for i in range(7)]]
    checkpoint = await execute("SELECT lifecycle_repair_cursor FROM schema_version:graph;")
    assert not checkpoint[0].get("lifecycle_repair_cursor")


async def test_experimental_v22_upgrades_and_force_restores_repair_schema(runtime):
    execute = runtime.client.execute_query
    await execute("REMOVE INDEX idx_entity_lifecycle_repair_key ON entity;")
    await execute("REMOVE FIELD lifecycle_repair_key ON entity;")
    await execute(
        "DEFINE FIELD lifecycle_repair_pending ON entity TYPE bool DEFAULT false "
        "VALUE IF attributes.source_validation_pending THEN true ELSE false END;"
    )
    await execute(
        "UPDATE entity SET lifecycle_repair_pending = false WHERE lifecycle_repair_pending IS NONE;"
    )
    await execute(
        "DEFINE INDEX idx_entity_lifecycle_repair ON entity FIELDS lifecycle_repair_pending, uuid;"
    )
    await execute("UPDATE schema_version:graph SET version = 22;")
    await runtime.entity_manager.create_direct(
        Entity(
            id="old-pending",
            name="Old pending",
            entity_type=EntityType.EPISODE,
            metadata={"source_validation_pending": True},
        ),
        generate_embedding=False,
    )
    await bootstrap_schema(runtime.client)
    params = {"cursor": "", "limit": 10, "group_id": runtime.client.group_id}
    rows = await execute(_PENDING_ROWS_QUERY + ";", **params)
    assert [row["uuid"] for row in rows] == ["old-pending"]
    info = await execute("INFO FOR TABLE entity;")
    assert "idx_entity_lifecycle_repair" not in info["indexes"]
    assert "lifecycle_repair_pending" not in info["fields"]
    assert (await execute("SELECT version FROM schema_version:graph;"))[0]["version"] == 23

    await execute("REMOVE INDEX idx_entity_lifecycle_repair_key ON entity;")
    await execute("REMOVE FIELD lifecycle_repair_key ON entity;")
    await runtime.entity_manager.create_direct(
        Entity(
            id="a-lost",
            name="Pending row without a derived key",
            entity_type=EntityType.EPISODE,
            metadata={"source_validation_pending": True},
        ),
        generate_embedding=False,
    )
    await execute("UPDATE schema_version:graph SET lifecycle_repair_cursor = 'zzz';")
    await bootstrap_schema(runtime.client, force=True)
    rows = await execute(_PENDING_ROWS_QUERY + ";", **params)
    assert [row["uuid"] for row in rows] == ["a-lost", "old-pending"]


async def test_owned_backfill_resumes_after_loss_without_stale_cursor_write(
    runtime, monkeypatch, *, concurrent=False
):
    monkeypatch.setattr(migration, "_BATCH_SIZE", 2)
    execute = runtime.client.execute_query
    for i in range(5):
        await runtime.entity_manager.create_direct(
            Entity(
                id=f"owned-{i}",
                name=f"Owned {i}",
                entity_type=EntityType.EPISODE,
                metadata={"source_validation_pending": True},
            ),
            generate_embedding=False,
        )
    await execute("REMOVE INDEX idx_entity_lifecycle_repair_key ON entity;")
    owner = await try_acquire_schema_ownership(execute)
    assert owner is not None
    pages = []

    async def lose_after_page(statement, **params):
        if statement.startswith("SELECT id, uuid"):
            pages.append(params["cursor"])
            if len(pages) == 2:
                await execute("UPDATE schema_lease:graph SET deadline = time::now() - 1s;")
        return await execute(statement, **params)

    with pytest.raises(SchemaOwnershipLost):
        await migration.migrate_lifecycle_repair(
            lose_after_page, ownership=owner, concurrent=concurrent
        )
    checkpoint = await execute("SELECT lifecycle_repair_cursor FROM schema_version:graph;")
    assert checkpoint[0]["lifecycle_repair_cursor"] == pages[1]
    info = await execute("INFO FOR TABLE entity;")
    assert "idx_entity_lifecycle_repair_key" not in info["indexes"]

    successor = await try_acquire_schema_ownership(execute)
    assert successor is not None
    try:
        await migration.migrate_lifecycle_repair(
            execute, ownership=successor, concurrent=concurrent
        )
        # A late predecessor cannot clear the successor's checkpoint or schema.
        with pytest.raises(SchemaOwnershipLost):
            await migration.migrate_lifecycle_repair(execute, resume=False, ownership=owner)
        await successor.heartbeat()
        rows = await execute(
            _PENDING_ROWS_QUERY + ";", cursor="", limit=10, group_id=runtime.client.group_id
        )
        assert [row["uuid"] for row in rows] == [f"owned-{i}" for i in range(5)]
    finally:
        await successor.release()
