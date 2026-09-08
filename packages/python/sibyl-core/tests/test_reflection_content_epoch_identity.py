"""Equal text after a correction still belongs to a new source epoch."""

from dataclasses import replace
from unittest.mock import AsyncMock

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.memory_reflection import persist_reflection_candidate
from sibyl_core.services.surreal_content import remember_raw_memory, save_raw_memory
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


async def test_reextraction_after_text_roundtrip_keeps_historical_epoch_retired(
    runtime, content_store, monkeypatch
):
    monkeypatch.setattr(
        "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    org = runtime.client.group_id
    source = await remember_raw_memory(
        organization_id=org,
        principal_id="user_a",
        source_id="identity-content-roundtrip",
        raw_content="Retain the audit trail.",
        embedding_provider=None,
    )
    candidate = ReflectionCandidate(
        kind="decision",
        title="Audit trail",
        content="Retain the audit trail.",
        confidence=1.0,
        reason="Recorded decision",
        raw_source_ids=[source.id],
    )

    async def publish(snapshot):
        return await persist_reflection_candidate(
            candidate=candidate,
            organization_id=org,
            principal_id="user_a",
            source_id=source.id,
            link_source_entity=False,
            source_memories=[snapshot],
        )

    original = await publish(source)
    assert original.response.success
    for text in ("Delete the audit trail.", source.raw_content):
        revised = await apply_memory_correction(
            organization_id=org,
            principal_id="user_a",
            source_id=source.id,
            action="revise",
            revised_content=text,
        )
        assert revised.applied
        restored = await apply_memory_correction(
            organization_id=org,
            principal_id="user_a",
            source_id=source.id,
            action="restore",
        )
        assert restored.applied and restored.updated_memory is not None
    current = await publish(restored.updated_memory)
    assert current.response.success
    assert current.response.id != original.response.id
    old_row = await runtime.entity_manager.get(original.response.id)
    new_row = await runtime.entity_manager.get(current.response.id)
    assert not graph_metadata_recallable(old_row.metadata)
    assert graph_metadata_recallable(new_row.metadata)
    assert old_row.metadata["source_bindings"] == {source.id: source.revision}

    bookkeeping = await save_raw_memory(
        replace(
            restored.updated_memory,
            metadata={**restored.updated_memory.metadata, "reflection_run_id": "later"},
        ),
        expected_revision=restored.updated_memory.revision,
        embedding_provider=None,
    )
    replay = await publish(bookkeeping)
    assert replay.response.success and replay.response.id == current.response.id
