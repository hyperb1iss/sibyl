"""Canonical capture lifecycle stays authoritative when propagation fails."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from sibyl_core.memory_pipeline.source_lifecycle import SOURCE_BINDINGS_KEY
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph_capture_availability import available_capture_projection_rows
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.surreal_content import get_raw_memory, remember_raw_memory, save_raw_memory
from tests.test_capture_corrections import captured_note
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


async def test_capture_read_preserves_content_epoch_after_failed_revision_and_restore(
    runtime, content_store, monkeypatch
):
    memory, note = await captured_note(runtime, monkeypatch)
    note.metadata[SOURCE_BINDINGS_KEY] = {memory.id: memory.revision}
    await runtime.entity_manager.update(note.id, {"metadata": note.metadata})
    original = await runtime.entity_manager.get(note.id)
    monkeypatch.setattr(
        runtime.entity_manager, "update", AsyncMock(side_effect=RuntimeError("stamp unavailable"))
    )
    for action, extra in (
        ("revise", {"revised_content": "New deployment advice"}),
        ("restore", {}),
    ):
        result = await apply_memory_correction(
            organization_id=runtime.client.group_id,
            source_id=memory.id,
            principal_id="user_a",
            action=action,
            **extra,
        )
        assert result.applied and not result.propagation_complete
        assert (
            await available_capture_projection_rows(runtime.client.group_id, {note.id: original})
            == {}
        )
    assert (await runtime.entity_manager.get(note.id)).model_dump() == original.model_dump()


async def test_capture_read_restores_unrevised_source_without_rebinding(
    runtime, content_store, monkeypatch
):
    memory, note = await captured_note(runtime, monkeypatch)
    original = await runtime.entity_manager.get(note.id)
    monkeypatch.setattr(
        runtime.entity_manager, "update", AsyncMock(side_effect=RuntimeError("stamp unavailable"))
    )
    for action, readable in (("hide", False), ("restore", True)):
        result = await apply_memory_correction(
            organization_id=runtime.client.group_id,
            source_id=memory.id,
            principal_id="user_a",
            action=action,
        )
        assert result.applied and not result.propagation_complete
        current = await available_capture_projection_rows(
            runtime.client.group_id, {note.id: original}
        )
        assert (note.id in current) is readable
    assert (await runtime.entity_manager.get(note.id)).model_dump() == original.model_dump()


async def test_capture_read_fails_only_dependent_rows_on_missing_or_failed_lookup(
    runtime, content_store, monkeypatch
):
    _memory, note = await captured_note(runtime, monkeypatch)
    unrelated = Entity(id="independent", name="Independent", entity_type=EntityType.NOTE)
    missing = note.model_copy(update={"id": "missing", "metadata": {"raw_memory_id": "missing"}})
    rows = {row.id: row for row in (note, unrelated, missing)}
    assert set(await available_capture_projection_rows(runtime.client.group_id, rows)) == {
        note.id,
        unrelated.id,
    }
    monkeypatch.setattr(
        runtime.client, "execute_query", AsyncMock(side_effect=RuntimeError("query unavailable"))
    )
    monkeypatch.setattr(
        "sibyl_core.services.content_client.select_many",
        AsyncMock(side_effect=RuntimeError("query unavailable")),
    )
    assert set(await available_capture_projection_rows(runtime.client.group_id, rows)) == {
        unrelated.id
    }


async def test_capture_read_checks_transitive_bound_sources_without_copying_stale_blockers(
    runtime, content_store, monkeypatch
):
    memory, note = await captured_note(runtime, monkeypatch)
    intermediate = await save_raw_memory(
        replace(
            memory,
            id="intermediate",
            observed_revision=None,
            metadata={SOURCE_BINDINGS_KEY: {memory.id: memory.revision}},
        )
    )
    derived = note.model_copy(
        update={
            "id": "bound-descendant",
            "metadata": {
                SOURCE_BINDINGS_KEY: {
                    intermediate.id: intermediate.revision,
                    memory.id: memory.revision,
                }
            },
        }
    )
    await runtime.entity_manager.create_direct(derived, generate_embedding=False)
    monkeypatch.setattr(
        runtime.entity_manager, "update", AsyncMock(side_effect=RuntimeError("stamp unavailable"))
    )
    result = await apply_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=memory.id,
        principal_id="user_a",
        action="delete",
    )
    assert result.applied
    assert (
        await available_capture_projection_rows(runtime.client.group_id, {derived.id: derived})
        == {}
    )


async def test_capture_batch_failure_preserves_other_healthy_batches(
    runtime, content_store, monkeypatch
):
    from sibyl_core.services import content_client

    memory, note = await captured_note(runtime, monkeypatch)
    healthy = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="healthy-capture",
        raw_content="Independent healthy text",
        embedding_provider=None,
    )
    healthy_note = note.model_copy(
        update={"id": "healthy-note", "metadata": {"raw_memory_id": healthy.id}}
    )
    await runtime.entity_manager.create_direct(healthy_note, generate_embedding=False)
    original_select = content_client.select_many
    original_batches = content_client.value_batches

    def batches(values, **kwargs):
        if set(values) != {memory.id, healthy.id}:
            yield from original_batches(values, **kwargs)
            return
        yield [memory.id]
        yield [healthy.id]

    async def partly_unavailable(client, query, **params):
        if params["source_ids"] == [memory.id]:
            raise RuntimeError("one batch unavailable")
        return await original_select(client, query, **params)

    monkeypatch.setattr(content_client, "value_batches", batches)
    monkeypatch.setattr(content_client, "select_many", partly_unavailable)
    rows = {row.id: row for row in (note, healthy_note)}
    assert set(await available_capture_projection_rows(runtime.client.group_id, rows)) == {
        healthy_note.id
    }


async def test_capture_tombstone_denies_legacy_graph_without_discarding_raw_archive(
    runtime, content_store, monkeypatch
):
    memory, note = await captured_note(runtime, monkeypatch)
    ordinary = Entity(id="tombstone-ordinary", name="Independent", entity_type=EntityType.NOTE)
    await runtime.entity_manager.create_direct(ordinary, generate_embedding=False)
    rows = {row.id: row for row in await runtime.entity_manager.get_many([note.id, ordinary.id])}
    assert set(
        await available_capture_projection_rows(
            runtime.client.group_id, rows, graph_client=runtime.client
        )
    ) == set(rows)
    tombstone = await save_raw_memory(
        replace(memory, deleted_at=datetime.now(UTC), revision=memory.revision + 1)
    )
    assert tombstone.deleted_at is not None and tombstone.revision > memory.revision
    assert set(
        await available_capture_projection_rows(
            runtime.client.group_id, rows, graph_client=runtime.client
        )
    ) == {ordinary.id}
    retained = await get_raw_memory(memory_id=memory.id, organization_id=runtime.client.group_id)
    assert retained is not None and retained.raw_content == memory.raw_content
    assert (await runtime.entity_manager.get(note.id)).model_dump() == rows[note.id].model_dump()
