"""Graph archive replacement respects later lifecycle and audience decisions."""

import pytest

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.tools.admin import create_backup, restore_backup
from tests.test_source_integrity_archive import runtime as runtime
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)


@pytest.mark.parametrize("clean", [False, True])
@pytest.mark.parametrize(
    "patch",
    [
        {"excluded_from_recall": True},
        {"lifecycle_state": "deleted"},
        {"superseded_by_source_id": "new-source"},
        {"principal_id": "new-owner", "memory_scope": "private"},
    ],
)
async def test_graph_restore_preserves_current_visibility(runtime, clean, patch):
    source = Entity(
        id="revoked-source",
        entity_type=EntityType.SESSION,
        name="Evidence",
        content="Amethyst evidence",
    )
    await runtime.entity_manager.create_direct(source)
    backup = await create_backup(organization_id=runtime.client.group_id)
    assert backup.success, backup.message
    await runtime.entity_manager.update(source.id, {"metadata": patch})
    changed = await runtime.entity_manager.get(source.id)
    if "principal_id" not in patch:
        assert not graph_metadata_recallable(changed.metadata)
    before = await runtime.client.execute_query(
        "RETURN crypto::sha256(type::string({"
        "row: (SELECT * FROM entity WHERE uuid=$id),"
        "state: (SELECT * FROM source_states WHERE source_id=$id)}));",
        id=source.id,
    )
    result = await restore_backup(
        backup.backup_data,
        organization_id=runtime.client.group_id,
        clean=clean,
        skip_existing=False,
    )
    assert result.success, result.message
    after = await runtime.client.execute_query(
        "RETURN crypto::sha256(type::string({"
        "row: (SELECT * FROM entity WHERE uuid=$id),"
        "state: (SELECT * FROM source_states WHERE source_id=$id)}));",
        id=source.id,
    )
    assert after == before
    restored = await runtime.entity_manager.get(source.id)
    for key, value in patch.items():
        assert restored.metadata[key] == value
    if "principal_id" not in patch:
        assert not graph_metadata_recallable(restored.metadata)
