"""Older public archives cannot undo newer source visibility decisions."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.persistence import content_archive
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.surreal_content import recall_raw_memory, remember_raw_memory


@pytest.fixture
async def revocation_store(monkeypatch):
    org = str(uuid4())
    client = SurrealContentClient(url="memory://")
    graph = SurrealGraphClient(group_id=org, url="memory://")
    close = client.close
    try:
        await bootstrap_content_schema(client)
        await prepare_graph_schema(graph)
        runtime = GraphRuntime(
            client=graph,
            entity_manager=EntityManager(graph, group_id=org),
            relationship_manager=RelationshipManager(graph, group_id=org),
        )
        for path in (
            "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
            "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
        ):
            monkeypatch.setattr(path, AsyncMock(return_value=runtime))
        monkeypatch.setattr(client, "close", AsyncMock())
        monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: client)
        monkeypatch.setattr(
            "sibyl_core.services.content_models.configured_raw_memory_embedding_provider",
            lambda: None,
        )

        @asynccontextmanager
        async def session():
            yield client

        monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
        yield client, org
    finally:
        await close()
        await graph.close()


async def capture(org, source_id="original"):
    return await remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id=source_id,
        raw_content="amethyst deployment evidence",
        embedding_provider=None,
    )


async def visible(org):
    return {
        row.id
        for row in await recall_raw_memory(
            organization_id=org,
            principal_id="owner",
            query="amethyst",
        )
    }


async def source_fingerprint(client, source_id):
    return await client.execute_query(
        "RETURN crypto::sha256(type::string({"
        "row: (SELECT * FROM raw_captures WHERE uuid=$source_id),"
        "state: (SELECT * FROM source_states WHERE source_id=$source_id)}));",
        source_id=source_id,
    )


@pytest.mark.parametrize("clean", [False, True])
@pytest.mark.parametrize("action", ["hide", "delete", "supersede"])
async def test_public_restore_retains_newer_raw_revocation(revocation_store, clean, action):
    client, org = revocation_store
    source = await capture(org)
    replacement = await capture(org, "replacement")
    archive = await content_archive.export_content_archive_payload(org)
    await apply_memory_correction(
        organization_id=org,
        principal_id="owner",
        source_id=source.id,
        action=action,
        **({"replacement_source_id": replacement.id} if action == "supersede" else {}),
    )
    assert source.id not in await visible(org)
    before = await source_fingerprint(client, source.id)
    result = await content_archive.restore_content_archive_payload(archive, clean=clean)
    assert result.success, result.errors
    assert source.id not in await visible(org)
    assert await source_fingerprint(client, source.id) == before
    assert any(
        row["source_id"] == source.id and row["reason"] == "retained_source_revocation"
        for row in result.integrity_conflicts
    )


async def test_clean_omission_retains_tombstone_before_later_restore(revocation_store):
    client, org = revocation_store
    empty = await content_archive.export_content_archive_payload(org)
    source = await capture(org)
    archive = await content_archive.export_content_archive_payload(org)
    await apply_memory_correction(
        organization_id=org,
        principal_id="owner",
        source_id=source.id,
        action="hide",
    )
    result = await content_archive.restore_content_archive_payload(empty, clean=True)
    assert result.success, result.errors
    assert not await client.execute_query(
        "SELECT * FROM raw_captures WHERE uuid=$id;", id=source.id
    )
    state = await client.execute_query(
        "SELECT * FROM source_states WHERE source_id=$id;", id=source.id
    )
    assert state[0]["deleted"] is True
    before = await source_fingerprint(client, source.id)
    result = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert result.success, result.errors
    assert source.id not in await visible(org)
    assert await source_fingerprint(client, source.id) == before
    assert any(row["reason"] == "retained_tombstone" for row in result.integrity_conflicts)


async def test_forward_archive_restores_explicitly_unhidden_source(revocation_store, monkeypatch):
    _, org = revocation_store
    source = await capture(org)
    await apply_memory_correction(
        organization_id=org,
        principal_id="owner",
        source_id=source.id,
        action="hide",
    )
    hidden = await content_archive.export_content_archive_payload(org)
    await apply_memory_correction(
        organization_id=org,
        principal_id="owner",
        source_id=source.id,
        action="restore",
    )
    active = await content_archive.export_content_archive_payload(org)
    destination = SurrealContentClient(url="memory://")
    close = destination.close
    try:
        await bootstrap_content_schema(destination)
        monkeypatch.setattr(destination, "close", AsyncMock())
        monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)

        @asynccontextmanager
        async def session():
            yield destination

        monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
        result = await content_archive.restore_content_archive_payload(hidden, clean=True)
        assert result.success, result.errors
        assert source.id not in await visible(org)
        result = await content_archive.restore_content_archive_payload(active, clean=True)
        assert result.success, result.errors
        assert not result.integrity_conflicts
        assert source.id in await visible(org)
    finally:
        await close()


async def test_older_active_content_remains_restorable_with_same_audience(revocation_store):
    from sibyl_core.services.surreal_content import get_raw_memory

    _, org = revocation_store
    source = await capture(org)
    archive = await content_archive.export_content_archive_payload(org)
    await apply_memory_correction(
        organization_id=org,
        principal_id="owner",
        source_id=source.id,
        action="revise",
        revised_content="amethyst changed deployment",
    )
    result = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert result.success, result.errors
    restored = await get_raw_memory(memory_id=source.id, organization_id=org)
    assert restored.raw_content == source.raw_content
    assert source.id in await visible(org)


async def test_older_archive_cannot_change_current_raw_owner(revocation_store):
    from dataclasses import replace

    from sibyl_core.services.content_raw_persistence import save_raw_memory
    from sibyl_core.services.surreal_content import get_raw_memory

    client, org = revocation_store
    source = await capture(org)
    archive = await content_archive.export_content_archive_payload(org)
    await save_raw_memory(replace(source, principal_id="other-owner"), embedding_provider=None)
    before = await source_fingerprint(client, source.id)
    result = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert result.success, result.errors
    assert await source_fingerprint(client, source.id) == before
    assert source.id not in await visible(org)
    restored = await get_raw_memory(memory_id=source.id, organization_id=org)
    assert restored.principal_id == "other-owner"
    assert any(row["reason"] == "retained_source_authority" for row in result.integrity_conflicts)
