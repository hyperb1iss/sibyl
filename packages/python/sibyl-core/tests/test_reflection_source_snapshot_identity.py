"""Re-extraction creates new evidence without relabeling an older source snapshot."""

from copy import deepcopy
from dataclasses import replace

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.memory_pipeline.source_lifecycle import public_memory_metadata
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.memory_reflection import (
    _verify_promotion_sources,
    persist_reflection_candidate,
)
from sibyl_core.services.surreal_content import remember_raw_memory, save_raw_memory
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


async def test_reextraction_preserves_old_snapshot_and_can_publish_current_evidence(
    runtime, content_store
):
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="snapshot-identity-source",
        title="Original evidence title",
        raw_content="We decided to retain the audit trail.",
        embedding_provider=None,
    )
    candidate = ReflectionCandidate(
        kind="decision",
        title="Retain the audit trail",
        content="Keep the audit trail.",
        reason="Source decision",
        confidence=1.0,
        raw_source_ids=[source.id],
    )

    async def publish(snapshot):
        return await persist_reflection_candidate(
            candidate=candidate,
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            source_id=source.id,
            link_source_entity=False,
            source_memories=[snapshot],
        )

    original = await publish(source)
    assert original.response.success
    changed = await save_raw_memory(
        replace(source, title="Corrected evidence title"),
        expected_revision=source.revision,
        embedding_provider=None,
    )
    assert not await _verify_promotion_sources(runtime, [source], entity_id=original.response.id)
    current = await publish(changed)
    assert current.response.success
    assert current.response.id != original.response.id
    old_row = await runtime.entity_manager.get(original.response.id)
    new_row = await runtime.entity_manager.get(current.response.id)
    assert not graph_metadata_recallable(old_row.metadata)
    assert graph_metadata_recallable(new_row.metadata)
    assert old_row.metadata["source_bindings"] == {source.id: source.revision}
    assert new_row.metadata["source_bindings"] == {source.id: changed.revision}

    bookkeeping = await save_raw_memory(
        replace(changed, metadata={**changed.metadata, "reflection_run_id": "later-run"}),
        expected_revision=changed.revision,
        embedding_provider=None,
    )
    replay = await publish(bookkeeping)
    assert replay.response.success and replay.response.id == current.response.id


def test_snapshot_fingerprints_are_redacted_without_mutating_stored_identity():
    metadata = {
        "source_snapshot_sha256": "private-fingerprint",
        "reflection_identity": {
            "version": 3,
            "source_snapshot_sha256": "private-fingerprint",
            "kind": "decision",
        },
    }
    before = deepcopy(metadata)
    public = public_memory_metadata(metadata)
    assert "source_snapshot_sha256" not in public
    assert public["reflection_identity"] == {"version": 3, "kind": "decision"}
    assert metadata == before
