"""Public graph restore commits sources and every companion as one unit."""

from unittest.mock import AsyncMock

import pytest

from sibyl_core.migrate.legacy_graph_archive import episode_from_payload, save_native_episode
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.tools.admin import create_backup, restore_backup
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
