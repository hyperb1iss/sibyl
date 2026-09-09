"""Durable source identity against the actual embedded schema and mutation owners."""

from __future__ import annotations

import pytest

from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.source_observations import GraphSourceSnapshot
from sibyl_core.services.source_state_store import load_source_snapshot
from tests.test_reflection_identity import runtime as _runtime

runtime = _runtime


async def test_graph_source_epoch_survives_delete_recreate(runtime: GraphRuntime) -> None:
    source = SourceIdentity(runtime.client.group_id, SourceKind.GRAPH_ENTITY, "source_a")

    async def snapshot():
        return await load_source_snapshot(
            source,
            organization_id=source.organization_id,
            execute_query=runtime.client.execute_query,
        )

    await runtime.entity_manager.create_direct(
        Entity(
            id=source.id,
            entity_type=EntityType.EPISODE,
            name="Original",
            content="Original evidence",
        )
    )
    first = await snapshot()
    assert isinstance(first, GraphSourceSnapshot)
    await runtime.client.execute_query(
        "UPDATE entity SET retrieval_count += 1, revision += 1 WHERE uuid=$uuid;", uuid=source.id
    )
    used = await snapshot()
    assert isinstance(used, GraphSourceSnapshot)
    assert used.observation.same_evidence(first.observation)
    assert used.observation.revision > first.observation.revision
    await runtime.entity_manager.delete(source.id)
    assert await snapshot() is None
    state = await runtime.client.execute_query(
        "SELECT * FROM source_states WHERE source_id=$uuid;", uuid=source.id
    )
    assert state[0]["deleted"] is True
    assert "content" not in state[0]
    await runtime.entity_manager.create_direct(
        Entity(
            id=source.id,
            entity_type=EntityType.EPISODE,
            name="Original",
            content="Original evidence",
        )
    )
    recreated = await snapshot()
    assert isinstance(recreated, GraphSourceSnapshot)
    assert recreated.observation.generation > first.observation.generation
    assert not recreated.observation.same_evidence(first.observation)


async def test_graph_source_event_rolls_back_with_failed_mutation(runtime: GraphRuntime) -> None:
    await runtime.entity_manager.create_direct(
        Entity(
            id="rollback",
            entity_type=EntityType.EPISODE,
            name="Original",
            content="Original evidence",
        )
    )
    before = await runtime.client.execute_query(
        "SELECT * FROM source_states WHERE source_id='rollback';"
    )
    with pytest.raises(Exception, match="reject"):
        await runtime.client.execute_query(
            "RETURN { UPDATE entity SET content='changed' WHERE uuid='rollback'; THROW 'reject'; };"
        )
    assert (
        await runtime.client.execute_query(
            "SELECT * FROM source_states WHERE source_id='rollback';"
        )
        == before
    )


async def test_graph_epoch_backfill_is_restartable_and_preserves_high_water(
    runtime: GraphRuntime,
) -> None:
    from sibyl_core.backends.surreal.schema_source_states import migrate_source_states

    await runtime.entity_manager.create_direct(
        Entity(id="backfill", entity_type=EntityType.EPISODE, name="Evidence")
    )
    await runtime.client.execute_query(
        "REMOVE EVENT maintain_source_state ON entity; DELETE source_states;"
    )
    source_rows = await runtime.client.execute_query("SELECT * FROM entity ORDER BY uuid;")
    from sibyl_core.backends.surreal.schema import bootstrap_schema

    await runtime.client.execute_query("UPDATE schema_version SET version=23 WHERE name='graph';")
    await bootstrap_schema(runtime.client, force=True)
    first = await runtime.client.execute_query("SELECT * FROM source_states ORDER BY source_id;")
    assert len(first) == 3
    assert await runtime.client.execute_query("SELECT * FROM entity ORDER BY uuid;") == source_rows
    await migrate_source_states(runtime.client.execute_query, kind=SourceKind.GRAPH_ENTITY)
    assert (
        await runtime.client.execute_query("SELECT * FROM source_states ORDER BY source_id;")
        == first
    )
    with pytest.raises(Exception, match="immutable"):
        await runtime.client.execute_query("UPDATE entity SET uuid='other' WHERE uuid='backfill';")
    assert (
        await runtime.client.execute_query("SELECT * FROM source_states ORDER BY source_id;")
        == first
    )


async def test_graph_epoch_reset_retires_retained_state(runtime: GraphRuntime) -> None:
    from sibyl_core.backends.surreal.schema import bootstrap_schema

    source = SourceIdentity(runtime.client.group_id, SourceKind.GRAPH_ENTITY, "project_a")
    original = await load_source_snapshot(
        source, organization_id=source.organization_id, execute_query=runtime.client.execute_query
    )
    assert isinstance(original, GraphSourceSnapshot)
    await bootstrap_schema(runtime.client, reset=True)
    tombstones = await runtime.client.execute_query(
        "SELECT * FROM source_states WHERE source_id=$uuid;", uuid=source.id
    )
    assert tombstones[0]["deleted"] is True
    await runtime.entity_manager.create_direct(
        Entity(id=source.id, entity_type=EntityType.PROJECT, name="project_a")
    )
    recreated = await load_source_snapshot(
        source, organization_id=source.organization_id, execute_query=runtime.client.execute_query
    )
    assert isinstance(recreated, GraphSourceSnapshot)
    assert recreated.observation.generation > original.observation.generation


async def test_raw_epoch_backfill_preserves_source_rows_and_deletion_state() -> None:
    from sibyl_core.backends.surreal import SurrealContentClient
    from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
    from sibyl_core.backends.surreal.schema_source_states import (
        migrate_source_states,
        source_state_event,
    )

    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client, reset=True)
        await client.execute_query("REMOVE EVENT maintain_source_state ON raw_captures;")
        await client.execute_query(
            "CREATE raw_captures:live SET uuid='live', organization_id='org-backfill', "
            "raw_content='Original evidence', revision=7; "
            "CREATE raw_captures:deleted SET uuid='deleted', organization_id='org-backfill', "
            "raw_content='Deleted evidence', revision=9, deleted_at=time::now();"
        )
        before = await client.execute_query("SELECT * FROM raw_captures ORDER BY uuid;")
        await client.execute_query(source_state_event(SourceKind.RAW_CAPTURE))
        await migrate_source_states(client.execute_query, kind=SourceKind.RAW_CAPTURE)
        assert await client.execute_query("SELECT * FROM raw_captures ORDER BY uuid;") == before
        states = await client.execute_query("SELECT * FROM source_states ORDER BY source_id;")
        assert [(row["source_id"], row["revision"], row["deleted"]) for row in states] == [
            ("deleted", 9, True),
            ("live", 7, False),
        ]
        await migrate_source_states(client.execute_query, kind=SourceKind.RAW_CAPTURE)
        assert (
            await client.execute_query("SELECT * FROM source_states ORDER BY source_id;") == states
        )
        await client.execute_query("UPDATE raw_captures:live SET raw_content='Changed evidence';")
        changed = await client.execute_query("SELECT * FROM source_states WHERE source_id='live';")
        assert changed[0]["generation"] == 2
    finally:
        await client.close()
