"""Legacy source repair preserves evidence across bookkeeping-only writes."""

from dataclasses import replace

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.projection.reconcile import reconcile_with_capture
from sibyl_core.services.memory_policy import raw_memory_source_fingerprint
from sibyl_core.services.memory_reflection import persist_reflection_candidate
from sibyl_core.services.surreal_content import save_raw_memory
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime
from tests.test_reflection_legacy_epoch_identity import legacy_source


async def test_legacy_source_repair_does_not_retire_unchanged_observed_text(runtime, content_store):
    source = await legacy_source(runtime)
    published = await persist_reflection_candidate(
        candidate=ReflectionCandidate(
            kind="decision",
            title="Audit trail",
            content=source.raw_content,
            confidence=1.0,
            reason="Recorded decision",
            raw_source_ids=[source.id],
        ),
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id=source.id,
        source_memories=[source],
        link_source_entity=False,
    )
    assert published.response.success
    await save_raw_memory(
        replace(source, metadata={**source.metadata, "reflection_run_id": "later"}),
        expected_revision=source.revision,
        embedding_provider=None,
    )
    await reconcile_with_capture(
        runtime.entity_manager,
        organization_id=runtime.client.group_id,
        metadata={"raw_memory_id": source.id},
        row_ids=[published.response.id],
        expected_signature=raw_memory_source_fingerprint(source),
    )
    row = await runtime.entity_manager.get(published.response.id)
    assert graph_metadata_recallable(row.metadata)
    assert row.metadata["source_bindings"] == {source.id: source.revision}
