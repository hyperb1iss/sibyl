"""Snapshots render current source state after relationship discovery."""

from functools import partial

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import graph_read_availability
from sibyl_core.services.graph_community_models import GraphSnapshot
from sibyl_core.services.graph_community_snapshot import _current_graph_snapshot
from sibyl_core.services.graph_visibility import graph_row_read_allowed
from sibyl_core.services.memory_correction import apply_memory_correction
from tests.test_capture_corrections import captured_note
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


async def test_snapshot_rechecks_source_after_relationship_validation(
    runtime, content_store, monkeypatch
):
    memory, parent = await captured_note(runtime, monkeypatch)
    ordinary = Entity(id="snapshot-ordinary", name="Resource", entity_type=EntityType.NOTE)
    await runtime.entity_manager.create_direct(ordinary, generate_embedding=False)
    rows = await runtime.entity_manager.get_many([parent.id, ordinary.id])
    snapshot = GraphSnapshot(
        entities=rows, relationships=[], entity_by_id={row.id: row for row in rows}
    )
    visible = partial(
        graph_row_read_allowed,
        principal_id="user_a",
        accessible_projects=set(),
        allowed_memory_scope_keys=None,
    )
    baseline = await _current_graph_snapshot(
        runtime.client, runtime.client.group_id, snapshot, source_visible=visible
    )
    assert set(baseline.entity_by_id) == {parent.id, ordinary.id}
    validate = graph_read_availability.available_graph_relationships
    applied = False

    async def retire_during_relationship_validation(*args, **kwargs):
        nonlocal applied
        correction = await apply_memory_correction(
            organization_id=runtime.client.group_id,
            source_id=memory.id,
            principal_id="user_a",
            action="delete",
        )
        assert correction.applied and correction.propagation_complete
        applied = True
        return await validate(*args, **kwargs)

    monkeypatch.setattr(
        graph_read_availability,
        "available_graph_relationships",
        retire_during_relationship_validation,
    )
    current = await _current_graph_snapshot(
        runtime.client, runtime.client.group_id, snapshot, source_visible=visible
    )
    assert applied
    assert set(current.entity_by_id) == {ordinary.id}
    assert current.relationships == []
