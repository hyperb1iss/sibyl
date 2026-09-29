"""Operator corrections cover the same capture projections readers use."""

from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph_read_availability import available_graph_entities
from sibyl_core.services.memory_correction import (
    apply_memory_correction,
    preview_memory_correction,
)
from sibyl_core.services.surreal_content import remember_raw_memory
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


async def captured_note(runtime, monkeypatch, *, raw_metadata=None):
    monkeypatch.setattr(
        "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    memory = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="capture-source",
        raw_content="The deployment requires the violet approval.",
        embedding_provider=None,
        metadata=raw_metadata,
    )
    note = Entity(
        id="capture-note",
        name="Deployment approval",
        content=memory.raw_content,
        entity_type=EntityType.NOTE,
        metadata={
            "raw_memory_id": memory.id,
            "raw_source_id": memory.source_id,
            "memory_scope": "private",
            "principal_id": "user_a",
        },
    )
    await runtime.entity_manager.create_direct(note, generate_embedding=False)
    return memory, note


@pytest.mark.parametrize("action", ["delete", "hide", "wrong"])
async def test_capture_correction_preview_finds_direct_note(
    runtime, content_store, monkeypatch, action
):
    memory, note = await captured_note(runtime, monkeypatch)
    preview = await preview_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=memory.id,
        principal_id="user_a",
        action=action,
    )
    assert preview.allowed
    assert note.id in preview.affected_derived_ids


@pytest.mark.parametrize("action", ["delete", "hide", "wrong"])
async def test_capture_correction_removes_direct_note_from_public_reads(
    runtime, content_store, monkeypatch, action
):
    memory, note = await captured_note(runtime, monkeypatch)
    assert note.id in await available_graph_entities(
        runtime.client.group_id, [note.id], runtime=runtime
    )
    result = await apply_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=memory.id,
        principal_id="user_a",
        action=action,
        reason="Withdraw this capture.",
    )
    assert result.applied and result.propagation_complete
    assert note.id not in await available_graph_entities(
        runtime.client.group_id, [note.id], runtime=runtime
    )


async def test_capture_preview_traverses_transitive_projection_without_writes(
    runtime, content_store, monkeypatch
):
    memory, note = await captured_note(runtime, monkeypatch)
    child = Entity(
        id="child-span",
        name="Child",
        entity_type=EntityType.NOTE,
        metadata={
            "projection_kind": "span",
            "parent_entity_id": note.id,
            "memory_scope": "private",
            "principal_id": "user_a",
        },
    )
    grandchild = Entity(
        id="grandchild-fact",
        name="Fact",
        entity_type=EntityType.NOTE,
        metadata={
            "projection_kind": "fact",
            "source_entity_id": child.id,
            "memory_scope": "private",
            "principal_id": "user_a",
        },
    )
    private = Entity(
        id="private-child",
        name="Foreign child",
        entity_type=EntityType.NOTE,
        metadata={
            "projection_kind": "span",
            "parent_entity_id": note.id,
            "memory_scope": "private",
            "principal_id": "other-user",
        },
    )
    for entity in (child, grandchild, private):
        await runtime.entity_manager.create_direct(entity, generate_embedding=False)
    original = await runtime.entity_manager.get_many([note.id, child.id, grandchild.id, private.id])
    preview = await preview_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=memory.id,
        principal_id="user_a",
        action="delete",
    )
    assert set(preview.affected_derived_ids) == {note.id, child.id, grandchild.id}
    assert preview.metadata["derived_lookup_complete"] is True
    current = await runtime.entity_manager.get_many([note.id, child.id, grandchild.id, private.id])
    assert [row.model_dump() for row in current] == [row.model_dump() for row in original]


async def test_preview_lookup_failure_reports_incomplete_impact(
    runtime, content_store, monkeypatch
):
    memory, _note = await captured_note(runtime, monkeypatch)
    monkeypatch.setattr(
        "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
        AsyncMock(side_effect=RuntimeError("graph unavailable")),
    )
    preview = await preview_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=memory.id,
        principal_id="user_a",
        action="delete",
    )
    assert preview.allowed
    assert preview.affected_derived_ids == []
    assert preview.metadata["derived_lookup_complete"] is False


async def test_preview_includes_readable_raw_descendants(runtime, content_store, monkeypatch):
    memory, _note = await captured_note(runtime, monkeypatch)
    child = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="derived-capture",
        raw_content="Derived text",
        metadata={"raw_source_ids": [memory.id]},
        embedding_provider=None,
    )
    foreign = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="foreign-user",
        source_id="private-derived-capture",
        raw_content="Foreign text",
        metadata={"raw_source_ids": [memory.id]},
        embedding_provider=None,
    )
    preview = await preview_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=memory.id,
        principal_id="user_a",
        action="delete",
    )
    assert set(preview.affected_source_ids) == {memory.id, child.id}
    assert foreign.id not in preview.affected_source_ids
    assert preview.metadata["derived_lookup_complete"] is True


async def test_preview_reports_failed_descendant_row_load(runtime, content_store, monkeypatch):
    memory, note = await captured_note(runtime, monkeypatch)
    original_get = runtime.entity_manager.get

    async def unreadable_row(entity_id):
        if entity_id == note.id:
            raise RuntimeError("row read failed")
        return await original_get(entity_id)

    monkeypatch.setattr(runtime.entity_manager, "get", unreadable_row)
    preview = await preview_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=memory.id,
        principal_id="user_a",
        action="delete",
    )
    assert preview.affected_derived_ids == []
    assert preview.metadata["derived_lookup_complete"] is False


async def test_preview_discloses_readable_refusal_without_traversing_claimed_lineage(
    runtime, content_store, monkeypatch
):
    memory, note = await captured_note(
        runtime, monkeypatch, raw_metadata={"derived_ids": ["readonly-target"]}
    )
    target = Entity(
        id="readonly-target",
        name="Read-only target",
        entity_type=EntityType.NOTE,
        metadata={"memory_scope": "project", "scope_key": "project_b", "project_id": "project_b"},
    )
    child = Entity(
        id="unrelated-child",
        name="Unrelated descendant",
        entity_type=EntityType.NOTE,
        metadata={
            "projection_kind": "fact",
            "source_entity_id": target.id,
            "memory_scope": "project",
            "scope_key": "project_b",
            "project_id": "project_b",
        },
    )
    for entity in (target, child):
        await runtime.entity_manager.create_direct(entity, generate_embedding=False)
    preview = await preview_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=memory.id,
        principal_id="user_a",
        accessible_projects=["project_b"],
        writable_projects=[],
        action="delete",
    )
    assert set(preview.affected_derived_ids) == {note.id, target.id}
    assert child.id not in preview.affected_derived_ids
