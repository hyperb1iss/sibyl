"""Public archive owners retain the complete protected source contract."""

import json

from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime
from tests.test_source_integrity_archive import destination as destination
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)


async def test_application_graph_backup_roundtrips_protected_target(
    runtime, destination, content_store, monkeypatch
):
    from dataclasses import asdict
    from unittest.mock import AsyncMock

    from sibyl_core.services.eval_publication_guards import unavailable_publication_ids
    from sibyl_core.tools.admin import BackupData, create_backup, restore_backup
    from tests.test_memory_projection_derivations import protected_session

    source, target, _ = await protected_session(runtime, monkeypatch)
    backup = await create_backup(organization_id=runtime.client.group_id)
    assert backup.success, backup.message
    transported = BackupData(**json.loads(json.dumps(asdict(backup.backup_data))))
    assert transported.version == "3.0"
    assert transported.source_integrity is not None
    monkeypatch.setattr(
        "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
        AsyncMock(return_value=destination),
    )
    result = await restore_backup(transported, organization_id=runtime.client.group_id)
    assert result.success, result.errors
    restored = await destination.entity_manager.get(target.id)
    assert target.id not in await unavailable_publication_ids(
        runtime.client.group_id, {target.id: restored.metadata}
    )
    await destination.entity_manager.update(source.id, {"content": "Changed after restore"})
    assert target.id in await unavailable_publication_ids(
        runtime.client.group_id, {target.id: restored.metadata}
    )


async def test_legacy_graph_import_preserves_content_but_quarantines_memory(
    runtime, destination, content_store, monkeypatch
):
    from dataclasses import asdict
    from unittest.mock import AsyncMock

    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.services.eval_publication_guards import unavailable_publication_ids
    from sibyl_core.tools.admin import BackupData, create_backup, restore_backup

    memory = Entity(
        id="old-memory",
        entity_type=EntityType.SESSION,
        name="Unverifiable memory",
        content="Keep these exact source bytes",
    )
    await runtime.entity_manager.create_direct(memory)
    backup = await create_backup(organization_id=runtime.client.group_id)
    assert backup.success
    payload = asdict(backup.backup_data)
    payload.update(version="2.0", source_integrity=None)
    monkeypatch.setattr(
        "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
        AsyncMock(return_value=destination),
    )
    result = await restore_backup(BackupData(**payload), organization_id=runtime.client.group_id)
    assert result.success, result.errors
    assert any(row["source_id"] == memory.id for row in result.quarantined)
    restored = await destination.entity_manager.get(memory.id)
    assert restored.content == memory.content
    assert restored.derivation_required is True
    denied = await unavailable_publication_ids(
        runtime.client.group_id, {memory.id: restored.metadata}
    )
    assert memory.id in denied
    for row in payload["entities"]:
        if row["entity_type"] == "project":
            project = await destination.entity_manager.get(row["id"])
            assert not project.derivation_required
            assert project.id not in await unavailable_publication_ids(
                runtime.client.group_id, {project.id: project.metadata}
            )


async def test_legacy_skip_preserves_existing_ordinary_memory(runtime, monkeypatch):
    from dataclasses import asdict

    from sibyl_core.memory_pipeline.observations import SourceKind
    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.services.source_archive_store import read_source_archive_snapshot
    from sibyl_core.tools.admin import BackupData, create_backup, restore_backup

    memory = Entity(
        id="ordinary-in-place",
        entity_type=EntityType.SESSION,
        name="Owned original",
        content="Original",
    )
    await runtime.entity_manager.create_direct(memory)
    backup = await create_backup(organization_id=runtime.client.group_id)
    payload = asdict(backup.backup_data)
    payload.update(version="2.0", source_integrity=None)
    before = await read_source_archive_snapshot(
        runtime.client.execute_query,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    result = await restore_backup(
        BackupData(**payload), organization_id=runtime.client.group_id, skip_existing=True
    )
    assert result.success, result.errors
    assert not result.quarantined
    after = await read_source_archive_snapshot(
        runtime.client.execute_query,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    assert after == before


async def test_full_graph_clean_replaces_episode_inventory_without_resetting_source_history(
    runtime,
):
    from sibyl_core.memory_pipeline.observations import SourceKind
    from sibyl_core.models.entities import Relationship, RelationshipType
    from sibyl_core.services.source_archive_store import read_source_archive_snapshot
    from sibyl_core.tools.admin import BackupData, create_backup, restore_backup

    org = runtime.client.group_id
    await runtime.relationship_manager.create(
        Relationship(
            id="original-link",
            source_id="project_a",
            target_id="project_b",
            relationship_type=RelationshipType.RELATED_TO,
        )
    )
    backup = await create_backup(organization_id=org)
    assert backup.success
    extra = BackupData(
        version="2.0",
        created_at="2026-09-09T00:00:00Z",
        organization_id=org,
        entity_count=0,
        relationship_count=0,
        entities=[],
        relationships=[],
        episode_count=1,
        episodes=[
            {
                "uuid": "post-archive-episode",
                "name": "New episode",
                "source": "message",
                "source_description": "chat",
                "content": "Post archive content",
                "created_at": "2026-09-09T00:00:00Z",
                "valid_at": "2026-09-09T00:00:00Z",
                "entity_edges": [],
            }
        ],
    )
    added = await restore_backup(extra, organization_id=org)
    assert added.success, added.errors
    assert await runtime.client.execute_query(
        "SELECT * FROM episode WHERE uuid='post-archive-episode';"
    )
    before = await read_source_archive_snapshot(
        runtime.client.execute_query, kind=SourceKind.GRAPH_ENTITY, organizations=[org]
    )
    result = await restore_backup(
        backup.backup_data, organization_id=org, clean=True, skip_existing=False
    )
    assert result.success, result.errors
    assert not await runtime.client.execute_query(
        "SELECT * FROM episode WHERE uuid='post-archive-episode';"
    )
    after = await read_source_archive_snapshot(
        runtime.client.execute_query, kind=SourceKind.GRAPH_ENTITY, organizations=[org]
    )
    assert after == before

    execute = runtime.client.execute_query
    injected = False

    async def race(statement, **params):
        nonlocal injected
        if "archive destination changed before restore" in statement and not injected:
            injected = True
            added = await restore_backup(extra, organization_id=org)
            assert added.success, added.errors
        return await execute(statement, **params)

    from unittest.mock import patch

    with patch.object(runtime.client, "execute_query", race):
        conflicted = await restore_backup(
            backup.backup_data, organization_id=org, clean=True, skip_existing=False
        )
    assert injected
    assert not conflicted.success
    assert any("destination changed" in error for error in conflicted.errors)
    assert await execute("SELECT * FROM episode WHERE uuid='post-archive-episode';")
    assert await execute("SELECT * FROM relates_to WHERE uuid='original-link';")
