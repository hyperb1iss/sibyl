"""A changed promotion source cannot overwrite the derived row's own verdict."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.projection.repair import repair_graph_lifecycle
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.memory_reflection import _verify_promotion_sources
from sibyl_core.services.surreal_content import remember_raw_memory, save_raw_memory
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


@pytest.mark.parametrize("own_state", [None, "deleted"])
async def test_changed_promotion_source_preserves_authored_verdict(
    runtime, content_store, monkeypatch, own_state
):
    monkeypatch.setattr(
        "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    org = runtime.client.group_id
    source = await remember_raw_memory(
        organization_id=org,
        principal_id="user_a",
        source_id="signature-source",
        raw_content="Original evidence",
        embedding_provider=None,
    )
    revised = await apply_memory_correction(
        organization_id=org,
        principal_id="user_a",
        source_id=source.id,
        action="revise",
        revised_content="Corrected evidence",
    )
    assert revised.applied
    restored = await apply_memory_correction(
        organization_id=org,
        principal_id="user_a",
        source_id=source.id,
        action="restore",
    )
    assert restored.applied
    metadata = {
        "memory_scope": "private",
        "principal_id": "user_a",
        "raw_source_ids": [source.id],
        "source_bindings": {source.id: source.revision},
    }
    if own_state is not None:
        metadata["lifecycle_state"] = own_state
    entity = Entity(
        id="stale-promotion",
        name="Derived evidence",
        entity_type=EntityType.EPISODE,
        content=source.raw_content,
        metadata=metadata,
    )
    await runtime.entity_manager.create_direct(entity, generate_embedding=False)

    assert not await _verify_promotion_sources(runtime, [source], entity_id=entity.id)

    row = await runtime.entity_manager.get(entity.id)
    assert not graph_metadata_recallable(row.metadata)
    assert row.metadata.get("lifecycle_state") == own_state
    assert "excluded_from_recall" not in row.metadata
    assert row.metadata["source_bindings"] == {source.id: source.revision}
    assert row.metadata["correction_blockers"][source.id]["blocking"] is True


@pytest.mark.parametrize("own_state", [None, "deleted"])
@pytest.mark.parametrize("hidden_during_check", [False, True])
async def test_signature_repair_requires_the_original_source_values(
    runtime, content_store, monkeypatch, own_state, hidden_during_check
):
    monkeypatch.setattr(
        "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="signature-title-source",
        raw_content="Retained evidence",
        title="Original title",
        embedding_provider=None,
    )
    changed = await save_raw_memory(
        replace(source, title="Changed title"),
        expected_revision=source.revision,
        embedding_provider=None,
    )
    metadata = {
        "memory_scope": "private",
        "principal_id": "user_a",
        "raw_source_ids": [source.id],
        "source_bindings": {source.id: source.revision},
    }
    if own_state is not None:
        metadata["lifecycle_state"] = own_state
    entity = Entity(
        id="signature-title-row",
        name="Derived evidence",
        entity_type=EntityType.EPISODE,
        content=source.raw_content,
        metadata=metadata,
    )
    await runtime.entity_manager.create_direct(entity, generate_embedding=False)
    if hidden_during_check:
        hidden = await apply_memory_correction(
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            source_id=source.id,
            action="hide",
        )
        assert hidden.applied
    assert not await _verify_promotion_sources(runtime, [source], entity_id=entity.id)
    if hidden_during_check:
        restored = await apply_memory_correction(
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            source_id=source.id,
            action="restore",
        )
        assert restored.applied and restored.updated_memory is not None
        changed = restored.updated_memory
    for _ in range(2):
        result = await repair_graph_lifecycle(runtime)
        assert result.checked == 1 and result.pending == 1 and result.failed == 0
        row = await runtime.entity_manager.get(entity.id)
        assert not graph_metadata_recallable(row.metadata)
        assert row.metadata.get("lifecycle_state") == own_state
        assert "excluded_from_recall" not in row.metadata

    await save_raw_memory(
        replace(changed, title=source.title),
        expected_revision=changed.revision,
        embedding_provider=None,
    )
    result = await repair_graph_lifecycle(runtime)
    assert result.recovered == 1 and result.failed == 0
    row = await runtime.entity_manager.get(entity.id)
    assert not row.metadata.get("source_validation_pending")
    assert graph_metadata_recallable(row.metadata) is (own_state is None)
    assert row.metadata.get("lifecycle_state") == own_state


async def test_promotion_source_read_outage_is_excluded_until_rechecked(
    runtime, content_store, monkeypatch
):
    from sibyl_core.services import memory_lifecycle, memory_reflection

    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="outage-source",
        raw_content="Evidence awaiting its post-create check",
        embedding_provider=None,
    )
    entity = Entity(
        id="outage-promotion",
        name="Derived evidence",
        entity_type=EntityType.EPISODE,
        content=source.raw_content,
        metadata={
            "raw_source_ids": [source.id],
            "source_bindings": {source.id: source.revision},
        },
    )
    await runtime.entity_manager.create_direct(entity, generate_embedding=False)
    with monkeypatch.context() as outage:
        for module in (memory_reflection, memory_lifecycle):
            outage.setattr(
                module, "get_raw_memory", AsyncMock(side_effect=ConnectionError("offline"))
            )
        assert not await _verify_promotion_sources(runtime, [source], entity_id=entity.id)
    row = await runtime.entity_manager.get(entity.id)
    assert not graph_metadata_recallable(row.metadata)
    assert row.metadata.get("lifecycle_reconciliation_pending")
    assert "excluded_from_recall" not in row.metadata
    result = await repair_graph_lifecycle(runtime)
    assert result.recovered == 1 and result.failed == 0
    row = await runtime.entity_manager.get(entity.id)
    assert graph_metadata_recallable(row.metadata)
