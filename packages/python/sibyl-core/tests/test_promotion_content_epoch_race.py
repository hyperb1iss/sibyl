"""A text round-trip during insertion must not publish an obsolete source epoch."""

from unittest.mock import AsyncMock

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.memory_reflection import persist_reflection_candidate
from sibyl_core.services.surreal_content import remember_raw_memory
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


async def test_promotion_rejects_source_epoch_roundtrip_during_insert(
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
        source_id="epoch-race-source",
        raw_content="Retain the audit trail.",
        embedding_provider=None,
    )
    insert = runtime.entity_manager.create_direct_if_absent

    async def insert_after_roundtrip(entity):
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
            assert restored.applied
        return await insert(entity)

    monkeypatch.setattr(runtime.entity_manager, "create_direct_if_absent", insert_after_roundtrip)
    result = await persist_reflection_candidate(
        candidate=ReflectionCandidate(
            kind="decision",
            title="Audit trail",
            content=source.raw_content,
            reason="Recorded decision",
            confidence=1.0,
            raw_source_ids=[source.id],
        ),
        organization_id=org,
        principal_id="user_a",
        source_id=source.id,
        link_source_entity=False,
        source_memories=[source],
    )
    assert not result.response.success
    row = await runtime.entity_manager.get(result.response.id)
    assert not graph_metadata_recallable(row.metadata)
    assert row.metadata["source_bindings"] == {source.id: source.revision}
