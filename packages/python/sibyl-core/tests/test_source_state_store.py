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


async def test_raw_source_generation_survives_actual_purge_and_archive_restore(monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from sibyl.persistence import content_archive
    from sibyl.persistence.surreal import content as api_content

    from sibyl_core.backends.surreal import SurrealContentClient
    from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
    from sibyl_core.services import content_client
    from sibyl_core.services.content_raw_persistence import remember_raw_memory
    from sibyl_core.services.source_state_store import RawSourceSnapshot

    client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(client)
    close = client.close
    monkeypatch.setattr(client, "close", AsyncMock())

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    monkeypatch.setattr(api_content, "surreal_content_client", session)
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: client)
    org, user = str(uuid4()), str(uuid4())
    try:
        raw = await remember_raw_memory(
            organization_id=org,
            principal_id=user,
            source_id="capture",
            raw_content="Original raw evidence",
            embedding_provider=None,
        )
        source = SourceIdentity(org, SourceKind.RAW_CAPTURE, raw.id)

        async def snapshot():
            return await load_source_snapshot(
                source, organization_id=org, execute_query=client.execute_query
            )

        original = await snapshot()
        assert isinstance(original, RawSourceSnapshot)
        await client.execute_query(
            "UPDATE raw_captures SET retrieval_count += 1, revision += 1 WHERE uuid=$uuid;",
            uuid=raw.id,
        )
        used = await snapshot()
        assert isinstance(used, RawSourceSnapshot)
        assert used.observation.same_evidence(original.observation)
        await client.execute_query(
            "UPDATE raw_captures SET metadata.memory_lifecycle = {state:'hidden'} WHERE uuid=$uuid;",
            uuid=raw.id,
        )
        hidden = await snapshot()
        assert isinstance(hidden, RawSourceSnapshot)
        assert hidden.observation.generation > used.observation.generation
        await client.execute_query(
            "UPDATE raw_captures SET metadata.memory_lifecycle = NONE WHERE uuid=$uuid;",
            uuid=raw.id,
        )
        archive = await content_archive.export_content_archive_payload(org)
        await api_content.soft_delete_private_raw_captures_for_user(
            user_id=user, purge_after=datetime.now(UTC) - timedelta(seconds=1)
        )
        assert await snapshot() is None
        await api_content.purge_due_deleted_raw_captures(now=datetime.now(UTC))
        assert not await client.execute_query(
            "SELECT * FROM raw_captures WHERE uuid=$uuid;", uuid=raw.id
        )
        retired = await client.execute_query(
            "SELECT * FROM source_states WHERE source_id=$uuid;", uuid=raw.id
        )
        assert retired[0]["deleted"] is True
        result = await content_archive.restore_content_archive_payload(archive)
        assert not result.errors
        restored = await snapshot()
        assert isinstance(restored, RawSourceSnapshot)
        assert restored.observation.generation > retired[0]["generation"]
        assert restored.observation.content_sha256 == original.observation.content_sha256
        result = await content_archive.restore_content_archive_payload(archive, clean=True)
        assert not result.errors
        replaced = await snapshot()
        assert isinstance(replaced, RawSourceSnapshot)
        assert replaced.observation.generation > restored.observation.generation
    finally:
        await close()


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
