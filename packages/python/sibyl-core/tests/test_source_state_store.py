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
    from sibyl_core.backends.surreal.schema import bootstrap_schema

    await runtime.client.execute_query("UPDATE schema_version SET version=23 WHERE name='graph';")
    await bootstrap_schema(runtime.client, force=True)
    first = await runtime.client.execute_query("SELECT * FROM source_states ORDER BY source_id;")
    assert len(first) == 3
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
