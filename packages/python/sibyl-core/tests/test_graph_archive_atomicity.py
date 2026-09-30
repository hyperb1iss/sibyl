"""Public graph restore commits sources and every companion as one unit."""

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.schema import bootstrap_schema
from sibyl_core.migrate.legacy_graph_archive import episode_from_payload, save_native_episode
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import content_client
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.tools.admin import create_backup, restore_backup
from tests.test_operational_projection import authority as authority
from tests.test_operational_projection import capture
from tests.test_source_integrity_archive import destination as destination
from tests.test_source_integrity_archive import runtime as runtime
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)

STAMP = "2026-09-10T02:47:11.572211763Z"


async def fingerprint(client):
    return await client.execute_query(
        "RETURN crypto::sha256(type::string({"
        "entities: (SELECT * FROM entity ORDER BY id),"
        "states: (SELECT * FROM source_states ORDER BY id),"
        "derivations: (SELECT * FROM memory_derivations ORDER BY id),"
        "episodes: (SELECT * FROM episode ORDER BY id),"
        "relationships: (SELECT * FROM relates_to ORDER BY id),"
        "mentions: (SELECT * FROM mentions ORDER BY id)}));"
    )


async def episode(client, identity):
    await save_native_episode(
        client,
        episode_from_payload(
            {
                "uuid": identity,
                "name": identity,
                "content": "Evidence",
                "created_at": STAMP,
                "valid_at": STAMP,
            },
            organization_id=client.group_id,
        ),
    )


@pytest.mark.parametrize("clean", [False, True])
async def test_late_mention_failure_rolls_back_all_graph_tables(
    runtime, destination, monkeypatch, clean
):
    await runtime.entity_manager.create_direct(
        Entity(id="incoming", entity_type=EntityType.SESSION, name="Incoming", content="New")
    )
    await runtime.relationship_manager.create_direct_bulk(
        [
            Relationship(
                id="incoming-edge",
                source_id="incoming",
                target_id="project_a",
                relationship_type=RelationshipType.RELATED_TO,
            )
        ]
    )
    await episode(runtime.client, "incoming-episode")
    for identity in ("must-survive", "retained-tombstone"):
        await destination.entity_manager.create_direct(
            Entity(id=identity, entity_type=EntityType.SESSION, name=identity, content="Old")
        )
    await destination.client.execute_query("DELETE entity WHERE uuid='retained-tombstone';")
    await episode(destination.client, "existing-episode")
    before = await fingerprint(destination.client)
    backup = await create_backup(organization_id=runtime.client.group_id)
    assert backup.success, backup.message
    backup.backup_data.mentions = [
        {
            "uuid": "missing-endpoint",
            "source_id": "incoming-episode",
            "target_id": "does-not-exist",
            "created_at": STAMP,
        }
    ]
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=destination)
    )
    result = await restore_backup(
        backup.backup_data, organization_id=runtime.client.group_id, clean=clean
    )
    assert not result.success
    assert any("mention endpoint is missing" in error for error in result.errors)
    assert (
        result.entities_restored == result.episodes_restored == result.relationships_restored == 0
    )
    assert await fingerprint(destination.client) == before


@pytest.mark.parametrize("collection", ["episodes", "relationships", "mentions"])
async def test_malformed_graph_companion_rejects_before_destination(
    runtime, monkeypatch, collection
):
    backup = await create_backup(organization_id=runtime.client.group_id)
    assert backup.success, backup.message
    setattr(backup.backup_data, collection, [{"id": "invalid-companion"}])
    loader = AsyncMock(side_effect=AssertionError("destination must not be opened"))
    monkeypatch.setattr("sibyl_core.tools.admin.get_graph_runtime", loader)
    result = await restore_backup(
        backup.backup_data, organization_id=runtime.client.group_id, clean=True
    )
    assert not result.success
    loader.assert_not_called()


@pytest.mark.parametrize("clean", [False, True])
async def test_companion_nanosecond_change_fences_source_restore(
    runtime, destination, monkeypatch, clean
):
    await episode(destination.client, "racing-episode")
    backup = await create_backup(organization_id=runtime.client.group_id)
    assert backup.success, backup.message
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=destination)
    )
    execute = destination.client.execute_query
    changed = False
    preserved = None

    async def race(query, **parameters):
        nonlocal changed, preserved
        if (
            "BEGIN TRANSACTION" in query
            and "archive_companion_fingerprint" in query
            and not changed
        ):
            changed = True
            await execute(
                "UPDATE episode SET created_at=d'2026-09-10T02:47:11.572211764Z' WHERE uuid='racing-episode';"
            )
            preserved = await fingerprint(destination.client)
        return await execute(query, **parameters)

    monkeypatch.setattr(destination.client, "execute_query", race)
    result = await restore_backup(
        backup.backup_data, organization_id=runtime.client.group_id, clean=clean
    )
    assert changed
    assert not result.success
    assert any("destination changed" in error for error in result.errors)
    assert await fingerprint(destination.client) == preserved


async def relationship_fingerprint(client, identity):
    return await client.execute_query(
        "RETURN crypto::sha256(type::string((SELECT * FROM relates_to WHERE uuid=$id)[0]));",
        id=identity,
    )


@pytest.fixture
async def native_archive_runtimes(monkeypatch):
    url = os.getenv("SIBYL_ARCHIVE_PREREQUISITE_TEST_URL")
    if not url:
        pytest.skip("native archive prerequisite database is not configured")
    credentials = {
        "url": url,
        "username": os.getenv("SIBYL_ARCHIVE_PREREQUISITE_TEST_USERNAME", "root"),
        "password": os.getenv("SIBYL_ARCHIVE_PREREQUISITE_TEST_PASSWORD", "root"),
    }
    organization = str(uuid4())
    namespace = "archive_restore_" + uuid4().hex
    clients = [
        SurrealGraphClient(
            **credentials, group_id=organization, namespace_prefix=namespace, database=name
        )
        for name in ("source", "destination")
    ]
    content = SurrealContentClient(**credentials, namespace=namespace + "_content")
    try:
        runtimes = []
        for client in clients:
            await bootstrap_schema(client)
            graph = GraphRuntime(
                client=client,
                entity_manager=EntityManager(client, group_id=organization),
                relationship_manager=RelationshipManager(client, group_id=organization),
            )
            for identity, kind in (
                ("project_a", EntityType.PROJECT),
                ("root", EntityType.NOTE),
                ("incoming-leaf", EntityType.NOTE),
                ("retained-leaf", EntityType.NOTE),
            ):
                await graph.entity_manager.create_direct(
                    Entity(id=identity, name=identity, entity_type=kind), generate_embedding=False
                )
            runtimes.append(graph)
        await bootstrap_content_schema(content)

        @asynccontextmanager
        async def session():
            yield content

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        for module in ("graph_runtime", "memory_lifecycle"):
            monkeypatch.setattr(
                f"sibyl_core.services.{module}.get_surreal_graph_runtime",
                AsyncMock(return_value=runtimes[1]),
            )
        yield tuple(runtimes)
    finally:
        for client in [*clients, content]:
            await client.close()


@pytest.mark.parametrize(
    ("skip_existing", "clean", "malformed"),
    [(True, False, False), (False, False, False), (True, True, False), (True, False, True)],
)
async def test_native_relationship_restore_respects_existing_identity(
    native_archive_runtimes, authority, monkeypatch, skip_existing, clean, malformed
):
    source_runtime, destination_runtime = native_archive_runtimes
    organization = destination_runtime.client.group_id
    monkeypatch.setattr(
        "sibyl_core.services.operational_relationships.get_source_authority_resolver",
        lambda: AsyncMock(return_value=authority),
    )
    _, observed = await capture(destination_runtime, authority)
    projection = await destination_runtime.entity_manager.publish_operational_entities(observed)
    await destination_runtime.relationship_manager.publish_operational_relationships(observed)
    protected = next(
        edge
        for edge in projection.relationships
        if edge.relationship_type == RelationshipType.PART_OF
    )
    before_protected = await destination_runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE uuid=$id;", id=protected.id
    )
    before_protected_fingerprint = await relationship_fingerprint(
        destination_runtime.client, protected.id
    )
    assert before_protected[0]["operational_derivation_required"] is True
    assert before_protected[0]["operational_source_binding"] is not None
    existing = Relationship(
        id="ordinary-collision",
        source_id="root",
        target_id="retained-leaf",
        relationship_type=RelationshipType.RELATED_TO,
        weight=0.9,
        metadata={"origin": "destination", "fact": "Retained destination evidence", "weight": 0.9},
    )
    await destination_runtime.relationship_manager.create_direct_bulk([existing])
    if malformed:
        await destination_runtime.client.execute_query(
            "UPDATE relates_to SET attributes.weight=-1 WHERE uuid=$id;", id=existing.id
        )
    before_ordinary = await destination_runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE uuid=$id;", id=existing.id
    )
    before_ordinary_fingerprint = await relationship_fingerprint(
        destination_runtime.client, existing.id
    )
    if malformed:
        assert before_ordinary[0]["attributes"]["weight"] == -1
    incoming = [
        Relationship(
            id=identity,
            source_id=source,
            target_id=target,
            relationship_type=kind,
            weight=0.35,
            metadata={"origin": "archive", "fact": "Incoming archive evidence", "weight": 0.35},
        )
        for identity, source, target, kind in (
            (existing.id, "root", "incoming-leaf", RelationshipType.DEPENDS_ON),
            ("new-ordinary", "incoming-leaf", "root", RelationshipType.RELATED_TO),
            (protected.id, "root", "retained-leaf", RelationshipType.DEPENDS_ON),
        )
    ]
    direct_control = incoming[1].model_copy(update={"id": "direct-write-control"})
    assert await destination_runtime.relationship_manager.create_direct_bulk([direct_control]) == [
        direct_control.id
    ]
    assert (
        await destination_runtime.relationship_manager.get(direct_control.id)
    ).target_id == "root"
    await source_runtime.relationship_manager.create_direct_bulk(incoming)
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=source_runtime)
    )
    backup = await create_backup(organization_id=organization)
    assert backup.success, backup.message
    assert backup.relationship_count == 3
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=destination_runtime)
    )
    result = await restore_backup(
        backup.backup_data,
        organization_id=organization,
        skip_existing=skip_existing,
        clean=clean,
    )
    assert result.success, result.errors
    after_ordinary = await destination_runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE uuid=$id;", id=existing.id
    )
    after_protected = await destination_runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE uuid=$id;", id=protected.id
    )
    if skip_existing and not clean:
        assert after_ordinary == before_ordinary
        assert await relationship_fingerprint(destination_runtime.client, existing.id) == (
            before_ordinary_fingerprint
        )
    else:
        restored = await destination_runtime.relationship_manager.get(existing.id)
        assert restored.target_id == "incoming-leaf"
        assert restored.relationship_type == RelationshipType.DEPENDS_ON
        assert restored.metadata["fact"] == "Incoming archive evidence"
        assert restored.weight == 0.35
        assert restored.metadata["origin"] == "archive"
        assert after_ordinary != before_ordinary
    if not clean:
        assert after_protected == before_protected
        assert await relationship_fingerprint(destination_runtime.client, protected.id) == (
            before_protected_fingerprint
        )
    else:
        assert after_protected[0].get("operational_derivation_required") is not True
        assert after_protected[0].get("operational_source_binding") is None
        assert (await destination_runtime.relationship_manager.get(protected.id)).target_id == (
            "retained-leaf"
        )
    new = await destination_runtime.relationship_manager.get("new-ordinary")
    assert new.source_id == "incoming-leaf"
    assert new.target_id == "root"
    assert new.metadata["fact"] == "Incoming archive evidence"
    assert result.relationships_restored == (3 if clean else 1 if skip_existing else 2)
    assert result.relationships_skipped == (0 if clean else 2 if skip_existing else 1)


async def test_native_relationship_skip_snapshot_change_fences_restore(
    native_archive_runtimes, monkeypatch
):
    source_runtime, destination_runtime = native_archive_runtimes
    organization = destination_runtime.client.group_id
    edge = Relationship(
        id="racing-ordinary",
        source_id="root",
        target_id="incoming-leaf",
        relationship_type=RelationshipType.RELATED_TO,
        metadata={"fact": "Destination evidence"},
    )
    await destination_runtime.relationship_manager.create_direct_bulk([edge])
    await source_runtime.relationship_manager.create_direct_bulk(
        [edge.model_copy(update={"metadata": {"fact": "Incoming evidence"}})]
    )
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=source_runtime)
    )
    backup = await create_backup(organization_id=organization)
    assert backup.success, backup.message
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=destination_runtime)
    )
    execute = destination_runtime.client.execute_query
    changed = False
    preserved = None

    async def race(query, **parameters):
        nonlocal changed, preserved
        if "BEGIN TRANSACTION" in query and "archive_companion_fingerprint" in parameters:
            assert not changed
            changed = True
            await destination_runtime.relationship_manager.create_direct_bulk(
                [edge.model_copy(update={"metadata": {"fact": "Concurrent evidence"}})]
            )
            preserved = await fingerprint(destination_runtime.client)
        return await execute(query, **parameters)

    monkeypatch.setattr(destination_runtime.client, "execute_query", race)
    result = await restore_backup(
        backup.backup_data, organization_id=organization, skip_existing=True
    )
    assert changed
    assert not result.success
    assert any("destination changed" in error for error in result.errors)
    assert await fingerprint(destination_runtime.client) == preserved
    current = await destination_runtime.relationship_manager.get(edge.id)
    assert current.metadata["fact"] == "Concurrent evidence"
